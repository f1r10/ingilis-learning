# Language Learning Platform

A production-grade, **self-hostable** web platform for one private teacher and their
students: Language Learning Content Management, Practice, Examination, Document
Import, Student Monitoring and Analytics.

The surface is meant to feel like a simple teacher tool; internally it is a robust
content / examination / analytics platform. It is **not** hard-coded to English and
its interface ships in **Azerbaijani, English, Russian and Turkish**. All branding is
editable in Admin Settings — the platform name is never hard-coded.

> **Current status:** delivery phases 1-8 are implemented and accepted — the complete
> domain schema, authentication, users and groups, branding + localization (az/en/ru/tr),
> the question bank with the full question engine, the vocabulary bank with learner study
> cards, the reading, listening and media modules on S3/MinIO object storage, the
> catalog builder with the learner's practice shelf, and the exam builder with assignments,
> the server-timed attempt engine and the teacher's grading queue: papers that pin the
> question version each line was answered at, sittings a learner can lose their tab to and
> come back to, marks a re-mark can move, and refusals the screen says in the learner's own
> language — plus the document importer, which reads seven formats from their own bytes with
> this repository's own parsers, turns them into candidate cards, and files content only when
> a person approves a card. Still to
> come: monitoring, analytics, exports/backups/restore,
> the local AI/OCR/STT/translation adapters, and the final security and performance pass.
> The adapter protocols and the worker exist now as disabled-by-default paths on purpose —
> the product must stay fully usable with every provider set to `none`.
>
> Acceptance is one command: **`bash scripts/verify_phase12.sh`** (also
> `make verify-phase12`; see [Tests](#tests)). It is the only run that may claim the
> database, Redis and object storage work, because unlike `pytest` it treats a skipped
> infrastructure check as a failure, and it needs no developer `.env` and no `make`.

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
│   └── verify_phase12.sh     # mandatory acceptance gate: 13 steps, whole product (see Tests)
├── .env.example              # all env-based configuration (copy to .env)
├── backend/
│   ├── app/
│   │   ├── core/             # config, security (argon2/sessions), db, redis, storage
│   │   │                     # (S3/MinIO adapter), media_types (byte sniffing), enums,
│   │   │                     # middleware (CSRF + request context), rate_limit, exceptions
│   │   ├── models/           # full domain schema (identity, system, content, assessment, activity, ops)
│   │   ├── api/v1/           # routers: auth, admin, students, groups, settings, health,
│   │   │                     # questions, topics, tags, vocabulary, media, reading,
│   │   │                     # listening, catalogs, exams + grading, and the /student mirrors
│   │   │                     # of them (practice, exams, attempts)
│   │   ├── schemas/          # pydantic v2 request/response models
│   │   ├── services/         # auth, settings/branding, audit, question bank + engine,
│   │   │                     # taxonomy, vocabulary, media library, passage/reading/listening,
│   │   │                     # catalog, activity log, practice runs, exams, attempts + grading
│   │   ├── adapters/         # AI/OCR/STT/translation/dictionary protocols + registry + impls
│   │   ├── workers/          # arq worker: import pipeline, attempt expiry (authoritative
│   │   │                     # timer), trash retention (reports what is due; removal waits
│   │   │                     # for the Phase 11 backup that makes it survivable)
│   │   └── main.py           # FastAPI app factory
│   ├── migrations/           # alembic env + frozen bootstrap + 0002..0006
│   └── scripts/seed.py       # minimal dev seed (bootstrap admin + languages + branding defaults)
└── frontend/
    └── src/                  # api client, i18n (az/en/ru/tr), branding shell, teacher and
                              # student pages: banks, editors, media library, reading, listening,
                              # catalogs, exams, the grading queue, the runner and its result
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
| `MAX_DOCUMENT_UPLOAD_MB` | Ceiling for one document handed to the importer (default 60). Separate because a document is *read*, not served: enforced while the bytes are still streaming, and published by `GET /imports/meta` so the screen states the number in its own refusal instead of guessing it. |
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

The history is linear and short by design: `0001_bootstrap`,
`0002_vocabulary_word_unique`, `0003_membership_and_checksum` (the two set-membership
tables plus `uq_media_asset_checksum`), `0004_media_reference_indexes`,
`0005_catalog_item_unique` (one piece of content named once per catalog) and
`0006_exam_attempt_integrity` (one assignment per student and per group, one attempt number,
one review row per answer, and the index the expiry sweep reads). The guard
scripts replay every revision in order without importing the ORM
(`tests/migration_replay.py`), so the bootstrap counts stay pinned at 45 tables / 90
indexes while the **live** floors grow with each phase - after Phase 7 a fresh process
reading the migrated database sees 47 model tables (48 with `alembic_version`), 486
columns, 61 foreign keys and 154 indexes. The acceptance gate walks the chain in both
directions for real: `downgrade 0001_bootstrap` proves each later revision removes only
what it added, then `upgrade head` rebuilds each index over rows staged underneath it and
refuses the duplicate the index exists to refuse.


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

GET  /api/v1/questions/types           the type vocabulary the editor is built from
GET  /api/v1/questions                 q/type/status/level/learning_language/
                                       context_kind/topic_id/tag_id/reading_id/
                                       listening_id/source_file_id/has_media/
                                       view(bank|trash|all)/sort/order/page/page_size
POST /api/v1/questions                 201, first version stored as v1
GET/PATCH /api/v1/questions/{id}       PATCH writes a new immutable QuestionVersion
DELETE /api/v1/questions/{id}          soft delete -> trash (restore keeps history)
POST /api/v1/questions/{id}/status | /restore | /clone | /taxonomy
GET  /api/v1/questions/{id}/preview    the learner payload: answer key never leaves
GET  /api/v1/questions/{id}/versions | /versions/{n}
POST /api/v1/questions/{id}/grade      scores now, `?at_version=n` scores a frozen version
POST /api/v1/questions/bulk            status/trash/restore/add_topic/remove_topic/
                                       add_tag/remove_tag/set_level/set_language,
                                       answered per id (updated / refused / not_found)
CRUD /api/v1/topics                    tree with question counts; rename/move refreshes
                                       the materialised path and refuses sibling clashes
CRUD /api/v1/tags                      flat, case-insensitively unique, deletable only
                                       when nothing references it

GET  /api/v1/vocabulary/meta           languages / levels / word types the editor builds from
GET  /api/v1/vocabulary                q/q_kind(contains|word_only|exact|starts_with)/
                                       learning_language/translation_language/level/
                                       part_of_speech/status/tag_id/has_audio/
                                       view(bank|trash|all)/sort/order/page/page_size
POST /api/v1/vocabulary                201; 409 word_exists when the live bank has it
GET/PATCH /api/v1/vocabulary/{id}      PATCH replaces the translation and example sets
                                       it mentions, and refuses an unknown key
DELETE /api/v1/vocabulary/{id}         soft delete -> trash, status and links preserved
POST /api/v1/vocabulary/{id}/restore   409 if another word took the slot meanwhile
POST /api/v1/vocabulary/{id}/status | /taxonomy
GET  /api/v1/vocabulary/{id}/preview   the learner's study card, on the teacher's screen
POST /api/v1/vocabulary/bulk           status/trash/restore/add_tag/remove_tag/set_level,
                                       answered per id (updated / refused / not_found)
GET  /api/v1/student/vocabulary        ready, alive words; search stays on the word
GET  /api/v1/student/vocabulary/meta   the learner's own filter options
GET  /api/v1/student/vocabulary/{id}   study card; a draft or trashed word is 404

GET  /api/v1/media/meta                formats accepted and size ceilings per kind
GET  /api/v1/media                     q/kind/source_origin/view(bank|trash|all)/sort
POST /api/v1/media                     multipart upload; the bytes decide the kind
GET/PATCH/DELETE /api/v1/media/{id}    read / caption or report measured duration+size /
                                       soft trash (refused while live content needs it)
POST /api/v1/media/{id}/restore | /bulk
GET  /api/v1/media/{id}/content        the file's bytes, one Range honoured (teacher)
GET  /api/v1/student/media/{id}/content the same file, only when ready content points at it

GET  /api/v1/reading/meta              languages, levels, layouts, sort names, caps
GET  /api/v1/reading                   q/language/level/status/layout/view/sort
POST /api/v1/reading                   201; word_count counted from the body, not sent in
GET/PATCH/DELETE /api/v1/reading/{id}  PATCH touches only what it sends; a body carrying
                                       `word_count` is refused, and trash keeps the questions
POST /api/v1/reading/{id}/status | /restore | /bulk
GET  /api/v1/reading/{id}/sets         the sets with the questions filed under each
POST /api/v1/reading/{id}/sets         append a set
POST /api/v1/reading/{id}/sets/reorder renumber the sets (must name every one)
POST /api/v1/reading/sets/{set_id}/questions  file exactly these questions, in order
PATCH/DELETE /api/v1/reading/sets/{set_id}     rename / remove the grouping, which sends
                                       its questions back to the pool
GET  /api/v1/reading/{id}/preview      the learner's reading block, on the teacher's screen
GET  /api/v1/student/reading | /meta | /{id}   ready texts; draft questions excluded

GET  /api/v1/listening/meta            languages, levels, replay rules, sort names
GET  /api/v1/listening                 q/language/level/status/has_audio/show_transcript
POST /api/v1/listening                 names a file already in the library
GET/PATCH/DELETE /api/v1/listening/{id} PATCH never claims transcript provenance
POST /api/v1/listening/{id}/status     refused when nothing could be heard
POST /api/v1/listening/{id}/restore | /bulk
GET/POST /api/v1/listening/{id}/sets | /sets/reorder
PATCH/DELETE /api/v1/listening/sets/{set_id}    a block may be a slice of the recording
GET  /api/v1/listening/{id}/preview    the player a learner would get
GET  /api/v1/student/listening | /meta | /{id}  ready recordings; transcript only if allowed

GET  /api/v1/catalogs/meta             kinds, views, statuses, timings, the caps
GET  /api/v1/catalogs                  q/kind/language/level/status/view(bank|trash|all)/
                                       parent_id/root_only/sort/order/page/page_size
POST /api/v1/catalogs                  201; a folder and a list at once, depth capped at 4
GET/PATCH/DELETE /api/v1/catalogs/{id} read (with the path above it) / rename / soft trash,
                                       which takes the folders inside it too
POST /api/v1/catalogs/{id}/restore | /status | /preview
GET  /api/v1/catalogs/{id}/items       the references, each described from its own bank row
POST /api/v1/catalogs/{id}/items       append; all checked or nothing written, no duplicates
POST /api/v1/catalogs/{id}/items/reorder  the whole list in the new order
PATCH/DELETE /api/v1/catalogs/items/{item_id}  re-point one block / drop one reference,
                                       which never touches the content it named
POST /api/v1/catalogs/bulk             status/trash/restore/set_level, answered per id

GET  /api/v1/student/practice/meta     the learner's own filter options
GET  /api/v1/student/practice/catalogs  openable collections, with this learner's run counts
GET  /api/v1/student/practice/catalogs/{id}  the same row, plus their recent runs
POST /api/v1/student/practice/catalogs/{id}/run  opens a run, returns a 32-hex session token
GET  /api/v1/student/practice/runs/{session}      the run as its activity log describes it
GET  /api/v1/student/practice/runs/{session}/steps  the steps, in the order they were served
POST /api/v1/student/practice/answer   one answer, graded by the bank's own rules
POST /api/v1/student/practice/runs/{session}/finish  closes the run and releases the marks
GET  /api/v1/student/practice/catalogs/{id}/known | POST the same  known / learning marks
GET  /api/v1/student/favorites | POST "" | POST /remove  what the learner saved to revisit

GET  /api/v1/exams/meta                statuses, kinds, timings, visibility rules, the caps
GET  /api/v1/exams                     q/status/language/level/view(bank|trash|all)/sort
                                       /order/page/page_size, each row with its own counts
POST /api/v1/exams                     201, always a draft, rules set as asked
GET/PATCH/DELETE /api/v1/exams/{id}    read whole (items pinned to their version, sections,
                                       assignments, publish blockers) / edit / soft trash,
                                       which leaves the sittings it already holds alone
POST /api/v1/exams/{id}/status         the lifecycle table's moves only; publishing checks
                                       the paper is servable and refuses with the same codes
                                       its blockers name
POST /api/v1/exams/{id}/restore | /clone | /preview | /bulk
GET/POST /api/v1/exams/{id}/items      a question, or every question filed under a text or
                                       a recording - references, never copies
POST /api/v1/exams/{id}/items/reorder  the whole paper in the new order
PATCH/DELETE /api/v1/exams/items/{item_id}  this line's mark or its section / drop the line,
                                       which never removes the question it named
GET/POST /api/v1/exams/{id}/sections | /sections/reorder
PATCH/DELETE /api/v1/exams/sections/{section_id}  removing a part sends its lines back to
                                       the unsectioned run, it does not delete them
GET  /api/v1/exams/{id}/assignments | POST the same   hand the paper out to students or
                                       groups (a group is read live, so a member who joins
                                       tomorrow is covered); DELETE one row to withdraw it
GET  /api/v1/exams/{id}/attempts | /attempts/{attempt_id}  the sittings and one sitting as
                                       the teacher sees it, with the marks still owed

GET  /api/v1/grading/queue             answers waiting for a person, one row per answer
GET  /api/v1/grading/answers/{review_id} | POST the same /grade   the answer, then the mark
                                       with the note the learner will read (re-marking is
                                       allowed and audited with the value it moved from)
GET  /api/v1/grading/summary           what is waiting, and who is waiting for it
POST /api/v1/grading/attempts/{attempt_id}/feedback | DELETE /feedback/{id}
GET  /api/v1/grading/students/{student_id}/feedback | /meta

GET  /api/v1/student/exams | /meta | /{exam_id}   the papers this learner was handed, with
                                       each one's own state and their own history on it
POST /api/v1/student/exams/{exam_id}/start  opens the sitting, or hands back the live one
GET  /api/v1/student/exams/{exam_id}/attempts
GET  /api/v1/student/attempts/{token} | /steps | /result   the sitting, its order, its result
POST /api/v1/student/attempts/answer        autosave one line, graded by the bank's rules
POST /api/v1/student/attempts/submit        close it through the one path every closing uses
POST /api/v1/student/attempts/tab-switch    the browser's own report, stored as such

GET  /api/v1/imports/meta             the kinds, remarks, statuses, levels, languages,
                                       sortable columns, page cap and upload ceiling the
                                       screen is built from - read from the code that
                                       enforces them
GET  /api/v1/imports                  the queues: q/status/kind/decision/sort/order/page
POST /api/v1/imports                  201 with the job; the same bytes again answer 200
                                       with `{job, duplicate: true}` and the queue that
                                       already exists; `?auto=true` files every complete
                                       card as it is read
GET  /api/v1/imports/{job_id}         the job, its counts and the reason it failed
DELETE /api/v1/imports/{job_id}       trash the queue; the stored file goes only when no
                                       other queue still cites it
GET  /api/v1/imports/{job_id}/document  the original bytes, as they were uploaded
POST /api/v1/imports/{job_id}/retry   read the paper again - refused with
                                       `job_has_candidates` once it produced any
GET  /api/v1/imports/{job_id}/items   the candidates, in the paper's own order, with the
                                       document's text, the teacher's correction, what is
                                       missing and where each row came from
PATCH /api/v1/imports/items/{item_id} edit `corrected` and `filing`; `extracted` is never
                                       overwritten, and a field the kind does not own is
                                       refused before it is stored
POST /api/v1/imports/items/{item_id}/decision  approve (writes through the bank's own
                                       services, one content item per candidate) or reject
                                       (a rejected row reopens to pending if it is edited)
POST /api/v1/imports/bulk             approve / reject / file over a page, answered row by
                                       row as done / refused / not_found
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

Implemented: **1) shell/branding/localization, 2) auth + users/groups,
3) Question Bank + full question engine, 4) vocabulary bank and study cards,
5) reading, listening and the media library on object storage,
6) catalogs and practice, 7) exams: authoring, assignments, the server-timed attempt engine
and the teacher's grading queue, 8) document import: seven formats parsed locally, a reviewed
candidate queue and approval that files into the banks** + the
complete schema foundation and adapter/worker skeletons.

Next: 9) monitoring/activity
· 10) analytics · 11) exports/backups · 12) local AI/OCR/transcription · 13) security &
performance hardening.

## Question engine (Phase 3)

Every question is one row plus one `QuestionVersion` row per saved revision. A edit
never rewrites history: `current_version` advances, and a past attempt can still be
re-scored from the snapshot it used (`POST /questions/{id}/grade?at_version=n`).

Types are a registry, not an enum column, so the editor and the learner payload are
built from `GET /questions/types` (`multiple_choice`, `multi_select`, `true_false`,
`short_answer`, `gap_fill`, `matching`, `ordering`, `translation`, `essay`). Each type
brings its own pydantic config (`extra="forbid"`), its own learner payload shape and
its own grader. Scoring knobs live in columns (`score`, `partial_scoring`,
`negative_scoring`), not inside the answer key.

Answer keys are never sent to a learner: `public_config()` strips them and replaces
the match/order elements with opaque refs (`sha256(role\x1findex\x1fvalue)[:12]`), so
the browser can grade a drag-and-drop without knowing which item was authored first.
Published matching/ordering items are additionally rotated off the authored order by
`_least_aligned()`, so the display never betrays the key.

Automatically gradable: everything except `essay`, which is queued for the teacher
(`requires_manual_grading`). Partial credit exists for `multi_select`, `gap_fill`,
`matching` and `ordering`, and negative marking for `multi_select` and `matching`.

## Vocabulary bank (Phase 4)

One row per live word, per learning language: `word`, `learning_language`, definition,
IPA, word type, level, synonym and antonym lists, a private teacher note, an optional
pronunciation asset and optional import provenance - plus two child sets, `translations`
(one meaning per language) and `examples` (a sentence, its language, its translation).

`0002_vocabulary_word_unique` adds the partial unique index
`uq_vocabulary_word_language (word, learning_language) WHERE deleted_at IS NULL`, so
`improve` in English and `improve` in Azerbaijani are two words while a second live
English `improve` is refused by the database itself, not only by the service's earlier
case-insensitive check. `POST /vocabulary` answers 409 `word_exists`, and a restore
answers 409 when another word has taken the slot ("merge them before restoring").
Trash is `deleted_at`, never a status: a binned word keeps its status, its tag links
and its children so a restore is exact, and it holds its tag hostage until then.

Lifecycle is `draft -> ready -> archived` through `POST /vocabulary/{id}/status`;
`PATCH /vocabulary/{id}` refuses `status` (and any unknown key) rather than ignoring it,
so a save can never claim to have published a word. A new word does get to choose its
starting status - `draft` or `ready`, the two states a word can enter - because writing a
word and publishing it in the same breath is the normal case, not an extra trip.

Learners get a study card, built by one function (`vocabulary_service.learner_view`)
that both `/student/vocabulary/{id}` and the teacher's `/vocabulary/{id}/preview` call -
the preview is the card, not a second copy of the rules. `notes`, `audio_asset_id`,
`source_file_id`, `deleted_at`, `status` and timestamps are never in it, and a learner's
search box is pinned to `q_kind=word_only`, so searching the list cannot match a private
note. A draft or trashed word answers 404 on the learner surface.

The word bank and the question bank share tags, and both counts are shown before a tag
is deleted (`{questions, words}`), because deletion is refused while either bank
references it.

## Reading, listening and media (Phase 5)

**Uploading is the only way a file enters the library, and its bytes decide what it is.**
`POST /media` reads the head of the file and stores it only if `app.core/media_types.py`
can name it: 17 formats (png, jpeg, gif, bmp, webp, tiff, mp3, wav, aiff, flac, ogg, m4a,
mp4, mov, webm, mkv, avi), each under a per-kind ceiling (15 MB image, 200 MB audio,
700 MB video) that `GET /media/meta` publishes so the browser refuses an oversized file
before sending it. The browser's `Content-Type`, the filename and the extension are all
discarded: the object key is generated (`<namespace>/<32 hex>.<ext>`), so a file called
`../../../../etc/passwd.png` cannot steer where it lands, and a PNG wearing a `.mp4` name
is stored as the image it is. Identical bytes are one asset - the sha256 is computed while
the upload spools, matched against the live rows, and for two requests that arrive together
settled by the partial unique index `uq_media_asset_checksum` from
`0003_membership_and_checksum`. The second teacher gets the existing asset and is told it
was already there, because that is information rather than a failure.

**Nothing is served around the application.** The bucket is private, no object is public
and no browser is ever handed a presigned URL. Every byte leaves through
`GET /media/{id}/content` (teacher) or `GET /student/media/{id}/content` (learner), each
honouring one byte range by asking the store for exactly that slice, and a learner may read
an asset only while some *ready*, un-trashed word, recording or question points at it.
Duration and pixel dimensions are reported by the player through `PATCH /media/{id}` and
stay `null` until it does: a number invented by a half-parser would end up on a learner's
timer.

**A reading and a listening are the same shape twice** - a body of content with ordered
sets, each holding some of the questions bound to it (`passage_service`, driven by a
`PassageKind` descriptor). Three rules do the real work. Filing a question under a set
writes a membership row and never touches `Question`, so reorganising a lesson cannot
append a dozen `QuestionVersion` snapshots of questions that did not change. A question
answers exactly one block of one passage: `question_id` is unique in the membership table,
so filing it in a second set *moves* it, and the response says so. And only a question
already bound to this passage (`context_kind` plus `reading_id` / `listening_id`) may be
filed in it - the binding is what stops a reading question from degrading into a loose one.

Derived fields are derived: `word_count` is counted from the text in the same transaction
(a `PATCH` that sends one is refused, not ignored), and `transcript_source` can only ever
come out as `manual` or `absent` here: `imported` is still reserved (the Phase 8 importer reads
documents, and a document carries no sound) and `auto` belongs to the Phase 12 speech adapter.
Going to `ready` is refused for a listening with no live
recording and no transcript - on its own or inside a bulk action - since an exercise with
nothing to hear is what a class discovers first. Clearing a transcript clears its cue
lines with it.

Learners get the passage plus only answerable questions: the gate is *ready and not
trashed* for the text or recording **and** for every question inside it, the reading body
is rendered once above its questions instead of inside each of them, and a listening's
transcript is `null` unless `show_transcript` was allowed - a listening exercise with its
words on the screen is a reading exercise, and the teacher decides which one this is.
`layout` (`above`, `split`, `tabbed`) is stored rather than inferred, because on a phone it
is a different exercise.

Trash is soft everywhere and the file is never thrown away with it: trashing a text or a
recording leaves its bound questions in the bank (they simply stop being served), and
trashing an asset is refused while live content references it, naming how many of each
kind. Playback rules (`replay_limit`, `allow_pause`, `allow_seek`) are stored as set and
served as delivered, so a player cannot quietly rewrite the conditions of the exercise.
This closes the Phase 4 gap: a word's pronunciation recording can now be attached from the
media picker and reaches the study card through the student route.

**The learner's player enforces the rules it was handed, and stops at the boundary.**
`replay_limit` counts the listens *after* the first one, so the budget is `1 + limit`
listenings; the page treats it as spent as soon as the last allowed one has started, which
is what removes the play affordance before an extra one can be offered. Reading the budget
as spent only after the next listening had already begun handed the class one replay more
than the teacher had written. `allow_seek` and `allow_pause` decide whether the browser's
own transport controls are drawn at all: a control the learner is not allowed to use is not
a control worth showing, and a player that still invites a fourth listening under a notice
that says there is none left is a decoration rather than a rule. The count is kept on the
page, so reloading restarts it - that is honest for a browse-anytime surface. On a paper the
same rules travel with the sitting's own frozen blueprint, and the learner is told which slice
of which recording they are being asked about; the counter itself is still the tab's, because a
replay limit the *server* enforced would mean one counted play per request, which is a
hardening decision rather than an authoring one.

**A learner's address asks for a learner's session.** The app restores whoever is holding
the cookie by probing the role endpoint that the address implies: a `/student…` URL tries
the learner endpoint first and a teacher URL tries the teacher endpoint first, falling
through to the other role before a browser is called signed out. A fixed order meant that
every learner page load carried one guaranteed 401 in its log, which read like a broken
session to anyone who opened the network panel.

Migrations: `0003_membership_and_checksum` (the two set-membership tables with their
unique and ordering indexes, and `uq_media_asset_checksum`) and
`0004_media_reference_indexes` (the three FK-side indexes media joins on). `0001_bootstrap`
and the Phase 3/4 revisions are untouched, and both new revisions downgrade.

## Catalogs and practice (Phase 6)

**A catalog is a table of references, and that is the whole design.** Each row of
`catalog_item` names one kind (`question`, `vocabulary`, `reading`, `listening`) and one id
in the bank that owns it, plus a `config` that may narrow it to a single block of that
text. Nothing is copied: correct a question in the bank and every collection that uses it
is corrected, and a catalog is always current because there is no second copy to go stale.
`uq_catalog_item_reference` from `0005_catalog_item_unique` makes `(catalog_id, kind,
ref_id)` unique in the live rows, so the same exercise cannot appear twice in one lesson
while still allowing two lessons to use it - and a reference a teacher removed can be added
again, because trashing the row leaves no tombstone in its way.

**The same table is a folder.** `parent_id` builds a tree of units and lessons, capped at
four levels deep, and a folder practises on its own: it may hold references as well as
children. The list therefore reports both numbers - how many references and how many
folders - because a teacher who cannot see that a unit contains four lessons cannot tell
an empty collection from an unopened one. A folder is never binned out from under what is
inside it: trashing one that still holds live folders is refused, and naming how many. A
bulk selection that holds a unit and its lessons is the exception - the action loops over
what it refused until it stops making progress, so the whole unit leaves the class's list
in one click while a folder that is genuinely still blocked is reported with its reason.
Restoring a catalog whose folder is itself trashed is allowed: the teacher must be able to
move it, and the chain rule keeps it invisible to learners until the folder is sorted out.

**Reading a reference is a join, not a stored string.** `title`, `detail` and `state` are
taken from the bank's row on every read, so a catalog cannot lie about its contents: if the
word behind a reference is now a draft, or was deleted, or moved to another level, the row
says `draft`, `missing` or the new level by itself. A state with no bank row behind it is
the honest answer to "what will my class see", and `available_to_learner` is that state
compressed to a yes. When a reference names a block of a text, the block is checked against
that text: a group that was deleted, or that belongs to a different passage, puts the row in
`broken_block` rather than serving the whole text and calling it the lesson.

**Adding is all-or-nothing, and the answer says what was added.** Every id is resolved
before anything is written, so a list of thirty references with one typo in it writes
nothing rather than twenty-nine things and a half-built lesson. Reordering takes the whole
list of item ids and refuses a request that omits one, because a partial order is a
guessed order. Bulk actions answer per id (`updated`, `refused` with the server's reason,
`not_found`) - "3 of 40 published" is not something a teacher can act on.

**A preview is the learner's screen without the write.** `GET /catalogs/{id}/preview` builds
the same steps, applies the same skipping and returns the same key-free projections, minus
the session token: nothing was started, so nothing can be filed against it. That is why the
teacher's preview and the learner's run cannot drift apart - they are the same function.

**Practice leaves a log, not a session table.** Opening a run issues a 32-hex token and
appends a `practice_run_start` event; each answer appends `practice_answer` with the grade
frozen into the payload; finishing appends `practice_run_finish`. Nothing is updated in
place, so a run's history is the activity log, and `GET /student/practice/runs/{token}`
rebuilds the score from those rows. A closed tab, a different device or a reload resumes
from the `shuffle_seed` stored in the start event - the order is re-derived, not
re-shuffled, and resuming does not write a second start. `time_spent_seconds` is what the
learner's screen measured and is stored as reported, with no server-side clock implied: an
authoritative timer is the attempt engine's job, not a practice shelf that a
learner may leave open. The exam engine has since grown exactly that clock (Phase 7); the
practice shelf deliberately still does not use it, because leaving a practice window open is
not a failure there.

**Marks belong to the learner, not to the lesson.** `known` / `learning` are recorded per
word across the whole product, so a word marked in one catalog reads as marked in the next,
which is what makes "words I still need" a list worth keeping. A catalog that switches
`known_states_enabled` skips those words when a run opens, and says how many it skipped -
the number, not the reason: the state of somebody else's draft is not a learner's business.
Feedback timing is the teacher's decision and is honoured by the server: under
`after_session` each answer returns `withheld`, and the marks appear when the run finishes.
An essay is never called wrong for being unwritten - it comes back
`requires_manual` and is counted as an answer the server did not mark, never as a zero and
never as a mark somebody promised to give: nothing in practice feeds a teacher's grading
queue - the queue Phase 7 built reads exam answers, and a practice essay has no review row to
grow - so the learner's own screen says "Not marked automatically".

**Learners see only what is actually openable.** A catalog appears when it is `ready` and
every folder above it is, and each reference has to be `ready` and alive at the moment the
run is built; whatever is not is skipped and counted. Favorites are a saved list of
pointers, and a saved exercise whose content has since gone is still listed as gone rather
than vanishing from the learner's own shelf.

Frontend (no redesign): `api/catalogs.ts` and `api/practice.ts` carry the same payloads the
server answers, with no field invented for a component's convenience.
`pages/Catalogs.tsx` is the tree - folders and lessons in one list with both counts, the
bank/trash views, level and status filters, sort, and a selection toolbar whose per-id answer
leaves refused rows on screen with the server's reason instead of a bare "done".
`pages/CatalogEditor.tsx` is the one editing screen: name, level, shuffle, feedback timing,
known-states switch, the reference list with reorder, a picker per reference that filters the
four banks by level and shows what is already in the lesson, the block picker that narrows a
text or recording to one set, and the preview that answers "what my class will see" from the
same projection the learner gets. On the learner's side,
`pages/StudentPractice.tsx` is the shelf (openable collections with their own run counts,
plus a saved list that names each saved thing from its own row),
`pages/StudentPracticeRun.tsx` is the run and the result screen, and
`components/PracticeStep.tsx` puts the text, the recording, the word card or the exercise in
front of `components/AnswerWidget.tsx` - the same widget the teacher's own preview uses, so a
question cannot look different in the lesson than it did in the bank.
Routes `/catalogs`, `/catalogs/new`, `/catalogs/:id`, `/student/practice`,
`/student/practice/run`, nav entries in both roles, and az/en/ru/tr copy (780 leaves per
locale, all four key-identical, verified by the offline locale contract).

Migrations: `0005_catalog_item_unique` (the unique live-row index above - added, not
swapped, since `catalog_item` has no soft delete and so no trashed twin to forgive; it
downgrades by dropping only itself). `0001_bootstrap` through `0004` are untouched.

## Exams, attempts and grading (Phase 7)

An exam is not a catalog with a timer on it. The two share the shape of their content -
references into the central banks, never copies - and differ in everything a teacher is asked
to do with them.

**A paper pins a version.** Every `ExamItem` names one answerable question at one
`QuestionVersion`, and the mark it carries is the mark that version had. Naming a reading or a
recording adds *its* questions, each bound to that text, so the passage travels with the
question it belongs to instead of being stored twice. A learner graded last term is still
gradable after the question has been rewritten twice, and `clone` re-points a reused paper at
the versions learners already met rather than at whatever the bank holds today.

**Composition freezes at the first sitting;** rules are copied per attempt. Adding, removing,
reordering or re-marking a line would rewrite what a finished attempt was made of, so it is
refused with 409 once somebody has sat. Timing, shuffling, feedback and visibility stay
editable, because they are copied into the attempt's own blueprint when it opens: editing them
reaches the learners who start afterwards and leaves the one mid-paper on the paper they were
given. A listening line keeps the slice the teacher cut, the body of a reading keeps its words,
and the audio is the exception on purpose - the asset id is frozen, the file is signed per
request, because a stored link would outlive its signature.

**The learner holds a token; the server holds the clock.** A sitting is opened with a 32-hex
token and `remaining_seconds` is recomputed from `expires_at - now()` on every read, so
switching device, closing a tab or moving the local clock changes nothing about the deadline.
The worker's fifteen-second sweep exists for papers nobody came back for, and a request that
arrives late closes the attempt on the way in, so a submit one second after the bell is graded
by the same rule as one second before it. Three cases are kept apart: a second tab on a live
sitting resumes it, a deadline that passed while the learner was away closes it and opens a
numbered second attempt, and `resume_after_disconnect=false` costs the sitting - which is
graded from the answers its autosave already stored rather than left open.

**A verdict is a policy, not a fact.** `feedback_timing` decides what one answer returns
(otherwise the reply says `withheld`), `result_visibility` decides what the result screen shows
and when, `show_correct_answers` and `show_explanations` decide what the marks are made of, and
`awaiting_teacher` is different news from `closed`: one means the marks exist and are being
checked, the other means the paper is still open. An answer no engine can grade is counted as
ungraded, never as zero; a line that took part of its mark is counted `partial`, because calling
it wrong would contradict the same line showing "4 of 5". `after_approval` needs no extra flag:
marking the last answer *is* the approval.

**The teacher's mark is the record.** `POST /grading/answers/{review}/grade` writes
`final_score`, moves the answer to match so the two screens cannot disagree, and lets the
paper's total follow it. Re-marking is allowed and audited with the value it moved from,
because a misread script is corrected rather than rewritten, and the sentence written with the
mark reaches the learner's result screen under that line.

Migration `0006_exam_attempt_integrity` is where three of those promises stop being habits:
`uq_assignment_student` and `uq_assignment_group` (partial, because a nullable half of a plain
unique index never equals anything and would let any number through), `uq_attempt_number`
("attempt 2 of 3" cannot have two rows numbered 1) and `uq_manual_review_answer` (one queue row
per answer: two is the same essay marked twice), plus `ix_attempt_open_expiry`, which keeps a
fifteen-second sweep a read of the open papers rather than of the whole attempt history. It
rewrites no row, so data that already breaks a rule reports the violation instead of choosing
which duplicate to keep.

Every rule on this surface refuses with a **code** and the numbers its sentence needs, and the
screen puts it into words: `errors.<code>` across az/en/ru/tr (80 refusal keys per locale), one
translation point in `api/client.ts`, dates and durations formatted by `i18n/format.ts` in the
interface's locale, and question type names no longer sent by the engine at all.
`tests/test_frontend_i18n_contract.py` reads the services' AST and fails on a refusal that names
no code, on a code missing from any locale, and on a locale key no server code produces - so the
vocabulary cannot drift from the rules again.

The two screens a paper's rules actually change: `pages/ExamEditor.tsx` lists each line with the
version it is pinned to, the mark it carries, whether it can be auto-marked, and the blockers
that will refuse Publish before the teacher presses it; `pages/StudentExamRun.tsx` shows the
server's countdown, autosaves every line, keeps a resumed sitting on the same line it left, and
shows the result exactly as far as the paper allows.

## Document import and review (Phase 8)

A teacher hands the platform a paper and decides, row by row, what of it becomes content. Three
separate decisions, and the code keeps them separate: **the bytes** decide the format, **the
shapes** decide the candidates, **a person** decides the content.

`app/core/doc_types.py` names the format from an 8 KiB head before it asks the filename: a zip is
Word or Excel only once its members say so, a file is text only if it decodes strictly under
UTF-8, cp1254, cp1251 or cp1252 (UTF-16 only behind its own BOM), and binary that happens to decode
is refused on its control characters. A table is proven by three lines agreeing on a column count,
so a lesson with one comma in it is not a CSV and a semicolon export from a Turkish Excel is.
`app/services/document_parsing.py` then reads the text with the standard library alone - no Office,
no Acrobat, no network: Word paragraphs with their split runs rejoined and their tables kept as
cells, Excel sheet by sheet, PDF through its own content-stream tokenizer (escape sequences, `TJ`
gaps, the positioning operators that separate lines, subset fonts through their `ToUnicode` maps,
compressed object streams), Markdown with its markup dropped. Every block keeps its provenance:
page, sheet, offset.

`app/services/import_candidates.py` turns shapes into cards. A header is read in whatever language
the teacher wrote it in (`soru/şıklar/doğru`, `word/meaning/example`), an answer may be a letter, a
number or the option's own words, and two identical options that both match mark **nothing** and
report the missing key rather than guessing. Prose is read the same careful way: a numbered stem and
the lettered lines under it are one question, options stop where the next stem starts, a heading plus
the paragraphs under it is a reading. What the reader cannot classify stays a note, **with a code
that says why** - `unmatched_header`, `word_without_meaning`, `options_without_answer`,
`row_without_role` - and every one of them exists in all four locales.

Nothing becomes content until a person says so. `PATCH /imports/items/{id}` never overwrites the
document's own words: the teacher's version goes in `corrected`, and both are shown, so an approval
stays provable against the paper it came from. Approving writes through the bank's services - the
word is filed in the language the *teacher* chose, not the one the file claimed - and one
candidate is filed exactly once: the row is re-read under a lock, into the object the decision
is made from, so two people who approve the same sentence in the same second leave one question
in the bank and a `candidate_filed` refusal for the second. `uq_import_item_result` holds the
other half - no two candidates may point at the same content row. Identical bytes uploaded twice return the queue that already exists instead of opening
a second one; re-reading a job that already produced candidates is refused; a document over
`MAX_DOCUMENT_UPLOAD_MB` is refused while it is still streaming and leaves nothing behind.

`pages/Imports.tsx` is the upload and the list of queues; `pages/ImportReview.tsx` is the queue
itself - filters, both orderings, page-scoped bulk selection ("50 of 300 selected", never more),
per-row editing and filing, and a bulk action that answers **row by row** with `done` / `refused` /
`not_found` and the reason, so a page of work never collapses into one silent failure.

---

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
app/OpenAPI wiring, the vocabulary and question rules that do not need a database,
the locale contract (four languages carrying the same keys, none of them blank, and
every label the frontend actually asks for), and all the offline migration checks
(they compile the migration's DDL instead of applying it). The integration tests print
their skip reason and are counted as `skipped`.

**A run in which the integration tests skipped has verified nothing about the
database, Redis or object storage.** Green here does not mean Phase 1-2 accepted.

### 2. Mandatory Phase 1-2 acceptance test - `scripts/verify_phase12.sh`

```bash
# from the repository root - this is the supported path, on WSL, Linux, macOS or
# Windows, and needs no `make`:
bash scripts/verify_phase12.sh

make verify-phase12          # exactly the same script
```

One command, deterministic, self-contained, and **it does not report success if any
required infrastructure was skipped or only pretended to run**. Its 13 steps:

1. Docker CLI + reachable daemon, and **one native interpreter for every Python,
   pytest, Alembic, ARQ and helper call** (see the platform rules below)
2. generates this run's throwaway environment (one file, one source of truth)
3. starts PostgreSQL **in an isolated compose project**
4. starts Redis
5. starts MinIO (pinned official Quay image - see below)
6. waits for each service's **real healthcheck**, then proves it at the protocol
   level: `pg_isready`, a host TCP connect, a Redis `PING` through the
   application's own client, and an authenticated S3 `ListBuckets`
7. creates the isolated verification database (`app_verify_phase12` by default),
   refusing any name that is not `*_test` / `test_*` / `*_verify_phase12`, asserting
   it is empty, and then **authenticating with the exact `DATABASE_URL` the
   integration suite will use** - a wrong credential fails here, not as 74 skips
8. `alembic upgrade head` - the committed migration, twice, proving the second
   application is a clean no-op - then reads the schema back from a **new process**
   and asserts `alembic_version` plus table, column, FK and index counts, and finally
   **downgrades to the revision the previous phase left behind and comes back up over
   rows written in between**. That transition has to hold two things: a granular
   revision removes only what it added (rolling a phase back must not drop a teacher's
   table), and its rebuilt index must still refuse a duplicate against data that
   already exists, not just against an empty table
9. runs the seed **twice** and compares row counts to prove idempotency
10. runs the offline unit/static suite
11. runs the **full integration suite** (Postgres + Redis + MinIO)
12. exercises MinIO object storage through the application's own storage
    abstraction and confirms the verify bucket exists over S3 afterwards
13. starts the ARQ worker with every optional adapter disabled and requires its
    attempt-expiry cron to **complete against live PostgreSQL** before stopping it

**Exit status is part of the contract.** A run that prints "NOT accepted" exits non-zero,
and the tear-down that follows cannot swallow that verdict - the `EXIT` trap only removes
containers and the throwaway credentials. This was checked against the shell's real
behaviour rather than assumed, because an `EXIT` trap is exactly where a forgotten status
tends to hide, and a gate that cannot fail is worse than no gate.

**Platform rules (the defect this gate used to have).** A WSL shell must never hand
`/mnt/c/...` paths to a Windows `python.exe`, so the verifier picks a virtualenv
belonging to the platform it is running on and asserts the interpreter matches:

| Running from | Interpreter used |
| --- | --- |
| WSL with the repo under `/mnt/c` | `$HOME/.local/share/verify-phase12/venv` (Linux venv kept off the Windows mount, where its symlinks are unreliable) |
| Linux/macOS, or WSL with the repo on ext4 | `backend/.verify-venv-wsl` |
| Windows PowerShell / Git Bash | `backend/.venv` |

`backend/.venv/Scripts/python.exe` is never selected from WSL, and the first run
builds the venv itself (`python3 -m venv`, bootstrapping pip if the distro ships
`python3` without `ensurepip`) and installs the backend with its dev extras.

**Self-contained configuration.** The run needs no developer `.env` - it generates
`backend/.verify-phase12/verify.env` (mode 600) with fresh random PostgreSQL
password, MinIO key pair, session/CSRF secrets and bootstrap admin password, then
feeds those same values to Compose interpolation, `psql`, Alembic, pytest, the
worker and every probe. Nothing re-derives a credential anywhere, and no secret
value is ever printed (URLs are shown with the password replaced by `***`). The file
is deleted with the run.

**Isolation from your development stack.** Verification always runs as compose
project `llp_phase12_verify` - never the project named in `docker-compose.yml` - on
loopback ports chosen per run (so a dev Postgres on 5432 is no obstacle), with its
own disposable volumes, Redis db 1 and `platform-media-verify` bucket. Only that
project is ever created, removed or `down --volumes`'d: **development volumes are
never touched.** A leftover verification volume from an earlier run is removed first,
because it would still hold the previous run's password.

Every step is mandatory: a step that fails, or whose tests **skip**, is recorded as a
failure. The script also refuses to run a suite whose infrastructure never came up,
and the final report separates **infrastructure failures** from **test failures** and
from **mandatory checks that could not run**, so a missing service is never mistaken
for a passing test. Exit code 0 plus
`PHASE 1-2 ACCEPTED: every mandatory check passed against live PostgreSQL, Redis and
MinIO.` is the only accepted outcome.

Flags and environment overrides: `--keep` (leave containers, volumes and the
generated env file for inspection and print the exact teardown command), `--down`,
`--venv=/path` (or `VERIFY_VENV`), `PYTHON3_BIN`, `HEALTH_TIMEOUT` /
`WORKER_TIMEOUT`, `VERIFY_COMPOSE_PROJECT`, `VERIFY_TEST_DB`,
`VERIFY_OBJECT_STORAGE_BUCKET`, `MINIO_IMAGE`, and forced
`POSTGRES_PORT` / `REDIS_PORT` / `MINIO_API_PORT` / `MINIO_CONSOLE_PORT`. JUnit
reports and the worker log land in `backend/.verify-phase12/`.

The MinIO service is pinned by digest to
`cgr.dev/chainguard/minio@sha256:a05a4497…`, the community AGPLv3 MinIO server
(`RELEASE.2026-09-22T19-25-18Z`). This is a deliberate choice, not a preference:
`docker.io/minio/minio` is no longer published at all (every tag returns
`pull access denied`), and MinIO's Quay organisation now serves only AIStor/EOS
builds, which start and pass a naive healthcheck but then log `No valid license
found … All S3 operations are denied` - an object store that cannot store objects.
ghcr.io and public.ecr.aws are closed for the product too. The verifier detects a
licensed build and says so instead of timing out silently. Its healthcheck
(`mc ready local || curl …/minio/health/ready`) uses only tools that ship in the
pinned image.

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
Question bank: a new version row for every edit with the old snapshot still readable,
grading a frozen `at_version`, all nine types round-tripping through config validation
and the grader, partial credit and negative marking, the learner payload proven free of
every answer-key field, preview → grade paths, clone/trash/restore/status rules,
filter + sort + pagination + search on the list, bulk actions answered per id, audit
rows, and CSRF/auth refusals on the question routes.
Taxonomy: topic tree paths and counts, rename/move refreshing the whole subtree,
sibling name clashes refused on create **and** on rename/move, cycle refusal, delete
refused while children or questions (including trashed ones) reference the topic, tag
case-insensitive uniqueness, tag delete refused while in use, the trash staying frozen
against re-filing.
Vocabulary: every field and both child sets round-tripping, the duplicate-word rule in
four spellings plus two concurrent creates of one word leaving exactly one row, the
`0002` index proven to be what refuses the race, translations replaced rather than
merged (and an omitted child set left alone), a `PATCH` that tries to set `status`
refused instead of silently dropped, publish/archive/trash/restore rules including a
restore colliding with a word that took its slot, every list filter against the rows in
the table, sort + pagination never repeating or dropping a row, the learner surface
serving only ready, alive words, a study card carrying exactly the allowed keys and never
the private note - not even through the learner's own search box, the card narrowed to
one language, the teacher's preview byte-identical to the learner's card, bulk answers
per id with one audit row for the whole operation, and shared tag counts refusing a
delete while either bank still references the tag.

```bash
# Lint - the rule families are pinned in `backend/pyproject.toml` ([tool.ruff.lint])
# so the gate means the same thing on every ruff upgrade.
cd backend && ruff check app tests migrations scripts

# Static type-check: `mypy` is listed in the dev extra but is NOT part of the
# Phase 1-2 acceptance gate (it is not installed in the shipped venv), so install
# it first if you want the signal: pip install -e ".[dev]" && mypy app

# Frontend type-check (no redesign, compatibility only)
cd frontend && npm install && npm run typecheck

# Same signal without a local Node toolchain - this is the path used here, because
# it is the compiler the shipped image actually runs (`tsc -b && vite build`):
docker build frontend -t language-learning-frontend
```

