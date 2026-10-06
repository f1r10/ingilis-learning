# Language Learning Platform

A production-grade, **self-hostable** web platform for one private teacher and their
students: Language Learning Content Management, Practice, Examination, Document
Import, Student Monitoring and Analytics.

The surface is meant to feel like a simple teacher tool; internally it is a robust
content / examination / analytics platform. It is **not** hard-coded to English and
its interface ships in **Azerbaijani, English, Russian and Turkish**. All branding is
editable in Admin Settings — the platform name is never hard-coded.

> **Current status:** this repository contains the runnable foundation — full domain
> schema, authentication, users/groups, branding + localization, and the local-first
> AI/OCR adapter + background-worker skeleton (implementation phases 1–2 of the
> delivery order). The remaining modules (Question Bank UI, catalog/exam/attempts,
> import review, monitoring, analytics, exports, backups) build on this foundation
> **without schema changes** — the complete data model is already in place.
>
> Phase 1–2 is accepted by one command: **`make verify-phase12`** (see
> [Tests](#tests)). It is the only run that may claim the database, Redis and object
> storage work, because unlike `pytest` it treats a skipped infrastructure check as a
> failure.

---

## Architecture

Independent frontend and backend, deployable from GitHub to the teacher's own Linux
server with Docker Compose. No Lovable / Supabase dependency in production; **all
persistent data lives in the self-hosted backend.**

| Layer            | Choice                                             |
|------------------|----------------------------------------------------|
| Frontend         | React + TypeScript + Vite, React Router, TanStack Query, i18next |
| Backend          | Python FastAPI, versioned REST (`/api/v1`)         |
| ORM / DB         | SQLAlchemy 2.0 (async) + PostgreSQL 16 (JSONB)     |
| Migrations       | Alembic (async env wired to ORM metadata)          |
| Cache / queue / presence | Redis 7 + `arq` background worker          |
| Object storage   | S3/MinIO-compatible (bundled MinIO)                |
| Auth             | Argon2id hashing, HttpOnly signed session cookies, double-submit CSRF, Redis login rate-limit |
| AI / OCR / STT / Translation | Replaceable provider adapters, **local-first, all optional** |

```
.
├── docker-compose.yml        # postgres, redis, minio, backend, worker, frontend
├── Makefile                  # dev shortcuts + `make verify-phase12`
├── scripts/
│   └── verify_phase12.sh     # mandatory Phase 1-2 acceptance gate (see Tests)
├── .env.example              # all env-based configuration (copy to .env)
├── backend/
│   ├── app/
│   │   ├── core/             # config, security (argon2/sessions), db, redis, storage,
│   │   │                     # enums, middleware (CSRF + request context), rate_limit, exceptions
│   │   ├── models/           # full domain schema (identity, system, content, assessment, activity, ops)
│   │   ├── api/v1/           # routers: auth, admin, students, groups, settings, health
│   │   ├── schemas/          # pydantic v2 request/response models
│   │   ├── services/         # auth, settings/branding, audit
│   │   ├── adapters/         # AI/OCR/STT/translation/dictionary protocols + registry + impls
│   │   ├── workers/          # arq worker: import pipeline, attempt expiry (authoritative timer), trash purge
│   │   └── main.py           # FastAPI app factory
│   ├── migrations/           # alembic env + bootstrap schema revision
│   └── scripts/seed.py       # minimal dev seed (admin + languages + branding defaults)
└── frontend/
    └── src/                  # api client, i18n (az/en/ru/tr), auth/session, branding shell, pages
```

### Key domain invariants (already encoded in the model)

- **Source ≠ Question. Catalog ≠ Exam.** Kept as separate entities.
- **Immutable question snapshots.** `QuestionVersion` is created on meaningful edits;
  `ExamItem` and `AttemptAnswer` pin a `question_version_id`, so editing a question
  never changes historical results, and old results are never recalculated.
- **Explicit content dependency.** `Question.context_kind` + `reading_id` /
  `listening_id` make reading-/listening-bound questions structurally unable to
  silently become detached random questions.
- **No duplicated physical content.** Catalogs and source collections reference the
  central banks via join rows (`CatalogItem`, `source_file_id`), not copies.
- **Secrets are hashed.** Passwords, student access keys and recovery codes store
  Argon2id hashes only; plaintext is shown exactly once.
- **Soft delete everywhere.** Students and content use `deleted_at` (trash), never
  hard-deleted by default.

---

## Local development

Prerequisites: Docker + Docker Compose. (For backend-only work: Python 3.11+ and a
reachable Postgres/Redis.)

```bash
cp .env.example .env
# edit .env: set SESSION_SECRET, CSRF_SECRET, POSTGRES_PASSWORD, BOOTSTRAP_ADMIN_PASSWORD ...
openssl rand -hex 32   # run twice, paste into SESSION_SECRET / CSRF_SECRET

# Full stack (creates DB, runs migrations, serves API + app):
docker compose up --build

# First-run database seed (bootstrap admin + languages + branding defaults):
docker compose exec backend python -m scripts.seed
```

Then open **http://localhost:5173** (frontend) and **http://localhost:8000/docs** (API).

### Backend-only dev (hot reload)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# run postgres/redis/minio via docker compose for just those services:
docker compose up -d postgres redis minio
export $(grep -v '^#' ../.env | xargs)
alembic upgrade head
python -m scripts.seed
uvicorn app.main:create_app --factory --reload
```

### Frontend-only dev

```bash
cd frontend
npm install
npm run dev            # vite dev server, /api proxied to :8000 (cookie-friendly)
npm run typecheck      # tsc --noEmit
```

---

## Environment variables

See `.env.example` for the annotated, complete list. Highlights:

| Var | Purpose |
|-----|---------|
| `SESSION_SECRET` / `CSRF_SECRET` | Sign session + CSRF cookies. Generate with `openssl rand -hex 32`. |
| `COOKIE_SECURE` | `true` in production behind TLS. |
| `FRONTEND_ORIGIN` | CORS allow-list (only this origin) and cookie scoping. |
| `DATABASE_URL` / `REDIS_URL` | Datastores. |
| `OBJECT_STORAGE_*` | S3/MinIO endpoint, credentials, bucket. |
| `MAX_VIDEO_UPLOAD_MB` / `MAX_FILE_UPLOAD_MB` | Configurable upload limits (default video 700 MB). |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | Admin created by the seed; changeable later in Settings. |
| `ENABLED_UI_LANGUAGES` / `LEARNING_LANGUAGES` / `TRANSLATION_LANGUAGES` | Seed defaults; overridable at runtime in Admin Settings. |
| `AI_PROVIDER` / `OCR_PROVIDER` / `STT_PROVIDER` / `TRANSLATION_PROVIDER` | `none` (default) or a local/paid option. |

**Secrets are only ever read by the backend.** Nothing sensitive is placed in the
frontend bundle.

---

## Database migrations

```bash
cd backend
alembic upgrade head                                  # apply (runs automatically in compose)
alembic upgrade head --sql                            # the exact DDL, offline, no database needed
alembic downgrade head:base --sql                     # and its reverse
alembic revision --autogenerate -m "add <feature>"    # every future schema change
alembic downgrade -1
```

`0001_bootstrap` is a **frozen, explicit DDL script**. It calls `op.create_table`,
`op.create_index`, `op.create_unique_constraint` and `op.create_foreign_key` for the
45 tables of the Phase 1-2 schema and mirrors each of them in `downgrade()`. It does
**not** import `Base`, does not read `Base.metadata`, and contains no
`create_all()`/`create_table(..., metadata)` - the migration never inspects the
current ORM state at run time. Running `alembic upgrade head` therefore reproduces
exactly the schema this revision describes, even if somebody later edits a model
without adding a new revision.

That property is enforced, not just documented:

| Check | What it proves | Needs a DB? |
|-------|----------------|-------------|
| `tests/test_migration_offline_sql.py` | `upgrade head --sql` compiles, and the emitted DDL has the expected CREATE TABLE / INDEX / FOREIGN KEY / UNIQUE counts | no |
| `tests/test_migration_schema_consistency.py` | the tables, columns, types, widths, nullability, server defaults, PKs, FKs, uniques and indexes created by the revision equal those the models declare | no |
| `tests/test_migration_immutability.py` | no revision module imports application ORM metadata or calls `create_all` (the dynamic-schema pattern cannot come back) | no |
| `tests/integration/test_schema_live.py` | the same comparison against **real Postgres introspection** | yes |

Enums are stored as `VARCHAR` holding the member name (e.g. `'ACTIVE'`) with no
native `CREATE TYPE` and no `CHECK` constraint, so a new enum member never needs a
migration. Add future changes as **granular autogenerated revisions**; never hand-edit
the bootstrap revision once real deployments exist.


---

## Background worker

Imports, OCR, document conversion, AI extraction, transcription, media processing,
exports, backups and exam timer enforcement must never freeze the web UI.

```bash
arq app.workers.main.WorkerSettings        # started automatically by the `worker` service
```

Job statuses surfaced to the teacher in plain language:
`Queued → Processing → Needs Review / Completed / Failed`, with retry.
Crons: attempt auto-submission every 15s window (server is authoritative on timers),
daily trash purge.

---

## Authentication

- **Teacher/admin:** username + password (both changeable in Admin Settings), Argon2id,
  normal server session via HttpOnly cookie, no mandatory 2FA.
- **Password recovery:** 5 randomly generated one-time codes. Stored as hashes only;
  each code works once; generating a new set invalidates the previous set.
  `POST /api/v1/admin/recovery-codes` (returns plaintext once).
- **Student:** login with a single **Access Key** field of the form
  `username@LONG_RANDOM_SECRET` (≥192-bit entropy). Raw keys are never stored after
  display. Students cannot change their key; the teacher can revoke, rotate,
  disable/archive, and terminate active sessions. One account may be used on multiple
  devices (multiple concurrent `StudentSession` rows).

---

## API (v1) implemented so far

```
GET  /api/v1/ping                     liveness + DB/Redis checks
GET  /api/v1/auth/bootstrap           public branding + UI language config (login screen)
POST /api/v1/auth/admin/login
POST /api/v1/auth/admin/recovery/login
POST /api/v1/auth/student/login
POST /api/v1/auth/logout              server-side: terminates the student session row
                                      and bumps the admin session_epoch (old cookies die)
GET  /api/v1/auth/me/admin | /me/student
POST /api/v1/admin/recovery-codes
POST /api/v1/admin/password            PATCH /api/v1/admin/username
CRUD /api/v1/students                  + suggest-username, access-key rotate/revoke,
                                       sessions list/terminate, status change
CRUD /api/v1/groups
GET/POST /api/v1/groups/{id}/members   list / add a student (409 on duplicate)
DELETE /api/v1/groups/{id}/members/{student_id}
GET/PUT /api/v1/settings/branding | /ui | /{category}
```

Admin sessions are stateless signed cookies, but each embeds a `session_epoch`;
logout or a password change bumps it so every previously issued admin cookie is
rejected server-side. Student sessions have a DB row and are terminated on logout,
key rotation/revocation, and when the student is disabled or archived.


---

## AI / OCR / STT / Translation (optional, local-first)

Business logic depends only on the adapter **protocols** in
`backend/app/adapters/base.py`; the active provider is chosen at runtime in Settings
(see `registry.py`). The platform is **fully usable with every provider disabled** and
will remain so without Gemini.

| Capability | Preferred free/self-hosted adapter | Optional paid |
|------------|-------------------------------------|---------------|
| Document parser | Docling-compatible | — |
| OCR | PaddleOCR / Tesseract | — |
| Local AI | Ollama / any OpenAI-compatible `/v1` | Gemini (teacher key) |
| Speech-to-text | Whisper | — |
| Translation | LibreTranslate / Argos | — |
| Dictionary enrichment | Wiktionary / Wiktextract / Kaikki | — |

AI is only used where the teacher explicitly enables it; AI-extracted decisions
normally route to the Import Review screen unless the teacher selects Auto mode.
Deterministic tasks must not call AI. `OpenAICompatibleAI` (Ollama) and `GeminiAI`
are wired as reference implementations; the OCR/parser/STT/translation/dictionary
adapters are intentionally **disabled stubs** until their service endpoints are
configured, so nothing pretends to run.

---

## Production deployment

1. Provision a Linux server with Docker + Compose and a domain.
2. Put the repo on the server (or deploy from GitHub via your CI or
   `docker compose up -d`).
3. Create a hardened `.env`:
   - strong `POSTGRES_PASSWORD`, `SESSION_SECRET`, `CSRF_SECRET`, MinIO keys
   - `APP_ENV=production`, `COOKIE_SECURE=true`
   - `FRONTEND_ORIGIN=https://your.domain`
   - `OBJECT_STORAGE_ENDPOINT=https://your-object-store` if using external storage
4. `docker compose up -d --build` → `docker compose exec backend python -m scripts.seed`.
5. Put Nginx/Caddy/Traefik in front for TLS termination. The frontend container already
   proxies `/api/` to the backend so cookies stay first-party.
6. Sign in, then immediately change the admin username/password and generate a fresh
   recovery-code set.

> Run only the `backend` + `frontend` + `worker` behind the reverse proxy; keep
> Postgres, Redis and MinIO private (do not expose their ports in production).

---

## Backup / restore

- `Backup` and `ExportJob` are modeled; the worker and a Management screen drive
  manual "Create Backup", scheduled backups (DB dump + media), and a restore workflow
  with strong confirmation.
- Default analytics/activity retention is **permanent**; cleanup is teacher-configurable
  (`ACTIVITY_RETENTION_DAYS`, `TRASH_RETENTION_DAYS`).
- **Portable packages:** export a catalog/content bundle (manifest JSON + questions +
  vocabulary + reading/listening metadata + selected media) with no proprietary
  lock-in, for import into another deployment.

*The backup/restore job handlers are stubs in this foundation and are completed in the
operations phase; the data model and job plumbing already exist.*

---

## Design principles honored in the UI

Plain language, visible state, sensible defaults, progressive disclosure, simple
buttons, confirmation dialogs, preview-before-destructive, undo/trash. The teacher
never sees developer terminology ("Processing document", not "Worker job #81 running
OCR pipeline"). Typography is Arial, the interface is mostly white/light with a single
editable accent color, no gradient or AI-color clutter, fully responsive, and the
student side is tuned for mobile.

---

## Roadmap (delivery order)

Implemented: **1) shell/branding/localization, 2) auth + users/groups** + the complete
schema foundation and adapter/worker skeletons.

Next: 3) Question Bank + full question engine · 4) vocabulary · 5) reading/listening/media
· 6) catalogs · 7) exams/attempts · 8) document import + review · 9) monitoring/activity
· 10) analytics · 11) exports/backups · 12) local AI/OCR/transcription · 13) security &
performance hardening.

## Tests

There are **two deliberately different tiers**. Do not read one as the other.

### 1. Developer loop - fast, may skip, never an acceptance result

```bash
cd backend
pytest tests                     # everything; the integration half self-skips
pytest tests -k "not integration"  # only the offline half
```

This runs with **no Postgres, no Redis, no MinIO**: pure crypto/config logic, the
CSRF double-submit middleware, the unified error envelope, the rate-limiter logic,
app/OpenAPI wiring, and all the offline migration checks (they compile the
migration's DDL instead of applying it). The integration tests print their skip
reason and are counted as `skipped`.

**A run in which the integration tests skipped has verified nothing about the
database, Redis or object storage.** Green here does not mean Phase 1-2 accepted.

### 2. Mandatory Phase 1-2 acceptance test - `make verify-phase12`

```bash
make verify-phase12
# equivalent, and the form to use if `make` is not installed:
bash scripts/verify_phase12.sh
```

One command, deterministic, and **it does not report success if any required
infrastructure was skipped**. It:

1. checks Docker (CLI + a reachable daemon) and the local virtualenv/deps
2. starts PostgreSQL
3. starts Redis
4. starts MinIO
5. **waits for each service's real healthcheck** (plus a TCP/`pg_isready` proof
   from this machine) instead of sleeping and hoping
6. creates a **fresh, isolated verification database** (`app_verify_phase12` by
   default), refusing to touch anything whose name is not `*_test`/`*_verify_phase12`
   and asserting it is empty before migrating
7. runs `alembic upgrade head` - the committed migration, twice, proving the
   second application is a clean no-op - then reads the schema back from a **new
   process** and asserts `alembic_version`, table, FK and index counts
8. runs the seed **twice** and compares row counts to prove idempotency
9. runs the offline unit/static suite
10. runs the **full integration suite** (Postgres + Redis + MinIO)
11. exercises MinIO object storage through the application's own storage
    abstraction and confirms the verify bucket exists
12. starts the ARQ worker and checks that it comes up and stays up with every
    optional adapter disabled

Configuration for the run is generated (throwaway session/CSRF secrets, throwaway
bootstrap admin password) and pointed at the isolated database, Redis db 1 and a
`-verify` bucket, so it never uses a real secret and never touches development
data. Nothing prints a credential.

Every step is mandatory: a step that fails, or whose tests **skip**, is recorded as
a failure. The script lists all failed steps and exits non-zero; only a full pass
ends with `All mandatory Phase 1-2 checks passed against live PostgreSQL, Redis and
MinIO.` Flags: `--down` stops the services afterwards, `--keep` keeps the
verification database for inspection. Reports and the worker log land in
`backend/.verify-phase12/`.

`make test-integration` remains available when you have started the services
yourself; like plain `pytest`, it skips instead of failing when one is missing, so
use `verify-phase12` for acceptance.

### What the integration tier covers

Admin: seed/bootstrap login, invalid login (identical envelope for wrong password
and unknown username), authenticated request, logout + **replay of the old cookie
is rejected**, password change invalidating **all** previous sessions, recovery code
working exactly once and reuse failing, `session_epoch` default/embedding/verification/
monotonic increment under five concurrent invalidations.
Students: create, duplicate username (409), read/list, update, disable (live sessions
terminated, valid key then refused with 403), reactivate, rotate access key (old key
dead immediately, exactly one ACTIVE row survives), revoke, revoked key refused,
archive hiding the student.
Groups: create, update, membership add/duplicate/list/remove, invalid student and
group ids, cascade integrity on raw deletes.
CSRF on real authenticated routes: missing token 403, wrong token 403, a foreign
session's token 403, correct token 2xx.
Rate limiting with real Redis at **production** thresholds: admin 8/60s, student
10/60s, recovery 5/300s, the per-key window TTL and the return to normal behaviour
after expiry.
Races (concurrent requests and raw transactions): one recovery code consumed
exactly once, concurrent rotations leaving one ACTIVE key, duplicate group
membership rejected by `uq_group_student`, concurrent disable + revoke.
MinIO: bucket init, upload, retrieve, byte-for-byte compare, delete, confirm
deletion - through `app.core.storage.ObjectStorage`, the abstraction the app uses.
Schema: live introspection of tables/columns/types/nullability/PKs/FKs/uniques/
indexes/server defaults against the models, including the partial unique index that
enforces "one ACTIVE access key per student".

```bash
# Lint - the rule families are pinned in `backend/pyproject.toml` ([tool.ruff.lint])
# so the gate means the same thing on every ruff upgrade.
cd backend && ruff check app tests migrations scripts

# Static type-check: `mypy` is listed in the dev extra but is NOT part of the
# Phase 1-2 acceptance gate (it is not installed in the shipped venv), so install
# it first if you want the signal: pip install -e ".[dev]" && mypy app

# Frontend type-check (no redesign, compatibility only)
cd frontend && npm install && npm run typecheck
```

