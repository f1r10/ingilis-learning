# PROJECT PROGRESS

Continuation state file. Reread this after any context loss and keep going - do not
re-plan work that is already recorded as done here.

- Current phase: **Phase 7 - exams, assignments and the attempt engine** (Phases 1-6 accepted)
- Git: branch `main`, remote `https://github.com/f1r10/ingilis-learning.git`
- Last commits: Phase 1-2 `2ab3294`, Phase 3 `c650bf5`, Phase 4 `20e5006`, Phase 5 `53911f2`,
  Phase 6 `feat(phase-6): complete catalogs and practice` - all pushed. `git rev-parse HEAD`
  is the authoritative tip; each phase commit rewrites this line with its own SHA

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
| 7 | Exams + assignments + attempt engine + grading | not started - **next** |
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
  12 probe), physical object GC (Phase 11), server-side replay and exam timers (Phase 7),
  English server *reason* strings inside a Turkish/Russian/Azerbaijani interface and the
  `div`-based login tabs (Phase 13).

## 8. Working commands

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

## 9. Environment facts worth not re-discovering

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

## 10. Known defects / blockers

None open. Phases 1-2 through 6 are accepted; the work now is Phase 7 (exams, assignments
and the attempt engine).

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
| The learner player's replay counter is counted in the tab (`StudentListening.tsx`), so a refresh starts the count again. The rule itself is the server's and is delivered with the payload; counting it durably needs an attempt, which does not exist yet | Phase 7 attempt engine |
| Sentences the server refuses with are shown in English inside a Turkish, Russian or Azerbaijani interface. Many distinct refusals share the single `validation_failed` code, so the client cannot look the wording up; the interface copy itself is complete in all four locales and this is only the server's prose | Phase 13, with stable reason keys per refusal |
| Labels the server supplies - question types from `/questions/types`, vocabulary parts of speech and language names - arrive in English and are printed as they came. They are data, not interface chrome, so they need the same reason-key treatment rather than a client-side guess | Phase 13 |
| A learner can answer a question only inside a practice run: `LearnerPreview` is still the projection the standalone `/student/reading` and `/student/listening` screens use (every control `readOnly`), because a run's log is keyed to a catalog and an ad-hoc answer needs an attempt to belong to | Phase 7 attempt engine |
| Nothing on the teacher's side reads the practice log yet. `activity_event` already holds every answer, mark and favorite with its `session_id`, and the learner's own summary is rebuilt from it - but a teacher has no timeline surface to open | Phase 9 monitoring/activity |
| An essay or a written answer in practice is filed and never marked: there is no grading queue behind it, which is why the learner's screen says "Not marked automatically" rather than promising a teacher | Phase 7 (manual grading on attempts) |

Closed by Phase 5: a word's pronunciation can now be recorded once in the media library
and attached to the word - `audio_asset_id` round-trips, and the study card plays it
through `/student/media/{id}/content`.

Phase 4 reminder: the integration `conftest.py` no longer hardcodes the head revision -
it compares `alembic_version` with the head the scripts define and insists the history
stays linear, so a new `0002_*` revision is verified without touching the test. The
offline migration-immutability check still pins `0001_bootstrap` byte for byte.
