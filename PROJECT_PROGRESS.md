# PROJECT PROGRESS

Continuation state file. Reread this after any context loss and keep going - do not
re-plan work that is already recorded as done here.

- Current phase: **Phase 3 - question bank + question engine** (Phase 1-2 accepted)
- Git: branch `main`, remote target `https://github.com/f1r10/ingilis-learning`
- Last commit: `e9ed2af Initial project upload`, then the Phase 1-2 acceptance commit below

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
| 3 | Question bank + full question engine (all types, `QuestionVersion` immutability) | **in progress** |
| 4 | Vocabulary module | not started |
| 5 | Reading + listening + media libraries (object storage) | not started |
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

## 4. Working commands

```bash
# mandatory acceptance (WSL / Linux / macOS / Windows; needs Docker, no make)
bash scripts/verify_phase12.sh                 # add --keep to inspect the run afterwards

# offline developer tests (never an acceptance result)
cd backend && pytest tests --ignore=tests/integration
cd backend && ruff check app tests migrations scripts

# dev stack
cp .env.example .env && docker compose up --build
docker compose up -d postgres redis minio
```

Windows venv (offline work only): `backend/.venv/Scripts/python.exe`.
From Git Bash on Windows the repo path is `C:/Users/firon/Documents/Qoder/2026-10-06/1438da7f`;
bash state does not persist between commands, so `cd` in every command.
WSL path for the same repo: `/mnt/c/Users/firon/Documents/Qoder/2026-10-06/1438da7f`
(run with `wsl -e bash -lc '...'`).

## 5. Environment facts worth not re-discovering

- WSL Ubuntu: Docker 29.1.3 + Compose 2.40.3 working, internet reachable,
  `python3` = 3.14.4 with **no pip/ensurepip** (venv needs the pip bootstrap), no
  `make` on the Windows PATH.
- Invoking WSL from Git Bash: `wsl.exe -- bash -lc '...'` (a bare `wsl -lc` is rejected);
  never hand `/mnt/...` paths to `wsl.exe` as arguments (MSYS rewrites them) and never
  measure an exit status through nested double quotes - `bash -c "exit 3"` arrives as
  `bash -c exit 3`, and the status you read is the wrapper's. bash 5.3.9 itself is fine.
  Run a script file and have the script write `$?` to a file.
- No MinIO container in the dev stack; the acceptance MinIO comes from the pinned
  Chainguard community image.
- The developer stack (`language-learning-platform` project) may already be running
  on 5432/6379 - the verifier must keep using its own project and auto-chosen ports.

## 6. Known defects / blockers

None open. Phase 1-2 is accepted; the work now is Phase 3.
