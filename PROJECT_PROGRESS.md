# PROJECT PROGRESS

Continuation state file. Reread this after any context loss and keep going - do not
re-plan work that is already recorded as done here.

- Current phase: **Phase 5 - reading, listening and media** (Phases 1-4 accepted)
- Git: branch `main`, remote `https://github.com/f1r10/ingilis-learning.git`
- Last commits: Phase 1-2 `2ab3294`, Phase 3 `c650bf5 feat(phase-3): complete question
  bank and question engine`, Phase 4 `feat(phase-4): complete vocabulary module` - all
  pushed. `git rev-parse HEAD` is the authoritative tip; the Phase 5 commit must rewrite
  this line with its own SHA

---

## 1. Contract that never changes

Self-hostable language-learning CMS / practice / exam / document-import / monitoring
/ analytics platform for one private teacher and their students; never hardcoded to
one person. No production dependency on Lovable/Supabase/Firebase/any hosted backend.

Stack (fixed): React + TypeScript + Vite + React Router + TanStack Query + i18next;
FastAPI under `/api/v1`; PostgreSQL 16 + SQLAlchemy 2 async + Alembic; Redis + arq
worker + S3/MinIO object storage + Docker Compose; Argon2id + HttpOnly session
cookies + CSRF + Redis rate limiting; AI/OCR/STT/translation behind replaceable,
optional, local-first adapters (the product must stay fully usable with all of them
set to `none`).

UI languages: az, en, ru, tr. Content/learning languages are configurable - the
product is not Azerbaijani-only. Design stays plain, light, Arial, one configurable
accent colour, no gradients, no card noise, responsive, mobile-first for students,
progressive disclosure for advanced features. Teacher-facing copy uses educational
language, never developer jargon. Heavy operations run in the worker.

### Architectural invariants (must survive every phase)

1. Source != Question.
2. Catalog != Exam.
3. Question history is immutable through `QuestionVersion`.
4. Exam/attempt history stays pinned to the question version it used.
5. Reading/listening questions keep explicit context relationships.
6. Catalogs reference central content; they never copy it.
7. Passwords, student keys and recovery codes are never stored in plaintext.
8. Soft delete is preferred over destructive deletion.
9. Historical results are never silently recalculated after content edits.
10. The server is authoritative for exam timers.

### Standing rules

- No placeholder product features (`TODO` / `FIXME` / `pass` / `NotImplemented` /
  dummy success / fake progress / hardcoded sample data). Stubs only for genuinely
  optional third-party providers, and the disabled-capability path must be real and
  tested.
- Never weaken security, rate limits or the "a mandatory skip is a failure" gate to
  make a test pass. Fix root causes.
- `0001_bootstrap` migration is frozen. Every later schema change is a new granular
  revision (`0002_...`, `0003_...`) with a working `downgrade` where reasonable.
- Never commit `.env`, virtualenvs, `node_modules`, verification artifacts, secrets,
  local database data or generated backups.
- Do not report something as tested if the environment prevented testing it.

---

## 2. Phase status

| Phase | Scope | Status |
| --- | --- | --- |
| 1-2 | Foundation: infra, identity, students, groups, settings/branding, security, migrations, tests, acceptance gate | **accepted - `verify_phase12.sh` exit 0, 13/13 steps, 0 mandatory skips** |
| 3 | Question bank + full question engine (all types, `QuestionVersion` immutability) | **accepted - `verify_phase12.sh` exit 0, 13/13 steps, 0 mandatory skips** |
| 4 | Vocabulary module | **accepted - `verify_phase12.sh` exit 0, 13/13 steps, 0 mandatory skips, live browser click-through on teacher and learner screens** |
| 5 | Reading + listening + media libraries (object storage) | not started - **next** |
| 6 | Catalogs + practice | not started |
| 7 | Exams + assignments + attempt engine + grading | not started |
| 8 | Document import + review pipeline | not started |
| 9 | Monitoring + activity | not started |
| 10 | Analytics | not started |
| 11 | Exports + backups + restore | not started |
| 12 | Local AI / OCR / STT / translation adapters | not started |
| 13 | Security + performance + product hardening, clean-room verification, final push | not started |

Commits to make, in order:

```
chore: complete phase 1-2 acceptance hardening
feat(phase-3): complete question bank and question engine
feat(phase-4): complete vocabulary module
feat(phase-5): complete reading listening and media modules
feat(phase-6): complete catalogs and practice
feat(phase-7): complete exams assignments and attempt engine
feat(phase-8): complete document import and review pipeline
feat(phase-9): complete monitoring and activity
feat(phase-10): complete analytics and progress reporting
feat(phase-11): complete exports backups and restore
feat(phase-12): complete local ai ocr stt and translation adapters
feat(phase-13): production hardening and product completion
```

---

## 3. Phase 1-2 - what is already true

Data model: 45 tables / 473 columns, complete Phase 1-13 domain already migrated
(contents, questions/versions, catalogs, exams/attempts, imports, activity,
analytics, exports, backups, trash) - later phases must not need schema changes
unless a genuinely new need appears, and then via a new revision.

Security: Argon2id hashing, itsdangerous signed session cookies with `session_epoch`
invalidation, CSRF double-submit bound to the session `sid`, fixed-window Redis login
limiters (admin 8/60s, recovery 5/300s, student 10/60s, fail-open), unified error
envelope, partial unique index enforcing one ACTIVE access key per student,
`uq_group_student`.

Tests: 70 offline unit/static + 74 integration (8 of which are the MinIO storage
round-trips). Integration skips are converted into failures by
`backend/scripts/_junit_gate.py`.

### Acceptance gate defects found from the real WSL run, and their fixes

| Defect (root cause) | Fix |
| --- | --- |
| `pull access denied for minio/minio` - Docker Hub no longer publishes MinIO | compose pins the community AGPLv3 server `cgr.dev/chainguard/minio@sha256:a05a4497e8dce3cb7a7a1bf1872ba5d30ea988f1e8c22c9e0920503761c4b5f1`. Discovery: `docker.io/minio/minio`, `ghcr.io/minio/minio` and `public.ecr.aws/minio/minio` are closed and `quay.io/minio/*` now serves **only licensed AIStor/EOS builds**, which start happily and then deny every S3 call - an intermediate pin of `quay.io/minio/aistor/minio` therefore timed out the healthcheck. Healthcheck is `mc ready local \|\| curl …/minio/health/ready` (that image ships `mc`, not `curl`), and the verifier now names a licensed build explicitly (`no valid license` / `all s3 operations are denied` in the container log) instead of letting the run hang |
| 24 integration tests failed on the first genuine live run | every one was a test-level defect, fixed at the root, none by weakening a control: 17 assumed an anonymous client where the fixture is now an authenticated admin (`client` logs in; `session_factory()` stays for unauthenticated cases), 5 were schema-reflection bugs (unique constraints use `column_names`, not `constrained_columns`; `pg_toast` leaked into "app schema"; primary-key and constraint-backed indexes double-counted), 1 asserted a login message that the product never claimed to hide, 1 forgot the `commit()` a service-level race test owns |
| Rotation tests contradicted each other | `test_rotation_leaves_no_window_where_both_keys_are_valid` logged in with the **new** key before asserting "no live sessions", so it failed on a session it had just created; the old-session assertions now run before that login, and the fresh login is re-checked as the single tracked session |
| Concurrent rotation assumed the first 200 response wins | under READ COMMITTED a waiting rotation re-runs its revoke over the winner's row, so it either loses on the partial unique index (409) or overwrites it (200, last write wins). The test asserts the invariant that actually matters: exactly one ACTIVE key, the survivor is a key some request was handed, and every non-survivor issued key is REVOKED |
| `suggest-username` skipped `base1` (real product defect) | the collision loop incremented the suffix before building the candidate, so with `adal` taken it returned `adal2`. It now walks `base`, `base1`, `base2`, … and the test pins two collisions |
| 11 float columns reported as live/model type drift | PostgreSQL alias: `FLOAT` **is** `DOUBLE PRECISION`. The comparison now normalises the alias spellings instead of the columns |
| Verifier used `backend/.venv/Scripts/python.exe` under WSL, so `/mnt/c/...` reached Windows Python and JUnit paths became `C:\mnt\c\...` | one native interpreter per platform, with an explicit host/guest platform assertion; pytest + gate are invoked with paths **relative to `backend/`**; WSL never selects the Windows venv |
| Gate required an existing developer `.env` | run generates `backend/.verify-phase12/verify.env` (mode 600, throwaway credentials, deleted with the run) and it is the single source for compose, psql, Alembic, pytest, the worker and every probe; no secret value is ever printed |
| Conflicting PostgreSQL credentials between compose and the tests | one generated env file exported before anything runs; verifier-owned compose project tears its volumes down first so an old password can never be reused |
| Verification could disturb the dev stack | compose project `llp_phase12_verify`, per-run free loopback ports, own volumes/Redis db 1/`platform-media-verify` bucket; only that project is ever stopped or removed |
| Auth failure surfaced as ~74 skipped integration tests | step 7 authenticates with the exact `DATABASE_URL` the suite will use and fails immediately; a suite whose infrastructure is down is reported as **blocked**, never as passed |
| pytest exited 1 on an otherwise green run (Windows temp-dir symlink cleanup) | `--basetemp` pinned inside the run's report directory |
| WSL Ubuntu has only `python3.14` and no `ensurepip`/`pip`, so `python3 -m venv` failed | verifier accepts any Python >= 3.11 and bootstraps pip into the venv when the distro lacks `ensurepip` |
| Worker check only proved the process started | requires the attempt-expiry cron to log both start and completion against live PostgreSQL |

### Phase 1-2 closure evidence

Last green run (2026-10-06, WSL Ubuntu + Docker 29.1.3 / Compose 2.40.3):

- `bash scripts/verify_phase12.sh` -> **exit 0**, `passed steps: 13 / 13`
- unit/static **70 passed**, integration **74 passed**, MinIO storage round-trip **8
  passed**; `0 failed | 0 errors | 0 skipped` in all three, so no mandatory skip existed
- `alembic upgrade head` from an empty cluster, seed run twice and proven idempotent,
  ARQ worker registered its functions and logged a real `cron:expire_attempts()`
  start + completion against the verification PostgreSQL
- teardown removed only project `llp_phase12_verify` and its own volumes

- [x] Full green run of `bash scripts/verify_phase12.sh` from WSL (exit 0, 0 mandatory skips)
- [x] Commit `chore: complete phase 1-2 acceptance hardening`

---

## 4. Phase 3 - question bank + question engine (accepted)

Backend: `app/services/question_engine.py` (registry of 9 types, per-type pydantic
config with `extra="forbid"`, opaque element refs, partial-credit and negative-marking
rules, key-free `public_config()` projectors) and `app/services/question_service.py`
(create/patch with an immutable `QuestionVersion` per content change, clone, trash and
restore, status lifecycle, taxonomy filing, list with 13 filters + sort + pagination,
per-id bulk, `student_view`, `grade` including `at_version` replay of a frozen
snapshot). Endpoints in `app/api/v1/endpoints/questions.py`, `topics.py`, `tags.py`.
No migration was needed - `0001_bootstrap` already carried every table, so it is still
the only revision.

Frontend (no redesign, same shell and tokens): `api/questions.ts` (payload types
mirrored from the real responses), `pages/Questions.tsx` (filter/sort/paginate/trash
view + bulk toolbar), `pages/QuestionEditor.tsx` (all 9 type forms, learner preview,
live "try an answer" against the real grader, version history), `pages/Topics.tsx`
(topic tree + tags), `components/QuestionConfigForm.tsx`, `components/LearnerPreview.tsx`;
routes `/questions`, `/questions/new`, `/questions/:id`, `/topics` and az/en/ru/tr copy.

### Defects found in this phase and fixed at the root

| Defect | Fix |
| --- | --- |
| Every write endpoint 500'd under asyncio (`MissingGreenlet` on `updated_at`) | `TimestampMixin.__mapper_args__ = {"eager_defaults": True}` - no DDL change, so the bootstrap revision stayed frozen |
| The learner payload could carry answer-key material, and matching/ordering published in authored order betrayed the key | `public_config()` strips the key per type, elements travel as `sha256(role\x1findex\x1fvalue)[:12]` refs only, `_least_aligned()` rotates the display order off the key, distractors are published like any other option, and a test greps every learner payload for a sentinel answer string |
| Renaming or moving a topic could put two siblings on one name (silent filing corruption; `parent_id.is_(uuid)` was also being trusted for a non-NULL value) | shared `_sibling_clash()` (case-insensitive, `IS NULL` aware, `exclude_id`) used by create **and** update, checked before anything is mutated; `topic_exists` 409 |
| The editor's trial answer hashed refs with a pseudo-implementation that treated `crypto.subtle.digest` as synchronous and called an undefined `jsSha256` | real `await crypto.subtle.digest`, and an explicit "this browser context cannot compute the ref" message instead of a fake grade |
| `frontend/vite.config.ts` did not compile (TS2580 `process`, plus a `credentials` key `ProxyOptions` has never had), which kept the whole build red before any Phase 3 file was type-checked | `loadEnv(mode, ".")` (still picks up a shell `VITE_PROXY_TARGET`) and the unsupported key dropped; same-origin dev cookies are unaffected |
| Bulk actions: the backend answers per id (`updated`/`refused`/`not_found` lists) but the UI declared counts, so the teacher's banner would have rendered a UUID dump | `BulkResult` now mirrors the API and the bank shows counts plus each refusal with its reason |
| Retyping a question was impossible from the editor: the patch omitted `partial_scoring`/`negative_scoring`, so the old type's values stayed on the row and the new type was refused | the editor always sends both objects (empty when the type has no such knob); pinned by `test_retyping_clears_the_scoring_knobs_the_new_type_has_not` |
| Three taxonomy tests failed while the product was correct | the helper `_find()` raised inside its own recursion; split into `_walk()`/`_find()` rather than bending the assertions |
| A taxonomy test asserted a tag could be detached from a trashed question | the trash is frozen by design (`allow_trash=False` -> 404); the test now asserts the refusal, restores, then unlinks |

### Evidence

- `bash scripts/verify_phase12.sh` -> **exit 0**, `passed steps: 13 / 13`,
  unit/static **168 passed**, integration (Postgres+Redis) **133 passed**, MinIO
  round-trip **8 passed**, `0 failed | 0 errors | 0 skipped` in all three
- developer loop: `295 passed, 7 skipped` (the 7 are MinIO-dependent and green in the
  gate), `ruff check app tests migrations scripts` clean
- frontend: `docker build frontend` (the shipped `node:20-alpine` stage running
  `tsc -b && vite build`) -> green, `dist/` produced; there is no Node toolchain on
  this machine, so the shipped compiler image is the type-check path

### Open Phase 3 notes

- The new screens were verified by the production compile and an endpoint-by-endpoint
  payload review against the real responses; they were not clicked through in a
  browser, because authenticating one here would mean printing the dev admin password.
- `npm install` in the frontend image reports 4 vulnerabilities (3 moderate, 1 high)
  in the dependency tree - a Phase 13 hardening item.

## 5. Phase 4 - vocabulary bank + study cards

Backend: `app/services/vocabulary_service.py` (create/patch with whole-set child
replacement, the duplicate-word rule in four spellings, status lifecycle, trash/restore
with a restore-time collision refusal, shared `_summary`/`to_read` payloads, list with
10 filters + sort + pagination for the teacher and a narrower read-only surface for the
learner, `learner_view` as the single study-card projection, per-id bulk with one audit
row for the operation) and `app/api/v1/endpoints/vocabulary.py` (two routers: `/vocabulary`
teacher, `/student/vocabulary` learner; `/meta` and `/bulk` declared before `/{entry_id}`
so a literal path is never parsed as a UUID).

Migration: `0002_vocabulary_word_unique` - partial unique index
`uq_vocabulary_word_language (word, learning_language) WHERE deleted_at IS NULL`, with a
working downgrade, verified by the replay guard (`expected_partial` set), the offline DDL
count test and a live downgrade/upgrade round trip in the acceptance gate. `0001_bootstrap`
was not touched.

Rules that are now contractual: TRASH is `deleted_at`, never a status; a trashed word keeps
its status, children and tag links (a restore must be exact, and a binned word still
occupies its tag); `PATCH /vocabulary/{id}` is `extra="forbid"`, so a body that tried to set
`status` gets 409-proof honesty rather than a 200 lie; a learner's search is pinned to
`q_kind=word_only` so list search can never match `notes`; a self-translation is refused
before the "not an enabled language" message, because that is the mistake the teacher
actually made; tags carry `{question_count, vocabulary_count}` and refuse deletion while
either bank references them.

Frontend (no redesign): `api/vocabulary.ts`, `pages/Vocabulary.tsx` (teacher bank: filters
incl. search kind, trash view, bulk toolbar with per-id refusals), `pages/VocabularyEditor.tsx`
(all fields, meanings per language, examples with their own translation, synonym/antonym
chips, lifecycle block, live learner preview), `components/StudentCard.tsx` (one card
component used by the teacher preview and the student screen), `pages/StudentVocabulary.tsx`
(learner list + card), routes `/vocabulary`, `/vocabulary/new`, `/vocabulary/:id`,
`/student/vocabulary`, nav entries, and az/en/ru/tr copy (300 keys per locale, verified
key-identical and every `t()` literal resolved).

### Defects found in this phase and fixed at the root

| Defect | Fix |
| --- | --- |
| Any `PATCH` carrying `translations` or `examples` returned 500 (`'dict' object has no attribute 'language'`) | `update_entry` read the children out of `payload.model_dump(exclude_unset=True)`, which flattens nested pydantic models to dicts, while every child rule reads them as rows; it now uses `payload.translations` / `payload.examples` (validated models) or the ORM rows already in the database |
| A learner searching the word list could match the teacher's private `notes` | `list_for_learner` pins `q_kind="word_only"`; pinned by a sentinel-note test that searches for it and gets nothing back |
| A `PATCH` body containing `status` answered 200 having changed nothing | `VocabularyUpdate` is `extra="forbid"`; the lifecycle endpoint is the only door to a status |
| A self-translation (`az` word translated into `az`) reported "not an enabled language" and hid the real mistake | the self-language check runs first in `_check_language` |
| A trashed word released its tag in the UI's counting model | both `_counts` (questions) and `_vocabulary_counts` (words) count every link row including soft-deleted ones, matching the Phase 3 rule; `TagRead` gained `vocabulary_count` and the refusal names both banks |
| The edit endpoint and the taxonomy decorator were merged onto one line by an insertion (`...response_model=None)async def assign_taxonomy(`) - the app would not import | caught by `python -c "import app.main"` + ruff before any test ran; the decorator/def pair restored |
| The learner page called `/vocabulary/meta`, an admin endpoint, so its own filter pickers would 401 | added `GET /student/vocabulary/meta` (declared before `/{entry_id}`, narrower payload: no statuses, no word types) and pointed the page at it |
| Three integration expectations were wrong while the product was right | the seed's inherited child rows and levels, the disabled-learner path (the API ends sessions, so 401 is the honest answer and the 403 branch needs a direct column write), and the bulk audit (filed for the operation with no `target_id`) - each test now asserts what the system actually guarantees |
| The editor printed the lifecycle explanation twice on a new word and printed an empty status chip next to it | the hint is create-only and the chip only renders when there is an id |
| A new word could only ever be born a draft, so publishing one meant save, then a second trip to the lifecycle endpoint | the create form carries its own initial-status picker, restricted to the states a word can *enter* (`draft`, `ready`) - archived and trash are not doors in |
| "Add a language" always opened on the first enabled translation language, so a second meaning hit the duplicate-language rule on save | it opens on the first language that is still free, and disables once every language has a meaning |
| The bulk level button was derived from the *filter* level, which made it dead in the normal case (no filter) and a no-op in the filtered one | its own picker with a placeholder, `Set the level to X`, and a separate `Remove the level` - which is `set_level` with an empty level, the way the API already documents unsetting one |
| Both dashboards showed future modules as if they were buttons, next to a "placeholder" sentence | the tiles are real links to the screens that exist, and the copy tells a teacher and a student what they can actually do now |
| Azerbaijani rendered the status names as verbs ("Layihə", "Arxivləmiş") and `questions.context_independent` was an empty string in all four locales, so the label rendered blank | corrected to "Qaralama"/"Arxivlənmiş" and real terms in az/en/ru/tr; a new offline test now fails on any blank value anywhere in the locales |
| A `//` comment was written *inside* the JSX children of the bulk toolbar, so React rendered it as visible text on the teacher's screen (`tsc` cannot catch this - a `//` line is legal JSX text) | moved it into a `{/* … */}` comment, then swept every `.tsx` in the app for a comment line sitting next to JSX: this was the only one |
| The create-time status picker filtered out a `"trash"` status that `/vocabulary/meta` can never return (`SETTABLE_STATUSES` is draft/ready/archived, and the service refuses `trash` as a status) | filter on `archived` alone, which is the state a word genuinely cannot enter through |
| The gate's own new round-trip step failed on its first live run: `duplicate = await conn.execute(...).scalars().all()` does **not** await the `execute` - `await` binds looser than the attribute chain, so Python awaited the chain, never ran the statement, and the check that was supposed to prove the rebuilt index works had proved nothing | `(await conn.execute(...)).scalars().all()`. Proven in both directions against the real index before re-running the gate: the fixed form returns 0 duplicate ids, the old form raises the exact `'coroutine' object has no attribute 'scalars'` the gate reported |
| The same step asserted `"WHERE deleted_at IS NULL" in indexdef`, but Postgres stores the predicate as its own text - `WHERE (deleted_at IS NULL)`, parenthesised - so the assertion would have failed even with the `await` fixed | compare a normalised form (parentheses and whitespace folded, lower-cased) instead of the migration's literal spelling, and the check still proves the partial-index predicate survived the rebuild |

### Localisation contract (new in this phase)

`backend/tests/test_frontend_i18n_contract.py` (5 offline tests) flattens the four locale
files and asserts: identical key sets, **no blank value**, every `t("literal")` in the
frontend source exists, every dynamic family (`t(\`status.${x}\`)`) has at least one key,
and the language menu in `i18n/index.ts` offers exactly the locales that are shipped. A
missing frontend tree is a failure, never a skip.

### Evidence

- `bash scripts/verify_phase12.sh` -> **exit 0**, `passed steps: 13 / 13`,
  unit/static **201 passed**, integration (Postgres+Redis+MinIO) **196 passed**,
  MinIO round trip **8 passed**, `0 failed | 0 errors | 0 skipped` in all three.
  Step 8 now also does the Phase 3 -> Phase 4 transition for real: `downgrade
  0001_bootstrap` (proving only the word index disappears and the table and its columns
  stay), a committed row staged underneath, then `upgrade head` rebuilding the index over
  that row and refusing a second live copy of it.
  The exit status is read from inside a script file, because a `$?` typed through the
  Windows side is expanded before WSL sees it - an earlier "the gate exits 0 while
  reporting failure" reading was that artifact, not the gate.
- `ruff check app tests migrations scripts` clean; `python -c "import app.main"` clean
- offline half on its own: **201 passed** (4.2s), which includes the 5 new locale tests
- the **whole suite against the running stack** (PostgreSQL + Redis + MinIO):
  **397 passed, 0 failed, 0 errors, 0 skipped** in 22m58s. With MinIO up the object
  storage round trip ran for real instead of skipping, so nothing in this phase quietly
  did nothing. Offline 201 + live-only integration 196 = 397.
- `docker build frontend` (the shipped `node:20-alpine` stage, `tsc -b && vite build`)
  green, and the built bundle contains the Phase 4 calls - rebuilt once more after the
  last two edits (the JSX comment and the status filter), because those changed source
  the first build had not seen
- **real browser click-through** on the compose stack (api + nginx frontend, same-origin
  `/api/v1`) as a teacher session in Azerbaijani, plus the learner screen. To avoid ever
  printing a developer credential, the run used a separate throwaway compose project
  (`-p llp_ui`) on auto-chosen ports with its own scratch admin/learner accounts, created
  for this verification and torn down with it; the developer stack and its volumes were
  never touched. Confirmed in the live UI: a new word created with two meanings and two
  examples; the duplicate-word rule surfacing the backend's 409 as a message on the field;
  the language picker refusing to open a third meaning on an already-used language; a
  draft staying invisible to the learner until it is published; the learner search box
  returning nothing for a phrase that exists only in the teacher's private note; the
  teacher's preview card being the same component the learner gets; trash and restore
  moving the learner's visible count; the bulk toolbar applying status / trash / restore
  / `set level` / `remove the level` per id, and the level round-trip verified as
  `C1, C1, C1 -> —, —, —` through the real endpoint. No console errors, and no request
  outside 200/201 or a deliberate 4xx.
  Screenshots are not available in this browser surface, so the evidence is structural
  (accessibility snapshots and DOM reads), not pixel-level.

## 6. Working commands

```bash
# mandatory acceptance (WSL / Linux / macOS / Windows; needs Docker, no make)
bash scripts/verify_phase12.sh                 # add --keep to inspect the run afterwards

# offline developer tests (never an acceptance result)
cd backend && pytest tests --ignore=tests/integration
cd backend && ruff check app tests migrations scripts

# integration tests against the dev stack's PostgreSQL/Redis (Windows venv).
# The URL is derived, never printed, and --basetemp keeps Windows temp dirs sane:
cd backend && mkdir -p ../.tmp \
  && DBURL=$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2- | sed 's|/app$|/app_test|') \
  && DATABASE_URL="$DBURL" .venv/Scripts/python.exe -m pytest tests -q --basetemp=../.tmp/bt
# one process at a time: two suites share app_test and truncate each other

# frontend compile, using the image the product actually ships (no local node here)
wsl.exe -- bash -lc 'rm -rf ~/fecheck; mkdir -p ~/fecheck; cp -r <repo>/frontend/. ~/fecheck/; cd ~/fecheck; docker build -t llp-fe-check .'

# dev stack
cp .env.example .env && docker compose up --build
docker compose up -d postgres redis minio
```

Windows venv (offline work only): `backend/.venv/Scripts/python.exe`.
From Git Bash on Windows the repo path is `C:/Users/firon/Documents/Qoder/2026-10-06/1438da7f`;
bash state does not persist between commands, so `cd` in every command.
WSL path for the same repo: `/mnt/c/Users/firon/Documents/Qoder/2026-10-06/1438da7f`
(run with `wsl -e bash -lc '...'`).

## 7. Environment facts worth not re-discovering

- WSL Ubuntu: Docker 29.1.3 + Compose 2.40.3 working, internet reachable,
  `python3` = 3.14.4 with **no pip/ensurepip** (venv needs the pip bootstrap), no
  `make` on the Windows PATH.
- Invoking WSL from Git Bash: `wsl.exe -- bash -lc '...'` (a bare `wsl -lc` is rejected);
  never hand `/mnt/...` paths to `wsl.exe` as arguments (MSYS rewrites them).
- **A `$` inside that single-quoted body is still expanded by the Windows-side shell.**
  `echo "GATE_EXIT=$?"` therefore reports the status of some earlier command, not the
  thing that just ran - which is how a gate that had clearly failed looked like it exited
  0. Loops, substitutions and anything whose value matters go into a literal script file
  under `C:\Users\firon\AppData\Local\Temp`, run as
  `wsl.exe -- bash -lc 'tr -d "\r" < /mnt/c/.../file > /tmp/file; bash /tmp/file'`
  (files written from Windows carry CRLF, and bash reads `\r` as part of the command).
- bash 5.3.9 keeps the original exit status through an `EXIT` trap whose action merely
  runs a function; `exit N` inside the trap overrides it. Proven with isolated scripts,
  so `trap 'cleanup' EXIT` in the gate is sound and a failed gate does exit non-zero.
- The dev stack now runs a MinIO too (9000/9001 published), so the whole suite can run
  against it with **zero skips**; the acceptance gate still brings its own MinIO from the
  pinned Chainguard image in its own project.
- The developer stack (`language-learning-platform` project) may already be running
  on 5432/6379 - the verifier must keep using its own project and auto-chosen ports.
- Running the whole suite against the live stack takes **~23 minutes** on this machine.
  Launch it in the background instead of assuming it hung: the per-module `TRUNCATE`
  reset is the slow part, and a `python.exe` that appears to be doing nothing is usually
  waiting on the database, not dead.
- A throwaway verification stack is cheap and safe: `docker compose -p <name> ... down -v`
  removes only `<name>_*` volumes, and `docker volume ls` proves which names belong to it
  before anything is removed.

## 8. Known defects / blockers

None open. Phases 1-2, 3 and 4 are accepted; the work now is Phase 5 (reading,
listening and media).

Carried forward, each one real and each one owned by a named later phase rather than
left unmentioned:

| Item | Owner |
| --- | --- |
| The login role tabs are clickable `<div>`s, not buttons - no focus ring, no Enter/Space | Phase 13 accessibility pass |
| `npm install` in the frontend image reports 4 vulnerabilities (3 moderate, 1 high) | Phase 13 dependency update |
| FastAPI/Starlette deprecation warnings (`ORJSONResponse`, the `HTTP_422_*` constants, per-request `cookies=` in the test client) | Phase 13, with the dependency upgrade |
| A word's pronunciation cannot be attached from the UI: `audio_asset_id` round-trips through the API and reaches the study card, but there is no upload surface until object storage and media exist. Nothing is faked in its place | Phase 5 |

Phase 4 reminder: the integration `conftest.py` no longer hardcodes the head revision -
it compares `alembic_version` with the head the scripts define and insists the history
stays linear, so a new `0002_*` revision is verified without touching the test. The
offline migration-immutability check still pins `0001_bootstrap` byte for byte.
