# PROJECT PROGRESS

Continuation state file. Reread this after any context loss and keep going - do not
re-plan work that is already recorded as done here.

- Current phase: **Phase 9 - monitoring and activity** (Phases 1-8 accepted)
- Git: branch `main`, remote `https://github.com/f1r10/ingilis-learning.git`
- Last commits: Phase 1-2 `2ab3294`, Phase 3 `c650bf5`, Phase 4 `20e5006`, Phase 5 `53911f2`,
  Phase 6 `1f0aa49`, Phase 7 `545de87` (`feat(phase-7): complete exams assignments and attempt
  engine`) - all pushed, `origin/main` == `HEAD`. `git rev-parse HEAD` is the authoritative tip;
  each phase commit rewrites this line with its own SHA

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
| 5 | Reading + listening + media libraries (object storage) | **accepted - `verify_phase12.sh` exit 0, 13/13 steps, 0 mandatory skips, live browser click-through on teacher and learner screens** |
| 6 | Catalogs + practice | **accepted - `verify_phase12.sh` exit 0, 13/13 steps, 0 mandatory skips, live browser click-through on teacher and learner screens** |
| 7 | Exams + assignments + attempt engine + grading | **accepted - `verify_phase12.sh` exit 0, 13/13 steps, 0 mandatory skips, live browser click-through on teacher, grading and learner screens** |
| 8 | Document import + review pipeline | **accepted - `verify_phase12.sh` exit 0, 13/13 steps, 0 mandatory skips, live browser click-through on the upload, queue and review screens** (the first run was 12/13; the three failures are in §9) |
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

## 6. Phase 5 - reading, listening and media

Backend, four modules with one shared core:

* `app/core/media_types.py` - byte signature table (17 formats: png/jpeg/gif/bmp/webp/
  tiff, mp3/wav/aif/flac/ogg/m4a, mp4/mov/webm/mkv/avi), `identify(head)`, `kind_of`,
  `label_for_mime`, and a `_REFUSED` list for containers that would be a lie to store.
* `app/services/media_service.py` - upload (spool → sha256 → identify → dedupe against
  live rows → store), reference counts, the trash veto, `learner_view` (the student-path
  projection), `update_metadata` (player-reported duration/dimensions), and the
  range-read path that asks the store for one slice instead of the whole object.
* `app/services/passage_service.py` - reading and listening as the same shape twice: one
  `PassageKind` descriptor naming the three tables, the set CRUD, the membership rules,
  the lifecycle words, the list/learner gates, bulk.
* `app/services/reading_service.py` / `listening_service.py` - what is not shared:
  `word_count` counted from the body (never accepted), `layout` stored not inferred, the
  transcript and its cue lines, `transcript_source` derived, the "nothing to hear cannot
  be published" veto, the playback rules.
* Endpoints: `media.py`, `reading.py`, `listening.py` (+ `/student/...` mirrors). 88 API
  paths in total now.

Migrations: `0003_membership_and_checksum` (`reading_set_question`,
`listening_set_question` with `uq_*_question` + `ix_*_set`, drops the non-unique
`ix_media_asset_checksum` for the partial unique `uq_media_asset_checksum`) and
`0004_media_reference_indexes` (the three FK-side indexes media joins on). `0001_bootstrap`
untouched; both new revisions downgrade and the gate's step 8 walks
`0001 -> 0004 -> 0001 -> 0004` for real.

Rules that are now contractual: the object key is generated and a hostile filename cannot
steer it; identical bytes are one asset and the second upload gets the existing row plus a
told-so; no presigned URL and no public object ever reaches a browser, and a learner may
read an asset only while ready, un-trashed content points at it; filing a question under a
set never writes a `QuestionVersion`; a question answers one block of one passage and
filing it twice moves it; `filed_under` freezes the binding while filed; a listening with
no live file and no transcript cannot reach `ready`; trashing a text/recording leaves its
questions, trashing a file that content needs is refused with counts.

Frontend (no redesign): `api/media.ts`, `api/reading.ts`, `api/listening.ts`;
`pages/MediaLibrary.tsx` (grid, upload with the server's own ceilings from `/media/meta`,
kind/origin filters, trash, reference counts, range-played preview), `components/MediaPicker.tsx`
(one picker used by the listening editor, the question editor and the vocabulary editor),
`pages/Reading.tsx` + `ReadingEditor.tsx` (body, live server word count, layout, sets and
filing by drag-free list, status veto shown as the server words it), `pages/Listenings.tsx` +
`ListeningEditor.tsx` (file, transcript, cue lines, playback rules, sets as slices),
`pages/StudentReading.tsx` + `StudentListening.tsx` (layout-aware text, range-streamed
player, replay counting, transcript only when allowed), routes `/reading`,
`/reading/new`, `/reading/:id`, `/listening`, `/listening/new`, `/listening/:id`,
`/media`, `/student/reading`, `/student/listening`, nav entries, and az/en/ru/tr copy
(600 leaves per locale, all four key-identical, verified by the offline locale contract).

### Defects found in this phase and fixed at the root

| Defect | Fix |
| --- | --- |
| **A fresh install could not upload anything.** Nothing in the application created its bucket: `ensure_bucket()` was called only by tests (and by the gate's step 12, which runs *after* the integration suite), so the first upload on a new MinIO answered 503 "storage unreachable" - 71 tests failed this way in the gate, and it was a real product failure, not flakiness | `ObjectStorage._prepare_write()`: a lock-guarded, once-per-client `ensure_bucket()` inside the `_translated(key)` block of both write paths (`put_bytes`, `put_file`). A store that refuses the create still raises. Pinned by `test_a_first_write_creates_the_bucket_it_needs`, which points `_settings` at a scratch bucket nothing else uses, asserts it does not exist, uploads, and only then sees it |
| `StudentReading` displayed a word count the server never sends (`page.word_count`, absent from `ReadingLearnerRead`) - `tsc` flagged it and the honest fix was to stop showing an invented number | the learner line now counts exercises from the payload's own sets (`set.question_count` summed); the teacher editor keeps the server's `word_count` |
| Six previously-green assertions had been outvoted by Phase 5 (question-bank media payload ×2, vocabulary audio/schema ×2, schema-live counts ×2) | each re-read against the product first, then tightened rather than loosened: the learner media set is now asserted key-by-key (`media_asset_id` must be absent), the exact refusal wordings are pinned, 47 tables / 98 indexes / the third partial unique index are the new floors |
| 7 TypeScript build errors (`StudentListening` truthy-string `controls`, `MediaLibrary` unused `t`, `ListeningEditor`/`ReadingEditor` draft-state type disagreement, `StudentReading` unknown `word_count`, unused `useMemo`) | fixed at the source: `Boolean(src)`, real module-level `DetailsDraft`/`BodyDraft` types so `setDraft` and the card props agree without casts, dropped dead imports - no `any`, no `@ts-ignore`, no field added to a type to please a component |
| The shipped `docker-compose.yml` defaulted the frontend's API base to `http://localhost:8000/api/v1` while nginx exists to serve `/api/` same-origin, so any stack that publishes the API on another port (or behind a domain) builds a bundle that cannot reach its own backend, and CORS then refuses it | the compose default is now `/api/v1` (the Dockerfile already had that default); an explicit `VITE_API_BASE_URL` still overrides |
| **A listening had no way to reach the questions bound to it.** The reading editor links to the question bank filtered by `reading_id`; the listening editor had no such link, so its block pool looked permanently empty and a teacher could not file anything they had just written | the same link added to `BlockList`, with `listening.open_bound_questions` in all four locales. Driven live: a short-answer question created through `/questions?listening_id=…` returns 201, appears in that recording's pool, and files into its block |
| **The learner's player kept inviting one more listening than the teacher allowed.** `usedUp` compared the number of started listenings with `plays > replay_limit + 1`, but `replay_limit` counts the listens *after* the first, so the budget is `1 + limit` and the comparison stayed false through the extra one | compared with `>=`, and the boundary was then driven in the browser at `replay_limit = 1`: the notice and the disabled play control appear exactly when the second listening has started, never later. A native player whose controls stay live under a "nothing left to hear" notice is a decoration, so the same `usedUp` also removes them |
| **Every learner page load logged a guaranteed 401.** The session restore probed `/auth/me/admin` first in a fixed order, so a signed-in learner failed one request before succeeding on the next - which reads like a broken session to anyone who opens the network panel, and was the single console error on every student screen | the probe is chosen by the address (`/student…` asks the learner endpoint first, anything else asks the teacher endpoint first) with the other role still tried before a browser is called signed out. Measured after the change: one `/auth/me/student` request and zero `/auth/me/admin` requests on a learner load |
| A learner's own landing page was labelled "İdarə paneli" (admin panel) in az | new `nav.home` key ("Ana səhifəm" / "Моя главная" / "Ana sayfam" / "Home") used by the student nav entry |
| The az copy called the pool of unfiled questions "hovuz" - a swimming pool - in six strings, and wore a Turkish suffix ("altındaki") in two more | rewritten as "blokda olmayan suallar" / "altındakı"; the built bundle is grepped for both, and the remaining `altındaki` hits belong to `tr.json`, where that spelling is correct Turkish |

### Evidence

- **`bash scripts/verify_phase12.sh` -> exit 0, `passed steps: 13 / 13`**, run alone on
  this machine: unit/static **356 passed**, integration (Postgres+Redis+MinIO)
  **368 passed**, MinIO round trip **11 passed**, each with
  `0 failed | 0 errors | 0 skipped`. Phase 4 left 201 + 196; Phase 5 added 146 offline
  rule tests (`test_media_rules` 99, `test_passage_rules` 47) and 169 live integration
  tests (`test_media_db` 48, `test_reading_db` 57, `test_listening_db` 56,
  `test_retention_db` 8) plus the new storage test.
- Step 8 read-back after a fresh `0001 -> 0004` run: 48 tables (47 model + alembic_version),
  486 columns, 61 foreign keys, 148 indexes; the downgrade to `0001_bootstrap` removed only
  what each later revision added, and the rebuild refused the duplicate it exists to refuse.
- Offline half alone on Windows: **356 passed** (5.2s); `ruff check app tests migrations
  scripts` clean; live per-file runs before the gate: `test_media_db` 48 passed (352s),
  `test_storage_minio` 11 passed (33s), `test_listening_db` 56 passed (414s), and the
  6 repaired assertions 14 passed (102s).
- `docker build frontend` (`tsc -b && vite build` in the shipped `node:20-alpine` stage)
  green after the 7 type fixes - and the build log is read, not `tail`ed into silence. It
  was run twice more after the last round of UI fixes (`nav.home`, the replay boundary, the
  role-aware session probe, the az copy), and the produced bundle is grepped inside the
  running image: `Ana səhifəm` present, `altındakı` present 3x, `hovuz` absent,
  `altındaki` present only as the 3 correct Turkish strings.
- Browser click-through, on the scratch stack (`llp_ui`, its own ports and volumes) as a
  real teacher session and a real student session:
  * listening lifecycle: publish refused at 422 with the server's own sentence while the
    recording has neither audio nor transcript → save → publish 200 → block created → a
    question created through the bank link scoped to that recording → filed into the block.
  * audience split, measured from the network log: every teacher surface asks
    `/api/v1/media/{id}/content` and every learner surface (two recordings, the reading
    question's image, the vocabulary study card) asks
    `/api/v1/student/media/{id}/content`; no storage host appears anywhere.
  * transcript gating: the recording with `show_transcript` on renders the cue lines with
    their times, the one with it off renders no transcript at all.
  * media lifecycle: `asset_in_use` refusal in the learner's language, a 93-byte PNG
    uploaded and read back as 30×26 with `—` references, trashed, restored, and its bytes
    still served.
  * replay boundary: with `replay_limit = 1` and both pause and seek refused, two real
    clicks on the page's own play control produced exactly two listenings, the file's own
    end did not consume a replay, and at the boundary the alert appears and the control is
    disabled. A scripted third `play()` still runs - the browser does not enforce this, the
    page does - which is why the control has to disappear rather than sit there enabled.
  * teacher app walked through `/dashboard`, `/listening`, `/media`, `/reading`,
    `/questions`, `/vocabulary`, `/settings` with zero console errors and no untranslated
    key left on screen; the media table shows its three files with kind, size, measured
    dimensions or duration and reference counts.
  * not exercised, and not claimed: the native `<audio>` shadow controls cannot be driven
    through this browser surface (a click on them lands on the seek bar), so the
    controls-are-removed assertion for that path rests on the `!usedUp` condition and the
    built bundle, not on a click. Replay counting is page-local by design today: a reload
    restarts the count, and the server-authoritative clock arrives with the Phase 7 attempt
    engine.

## 7. Phase 6 - catalogs + practice

Backend, three new services and two routers:

* `app/services/catalog_service.py` - the catalog as what it is: an ordered list of
  references. `to_read` resolves each reference against the live bank row (so `title`,
  `detail`, `state` and the level are never stored strings), the folder tree through
  `parent_id` (4 levels, siblings cannot share a name, a node cannot be moved inside
  itself, and the depth cap is measured on the way down as well as up), the availability
  chain (a learner reaches a catalog only when it and every folder above it is `ready` and
  alive), all-or-nothing item adding, whole-list reorder that refuses an omitted id, the
  block reference validated against the passage it claims to belong to (`broken_block`
  rather than the whole text served as a lesson), the trash veto that names how many
  folders are still inside, the bulk loop that keeps acting on what it refused until it
  stops making progress, and per-id answers (`updated` / `refused` + reason / `not_found`).
* `app/services/activity_service.py` - the only writer of `activity_event`. Append-only:
  a verdict is never edited in place, a second mark is a second row, `occurred_at` is
  written here rather than left to a default, and the coarse `device`/`browser` labels come
  from the client's own header with truncation instead of refusal (losing the row to a long
  header would lose the answer with it).
* `app/services/practice_service.py` - the run. `start_run` issues the 32-hex token and
  puts the shuffle seed on the start event; `_build_steps` resolves every reference through
  the same projection the teacher's preview uses and counts what it had to skip;
  `answer` re-authorises the token against the learner, grades with the bank's own engine
  and freezes the grade in the payload; `resume_run` re-derives the served order from the
  seed and writes nothing; `finish` is idempotent and releases held marks;
  `build_summary` rebuilds score/counts from the log alone; `known`/`learning` marks are per
  learner per word across the whole product; favorites are pointers that keep naming a gone
  exercise as gone.
* Endpoints: `catalogs.py` (10 paths) and `practice.py` (+`favorites_router`, 11 paths
  together). Schemas `catalog.py` / `practice.py`. The API is now 109 paths / 147 operations,
  22 of them under `/student/`.
* Phase 6 also widened Phase 5: `only_set_ids` threading through
  `passage_service.list_sets` / `learner_tree` and `reading_service` /
  `listening_service.learner_detail`, so one practice step can be served one block of a
  text or recording instead of the whole passage.

Migration: `0005_catalog_item_unique` - adds `uq_catalog_item_reference`
(`catalog_id, kind, ref_id`, unique) and downgrades by dropping only itself. It is added,
not swapped: `catalog_item` has no soft delete, so there is no trashed twin to forgive, and
a reference the teacher removed can be added again. `0001_bootstrap` through `0004` are
untouched; `test_migration_offline_sql` now pins the `CREATE UNIQUE INDEX` and its
`DROP INDEX` reversal, and `test_migration_schema_consistency` insists the new index is
declared by a model.

Rules that are now contractual: a Catalog is not an Exam and holds no content of its own;
grouping writes no `QuestionVersion`; one catalog names one piece of content once, enforced
in the database and not only in the service; a reference to a missing or trashed row is
refused at the door; adding is all-or-nothing; a bulk action answers per id; a preview
writes nothing and carries no token; a run is its events, and its summary is a read of them;
resuming never re-shuffles and never writes a second start; `after_session` withholds every
verdict until the finish; a type nobody can grade is reported as ungraded, never as zero;
`time_spent_seconds` is stored as reported and no server-side clock is implied.

Frontend (no redesign): `api/catalogs.ts`, `api/practice.ts`; `pages/Catalogs.tsx` (tree with
both counts, bank/trash views, filters, sort, and the selection toolbar that keeps refused
rows on screen with the server's reason); `pages/CatalogEditor.tsx` (settings, reference list
with reorder, a picker per kind filtered by level that shows what is already in the lesson,
the block picker, the preview); `pages/StudentPractice.tsx` (the shelf with the learner's own
run counts, plus the saved list) and `pages/StudentPracticeRun.tsx` (run, rail, result
screen); `components/PracticeStep.tsx` and `components/AnswerWidget.tsx`, the same widgets
the teacher's own previews use, so an exercise cannot look different in a lesson than it did
in the bank. Routes `/catalogs`, `/catalogs/new`, `/catalogs/:id`, `/student/practice`,
`/student/practice/run`; nav entries in both roles (and `end` on the learner's Home link, so
it stops being active on every student sub-page); 780 leaves per locale, four key-identical
files.

### Defects found in this phase and fixed at the root

| Defect | Fix |
| --- | --- |
| **A block could never be chosen.** `BlockPicker`'s query was gated on `enabled: Boolean(setId)`, so a catalog reference that had no block yet asked for none, and the panel the teacher opened to pick one listed only "All of it" | enabled becomes `Boolean(setId) || open`: the list is fetched when the row is closed (it has to name the block it points at) *and* when the panel opens. Rebuilt and driven in the browser: the reading reference now binds "After you read" and the listening one "Listen once", and both titles show on the closed rows |
| **The learner was promised a marking that no phase performs.** An essay came back `requires_manual` and the UI said "Waiting for your teacher" - in practice nothing feeds a grading queue, and the code's own comment on the neighbouring branch says there is no teacher in the loop | the copy now states what the server actually did: `practice.not_auto_marked` "Not marked automatically", in all four locales, at all three call sites (`AnswerWidget`, the result chip, the summary count). Verified against the built bundle (`Waiting for your teacher` absent, the new string present) and on the finished run's screen: "1 not marked automatically" |
| `session_factory` had no infrastructure guard of its own, so a Phase 6 test that drove the HTTP surface through it ran in an offline pass and failed on authentication instead of skipping with the reason the rest of the suite gives | the fixture now depends on `migrated_schema`, so the guard is the same one every other integration fixture uses |
| The learner nav marked "Home" active on every `/student/...` screen, because `NavLink` matches by prefix | `end` on that link |

Deliberately **not** changed: `Normalization.ignore_punctuation` stays `False` by default, so
a translation answered with the sentence's own full stop kept is graded against the key as
written. It is a teacher's switch, exposed in the question editor's "Answer matching" group
(punctuation / articles / accents) for `short_answer`, `gap_fill` and `translation`, and
`test_normalization_switches_are_honoured` pins both directions. Weakening it to make one
click-through look better would have silently changed how every existing key is graded.

### Evidence

- **Acceptance gate, clean room: `bash scripts/verify_phase12.sh` exit 0, passed steps 13/13**
  with `unit/static: 457 passed | 0 skipped`, `integration (Postgres+Redis+MinIO): 434 passed |
  0 skipped`, `MinIO storage round-trip: 11 passed | 0 skipped`, and step 13 executing the
  `expire_attempts` cron against live PostgreSQL. Its own throwaway project and volumes were
  removed by the script afterwards.
- **Full live suite, nothing skipped: `891 passed in 690.60s (0:11:30)`, EXIT=0, `grep -c
  SKIPPED` = 0** against the dev stack's PostgreSQL + Redis + MinIO (Windows venv,
  `.tmp/live.env` sourced, storage keys derived from it and never printed). Offline half
  alone: **468 passed**, `ruff check app tests migrations scripts` clean.
- Phase 6's own tests: `test_catalog_rules` **47** and `test_practice_rules` **53** offline,
  `test_catalog_db` **31** and `test_practice_db` **35** live - the last two re-run alone
  with `-rs` to prove it: **66 passed, 0 skipped**.
- `docker build frontend` (`tsc -b && vite build`) green, twice: once for the `BlockPicker`
  fix and once for the copy fix. The served bundle is grepped inside the running container -
  `Not marked automatically` present, `Waiting for your teacher` absent.
- Browser click-through on a throwaway `-p llp_ui` stack (own ports, own volumes, torn down
  with `down -v`), driven as a real teacher session and a real learner session, with every
  row created through the product's own HTTP API rather than inserted behind its back:
  * catalog tree built live: unit + lesson under it, references added from all four banks,
    a duplicate reference refused, reorder applied, preview shown, publish, then the bulk
    toolbar - the parent's trash **refused** with the server's own sentence ("this catalog
    still holds 1 folder(s) inside it") and the loop that takes a unit and its lessons in
    one selection, restore verified afterwards.
  * block binding, which is what caught the `BlockPicker` defect: after the fix the reading
    reference reads "Block: After you read" and the listening one "Block: Listen once".
  * one learner run covering **every widget the engine ships**: multiple choice, multi select
    (partial selection graded all-or-nothing), true/false, short answer, gap fill (2 of 2),
    matching (3 of 3), ordering, translation, essay (server `requires_manual`, counted, not
    zeroed), plus a reading-block question and a listening-range question served with their
    context. Word-card marks and "Save for later" exercised on the same run.
  * result screen read back from the log: `7 of 23`, 11 answered, 4 right, 6 wrong,
    1 not marked automatically, per-question lines with the stored explanations.
  * resume, proven on the server rather than only on the screen: a second run was opened by
    "Do it again", then the tab was really reloaded. `GET /runs/{id}/steps` returned the
    **same 16-step order** (identical `kind:ref_id` sequence) and `activity_event` still
    held exactly **one `practice_run_start` per session** - 14 answers, 1 finish, 5 favorite
    adds, 1 remove, 1 `practice_mark_known`, 1 `practice_mark_learning`.
  * the saved shelf (`/student/practice?tab=saved`) names each pointer from its own row
    ("Which of these are kinds of weather? / Question / multi_select") and "Take off the
    list" dropped the row and appended `favorite_remove` without touching the add.
- Carried, each owned by a named phase: the `0 s` duration on a listening picker row (Phase
  12 probe), physical object GC (Phase 11), server-side replay and exam timers (closed by
  Phase 7), English server *reason* strings inside a Turkish/Russian/Azerbaijani interface
  (closed for the exam/attempt surface by Phase 7, Phase 13 for the rest) and the `div`-based
  login tabs (Phase 13).

## 8. Phase 7 - exams, assignments, the attempt engine and grading (accepted)

Backend: two new services, two new router files (four routers), two schema modules and one
migration. The API is now **146 paths / 191 operations**, 32 of them under `/student/`; Phase 7
adds **37 paths / 44 operations** and removes nothing (the pre-Phase-7 surface is still exactly
109 / 147).

* `app/services/exam_service.py` (66 functions) - the teacher's side. An exam pins a
  `QuestionVersion` and a catalog does not, so `resolve_items` reads the *stored* version for
  every line and reports the item's `state` (`ready` / `draft` / `trashed` / `version_gone`) from
  the chain above it. Naming a reading or a listening adds **its** questions rather than the
  text, each bound to that passage so the context travels with the question and no second copy is
  stored. Composition locks the moment somebody sits (`LockedComposition` -> 409) while timing,
  shuffling, feedback and visibility stay editable, because those are copied into the attempt's
  own blueprint at start. `_publish_blockers` answers with the **same codes** the lifecycle check
  refuses with, so the note above the form and the refusal below it are one sentence;
  `freeze_composition` + `_deal` produce the sitting's order from its seed, `preview` runs the
  same code with no write and no token, and `clone` re-points a paper at the versions learners
  already met instead of at whatever the bank holds now.
* `app/services/attempt_service.py` (73 functions) - the sitting. **The learner holds a token, the
  server holds the clock**: a 32-hex token, and `remaining_seconds` recomputed from
  `expires_at - now()` on every read, so a device switch, a closed tab or a moved local clock
  changes nothing. `start` tells three cases apart - a second tab on a live sitting resumes it,
  a deadline that passed while the learner was away closes it and opens a numbered second one,
  and `resume_after_disconnect=False` closes the abandoned sitting from its autosaved answers.
  `save_answer` grades with the bank's own engine against the pinned snapshot and returns the
  verdict only when `feedback_timing` is `instant` (otherwise `withheld`, with the notice carried
  as a code), counts `changed_count` as a fact about the sitting rather than an accusation, and
  closes an expired attempt *on the way in* so a late submit is not graded by a different rule
  than an early one. `finalise` is the one path every closing goes through (submit, expiry sweep,
  tab limit), `_passed` refuses to guess a verdict while a mark is owed, `_result_state` derives
  `state`/`visible` from the paper's own `result_visibility` (`awaiting_teacher` and `closed` are
  different news), and `grade_answer` lets the teacher's `final_score` move the total, with the
  value it moved from in the audit row because a misread script is corrected, not rewritten.
* Refusals: every exam, attempt and grading rule now raises a `RuleBroken` carrying `code` and
  `params`, and `as_invalid` / `as_missing` / `as_conflict` map it to 422 / 404 / 409 with those
  two fields in the envelope (`backend/app/core/exceptions.py`). `meta()` on both sides serves the
  word lists (statuses, close reasons, notices, availability reasons) from the code that produces
  them, so no screen has to guess a literal.
* `app/services/question_engine.py`: the descriptor's English `label` is **gone** - a question
  type is data, and the screen now says it through `questions.type_<key>` in all four locales.
* Endpoints: `exams.py` (`router` + `grading_router`), `student_exams.py` (`router` +
  `attempts_router`). Schemas `exam.py` / `attempt.py`. Worker: `expire_attempts` now calls
  `attempt_service.expire_due` and returns what it did, so the cron and the request path close a
  paper the same way.

Migration: `0006_exam_attempt_integrity` - `uq_assignment_student` and `uq_assignment_group`
(two **partial** unique indexes, because a nullable column is never equal to anything under a
plain unique index and a single index over `(exam_id, student_id, group_id)` would let any number
of half-filled rows through), `uq_attempt_number` (`exam_id, student_id, attempt_number`),
`uq_manual_review_answer` (`answer_id`) and `ix_attempt_open_expiry` (`status, expires_at`), which
is what makes a fifteen-second sweep a read of the open papers rather than of the whole attempt
history. Nothing rewrites a row, so an upgrade over data that already breaks a rule reports the
violation instead of choosing which duplicate to keep; `downgrade()` drops only these five.
`0001`-`0005` are untouched; `test_migration_offline_sql` pins each `CREATE UNIQUE INDEX` with its
`DROP INDEX` reversal and `test_migration_schema_consistency` insists all five are declared by a
model.

Rules that are now contractual: an exam is not a catalog and holds references, never content;
every item is exactly one answerable question at a pinned version; composition freezes at the
first sitting while rules are copied per attempt; a paper is assigned, not opened - an
unassigned learner never sees it; one assignment per student and per group, one attempt number
per student, one review row per answer, all enforced in the table and not only in the service;
the server is the only clock; `no resume after disconnect` costs the sitting rather than leaving
it open; a hand-out to nobody is a refusal (`exam_no_audience`), not a success that wrote nothing;
a verdict is withheld when the paper says so, and an answer nobody can auto-mark is reported as
ungraded, never as zero; a re-mark moves the total; `after_approval` needs no extra flag because
marking the last answer *is* the approval; a tab switch is stored as the browser's own report and
the platform never claims it as evidence.

Frontend (no redesign): `api/exams.ts`; `pages/Exams.tsx` (the bank with lifecycle, filters and
the bulk toolbar), `pages/ExamEditor.tsx` (rules, sections, item picker over all four banks with
the pinned version and mark on every row, assignments, the sittings table, the preview that runs
the learner's own deal), `pages/ExamGrading.tsx` (queue, per-answer mark with the note the learner
will read, `changed_count` and time on the line, teacher feedback, the summary),
`pages/StudentExams.tsx` (the shelf with each paper's own state), `pages/StudentExam.tsx` (the
brief: what it is worth, how many sittings, when it closes), `pages/StudentExamRun.tsx` (the
runner: server countdown, autosave, the rail, the result screen as far as the paper's rules
reach). `components/AnswerWidget.tsx` now serves both products - `stepKey` (an exam line is keyed
by `exam_item_id`, because a paper holds a version and may hold two of them), `initial` (a
resumed sitting shows what was already typed) and `heading`. `i18n/format.ts` is the copy
formatter: `when()` renders an instant in the interface's locale and `span()` renders a duration
in it, so a sitting is "2 min 32 s" in English and "2 dəq 32 san" in Azerbaijani rather than
`152 s` everywhere. Routes `/exams`, `/exams/new`, `/exams/grading`, `/exams/:id`,
`/student/exams`, `/student/exams/run`, `/student/exams/:id` (`new` and `grading` are matched
before `:id`, or the browser goes looking for a paper called "grading"); nav entries and dashboard
tiles in both roles. **1228 keys per locale** (83 sections' worth of them new here), of which 80
are `errors.*` refusal codes, four key-identical files.

### Defects found in this phase and fixed at the root

| Defect | Fix |
| --- | --- |
| **A schema complaint pre-empted the rule it was reporting.** `AssignmentCreate` carried a `model_validator` that raised on an empty audience, so `POST /exams/{id}/assignments` with no names answered `validation_failed` with an English pydantic sentence, and the coded refusal written one layer below it (`exam_no_audience`) never reached anybody | the validator is gone. The payload accepts the shape; `exam_service.assign` refuses it with its own code, and the integration test asserts that code. The rule did not move and was not weakened - only the voice it answers in |
| **A test that read like a rule was actually reading two.** `test_a_scheduled_paper_needs_an_opening_time_before_it_can_be_issued` insisted the blocker list was exactly `["exam_schedule_needs_opening_time"]` on a paper that also had nobody assigned - so once the audience blocker became truthful, the exact list reported both rules at once | the test now puts a learner on the paper first, so the assertion isolates the schedule rule it is named for, and the audience rule stays owned by `test_an_unassigned_paper_publishes_and_the_editor_says_it_has_nobody`. The assertion was not loosened into a `in`-check |
| **The paper's answers were kept back and the screen accused the teacher of not marking them.** Under `show_correct_answers=false` every line rendered "Not marked", which is a sentence about a mark that is owed, not about a rule that hides the key | one line above the card says what the paper does (`practice.verdict_withheld`, in all four locales) and the per-line chip is dropped, so a learner reads the rule once instead of five times |
| **The teacher's sentence about an answer never reached the learner who asked for it.** `manual_review.reviewer_note` was written by the grading screen and read by nobody: `AttemptResultLine` had no such field | `_reviewer_notes` keys the notes by answer and `_result_lines` carries them; the result screen prints "Your teacher wrote: …" under the line. Pinned live and in `test_attempt_db.py`, including that a paper nobody marked by hand carries `None` rather than an invented sentence |
| **A sitting's length was a counter, not a sentence.** `2 min 32 s used` / `152 s` were assembled in the browser or read raw from the payload, so a Russian interface said it in English word order | `span(seconds)` in `i18n/format.ts` picks the shape (hours / minutes and seconds / seconds) and asks the locale for the words; three new `common.duration_*` keys and four re-pointed sentences (`exams.used_n`, `exams.left_n`, `grading.took`, `student_exams.took`). Verified in the browser in all four languages |
| **`<html lang>` lied.** It was set only when somebody touched the switcher, so a restored Azerbaijani session announced itself as English to a screen reader and a spell-checker | `i18n/index.ts` sets it after `init` (a restored language arrives with init and fires no change event) and on every `languageChanged` |
| **The learner's own language choice was undone on every load.** `AppProvider` re-applied `me.ui_language` each session read, so switching to Azerbaijani flipped back to the school's default after any reload | the profile value is applied only when the device holds no choice yet: `if (!localStorage.getItem("ui_lang") && me.ui_language)` |
| **A partially credited line was called wrong.** `correct` is a two-valued flag, so an answer scored 4 of 5 fell into `incorrect_count` while the same line on screen said "4 of 5" - in both the exam summary and the Phase 6 practice summary | `_totals` counts a line with a mark above zero and no full mark as `partial_count` (a new field on both read schemas), and the chip reads "Not the full mark" beside the numbers |
| **Every query retried a refusal, and paused when the tab was not focused.** `retry: 1` with react-query's default online check meant a 404 for "no such attempt" sat unanswered until focus returned - a spinner over an answer the server had already given | retry only when nothing answered (`status === 0`, which `ApiError` reserves for the wire refusing rather than the rule), plus `networkMode: "offlineFirst"` on queries and mutations so the first request goes out regardless of what `navigator.onLine` claims |
| **A lost connection said "Failed to fetch".** `fetch` rejects with the browser's own English sentence when nothing answers, and that prose went straight onto a learner's screen | the client turns it into `ApiError(0, "network_unreachable", …)` and the copy comes from `errors.network_unreachable`, the one refusal the screen makes without a server |

### Evidence

- **Acceptance gate, clean room, 2026-10-08: `bash scripts/verify_phase12.sh` exit 0,
  `passed steps: 13 / 13`** - unit/static **651 tests | 651 passed**, integration
  (Postgres+Redis+MinIO) **530 tests | 530 passed**, MinIO storage round-trip
  **11 tests | 11 passed**, each with `0 failed | 0 errors | 0 skipped`, so no mandatory
  test skipped and the migration round trip in step 8 carried `0006` up and back down
  against a live server.
- **Phase 7's own tests:** `test_exam_rules` **84** and `test_attempt_rules` **105** offline,
  `test_exam_db` **53** and `test_attempt_db` **43** live. Offline half of the repository:
  **651 passed**, `ruff check app tests migrations scripts` clean.
- **The coded-refusal contract**, `tests/test_frontend_i18n_contract.py`: an AST walk over
  `exam_service.py`, `attempt_service.py` and the two routers collects every code a refusal can
  answer with - from a `code=` at the raise site, a refusal class's default, or a refusal table -
  and fails on a raise that names none, on a code missing from any of the four locales, and on a
  locale key no server code produces any more. It also reads `refusalText("…")` in the client so
  the one screen-authored refusal is checked like the rest.
- Frontend compile through the shipped image (`tsc -b && vite build`) green; the served bundle
  grepped for the new strings inside the running container.
- Browser click-through on a throwaway `-p llp_ui` stack, every row created through the product's
  own HTTP API: a paper built from four banks, published, handed to a class, sat by two learners
  in parallel, resumed after a real reload, an essay marked by hand from the grading screen and
  the learner's result re-read with the teacher's sentence under it; refused at the door - an
  empty hand-out, a paper with a trashed question, a second attempt on a one-sitting paper, a
  learner trying to open a paper nobody assigned them to. Durations and dates read back in az/en/
  ru/tr on the teacher's sittings table and the learner's result header.

## 9. Phase 8 - document import and review pipeline (accepted)

Backend: three new services, one new core module, one new router, one new worker task and one
migration. The API is now **155 paths / 202 operations**; Phase 8 adds **9 paths / 11 operations**
all under `/imports` (`POST/GET /imports`, `GET /imports/meta`, `GET|DELETE /imports/{job_id}`,
`GET /imports/{job_id}/document`, `GET /imports/{job_id}/items`, `PATCH` +
`POST /imports/items/{item_id}[/decision]`, `POST /imports/{job_id}/retry`, `POST /imports/bulk`)
and removes nothing (the pre-Phase-8 surface is still exactly 146 / 191).

The phase's whole promise is that **the bytes decide the format, the shapes decide the candidates,
and a person decides the content.** Nothing in it invents meaning: a candidate carries only text
that is in the document, an incomplete row is kept and named as incomplete, and a row becomes
content only when somebody approves it.

* `app/core/doc_types.py` (10 functions) - what these bytes actually are. `identify` reads an
  8 KiB head and only then asks the filename: a `PK` zip is Word or Excel **once its members say
  so** (`word/document.xml`, `xl/workbook.xml`), a file is text only if it decodes strictly under
  `utf-8-sig`, `cp1254`, `cp1251` or `cp1252` (UTF-16 only behind its own BOM - an even-length
  Windows text file "decodes" as UTF-16 without ever failing and comes back as noise), and binary
  that happens to decode is still refused on its control characters. `delimiter_of` proves a table
  by three lines agreeing on a column count, so a prose lesson containing one comma is not a table
  and a semicolon export from a Turkish Excel is. `refusal_reason` names what is missing
  ("that file is Rich Text, and this importer reads Word 2007 or later") instead of guessing a
  format for it. `DocFormat` is the single registry the parser, the upload form's `accept`, the
  list screen's format column and `/imports/meta` all read, so the screen cannot promise a format
  the importer would refuse.
* `app/services/document_parsing.py` (40 functions, 1001 lines) - the document's own text, stdlib
  only. Word: paragraphs in order, `w:t` runs that the formatter split rejoin into one sentence,
  tables keep their cells as cells, `missing body` and `truncated` are different refusals. Excel:
  sheets read by name and cells by column, so a candidate can say which sheet it came from. PDF:
  its own content-stream tokenizer (octal escapes, balanced parentheses inside literal strings,
  `TJ` arrays whose wide gaps become the space between two words), line position from `Td`/`TD`/
  `T*` so a page written by stepping down the page keeps its lines apart, subset fonts read
  through their own `ToUnicode` map, object streams inflated, and a picture-only PDF failing with
  the capability it needs named rather than returning an empty document. Text/Markdown: the
  encoding it decoded under is recorded, `#` headings become headings with the markup dropped.
  Every block carries provenance (`page`, `sheet`, `offset`) and no parser returns a word the
  document did not have.
* `app/services/import_candidates.py` (26 functions) - shapes, not meanings. `candidates()` sends
  csv/tsv/xlsx to the header-reading path and everything else to the shape-reading path.
  `header_roles` understands the headings a teacher actually writes - `soru/şıklar/doğru`,
  `срок/вопрос/варианты/ответ`, `word/meaning/example` - and gives each role to one cell only.
  An answer may be a letter, a one-based number or the option's own words; two identical options
  that both match mark **no** option and report `missing: ["answer"]`. Lettered option columns are
  only accepted when they run A,B,C without a gap. Prose: a numbered stem and the lettered lines
  under it are one question, options stop where the next stem begins, a stem followed by prose is
  not a choice question, a heading plus the paragraphs under it is a reading, a passage with no
  heading of its own takes its first sentence as one and says so, a paragraph too short to be a
  text stays a note, and a two-column word table becomes vocabulary entries. `NOTE_CODES` is the
  seven remarks a card can carry - each a code, none a sentence.
* `app/services/import_service.py` (63 functions) - the queue and the decision. An upload is
  spooled and hashed while it streams, so `MAX_DOCUMENT_UPLOAD_MB` (60, published by `/meta`) is
  enforced **while the bytes are still arriving** and a file over the ceiling leaves no stored
  object and no row; the sha256 goes on `source_file.checksum`, and identical bytes answered by
  `{job, duplicate: true}` return the queue that already exists instead of opening a second one a
  teacher would have to work through twice. `run_job` claims the job with a conditional `UPDATE`
  committed on its own, so a second worker arriving mid-parse finds nothing to claim and answers
  `not_claimed` rather than doubling every candidate. `PATCH /imports/items/{id}` **never
  overwrites `extracted`** - the teacher's words land in `corrected`, which is what the approval
  path reads and what the card shows as "the document said / you wrote". A field the kind does not
  own is refused before it is stored, provenance fields cannot be smuggled in through `filing`, a
  filed row is closed to editing, and a refused row reopens to `pending` when it is edited, because
  a changed mind is a new decision. Approval writes through the bank's own services (a question
  arrives as the bank would build one, a word is filed in the language the **teacher** chose and
  not the one the paper claimed, a passage becomes one text), and `uq_import_item_result` is what
  makes one candidate one piece of content even when two people click at the same second. Bulk
  actions run over a page capped at `/meta`'s number and answer **row by row** with
  `done` / `refused` / `not_found`; a re-read of a job that already produced candidates is refused
  with `job_has_candidates` and its count in `params`. An unreachable worker is a 503 about the
  platform, not a refusal of the teacher's paper.
* `app/core/tasks.py` and `app/workers/jobs.py`: one task name (`process_import_job`) shared by the
  enqueue side and the worker, asserted equal by a test; a malformed id is `bad_id` in the log
  rather than a queue parked in `processing` forever, and any other exception is written as a
  failure with a sentence, because a worker that gives up quietly leaves a queue waiting for news
  that will never arrive.
* `media_service` lost its private `_display_name`; the rule moved to
  `storage.safe_display_name`, which both the media library and the importer now call.
* `app/models/content.py`: `source_file.checksum`, and `import_item.missing` / `filing` / `note` /
  `position` as columns of their own rather than keys inside `extracted`, because what a teacher
  approved has to stay provable as the document's own text.

Migration: `0007_import_integrity` - `uq_source_file_checksum` (**partial**, `WHERE deleted_at IS
NULL`, because trash is a soft delete and the same bytes may legitimately be imported again after
the first copy is gone), `uq_import_item_result` (partial on `result_ref_id IS NOT NULL`: two
teachers approving one row in one second would otherwise put two questions in the bank from one
sentence of one paper, and neither read could tell the learner which was the duplicate),
`ix_import_item_job_decision` ("what is still waiting in this job" is asked on every visit to the
review screen, and bootstrap only indexes `job_id`), and `ix_import_item_job_position`
(`import_item.id` is a random UUID, so an unordered read arrives in a different sequence on every
page - a teacher works through a paper question 1, then 2, then 3), plus the four columns.
`0001`-`0006` are untouched; `test_migration_offline_sql` pins each new index with its reversal
and `test_migration_schema_consistency` insists all seven revisions' objects are declared by a
model. The replay harness (`tests/migration_replay.py`) had to learn `op.add_column` for exactly
this revision: a schema change that *widens* a table has to reach the replayed `MetaData` through
the literal call a real `alembic upgrade` would render, defaults and nullability included, or the
consistency guard would quietly be comparing the models with themselves. Nothing rewrites a row, so an upgrade over data that already breaks a rule reports the
violation instead of choosing which duplicate to keep.

Rules that are now contractual: a document is named from its bytes and its name only ever breaks a
tie between text formats; a file this importer cannot read is refused with the reason, never
emptied into zero candidates and called success; a candidate never contains a word the document
does not; a choice question with no key is a candidate a person has to finish, not a guess; the
reader's remarks and the service's refusals are codes, and every one of them exists in four
locales; a page of the queue is a window, not a way to break a limit; nothing an import produces is
live content until a person files it, and filing it once closes it - the second approval is refused
from the row as it stands when the write happens, re-read under a lock rather than as the request
first remembered it; a trashed source is only
trashed when no queue still cites it; re-reading a job that already produced candidates is refused
rather than appended to; a duplicate upload answers with the queue that exists, and says so.

Frontend (no redesign): `api/imports.ts`; `pages/Imports.tsx` (the upload form - formats listed
from `/meta`, an optional name, the "file complete cards on their own" switch, and the ceiling
spoken from `/meta` in the refusal rather than guessed in the client - and the list: title search,
status filter, four sorts with a direction toggle,
pagination, and per row Open / Original file / Read it again / Remove); `pages/ImportReview.tsx`
(the queue: kind/decision filters, position and confidence ordering, page-scoped selection with
"50 of 300 selected" said honestly, the bulk bar whose `file` action carries its own lifecycle
choice, the per-card editor that shows the document's words and the teacher's words separately, the
filing pickers for status/level/language/topics/tags, the coded per-row bulk result, the empty
queue explained, and the failed job's reason as the locale's sentence). `i18n/format.ts` gains two
formatters: `size()` renders a byte count with the interface's own thousands separator, so the media
library and the import history cannot write the same 1.5 KB two different ways, and `wordsFor()`
resolves a remark code through
`imports.note_*`, then
`imports.outcome_*`, then `errors.*`, then the server's sentence, then the bare code - so a screen
can never print an unfilled `{{placeholder}}` and an undeclared code still reads as itself instead
of as nothing. Routes `/imports` and `/imports/:jobId`, one nav entry. **1392 keys per locale**
(125 of them new under `imports.*`, 8 codes under `imports.note_*`, 108 `errors.*`), four
key-identical CRLF files.

### Defects found in this phase and fixed at the root

| Defect | Fix |
| --- | --- |
| **A paper saved on Windows came back as one long note.** `_parse_plain` split paragraphs on `\n\n`, and a `\r\n\r\n` file - every text or Markdown lesson written by a Windows editor - contains none. The whole lesson collapsed into a single 918-character `note` card, and the heading inside it never became a heading | `text.replace("\r\n", "\n").replace("\r", "\n")` before the split. Three tests (`test_a_paper_saved_by_a_windows_editor_still_splits_into_paragraphs`, `test_a_markdown_lesson_saved_with_crlf_keeps_its_heading_as_a_heading`, `test_a_markdown_paper_saved_on_windows_is_still_a_heading_and_a_passage`), and proven live: the same `.md` re-uploaded after the fix yields one reading card with its title, where before it yielded one note. Checked by mutation - removing the line turns those three red |
| **An unfamiliar header was silently demoted.** `_table_candidates` kept every row of an unknown-header sheet as a `note` with `note=None`, while the prose path had already been giving `unmatched_header` for the same event. A teacher saw a row of text and no reason | the branch now names its remark, and its remark says which header it came from. Splitting it exposed a second case that is genuinely different - a *known* header whose row fills no content column (`level;type` in one row) - which now has its own code, `row_without_role` |
| **A remark was an English sentence, stored and printed.** The candidate `note` was free text and the job's failure was read out of `job.error` directly, so a Turkish screen showed English and could not tell "needs OCR" from "needs a language" | `NOTE_CODES` declares the seven codes the reader can leave, `note_codes()` advertises them (merged with the filing side's `bank_refused`) through `/imports/meta`, and the DB stores the **code**. The screen resolves it - `imports.note_*` for a card, `imports.outcome_*` for a job - through `wordsFor()`, with the server's sentence kept only as the fallback for a code no locale names. Two guards keep it true: an AST walk fails if any `.note =` / `note=` assignment in `import_candidates.py` names something not declared, and the locale test fails if any advertised code is missing from any of the four files. What is left coarse - one outcome code covering "empty" and "damaged" alike - is recorded in §12 rather than hidden |
| **A rule the worker claimed in prose had no test.** `run_job`'s docstring promised that a second delivery answers `not_claimed`, and nothing in either suite reached it - the live file only ever called the worker once per job, and the API-side refusal (`job_has_candidates`) is a different rule on a different path | `test_a_message_delivered_twice_does_not_read_the_paper_twice` reads one job through `jobs.process_import_job` twice and asserts the second answer, the unchanged candidate count, and that the message which claimed nothing also wrote no audit row. Proven non-vacuous: with `if claimed.rowcount != 1` disabled the second call returned `ok` and filed a second copy |
| **A spreadsheet missing column B would have shifted every answer in the file.** Reading `a`/`c` as two option columns silently renumbers the key, so `answer: C` would have marked the second option and looked like a clean success | `_letter_option_columns` only accepts a run that starts at `a` and has no gap, and `test_lettered_columns_only_count_when_they_run_from_a_without_a_gap` pins both halves of that |
| **The importer was about to grow its own copy of the upload-name cleaner.** `media_service._display_name` held the only rule for a field a client controls completely, and a second private copy in a second module is how one of them gets edited and the other forgotten | the function moved to `storage.safe_display_name` (with `DISPLAY_NAME_LIMIT`) and both callers use it, so there is one place the platform decides what an uploaded filename may look like. Its tests moved with it: `test_storage_keys.py` now covers the rule, and the media suite's copy of those cases is gone |
| **A byte count had one formatter and one caller who had to remember the locale.** `MediaLibrary.tsx` owned a private `formatSize(bytes, locale)` - correct, but local, and it needed `i18n.language` threaded in by hand. The import history shows the same fact (`· 1.5 KB`) on a second screen, and the honest options there were to write it badly or to copy that function | `size(bytes)` in `i18n/format.ts` is the one formatter and reads the interface's locale itself. The media screen's private copy is deleted, so a third screen gets the same words for free instead of a second implementation to forget |
| **A guard that held only most days.** The exam fixture keyed a paper's parts with `uuid.uuid4()`, and `_deal` shuffles a part with `seed + str(section.id)` - so an assertion about a shuffled part's dealt order was re-drawn on every run, and could pass on the machine that wrote it and fail on the one that reads it | the fixture uses stable ids (`uuid.UUID(int=position + 1)`) with the reason written above it. Not a loosened assertion: the question "was this part ever dealt in a different order" now gets the same answer every time it is asked |
| **The same paper sent twice in one moment answered 500.** The clean-room gate's live run raised `duplicate key value violates unique constraint "uq_source_file_checksum"` out of an `INSERT INTO source_file`. `_live_by_checksum` asks, the library says "not here", the colleague's transaction commits in between - and that is exactly the moment the index exists for. The `IntegrityError` handler was there, but it wrapped the *second* flush (`import_job`), which never gets reached: the source row is the one that conflicts | both writes are inside the one guard now, so the loser rolls back, takes its copy of the paper back out of the bucket and answers with the queue that won. `test_a_paper_taken_over_by_a_colleague_between_the_lookup_and_the_write_is_a_duplicate` fixes the interleaving the concurrent test only sometimes hits - the pre-check made to answer `None` once - and asserts one source, one job, one stored object, one audit row and no second message to the worker. Checked by mutation: with the source flush back outside the `try`, that test and the concurrent one both raise the same `UniqueViolationError` the gate reported |
| **Two teachers approving one candidate put two questions in the bank.** The same gate run answered `[200, 200]` where the rule says `[200, 409]`. The row lock was there, and the second request did wait - but a locking ORM read hands back the instance its session's identity map already holds *without refreshing the attributes on it*, so the teacher who arrived second was given the row back and still read the `pending` their own session had loaded before the lock was won. `_locked`'s docstring claimed the refusal "comes from the state"; the state it read was a moment out of date | `.execution_options(populate_existing=True)` on the locking select. The claim is rewritten wherever it was made - in `_locked`, in `SourceFile`/`ImportItem`'s `__table_args__` comments and in `0007`'s docstring - to say which half of "one candidate becomes one content item" an index can hold (no two candidates may point at the same row, since each mints its own id the index has nothing to disagree about) and which half only the re-read can hold. `test_a_candidate_whose_filing_happened_after_the_request_read_it_is_refused` makes that interleaving the setup instead of the luck, and refuses with `candidate_filed` with one question left in the bank. Checked by mutation: without the option it fails `DID NOT RAISE AlreadyFiled` while the concurrent test still passes - which is the reason the deterministic one exists |
| **The partial-index guard named five and the schema held seven.** `test_indexes_exist_are_unique_where_declared_and_ordered` compares the live partial unique indexes both with the models and with a set spelled out in the test, and `0007` added `uq_source_file_checksum` and `uq_import_item_result` without that second half being kept up. The guard did its job - it refused to let a new partial unique index arrive unargued | the two names are in the list with their argument written next to them (a trashed source frees its bytes for a re-import; a filed pair is unique once it is written at all). Nothing about the indexes themselves was changed to make the test pass: the set derived from the models already matched the live database |

### Evidence

- **Acceptance gate, clean room: `bash scripts/verify_phase12.sh` exit **0**, `passed steps: 13 / 13`
  - unit/static **813 passed / 0 failed / 0 skipped**, integration (Postgres + Redis + MinIO)
  **568 passed / 0 failed / 0 errors / 0 skipped** in 13 m 28 s, MinIO round-trip **11 / 11**, and
  the ARQ worker registered its functions and ran `cron:expire_attempts()` against live PostgreSQL.
  The verification project's containers and volumes were removed by the run itself.
- **The first gate run of this phase reported 12 / 13**, with three integration failures, and every
  one of them was a real defect rather than a flaky environment: the two races in the table above
  (which the concurrent tests only sometimes reach, and which the clean room reached) and the partial
  index guard's named set. The 568 above are those two fixes plus the two deterministic tests that
  keep them honest.
- **Phase 8's own tests:** offline `test_document_parsing` **38**, `test_import_candidates` **42**,
  `test_import_rules` **63**; live `tests/integration/test_import_db.py` **38**, run against the dev
  stack together with `test_schema_live.py` as **48 passed / 0 failed / 0 skipped** in 6 m 33 s. The
  whole offline half of the repository: **813 passed / 568 deselected**,
  `ruff check app tests migrations scripts` clean.
- Frontend compile through the shipped image (`tsc -b && vite build`) green, and the new locale keys
  verified inside the built bundle the container actually serves.
- Browser click-through on the dev stack, every row created through the product's own HTTP API or the
  upload widget itself: Word, Excel, CSV, TXT and Markdown papers read into cards (PDFs and the
  picture-only refusal are proven in the live suite);
  a keyed question approved straight into the bank; a word row completed by hand and filed in the
  language the teacher chose; a foreign-header CSV kept as text and *named*; a bulk approve refused
  row by row ("0 done · 3 refused") with the reason per row, then a bulk re-file and a bulk refuse
  over a whole page; both sort directions; page-scoped selection on a 60-row paper shown as
  "50 of 300 selected"; the CRLF Markdown passage approved into the reading bank; a re-upload of the
  same bytes answered with the queue that already exists; `Original file` returning the stored bytes
  unchanged (`text/markdown`, `attachment; filename="crlf_paper.md"`, the same 575 bytes and the same
  CRLF); and a queue trashed from the list, after which the job answers 404 with
  `import_not_found`.

## 10. Working commands

```bash
# mandatory acceptance (WSL / Linux / macOS / Windows; needs Docker, no make)
bash scripts/verify_phase12.sh                 # add --keep to inspect the run afterwards
# Run it alone. Two concurrent runs share the compose project `llp_phase12_verify`, and the
# second one removes the first one's database at startup: the first then reports step 11 as
# hundreds of errored tests and step 12/13 as skips, which is a collision, not a defect.

# offline developer tests (never an acceptance result)
cd backend && pytest tests --ignore=tests/integration
cd backend && ruff check app tests migrations scripts

# integration tests against the dev stack's PostgreSQL/Redis (Windows venv).
# The URL is derived, never printed, and --basetemp keeps Windows temp dirs sane:
cd backend && mkdir -p ../.tmp \
  && DBURL=$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2- | sed 's|/app$|/app_test|') \
  && MSYS_ENV_CONV_EXCL='*' PYTHONIOENCODING=utf8 DATABASE_URL="$DBURL" \
     .venv/Scripts/python.exe -m pytest tests/integration -q -p no:cacheprovider --basetemp=../.tmp/bt
# one process at a time: two suites share app_test and truncate each other.
# `MSYS_ENV_CONV_EXCL='*'` stops Git Bash rewriting the URL, and the exam/attempt
# pair is the slow one: `tests/integration/test_exam_db.py
# tests/integration/test_attempt_db.py` alone is ~14 minutes for 96 tests.

# frontend compile, using the image the product actually ships (no local node here)
wsl.exe -- bash -lc 'rm -rf ~/fecheck; mkdir -p ~/fecheck; cp -r <repo>/frontend/. ~/fecheck/; cd ~/fecheck; docker build -t llp-fe-check .'

# dev stack - on this machine, with the compose defaults rather than the local .env
# (a fresh clone elsewhere: `cp .env.example .env`, fill the secrets, then plain `up --build`)
wsl.exe -e bash -lc 'cd /mnt/c/Users/firon/Documents/Qoder/2026-10-06/1438da7f \
  && docker compose --env-file /dev/null up -d --build'
docker compose up -d postgres redis minio

# every docker command on this machine goes through WSL (no docker on the Windows PATH)
wsl.exe -e bash -lc 'cd /mnt/c/Users/firon/Documents/Qoder/2026-10-06/1438da7f \
  && docker compose --env-file /dev/null up -d --build --force-recreate frontend'
# after a rebuild check the asset hash in the served bundle changed.
```

Windows venv (offline work only): `backend/.venv/Scripts/python.exe`.
From Git Bash on Windows the repo path is `C:/Users/firon/Documents/Qoder/2026-10-06/1438da7f`;
bash state does not persist between commands, so `cd` in every command.
WSL path for the same repo: `/mnt/c/Users/firon/Documents/Qoder/2026-10-06/1438da7f`
(run with `wsl -e bash -lc '...'`).

## 11. Environment facts worth not re-discovering

- **Bringing the dev stack up on this machine: `docker compose --env-file /dev/null up -d --build`**
  (from WSL, in the repo). The null env file is what keeps the stack on the compose defaults
  - verified: `docker compose --env-file /dev/null config` publishes
  `8000 / 5173 / 5432 / 6379 / 9000-9001`. Without it Compose interpolates from the repo `.env`,
  whose ports are this machine's scratch values (see the `.env` bullet below), and the stack comes
  up on `55432`, which the Windows venv's `DATABASE_URL` (`localhost:5432`) then cannot see.
- **The repo `.env` on this machine is not the file the product was designed around**: it pins
  `BACKEND_PORT=18000 FRONTEND_PORT=15173 POSTGRES_PORT=55432 REDIS_PORT=6380
  MINIO_API_PORT=19000/19001` while its own `DATABASE_URL`/`REDIS_URL` point at `localhost:5432`
  and `localhost:6379`, and it sets `BOOTSTRAP_ADMIN_USERNAME=uiverify` where `.env.example` says
  `admin`. Because the stack runs on compose defaults, that username never took effect - the dev
  database only holds `admin`. It is a local file, it is gitignored, and it is not a secret to
  commit; read it, do not trust it as documentation.
- **A transient notice does not re-translate.** `setMessage(t("imports.duplicate", …))` resolves
  the sentence when the action happens (three sites do this), so switching the interface language
  while the banner is on screen leaves the old sentence until the next action. That is the app's
  convention, not a leak: the copy comes from a key, and the key exists in all four locales.
- Browser automation: `upload_file` needs the **snapshot uid** of the `input[type=file]`, so take a
  snapshot first; after the file lands, re-query the submit button (the upload enables it, and a
  stale uid or handle is a disabled-button click that silently does nothing). The duplicate answer
  appears as `main .alert`, not as a toast that sweeps itself up.

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
- The dev stack runs a MinIO too (9000/9001 published), so the whole suite can run against
  it with **zero skips** - but only with the credentials the volume was *initialised* with.
  The repo `.env` names a different root pair, so `_minio_probe()` answered
  `InvalidAccessKeyId` and **114 storage-guarded tests skipped on a run that looked live**
  (777 passed / 114 skipped). Fix without touching a volume: read the running container's
  own `MINIO_ROOT_*` into the gitignored `.tmp/live.env` and never echo them -
  `docker inspect <minio> --format "{{range .Config.Env}}{{println .}}{{end}}" | grep MINIO_ROOT_`
  redirected into the file. Then the same command reports **891 passed, 0 skipped**.
  A skip in the developer loop is a signal to read, not a result to publish.
- **Browser automation against this UI.** A `click` on a snapshot `uid` frequently does not
  fire the React `onClick` (stale uid) - drive it with `evaluate_script` and
  `element.click()` instead. React-controlled inputs need the *native* setter before the
  `input` event: `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set
  .call(el, v); el.dispatchEvent(new Event("input", {bubbles: true}))` (textareas use
  `HTMLTextAreaElement.prototype`, selects use the `select` prototype plus a `change` event).
  `evaluate_script` gives up at 15 s, so batch at most two or three interactions per call.
  The login role tabs are `<div class="tab">`, not buttons.
- Reading the verification stack's database is a one-liner, and the table names are
  **singular** (`activity_event`, `favorite`, `catalog_item`):
  `docker exec -i "$PG" sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -A -t -f -' < q.sql`
  - pass the SQL on stdin from a file rather than fighting nested quotes, and note that a
  `count(distinct (a, b))` is not valid here.
- The frontend container serves a **built** bundle: after any `frontend/src` edit,
  `docker compose ... build frontend && up -d frontend`, then grep the served
  `assets/index-*.js` for the string you changed. The asset hash changing is the proof the
  build was not cached.
- Invoking WSL from Git Bash: `wsl.exe -- bash -lc '...'` (a bare `wsl -lc` is rejected);
  never hand `/mnt/...` paths to `wsl.exe` as arguments (MSYS rewrites them) - either keep
  them inside the single-quoted body or prefix the call with `MSYS_NO_PATHCONV=1`.
- The developer stack (`language-learning-platform` project) may already be running
  on 5432/6379 - the verifier must keep using its own project and auto-chosen ports.
- Running the whole suite against the live stack takes **~23 minutes** on this machine.
  Launch it in the background instead of assuming it hung: the per-module `TRUNCATE`
  reset is the slow part, and a `python.exe` that appears to be doing nothing is usually
  waiting on the database, not dead.
- A throwaway verification stack is cheap and safe: `docker compose -p <name> ... down -v`
  removes only `<name>_*` volumes, and `docker volume ls` proves which names belong to it
  before anything is removed.
- **The gate must run alone.** `verify_phase12.sh` builds images, starts six containers and
  hammers them for ~15 minutes on Docker Desktop. Running it at the same time as a dev-stack
  rebuild or a heavy live pytest run produced 71 upload failures that were *not* the product's
  fault in the first instance - and discovering that required re-running it clean. Sequential
  is the only honest schedule here.
- **The repo-root `.env` is what `docker compose` reads for interpolation, and on this machine
  it pins non-default host ports** (`BACKEND_PORT=18000 FRONTEND_PORT=15173 POSTGRES_PORT=55432
  REDIS_PORT=6380 MINIO_API_PORT=19000/19001`) while `backend/.env` (the Windows venv's own
  file) points at `localhost:5432`. Recreating the dev stack with `docker compose up -d backend
  frontend` therefore recreates the *whole* project - and because the postgres **volume** keeps
  the password it was initialised with, the new backend came up with a different
  `POSTGRES_PASSWORD` and crash-looped on "password authentication failed for user app", while
  Windows pytest saw `ConnectionRefusedError` on 5432. Recovery without touching any volume:
  pull the volume's real password out of `backend/.env` without printing it and override the
  ports back to defaults -
  `PW=$(grep -m1 "^DATABASE_URL=" backend/.env | sed "s|.*://[^:]*:\([^@]*\)@.*|\1|") &&
  POSTGRES_PASSWORD="$PW" POSTGRES_PORT=5432 REDIS_PORT=6379 MINIO_API_PORT=9000
  MINIO_CONSOLE_PORT=9001 BACKEND_PORT=8000 FRONTEND_PORT=5173 docker compose up -d postgres
  redis minio backend frontend`.
  Two consequences that are easy to forget: never `up -d` a couple of services on this
  repository without deciding which ports the stack should end up on, and a verification
  stack on the `.env` ports needs the frontend's API base to be same-origin (`/api/v1`), not
  a literal `localhost:8000`.

## 12. Known defects / blockers

None open. Phases 1-2 through 8 are accepted, Phase 8 on a clean-room gate run that reported
13/13 steps with zero skips after its first run found the three defects recorded in §9. The work
now is Phase 9 (monitoring and activity).

Carried forward, each one real and each one owned by a named later phase rather than
left unmentioned:

| Item | Owner |
| --- | --- |
| The login role tabs are clickable `<div>`s, not buttons - no focus ring, no Enter/Space | Phase 13 accessibility pass |
| `npm install` in the frontend image reports 4 vulnerabilities (3 moderate, 1 high) | Phase 13 dependency update |
| FastAPI/Starlette deprecation warnings (`ORJSONResponse`, the `HTTP_422_*` constants, per-request `cookies=` in the test client) | Phase 13, with the dependency upgrade |
| Duration and pixel dimensions are reported by the player through `PATCH /media/{id}`, so a file nobody has opened keeps `null` and the UI says it has not been measured. This image has no codec library, and a number invented by a half-parser would end up on a learner's timer | Phase 12 (a real worker-side probe behind the optional adapter switch) |
| Trashing a media asset removes it from the library, never from the bucket: no code path deletes an object except the tests' own cleanup. Physical garbage collection has to be a decision made by the phase that can restore a mistake | Phase 11 backups |
| `transcript_source` can only ever be `manual` or `absent`, and a stored asset's `source_origin` is only `upload`: a client that claims `imported` or `auto` is refused, because provenance asserted by a browser is a claim, not a record | Phase 8 importer, Phase 12 speech adapter |
| The daily retention job counts trashed rows past `TRASH_RETENTION_DAYS` and **removes nothing** (`removed: 0`, audit row says so) | Phase 11, once a backup makes removal survivable |
| The learner player's replay counter is counted in the tab (`StudentListening.tsx`), so a refresh starts the count again. The rule itself is the server's and is delivered with the payload; the standalone listening screen has no attempt to belong to, and an attempt is the only thing a durable count could hang from | Phase 9, where a durable activity counter has a home |
| Sentences the server refuses with are shown in English inside a Turkish, Russian or Azerbaijani interface - **closed for exams, attempts and grading**, which answer with a code and its `params` and are translated by `refusalText()` (`errors.*`, 80 keys per locale, contract-tested). The other modules still share one `validation_failed` code across many distinct refusals, and pydantic's structural messages (`field_required`, `extra inputs not permitted`) arrive as prose | Phase 13, with stable reason keys per refusal |
| Labels the server supplies - vocabulary parts of speech and language names - arrive in English and are printed as they came. They are data, not interface chrome, so they need the same code-and-locale treatment rather than a client-side guess. Question-type labels are **closed**: the engine's descriptor no longer carries a `label` at all, and the screen says `questions.type_<key>` | Phase 13 |
| `allow_previous` and `restrict_copy_paste` are enforced by the runner screen (`StudentExamRun.tsx`): the paper does not offer a way back and the copy handlers are suppressed. A browser cannot make that unbreakable - view-source and a second device are outside what any front end can refuse - and pretending otherwise would be a claim the server cannot support | Phase 13 (server-side step gating: only the current line is ever sent) |
| A listening line on a paper carries its `replay_limit`, `allow_pause`, `allow_seek` and slice in the sitting's own blueprint, and the runner **names** them to the learner, but the plain `<audio controls>` it renders honours none of them: the browser's own seek bar replays as often as the learner likes. The exam's tab-switch counter is a server fact, the replay counter is not | Phase 13, with the runner's other browser-side rules (one counted play per request, or a single-use signed slice) |
| An answer typed while the learner was offline is buffered in the tab and is lost if the deadline passes before the wire comes back. The server refuses it on arrival (`attempt_time_up`) and closes the sitting, which is the same rule a late submit meets | Phase 13 (a queue that survives a reload, or the notice says so) |
| `grading_mode=ai_assisted` is served as `{"available": false}` and refused at the door: no local grader exists yet, and a mark invented by a stub would be a grade a learner cannot appeal | Phase 12 (the optional adapter, with a tested disabled path) |
| A teacher's per-answer mark, the learner's `changed_count` and the tab-switch count are all stored and shown, but nothing on the teacher's side reads the **practice** log yet. `activity_event` already holds every exam answer, mark and feedback row with its `session_id` too - a timeline surface is what is missing | Phase 9 monitoring/activity |
| An essay in a **practice run** is filed and never marked: the grading queue reads exam answers only (`manual_review.answer_id` points at an `attempt_answer`), which is why the practice screen says "Not marked automatically" rather than promising a teacher | Phase 9 or 10, once a practice answer has a review row to grow |
| A learner's own language choice is device-local (`localStorage.ui_lang`). The profile's `ui_language` is only a starting point now - a teacher sets it, and there is no endpoint for a learner to write it back for themselves | Phase 13 (self-service profile) |
| A failed import says **why** in coarse terms on screen: every reason the parsers give (`That file contains no text.`, `That Word document has no body to read.`) is stored on the job's `error` and in the audit row, but the screen shows the one locale sentence for `document_unreadable`, because the reason is a sentence and not a code. A teacher with an empty file therefore reads "This document could not be read" rather than "there was nothing in it" - true, but blunter than what the parser knows. Proven live on a 6-byte empty `.txt`: the job's `error` is `That file contains no text.` while `progress.outcome` is `document_unreadable` | Phase 13, with the rest of the code-and-locale sweep: a small set of failure codes (`document_has_no_text`, `document_damaged`, `document_needs_ocr`) advertised by `/imports/meta` like `note_codes` already is |
| An import cannot produce a **listening** item. `KINDS` is `question / vocabulary / reading / note` and `APPROVABLE_KINDS` the first three, each with exactly one target in the bank; a document carries no sound, so naming `listening` on a card is refused (`kind_invalid`) rather than filed as a text with an empty player | Phase 12 (the speech adapter, with a real asset to attach) |
| `listening.seconds` on the media column and the duration probe still read as bare numbers in some teacher tables (`A2 · 0 s`), because `span()` is applied where a sitting is described, not everywhere a file is | Phase 12 probe + Phase 13 sweep |

Closed by Phase 8: a paper is now *read* rather than typed in - seven formats named from their own
bytes, their text parsed by this repository's own code with no Office, no Acrobat and no network
call, and their content reviewed row by row before any of it becomes a bank entry. The
provenance a teacher relies on (`extracted` untouched, `corrected` as theirs, `source_page` and
`source_sheet` as the document's own place) is stored, not inferred, and a duplicate upload answers
with the queue that exists instead of opening a second one.

Closed by Phase 7: the exam server is the only clock (`remaining_seconds` recomputed from
`expires_at`, the worker's sweep for papers nobody came back for, one `finalise` path for every
closing), and an answer a teacher has not marked yet is counted as ungraded rather than as zero.
The standalone `/student/reading` and `/student/listening` screens are still read-only
projections, because an ad-hoc answer needs an attempt to belong to and an attempt belongs to a
paper.

Closed by Phase 5: a word's pronunciation can now be recorded once in the media library
and attached to the word - `audio_asset_id` round-trips, and the study card plays it
through `/student/media/{id}/content`.

Phase 4 reminder: the integration `conftest.py` no longer hardcodes the head revision -
it compares `alembic_version` with the head the scripts define and insists the history
stays linear, so a new `0002_*` revision is verified without touching the test. The
offline migration-immutability check still pins `0001_bootstrap` byte for byte.
