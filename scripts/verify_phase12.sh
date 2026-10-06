#!/usr/bin/env bash
# ==========================================================================
# verify_phase12.sh - the MANDATORY Phase 1-2 acceptance check.
#
#   ./scripts/verify_phase12.sh          (or:  make verify-phase12)
#
# This is NOT the developer test loop. `cd backend && pytest tests` stays fast
# and is allowed to skip anything that needs live infrastructure. This script is
# the gate that proves Phase 1-2 actually works against real PostgreSQL, Redis and
# MinIO: it brings the services up, waits for real health, builds an isolated
# verification database, runs the committed Alembic migration, seeds twice, runs
# the whole backend suite, exercises object storage and the ARQ worker.
#
# Any mandatory check that cannot run is reported as a FAILURE - a skip is never
# silently accepted as success. Exits non-zero if any mandatory step failed.
#
# Options:
#   --down    stop the services afterwards (default: leave them up for inspection)
#   --keep    keep the verification database instead of dropping it
#
# Requirements: Docker with a running daemon, and the backend virtualenv
# (.venv) with `pip install -e .[dev]` already applied.
# ==========================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
COMPOSE_FILE="$ROOT/docker-compose.yml"
REPORT_DIR="$BACKEND/.verify-phase12"

# --------------------------------------------------------------------------- #
# Settings, resolved exactly the way docker-compose.yml resolves them, so the
# script connects with the same credentials the stack itself uses. Secrets are
# only ever read here - never printed, never written to a file.
# --------------------------------------------------------------------------- #
LOAD_ENV=1
STOP_DOWN=0
KEEP_DB=0
for arg in "$@"; do
  case "$arg" in
    --down) STOP_DOWN=1 ;;
    --keep) KEEP_DB=1 ;;
    --no-env) LOAD_ENV=0 ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$LOAD_ENV" -eq 1 ] && [ -f "$ROOT/.env" ]; then
  # `tr -d '\r'` because a .env edited on Windows carries CRLF line endings.
  set -a
  # shellcheck disable=SC1090
  source <(tr -d '\r' < "$ROOT/.env")
  set +a
fi

PG_USER="${POSTGRES_USER:-app}"
PG_DB="${POSTGRES_DB:-app}"
PG_PASSWORD="${POSTGRES_PASSWORD:-change-me-strong-password}"
PG_HOST="${VERIFY_PG_HOST:-127.0.0.1}"
PG_PORT="${POSTGRES_PORT:-5432}"
REDIS_HOST="${VERIFY_REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_DB="${VERIFY_REDIS_DB:-1}"
MINIO_HOST="${VERIFY_MINIO_HOST:-127.0.0.1}"
MINIO_API_PORT="${MINIO_API_PORT:-9000}"
MINIO_ACCESS_KEY="${MINIO_ROOT_USER:-minioadmin}"
MINIO_SECRET_KEY="${MINIO_ROOT_PASSWORD:-minioadmin}"

# Dedicated, disposable targets: the verification run never touches the app
# database or the real media bucket.
VERIFY_DB="${VERIFY_TEST_DB:-app_verify_phase12}"
VERIFY_BUCKET="${VERIFY_OBJECT_STORAGE_BUCKET:-platform-media-verify}"

HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
WORKER_TIMEOUT="${WORKER_TIMEOUT:-60}"

# --------------------------------------------------------------------------- #
# Step machinery: collect failures, never abort mid-flight, report at the end.
# --------------------------------------------------------------------------- #
PASSED_STEPS=()
FAILED_STEPS=()
NOTES=()

say() { printf '%s\n' "$*"; }
hdr() { printf '\n\033[1m── Step %s: %s\033[0m\n' "$1" "$2"; }
ok() { printf '   \033[32mok\033[0m %s\n' "$*"; }
bad() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; }
info() { printf '   .. %s\n' "$*"; }

step() {
  local number="$1" name="$2"
  shift 2
  hdr "$number" "$name"
  if "$@"; then
    ok "$name"
    PASSED_STEPS+=("$number")
  else
    bad "$name"
    FAILED_STEPS+=("step $number - $name")
  fi
}

COMPOSE=""
PY=""

# --------------------------------------------------------------------------- #
# 1. Docker
# --------------------------------------------------------------------------- #
check_docker() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    bad "no Docker Compose found (tried 'docker compose' and 'docker-compose')"
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    bad "the Docker daemon is not reachable - start Docker Desktop (or the daemon) and re-run"
    return 1
  fi
  if ! command -v git >/dev/null 2>&1 && [ ! -d "$BACKEND/.venv" ] && [ ! -d "$BACKEND/venv" ]; then
    info "note: no backend virtualenv detected; the next step will fail if none is found"
  fi
  info "using: $COMPOSE"

  if [ ! -f "$BACKEND/pyproject.toml" ]; then
    bad "backend not found at $BACKEND"
    return 1
  fi
  for candidate in "$BACKEND/.venv/Scripts/python.exe" "$BACKEND/.venv/bin/python" \
                   "$BACKEND/venv/Scripts/python.exe" "$BACKEND/venv/bin/python"; do
    if [ -x "$candidate" ]; then PY="$candidate"; break; fi
  done
  if [ -z "$PY" ]; then
    bad "no backend virtualenv found - create it and run: pip install -e .[dev]"
    return 1
  fi
  info "python: $PY"
  if ! "$PY" -c "import pytest, sqlalchemy, asyncpg, alembic, boto3, redis" 2>/dev/null; then
    bad "the virtualenv is missing test dependencies (install: pip install -e .[dev])"
    return 1
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# 2-4. Services
# --------------------------------------------------------------------------- #
start_service() {
  local service="$1"
  if ! $COMPOSE -f "$COMPOSE_FILE" up -d "$service"; then
    bad "could not start $service"
    return 1
  fi
  info "$service requested"
  return 0
}

# --------------------------------------------------------------------------- #
# 5. Real health, not "container exists"
# --------------------------------------------------------------------------- #
wait_healthy() {
  local service="$1" cid status waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    cid="$($COMPOSE -f "$COMPOSE_FILE" ps -q "$service" 2>/dev/null | head -1)"
    if [ -n "$cid" ]; then
      status="$(docker inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null)"
      case "$status" in
        healthy) info "$service reports healthy"; return 0 ;;
        starting|"") : ;;
        exited|dead)
          bad "$service container is $status - recent logs:"
          docker logs --tail 20 "$cid" 2>&1 | sed 's/^/       /'
          return 1 ;;
        *) bad "$service container state is '$status'"; return 1 ;;
      esac
    fi
    sleep 2
    waited=$((waited + 2))
  done
  bad "$service did not become healthy within ${HEALTH_TIMEOUT}s"
  return 1
}

wait_for_services() {
  local s
  for s in postgres redis minio; do
    wait_healthy "$s" || return 1
  done
  # Independent proof, from this side of the wire, that each service answers.
  if ! $COMPOSE -f "$COMPOSE_FILE" exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    bad "pg_isready refused connections"
    return 1
  fi
  if ! "$PY" - "$REDIS_HOST" "$REDIS_PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.create_connection((host, port), timeout=5):
    pass
PYEOF
  then
    bad "Redis port ${REDIS_HOST}:${REDIS_PORT} is not reachable from this machine"
    return 1
  fi
  if ! "$PY" - "$MINIO_HOST" "$MINIO_API_PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.create_connection((host, port), timeout=5):
    pass
PYEOF
  then
    bad "MinIO port ${MINIO_HOST}:${MINIO_API_PORT} is not reachable from this machine"
    return 1
  fi
  info "Postgres, Redis and MinIO are all answering"
  return 0
}

# --------------------------------------------------------------------------- #
# Environment used by every backend command below
# --------------------------------------------------------------------------- #
export_app_env() {
  # Throwaway secrets for this run only. They are generated rather than taken
  # from the developer's .env so that (a) no real secret is ever used here and
  # (b) nothing about the result depends on local config. Never printed.
  local secrets
  secrets="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(24))')"
  read -r verify_session_secret verify_csrf_secret verify_admin_pw <<<"$secrets"

  export APP_ENV=development
  export DATABASE_URL="postgresql+asyncpg://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}/${VERIFY_DB}"
  export REDIS_URL="redis://${REDIS_HOST}:${REDIS_PORT}/${REDIS_DB}"
  export OBJECT_STORAGE_ENDPOINT="http://${MINIO_HOST}:${MINIO_API_PORT}"
  export OBJECT_STORAGE_ACCESS_KEY="$MINIO_ACCESS_KEY"
  export OBJECT_STORAGE_SECRET_KEY="$MINIO_SECRET_KEY"
  export OBJECT_STORAGE_BUCKET="$VERIFY_BUCKET"
  export OBJECT_STORAGE_SECURE=false
  export SESSION_SECRET="$verify_session_secret"
  export CSRF_SECRET="$verify_csrf_secret"
  export BOOTSTRAP_ADMIN_USERNAME=admin
  export BOOTSTRAP_ADMIN_PASSWORD="$verify_admin_pw"
  export AI_PROVIDER=none OCR_PROVIDER=none STT_PROVIDER=none TRANSLATION_PROVIDER=none DICTIONARY_PROVIDER=none
  export COOKIE_SECURE=false
  info "pointed at database '$VERIFY_DB', Redis db $REDIS_DB, bucket '$VERIFY_BUCKET' (isolated from dev data)"
  return 0
}

# --------------------------------------------------------------------------- #
# 6. Isolated verification database, created safely
# --------------------------------------------------------------------------- #
reset_test_database() {
  case "$VERIFY_DB" in
    "$PG_DB")
      bad "refusing to reset '$VERIFY_DB' - it is the application database (set VERIFY_TEST_DB)"
      return 1 ;;
    *_verify_phase12|*_test|test_*) : ;;
    *)
      bad "refusing to reset '$VERIFY_DB' - the verification database must be named *_test, test_* or *_verify_phase12"
      return 1 ;;
  esac

  psql_admin -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${VERIFY_DB}' AND pid <> pg_backend_pid()" \
    -c "DROP DATABASE IF EXISTS \"${VERIFY_DB}\" WITH (FORCE)" \
    -c "CREATE DATABASE \"${VERIFY_DB}\" OWNER ${PG_USER} TEMPLATE template1" || return 1

  info "dropped and recreated '$VERIFY_DB' (empty schema, owned by $PG_USER)"
  # Prove it is really empty: nothing may exist in the public schema yet.
  local objects
  objects="$(psql_admin -At -c \
    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r','v','p')" "${VERIFY_DB}")"
  if [ "$objects" != "0" ]; then
    bad "the fresh verification database already contains $objects relation(s)"
    return 1
  fi
  info "verified: the new database contains no relations before the migration runs"
  return 0
}

psql_admin() {
  $COMPOSE -f "$COMPOSE_FILE" exec -T postgres psql -U "$PG_USER" "$@"
}

# --------------------------------------------------------------------------- #
# 7. The committed migration builds the schema
# --------------------------------------------------------------------------- #
run_migrations() {
  ( cd "$BACKEND" && "$PY" -m alembic upgrade head ) || { bad "alembic upgrade head failed"; return 1; }
  ( cd "$BACKEND" && "$PY" -m alembic upgrade head ) || { bad "second 'alembic upgrade head' was not a clean no-op"; return 1; }
  info "upgrade head applied, and applying it again is a no-op"

  # A brand new process reading the same database proves the DDL survived the
  # process that created it (schema-level restart persistence).
  ( cd "$BACKEND" && "$PY" - <<'PYEOF'
import asyncio

from sqlalchemy import text

from app.core.database import engine


async def main() -> None:
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        tables = (
            await conn.execute(
                text("SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
            )
        ).scalar()
        fks = (
            await conn.execute(
                text("SELECT count(*) FROM information_schema.referential_constraints "
                     "WHERE constraint_schema = 'public'")
            )
        ).scalar()
        indexes = (
            await conn.execute(
                text("SELECT count(*) FROM pg_indexes WHERE schemaname = 'public'")
            )
        ).scalar()
    print(f"   .. read back: alembic_version={version} tables={tables} foreign_keys={fks} indexes={indexes}")
    assert version == "0001_bootstrap", f"unexpected alembic_version: {version!r}"
    assert tables >= 45, f"expected at least 45 tables, found {tables}"
    assert fks >= 50, f"expected at least 50 foreign keys, found {fks}"
    assert indexes >= 90, f"expected at least 90 indexes, found {indexes}"
    await engine.dispose()


asyncio.run(main())
PYEOF
  ) || { bad "the migrated schema could not be read back by a fresh process"; return 1; }
  return 0
}

# --------------------------------------------------------------------------- #
# 8. Seed, twice, and prove the second run changed nothing
# --------------------------------------------------------------------------- #
seed_rows() {
  ( cd "$BACKEND" && "$PY" -c '
import asyncio
from sqlalchemy import func, select
from app.core.database import SessionLocal, engine
from app.models.identity import AdminUser
from app.models.system import Language, SystemSetting

async def main():
    async with SessionLocal() as db:
        admins = (await db.execute(select(func.count()).select_from(AdminUser))).scalar()
        langs = (await db.execute(select(func.count()).select_from(Language))).scalar()
        settings_rows = (await db.execute(select(func.count()).select_from(SystemSetting))).scalar()
    print(f"admins={admins} languages={langs} settings={settings_rows}")
    await engine.dispose()

asyncio.run(main())
' )
}

run_seed_twice() {
  local first second
  ( cd "$BACKEND" && "$PY" -m scripts.seed ) || { bad "seed run #1 failed"; return 1; }
  first="$(seed_rows)" || { bad "could not count seeded rows"; return 1; }
  ( cd "$BACKEND" && "$PY" -m scripts.seed ) || { bad "seed run #2 failed"; return 1; }
  second="$(seed_rows)" || { bad "could not count seeded rows"; return 1; }
  info "after seed #1: $first"
  info "after seed #2: $second"
  if [ "$first" != "$second" ]; then
    bad "the seed is not idempotent: '$first' vs '$second'"
    return 1
  fi
  info "the seed is idempotent (two runs, identical row counts)"
  return 0
}

# --------------------------------------------------------------------------- #
# 9-10. The test suites. A skip in a mandatory check is a failure.
# --------------------------------------------------------------------------- #
run_pytest_suite() {
  local label="$1" report="$2"
  shift 2
  mkdir -p "$REPORT_DIR"
  ( cd "$BACKEND" && "$PY" -m pytest "$@" -p no:cacheprovider \
      --junitxml="$report" -q --tb=short -rA )
  local pytest_status=$?
  "$PY" "$BACKEND/scripts/_junit_gate.py" "$report" "$label"
  local gate_status=$?
  if [ "$gate_status" -ne 0 ]; then
    return 1
  fi
  if [ "$pytest_status" -ne 0 ]; then
    info "pytest itself exited $pytest_status while the report looked clean - see the output above"
    return 1
  fi
  return 0
}

run_unit_suite() {
  run_pytest_suite "unit/static" "$REPORT_DIR/junit-unit.xml" \
    tests --ignore=tests/integration || return 1
  return 0
}

run_integration_suite() {
  run_pytest_suite "integration (Postgres+Redis+MinIO)" "$REPORT_DIR/junit-integration.xml" \
    tests/integration -v || return 1
  return 0
}

exercise_minio_storage() {
  run_pytest_suite "MinIO storage round-trip" "$REPORT_DIR/junit-storage.xml" \
    tests/integration/test_storage_minio.py -v || return 1
  # Independent confirmation that the bucket the application uses really exists
  # now, seen through the same credentials the app holds.
  ( cd "$BACKEND" && "$PY" -c '
import boto3
from botocore.config import Config
from app.core.config import get_settings

s = get_settings()
client = boto3.client(
    "s3",
    endpoint_url=s.object_storage_endpoint,
    region_name=s.object_storage_region,
    aws_access_key_id=s.object_storage_access_key,
    aws_secret_access_key=s.object_storage_secret_key,
    use_ssl=s.object_storage_secure,
    config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=10),
)
buckets = {b["Name"] for b in client.list_buckets()["Buckets"]}
assert s.object_storage_bucket in buckets, f"bucket {s.object_storage_bucket!r} missing from {sorted(buckets)}"
print(f"bucket present: {s.object_storage_bucket}")
' ) || { bad "the verification bucket is not visible over S3"; return 1; }
  return 0
}

# --------------------------------------------------------------------------- #
# 11. ARQ worker
# --------------------------------------------------------------------------- #
check_worker() {
  local log="$REPORT_DIR/worker.log" waited=0
  mkdir -p "$REPORT_DIR"
  : > "$log"
  # AI/OCR/STT providers are all 'none' here on purpose: a disabled optional
  # adapter must not stop the worker from starting.
  ( cd "$BACKEND" && exec "$PY" -m arq app.workers.main.WorkerSettings ) >>"$log" 2>&1 &
  local pid=$!
  while [ "$waited" -lt "$WORKER_TIMEOUT" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      bad "the ARQ worker process exited during startup (log follows)"
      sed 's/^/       /' "$log" | tail -25
      return 1
    fi
    if grep -qiE "Traceback|CRITICAL|AddressError|ConnectionError" "$log"; then
      bad "the ARQ worker logged an error during startup (log follows)"
      sed 's/^/       /' "$log" | tail -25
      kill "$pid" 2>/dev/null
      return 1
    fi
    if [ -s "$log" ]; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if [ ! -s "$log" ]; then
    bad "the ARQ worker produced no log output within ${WORKER_TIMEOUT}s"
    kill "$pid" 2>/dev/null
    return 1
  fi
  # Give it a few seconds to prove it stays up rather than starting then dying.
  sleep 5
  if ! kill -0 "$pid" 2>/dev/null; then
    bad "the ARQ worker stopped after coming up (log follows)"
    sed 's/^/       /' "$log" | tail -25
    return 1
  fi
  info "worker running as pid $pid; startup log:"
  tail -5 "$log" | sed 's/^/       /'
  kill "$pid" 2>/dev/null
  local i=0
  while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 15 ]; do sleep 1; i=$((i + 1)); done
  kill -9 "$pid" 2>/dev/null
  info "worker stopped"
  return 0
}

# --------------------------------------------------------------------------- #
# Cleanup (best effort; never masks a failure)
# --------------------------------------------------------------------------- #
cleanup() {
  if [ "$KEEP_DB" -eq 0 ] && [ "${DB_WAS_CREATED:-0}" = "1" ]; then
    info "dropping the verification database (kept with --keep)"
    psql_admin -q -c "DROP DATABASE IF EXISTS \"${VERIFY_DB}\" WITH (FORCE)" >/dev/null 2>&1
  fi
  if [ "$STOP_DOWN" -eq 1 ]; then
    $COMPOSE -f "$COMPOSE_FILE" down >/dev/null 2>&1
    info "services stopped (--down)"
  else
    info "services left running; stop them with: $COMPOSE -f $COMPOSE_FILE down"
  fi
  info "reports: $REPORT_DIR"
}

# --------------------------------------------------------------------------- #
# Drive the steps
# --------------------------------------------------------------------------- #
say "=========================================================================="
say " Phase 1-2 mandatory acceptance check"
say "=========================================================================="
DB_WAS_CREATED=0

step 1 "Docker and the local toolchain" check_docker
if [ "${#FAILED_STEPS[@]}" -gt 0 ]; then
  say ""
  bad "cannot continue: Docker/python prerequisites failed. Failing the mandatory check."
  printf '  - %s\n' "${FAILED_STEPS[@]}"
  exit 1
fi

step 2 "Start PostgreSQL"            start_service postgres
step 3 "Start Redis"                 start_service redis
step 4 "Start MinIO"                 start_service minio
step 5 "Wait for real service health" wait_for_services

say ""
say "Configuring this run's targets (isolated database, Redis db, verify bucket, throwaway secrets)"
export_app_env

step 6 "Create/reset the isolated test database" reset_test_database && DB_WAS_CREATED=1
step 7 "alembic upgrade head (committed migration)" run_migrations
step 8 "Run the seed twice"          run_seed_twice
step 9 "Backend unit/static suite"   run_unit_suite
step 10 "Backend integration suite (no silent skips)" run_integration_suite
step 11 "Exercise MinIO object storage" exercise_minio_storage
step 12 "Start and check the ARQ worker" check_worker

cleanup

# --------------------------------------------------------------------------- #
# 13. Report + exit code
# --------------------------------------------------------------------------- #
say ""
say "=========================================================================="
say " Result"
say "=========================================================================="
say "passed steps: ${#PASSED_STEPS[@]} / 12"
if [ "${#FAILED_STEPS[@]}" -gt 0 ]; then
  say ""
  say "MANDATORY CHECKS THAT FAILED:"
  printf '  - %s\n' "${FAILED_STEPS[@]}"
  say ""
  say "Phase 1-2 is NOT accepted. Fix the items above and re-run: make verify-phase12"
  exit 1
fi
say ""
say "All mandatory Phase 1-2 checks passed against live PostgreSQL, Redis and MinIO."
exit 0
