"""Authentication service: admin login + recovery codes, student access-key login,
session creation/termination, access-key issuance.

Login brute-force protection is enforced in the router using a short Redis rate
limit. Only hashes of passwords / access keys / recovery codes are stored."""
from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums, security
from app.core.config import get_settings
from app.core.exceptions import Forbidden, Unauthorized
from app.core.middleware import request_context
from app.models.identity import (
    AdminRecoveryCode,
    AdminUser,
    Student,
    StudentAccessKey,
    StudentSession,
)
from app.services import audit_service

_settings = get_settings()
_utcnow = security.utcnow


async def get_admin_by_username(db: AsyncSession, username: str) -> AdminUser | None:
    result = await db.execute(select(AdminUser).where(AdminUser.username == username))
    return result.scalar_one_or_none()


async def verify_admin_password(db: AsyncSession, username: str, password: str) -> AdminUser | None:
    admin = await get_admin_by_username(db, username)
    if not admin or not security.verify_secret(admin.password_hash, password):
        return None
    # Transparent rehash if parameters changed.
    if security.needs_rehash(admin.password_hash):
        admin.password_hash = security.hash_secret(password)
    admin.last_login_at = _utcnow()
    await db.flush()
    return admin


def issue_admin_session_token(admin: AdminUser) -> tuple[str, str]:
    """Admin 'session' id is the admin's own UUID; token is signed + time-limited.

    Embeds session_epoch so bumping it invalidates every issued admin cookie."""
    sid = str(admin.id)
    token = security.create_session_token(
        security.SubjectType.ADMIN, sid, epoch=admin.session_epoch
    )
    csrf = security.create_csrf_token(sid)
    return token, csrf


async def bump_admin_session_epoch(db: AsyncSession, admin: AdminUser) -> int:
    """Invalidate ALL previously issued admin session tokens (logout, pw change).

    The increment is a single SQL `session_epoch = session_epoch + 1`, never a
    Python read-modify-write: two concurrent invalidations must not both compute
    `n+1` from a stale read and thereby reset the epoch back down, which would
    silently re-validate a token one of them was supposed to kill.
    `synchronize_session=False` keeps the ORM from re-writing its own (possibly
    stale) value afterwards, and `refresh` re-reads the true row value.
    """
    await db.execute(
        update(AdminUser)
        .where(AdminUser.id == admin.id)
        .values(session_epoch=AdminUser.session_epoch + 1)
        .execution_options(synchronize_session=False)
    )
    await db.refresh(admin, ["session_epoch"])
    return admin.session_epoch


async def logout_current(request, db: AsyncSession) -> None:
    """Best-effort server-side invalidation of whatever session the cookie holds.

    Admin tokens are stateless: bump the epoch. Student sessions have a DB row:
    mark it terminated. Missing/expired cookies are ignored (the cookie is
    cleared by the caller regardless)."""
    token = request.cookies.get(constants.SESSION_COOKIE)
    if not token:
        return
    # generous max_age: we want to act even on a token past its short TTL.
    payload = security.read_session_token(
        token, max_age_seconds=_settings.student_session_ttl_hours * 3600
    )
    if not payload:
        return
    subject = payload.get("t")
    if subject == security.SubjectType.ADMIN.value:
        admin = await db.get(AdminUser, uuid.UUID(payload["sid"]))
        if admin:
            await bump_admin_session_epoch(db, admin)
            await audit_service.record_audit(
                db, action="admin.logout", actor_id=admin.id, ip=request_context.get().ip
            )
    elif subject == security.SubjectType.STUDENT.value:
        sid = payload.get("sid")
        result = await db.execute(
            select(StudentSession).where(
                StudentSession.session_id == sid,
                StudentSession.terminated_at.is_(None),
            )
        )
        session = result.scalar_one_or_none()
        if session:
            session.terminated_at = _utcnow()
            await db.flush()


async def generate_recovery_codes(db: AsyncSession, admin: AdminUser) -> list[str]:
    """Create a fresh set of 5 codes. Invalidates (supersedes) the previous set.
    Returns plaintext codes exactly once."""
    await db.execute(
        update(AdminRecoveryCode)
        .where(AdminRecoveryCode.admin_id == admin.id, AdminRecoveryCode.superseded == False)  # noqa: E712
        .values(superseded=True)
    )
    pairs = security.generate_recovery_codes(5)
    for _display, code_hash in pairs:
        db.add(AdminRecoveryCode(admin_id=admin.id, code_hash=code_hash))
    await db.flush()
    await audit_service.record_audit(db, action="admin.recovery_codes.regenerated", actor_id=admin.id)
    return [display for display, _ in pairs]


async def login_with_recovery_code(db: AsyncSession, username: str, code: str) -> AdminUser | None:
    admin = await get_admin_by_username(db, username)
    if not admin:
        raise Unauthorized("Invalid credentials")
    result = await db.execute(
        select(AdminRecoveryCode).where(
            AdminRecoveryCode.admin_id == admin.id,
            AdminRecoveryCode.superseded == False,  # noqa: E712
            AdminRecoveryCode.used_at.is_(None),
        )
    )
    code = code.strip().upper()
    for row in result.scalars():
        if security.verify_secret(row.code_hash, code):
            # Atomic single-use claim: only flips used_at if still unused, so two
            # concurrent requests can never both consume the same code.
            claim = await db.execute(
                update(AdminRecoveryCode)
                .where(
                    AdminRecoveryCode.id == row.id,
                    AdminRecoveryCode.used_at.is_(None),
                )
                .values(used_at=_utcnow())
            )
            if (claim.rowcount or 0) != 1:
                continue  # another request claimed it first
            admin.last_login_at = _utcnow()
            await db.flush()
            await audit_service.record_audit(
                db, action="admin.login.recovery_code", actor_id=admin.id, ip=request_context.get().ip
            )
            return admin
    raise Unauthorized("Invalid or used recovery code")


# --------------------------------------------------------------------------- #
# Student access keys & sessions
# --------------------------------------------------------------------------- #
async def revoke_student_access_keys(db: AsyncSession, student_id: uuid.UUID) -> int:
    """Mark every ACTIVE key of a student as revoked. Returns how many."""
    result = await db.execute(
        update(StudentAccessKey)
        .where(
            StudentAccessKey.student_id == student_id,
            StudentAccessKey.status == enums.AccessKeyStatus.ACTIVE,
        )
        .values(status=enums.AccessKeyStatus.REVOKED, revoked_at=_utcnow())
    )
    return int(result.rowcount or 0)


async def issue_student_access_key(db: AsyncSession, student: Student) -> str:
    """Revoke old keys, create a new high-entropy access key. Returns plaintext once.

    At most one live key per student is a DATABASE invariant
    (`uq_student_access_key_active`), so a losing concurrent rotation raises
    IntegrityError instead of leaving two ACTIVE rows behind.
    """
    await revoke_student_access_keys(db, student.id)
    display, key_hash = security.generate_access_key(student.username)
    db.add(
        StudentAccessKey(
            student_id=student.id,
            key_hash=key_hash,
            key_prefix=security.access_key_fingerprint(display),
        )
    )
    await db.flush()
    return display


async def authenticate_student_access_key(db: AsyncSession, access_key: str) -> Student | None:
    """Find the active key whose hash matches, return its student (or None).

    The submitted key is looked up through its non-secret fingerprint first, so a
    login costs one Argon2 verification instead of one per stored key.
    """
    raw = access_key.strip()
    fingerprint = security.access_key_fingerprint(raw)
    result = await db.execute(
        select(StudentAccessKey, Student)
        .join(Student, Student.id == StudentAccessKey.student_id)
        .where(
            StudentAccessKey.key_prefix == fingerprint,
            StudentAccessKey.status == enums.AccessKeyStatus.ACTIVE,
        )
    )
    for key, student in result.all():
        if security.verify_secret(key.key_hash, raw):
            if student.status != enums.StudentStatus.ACTIVE or student.deleted_at is not None:
                raise Forbidden("Student account is not active")
            key.last_used_at = _utcnow()
            await db.flush()
            return student
    return None


async def create_student_session(db: AsyncSession, student: Student) -> tuple[StudentSession, str, str]:
    session_id = secrets.token_urlsafe(24)
    ctx = request_context.get()
    expires_at = _utcnow() + timedelta(hours=_settings.student_session_ttl_hours)
    session = StudentSession(
        student_id=student.id,
        session_id=session_id,
        ip=ctx.ip,
        user_agent=ctx.user_agent,
        last_seen_at=_utcnow(),
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()
    token = security.create_session_token(security.SubjectType.STUDENT, session_id)
    csrf = security.create_csrf_token(session_id)
    return session, token, csrf


async def terminate_student_sessions(db: AsyncSession, student_id: uuid.UUID, *, session_id: str | None = None) -> int:
    stmt = (
        update(StudentSession)
        .where(StudentSession.student_id == student_id, StudentSession.terminated_at.is_(None))
    )
    if session_id:
        stmt = stmt.where(StudentSession.session_id == session_id)
    result = await db.execute(stmt.values(terminated_at=_utcnow()))
    await db.flush()
    return result.rowcount or 0
