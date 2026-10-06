"""Topic taxonomy API: the teacher's own hierarchy (Grammar -> Tenses -> Past -> …).

A topic is structure, not content: it never owns questions, it labels them, and a
question may carry several. Deleting a topic is refused while it still has children
or labels - the alternative would silently rewrite how past questions were filed.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.database import get_db
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.models.content import QuestionTopic, Topic
from app.models.identity import AdminUser
from app.schemas import question as q_schemas
from app.services import audit_service

router = APIRouter(prefix="/topics", tags=["topics"])


async def _path_for(db: AsyncSession, name: str, parent_id: UUID | None) -> str:
    if parent_id is None:
        return name
    parent = await db.get(Topic, parent_id)
    if parent is None:
        raise ValidationFailed(f"parent topic '{parent_id}' does not exist")
    return f"{parent.level_path}/{name}"


async def _sibling_clash(
    db: AsyncSession, name: str, parent_id: UUID | None, *, exclude_id: UUID | None = None
) -> Topic | None:
    """Another topic with the same name under the same parent, however it got there.

    Case-insensitive because 'Tenses' and 'tenses' file identically in a picker, and
    parentless topics are siblings of each other, so `IS NULL` is part of the match.
    """
    stmt = select(Topic).where(func.lower(Topic.name) == name.lower())
    stmt = stmt.where(Topic.parent_id.is_(None) if parent_id is None else Topic.parent_id == parent_id)
    if exclude_id is not None:
        stmt = stmt.where(Topic.id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _reject_cycle(db: AsyncSession, topic_id: UUID, parent_id: UUID | None) -> None:
    """A topic cannot live inside itself, however the chain is spelled."""
    if parent_id is None:
        return
    if parent_id == topic_id:
        raise ValidationFailed("a topic cannot be its own parent")
    cursor: UUID | None = parent_id
    while cursor is not None:
        parent = await db.get(Topic, cursor)
        if parent is None:
            raise ValidationFailed(f"parent topic '{cursor}' does not exist")
        if parent.id == topic_id:
            raise ValidationFailed("that move would put a topic inside itself")
        cursor = parent.parent_id


async def _refresh_subtree_paths(db: AsyncSession, topic: Topic) -> None:
    topic.level_path = await _path_for(db, topic.name, topic.parent_id)
    children = (
        await db.execute(select(Topic).where(Topic.parent_id == topic.id).order_by(Topic.name))
    ).scalars().all()
    for child in children:
        await _refresh_subtree_paths(db, child)


async def _question_counts(db: AsyncSession, topic_ids: list[UUID]) -> dict[UUID, int]:
    if not topic_ids:
        return {}
    rows = (
        await db.execute(
            select(QuestionTopic.topic_id, func.count())
            .where(QuestionTopic.topic_id.in_(topic_ids))
            .group_by(QuestionTopic.topic_id)
        )
    ).all()
    return {topic_id: int(count) for topic_id, count in rows}


async def _node(db: AsyncSession, topic: Topic, counts: dict[UUID, int]) -> dict:
    children = (
        await db.execute(select(Topic).where(Topic.parent_id == topic.id).order_by(Topic.name))
    ).scalars().all()
    return q_schemas.TopicRead(
        id=topic.id,
        name=topic.name,
        parent_id=topic.parent_id,
        level_path=topic.level_path,
        language=topic.language,
        question_count=counts.get(topic.id, 0),
        children=[await _node(db, child, counts) for child in children],
    ).model_dump(mode="json")


@router.get("", response_model=None)
async def list_topics(
    _admin: AdminUser = Depends(deps.get_current_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """The whole tree (roots first), each node carrying how many questions it labels."""
    roots = (
        await db.execute(select(Topic).where(Topic.parent_id.is_(None)).order_by(Topic.name))
    ).scalars().all()
    all_ids = (await db.execute(select(Topic.id))).scalars().all()
    counts = await _question_counts(db, list(all_ids))
    items = [await _node(db, root, counts) for root in roots]
    return {"items": items, "total": len(all_ids)}


@router.post("", response_model=None, status_code=201)
async def create_topic(
    payload: q_schemas.TopicCreate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise ValidationFailed("a topic needs a name")
    duplicate = await _sibling_clash(db, name, payload.parent_id)
    if duplicate is not None:
        raise Conflict("topic_exists", "This parent already has a topic with that name")

    topic = Topic(
        name=name,
        parent_id=payload.parent_id,
        language=payload.language,
        level_path=await _path_for(db, name, payload.parent_id),
    )
    db.add(topic)
    await db.flush()
    await audit_service.record_audit(
        db, action="topic.created", actor_id=admin.id, target_type="topic", target_id=topic.id, after={"name": name}
    )
    counts = await _question_counts(db, [topic.id])
    return await _node(db, topic, counts)


@router.patch("/{topic_id}", response_model=None)
async def update_topic(
    topic_id: UUID,
    payload: q_schemas.TopicUpdate,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    topic = await db.get(Topic, topic_id)
    if topic is None:
        raise NotFound("Topic not found")
    changes = payload.model_dump(exclude_unset=True)

    name = topic.name
    if "name" in changes:
        name = (changes["name"] or "").strip()
        if not name:
            raise ValidationFailed("a topic needs a name")
    parent_id = topic.parent_id
    if "parent_id" in changes:
        await _reject_cycle(db, topic.id, changes["parent_id"])
        parent_id = changes["parent_id"]

    # Checked before anything is mutated: a rename or a move can put two siblings on
    # one name exactly as easily as a create can, and refusing it must not have to
    # rely on the transaction being rolled back afterwards.
    if name != topic.name or parent_id != topic.parent_id:
        clash = await _sibling_clash(db, name, parent_id, exclude_id=topic.id)
        if clash is not None:
            raise Conflict("topic_exists", "This parent already has a topic with that name")
        topic.name = name
        topic.parent_id = parent_id
    if "language" in changes:
        topic.language = changes["language"]

    # Renaming or moving changes the materialised path of everything below it.
    await _refresh_subtree_paths(db, topic)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="topic.updated",
        actor_id=admin.id,
        target_type="topic",
        target_id=topic.id,
        after={"fields": sorted(changes), "level_path": topic.level_path},
    )
    counts = await _question_counts(db, [topic.id])
    return await _node(db, topic, counts)


@router.delete("/{topic_id}", response_model=None)
async def delete_topic(
    topic_id: UUID,
    admin: AdminUser = Depends(deps.get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    topic = await db.get(Topic, topic_id)
    if topic is None:
        raise NotFound("Topic not found")

    children = (
        await db.execute(select(func.count()).select_from(Topic).where(Topic.parent_id == topic.id))
    ).scalar_one()
    if children:
        raise Conflict(
            "topic_in_use", f"Move or remove the {children} subtopic(s) under '{topic.name}' first"
        )
    # Every link counts, including those on trashed questions: deleting the topic
    # would drop them, and a restored question would silently lose its filing.
    used_by = (
        await db.execute(select(func.count()).select_from(QuestionTopic).where(QuestionTopic.topic_id == topic.id))
    ).scalar_one()
    if used_by:
        raise Conflict("topic_in_use", f"'{topic.name}' still labels {used_by} question(s)")

    name = topic.name
    await db.delete(topic)
    await db.flush()
    await audit_service.record_audit(
        db, action="topic.deleted", actor_id=admin.id, target_type="topic", target_id=topic_id, before={"name": name}
    )
    return {"ok": True, "deleted": str(topic_id)}
