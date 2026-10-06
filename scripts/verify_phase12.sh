#!/usr/bin/env bash
# ==========================================================================
# verify_phase12.sh - the MANDATORY Phase 1-2 acceptance check.
#
#   bash scripts/verify_phase12.sh          (or:  make verify-phase12)
#
# This is NOT the developer test loop. `cd backend && pytest tests` stays fast
# and is allowed to skip anything that needs live infrastructure. This script is
# the gate that proves Phase 1-2 actually works against real PostgreSQL, Redis
# and MinIO: it brings an isolated stack up, waits for real health, creates an
# isolated verification database, runs the committed Alembic migration, seeds
# twice, runs every backend suite, exercises object storage and the ARQ worker.
#
# Self-contained on purpose: it needs NO `.env` and NO pre-existing password.
# All credentials for the run are generated fresh into a throwaway, gitignored
# file (backend/.verify-phase12/verify.env, mode 600) and are deleted with the
# run. Nothing that looks like a secret is ever printed.
#
# Isolated on purpose: it runs its own Docker Compose project with its own
# volumes and its own host ports, so it never touches - and is never poisoned by
# - the development stack or its database volumes.
#
# Cross-platform on purpose: on Linux/WSL it uses a native Linux interpreter
# (creating backend/.verify-venv-wsl, or a WSL-native venv when the repository
# lives on /mnt/c). A Windows `.venv/Scripts/python.exe` is never handed WSL
# paths, and it is never selected when this script runs under WSL.
#
# Any mandatory check that cannot run is a FAILURE - a skip is never accepted as
# success. A service that will not start is reported as an infrastructure
# failure, and the checks that depended on it are reported as blocked instead of
# quietly passing. Exits non-zero unless everything mandatory passed.
#
# Usage:
#   bash scripts/verify_phase12.sh [options]
#
# Options:
#   --keep          leave the stack, its volumes and the generated env file up
#                   for debugging (default: tear down only verifier-owned
#                   resources, on success as well as on failure)
#   --down          explicit teardown (this is already the default)
#   --venv PATH     use a specific virtualenv as the verification interpreter
#
# Tunables (environment):
#   VERIFY_COMPOSE_PROJECT   default: llp_phase12_verify (must contain 'verify')
#   VERIFY_TEST_DB           default: app_verify_phase12
#   VERIFY_OBJECT_STORAGE_BUCKET  default: platform-media-verify
#   MINIO_IMAGE              default: the pinned image in docker-compose.yml
#   POSTGRES_PORT / REDIS_PORT / MINIO_API_PORT / MINIO_CONSOLE_PORT
#                            force fixed host ports (default: free ports chosen
#                            per run so a running dev stack cannot collide)
#   PYTHON3_BIN              force the python used to build the venv
#   HEALTH_TIMEOUT / WORKER_TIMEOUT  seconds (defaults 180 / 90)
#
# Requirements: a reachable Docker daemon (Docker Desktop from WSL is fine) and a
# Python 3.11 or newer (any 3.11+ that can build a venv; pip is bootstrapped into
# the virtualenv if the distro's interpreter has no ensurepip). The first run
# builds the venv and installs the backend with its dev extras.
# ==========================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
COMPOSE_FILE="$ROOT/docker-compose.yml"
REPORT_DIR="$BACKEND/.verify-phase12"
REPORT_REL=".verify-phase12"          # the same directory, relative to $BACKEND
VERIFY_ENV="$REPORT_DIR/verify.env"
GATE_REL="scripts/_junit_gate.py"     # relative to $BACKEND, so the path is usable on every platform

NUM_STEPS=13

# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
KEEP=0
VENV_OVERRIDE=""
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    --down) KEEP=0 ;;
    --venv) echo "--venv needs a path (use --venv=/some/path)" >&2; exit 2 ;;
    --venv=*) VENV_OVERRIDE="${arg#--venv=}" ;;
    -h|--help) awk 'NR>1{ if ($0 ~ /^#/) { sub(/^# ?/, ""); print } else exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

# Isolated, disposable targets. The verification run never touches the app
# database, the real media bucket or the development compose project.
COMPOSE_PROJECT="${VERIFY_COMPOSE_PROJECT:-llp_phase12_verify}"
VERIFY_DB="${VERIFY_TEST_DB:-app_verify_phase12}"
VERIFY_BUCKET="${VERIFY_OBJECT_STORAGE_BUCKET:-platform-media-verify}"
MINIO_IMAGE_DEFAULT="${MINIO_IMAGE:-cgr.dev/chainguard/minio@sha256:a05a4497e8dce3cb7a7a1bf1872ba5d30ea988f1e8c22c9e0920503761c4b5f1}"
PG_USER_WANT="${VERIFY_PG_USER:-app}"
PG_DB_WANT="${VERIFY_PG_DB:-app}"
REDIS_DB="${VERIFY_REDIS_DB:-1}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
WORKER_TIMEOUT="${WORKER_TIMEOUT:-90}"

# --------------------------------------------------------------------------- #
# Reporting machinery: classify failures, never abort mid-flight, report once.
# --------------------------------------------------------------------------- #
PASSED_STEPS=()
INFRA_FAILURES=()
TEST_FAILURES=()
BLOCKED_STEPS=()
SUITE_SUMMARY=()

# An infrastructure flag of 0 means "this service could not be proven to work",
# which blocks exactly the mandatory checks that need it.
TOOL_OK=1
HEALTH_OK=1
PG_OK=1
REDIS_OK=1
MINIO_OK=1

COMPOSE=""
PY=""
VENV=""
RUNNER=""
ON_WIN_MOUNT=0

say() { printf '%s\n' "$*"; }
hdr() { printf '\n\033[1m── Step %s: %s\033[0m\n' "$1" "$2"; }
ok() { printf '   \033[32mok\033[0m %s\n' "$*"; }
bad() { printf '   \033[31mFAIL\033[0m %s\n' "$*"; }
warn() { printf '   \033[33m--\033[0m %s\n' "$*"; }
info() { printf '   .. %s\n' "$*"; }

# Infrastructure steps: a failure here is an infrastructure failure, not a test
# result, and it blocks the mandatory checks that depend on it.
infra_step() {
  local number="$1" name="$2" flag="$3"
  shift 3
  hdr "$number" "$name"
  if "$@"; then
    ok "$name"
    PASSED_STEPS+=("$number")
  else
    bad "$name"
    printf -v "$flag" '%s' 0
    INFRA_FAILURES+=("step $number - $name")
  fi
}

# Mandatory work steps: run only when everything they need is actually up.
work_step() {
  local number="$1" name="$2" needs="$3"
  shift 3
  hdr "$number" "$name"
  local missing="" flag
  for flag in $needs; do
    case "$flag" in
      tool) [ "$TOOL_OK" -eq 0 ] && missing="$missing toolchain" ;;
      pg) [ "$PG_OK" -eq 0 ] && missing="$missing PostgreSQL" ;;
      redis) [ "$REDIS_OK" -eq 0 ] && missing="$missing Redis" ;;
      minio) [ "$MINIO_OK" -eq 0 ] && missing="$missing MinIO" ;;
    esac
  done
  if [ -n "$missing" ]; then
    local reason="not run - required infrastructure is unavailable:${missing}"
    printf '   \033[33mBLOCKED\033[0m %s\n' "$reason"
    BLOCKED_STEPS+=("step $number - $name (blocked by:${missing})")
    return 0
  fi
  if "$@"; then
    ok "$name"
    PASSED_STEPS+=("$number")
  else
    bad "$name"
    TEST_FAILURES+=("step $number - $name")
  fi
}

# Compose is always invoked through this: own project, own env file, own file.
dc() {
  $COMPOSE -p "$COMPOSE_PROJECT" --env-file "$VERIFY_ENV" -f "$COMPOSE_FILE" "$@"
}

indented() { sed 's/^/       /'; }

# --------------------------------------------------------------------------- #
# 1. Platform, interpreter and Docker CLI
# --------------------------------------------------------------------------- #
detect_platform() {
  local uname_s
  uname_s="$(uname -s 2>/dev/null || echo unknown)"
  case "$uname_s" in
    Linux*)
      if grep -qi 'microsoft' /proc/version 2>/dev/null; then RUNNER="wsl"; else RUNNER="linux"; fi ;;
    Darwin*) RUNNER="macos" ;;
    MINGW*|MSYS*|CYGWIN*) RUNNER="windows" ;;
    *) RUNNER="unknown" ;;
  esac
  ON_WIN_MOUNT=0
  case "$ROOT" in
    /mnt/[A-Za-z]/*) ON_WIN_MOUNT=1 ;;
  esac
}

venv_python_path() {
  # $1 = virtualenv directory -> prints the interpreter inside it
  if [ "$RUNNER" = "windows" ]; then printf '%s' "$1/Scripts/python.exe"; else printf '%s' "$1/bin/python"; fi
}

choose_venv() {
  if [ -n "$VENV_OVERRIDE" ]; then
    VENV="$VENV_OVERRIDE"
    info "verification virtualenv: $VENV (--venv)"
    return 0
  fi
  if [ -n "${VERIFY_VENV:-}" ]; then
    VENV="$VERIFY_VENV"
    info "verification virtualenv: $VENV (VERIFY_VENV)"
    return 0
  fi
  if [ "$RUNNER" = "windows" ]; then
    # This script itself runs on Windows: a Windows interpreter is the right one.
    VENV="$BACKEND/.venv"
  elif [ "$ON_WIN_MOUNT" -eq 1 ]; then
    # WSL with the repository on a Windows filesystem. A Linux venv must not live
    # there: drvfs cannot reliably hold its bin/python symlink and pip is slow.
    VENV="${VERIFY_VENV_HOME:-$HOME/.local/share/verify-phase12/venv}"
  else
    VENV="$BACKEND/.verify-venv-wsl"
  fi
}

create_venv() {
  local base="" candidate
  # The backend supports any Python >= 3.11 (pyproject `requires-python`), so the
  # newest interpreter wins and older ones are only fallbacks: a WSL distro whose
  # only python3 is 3.14 must still be able to run the gate.
  for candidate in ${PYTHON3_BIN:-} python3.14 python3.13 python3.12 python3.11 python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || continue
    base="$candidate"
    break
  done
  if [ -z "$base" ]; then
    bad "no Python 3.11 or newer found to build the verification virtualenv"
    info "on WSL/Ubuntu: sudo apt-get install -y python3 python3-venv python3-pip"
    return 1
  fi
  info "building a native verification virtualenv with $base ($("$base" -V 2>&1))"
  info "location: $VENV"
  [ "$ON_WIN_MOUNT" -eq 1 ] && info "(kept outside /mnt/c: a Linux venv needs symlinks, which a Windows mount cannot provide reliably)"
  mkdir -p "$(dirname "$VENV")" || { bad "cannot create $(dirname "$VENV")"; return 1; }

  local venv_err
  if ! venv_err="$("$base" -m venv --clear "$VENV" 2>&1)"; then
    # Debian and Ubuntu moved `ensurepip` into a separate python3-venv package, and
    # `python -m venv` fails once it is missing. The virtualenv itself does not need
    # it: create it without pip and bootstrap pip below, so the gate still runs on a
    # distro that only ships the base interpreter.
    if ! venv_err="$("$base" -m venv --clear --without-pip "$VENV" 2>&1)"; then
      bad "python -m venv '$VENV' failed"
      printf '%s\n' "$venv_err" | indented
      info "or install the missing packages: sudo apt-get install -y python3-venv python3-pip"
      return 1
    fi
  fi
  if ! "$(venv_python_path "$VENV")" -m pip --version >/dev/null 2>&1; then
    bootstrap_pip_in_venv || return 1
  fi
  return 0
}

# Installs pip into a virtualenv whose interpreter has no ensurepip module. The
# script keeps exactly one Python for every invocation, so this runs instead of
# falling back to some other interpreter that could not read these paths.
bootstrap_pip_in_venv() {
  local py err
  py="$(venv_python_path "$VENV")"
  warn "this interpreter has no 'ensurepip' (Ubuntu/Debian split it into the python3-venv package): bootstrapping pip"
  if ! err="$("$py" - <<'PYEOF' 2>&1
import runpy
import sys
import urllib.request

# Same official bootstrap installer pip's own documentation points at.
url = "https://bootstrap.pypa.io/get-pip.py"
target, _headers = urllib.request.urlretrieve(url)
sys.argv = ["get-pip.py", "-q"]
runpy.run_path(target, run_name="__main__")
PYEOF
  )"; then
    bad "could not bootstrap pip into $VENV"
    printf '%s\n' "$err" | tail -5 | indented
    info "offline alternative: sudo apt-get install -y python3-venv python3-pip, then re-run"
    return 1
  fi
  "$py" -m pip --version >/dev/null 2>&1 || {
    bad "pip was bootstrapped but '$py -m pip' still does not work"
    return 1
  }
  info "pip bootstrapped: $("$py" -m pip --version)"
  return 0
}

install_backend_into_venv() {
  info "installing the backend and its dev extras into $VENV (first run only)"
  ( cd "$BACKEND" && "$PY" -m pip install --disable-pip-version-check -q --upgrade pip setuptools wheel ) || {
    bad "could not upgrade pip in $VENV"
    return 1
  }
  ( cd "$BACKEND" && "$PY" -m pip install --disable-pip-version-check -q -e ".[dev]" ) || {
    bad "pip install -e .[dev] failed in $VENV"
    info "check network access to PyPI, and that $("$PY" -V 2>&1) is 3.11 or newer"
    return 1
  }
  return 0
}

check_toolchain() {
  detect_platform
  info "this script is running as: ${RUNNER} ($(uname -sr 2>/dev/null))"
  info "repository: $ROOT"

  if [ "$RUNNER" = "unknown" ]; then
    bad "unsupported platform '$(uname -s)' - run this on Linux/WSL, macOS or Windows"
    return 1
  fi

  if ! command -v docker >/dev/null 2>&1; then
    bad "the 'docker' CLI is not on PATH for this shell"
    [ "$RUNNER" = "wsl" ] && info "with Docker Desktop: Settings > Resources > WSL integration > enable this distro"
    return 1
  fi
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
  info "using: $COMPOSE, daemon reachable"

  if [ ! -f "$BACKEND/pyproject.toml" ]; then
    bad "backend not found at $BACKEND"
    return 1
  fi

  choose_venv
  if [ "$RUNNER" != "windows" ] && [ -x "$BACKEND/.venv/Scripts/python.exe" ]; then
    info "ignoring backend/.venv/Scripts/python.exe: that is the Windows interpreter and it cannot read $ROOT paths"
  fi
  PY="$(venv_python_path "$VENV")"
  if [ ! -x "$PY" ]; then
    create_venv || return 1
    PY="$(venv_python_path "$VENV")"
    [ -x "$PY" ] || { bad "the virtualenv was created but $PY is missing"; return 1; }
    install_backend_into_venv || return 1
  fi

  # Proof that the interpreter matches the shell running this script. This is the
  # exact defect the WSL pass hit: a Windows python handed /mnt/c/... paths.
  local host_is_windows guest_is_windows
  host_is_windows=0; [ "$RUNNER" = "windows" ] && host_is_windows=1
  guest_is_windows="$("$PY" -c 'import sys;print(1 if sys.platform=="win32" else 0)' 2>/dev/null)"
  if [ -z "$guest_is_windows" ]; then
    bad "$PY is not a working interpreter - delete $VENV and re-run"
    return 1
  fi
  if [ "$host_is_windows" -ne "$guest_is_windows" ]; then
    bad "platform mismatch: this shell is $RUNNER but '$PY' is a $([ "$guest_is_windows" = 1 ] && echo Windows || echo Linux) interpreter"
    info "delete '$VENV' and re-run, or point --venv at a $RUNNER virtualenv"
    return 1
  fi
  info "interpreter: $PY ($("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')) - native to $RUNNER"

  if ! "$PY" -c 'import pytest, pytest_asyncio, sqlalchemy, asyncpg, alembic, boto3, redis, arq, httpx, argon2' 2>/dev/null; then
    warn "the virtualenv is missing backend/test dependencies; installing them now"
    install_backend_into_venv || return 1
    "$PY" -c 'import pytest, pytest_asyncio, sqlalchemy, asyncpg, alembic, boto3, redis, arq, httpx, argon2' || {
      bad "the verification virtualenv still cannot import the backend's dependencies"
      return 1
    }
  fi
  if [ ! -f "$BACKEND/$GATE_REL" ]; then
    bad "missing $BACKEND/$GATE_REL (the JUnit no-skip gate)"
    return 1
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# 2. Generate this run's throwaway environment (one source of truth)
# --------------------------------------------------------------------------- #
generate_verifier_environment() {
  # Prove the isolation before any credential is written: a run that could tear
  # down the development project must fail here, not after it has state on disk.
  case "$COMPOSE_PROJECT" in
    *verify*) : ;;
    *)
      bad "refusing to use compose project '$COMPOSE_PROJECT': a verification project name must contain 'verify'"
      return 1 ;;
  esac
  local dev_project
  dev_project="$(grep -m1 '^name:' "$COMPOSE_FILE" | sed 's/^name:[[:space:]]*//' | tr -d '"')"
  if [ "$COMPOSE_PROJECT" = "$dev_project" ]; then
    bad "refusing to run verification inside the development compose project '$dev_project'"
    return 1
  fi
  info "isolated compose project: $COMPOSE_PROJECT (dev project: ${dev_project:-<none>})"

  if [ -f "$ROOT/.env" ]; then
    info "a developer .env exists but is deliberately NOT read: this gate must be self-contained"
  fi
  mkdir -p "$REPORT_DIR" || { bad "cannot create $REPORT_DIR"; return 1; }
  umask 077
  : > "$VERIFY_ENV" || { bad "cannot write $VERIFY_ENV"; return 1; }

  local gen_out status
  gen_out="$(
    VERIFY_DB="$VERIFY_DB" VERIFY_BUCKET="$VERIFY_BUCKET" MINIO_IMAGE="$MINIO_IMAGE_DEFAULT" \
    PG_USER="$PG_USER_WANT" PG_DB="$PG_DB_WANT" REDIS_DB="$REDIS_DB" \
    WANT_PG_PORT="${POSTGRES_PORT:-}" WANT_REDIS_PORT="${REDIS_PORT:-}" \
    WANT_MINIO_API_PORT="${MINIO_API_PORT:-}" WANT_MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-}" \
    "$PY" - "$VERIFY_ENV" <<'PYEOF'
"""Write one throwaway environment file: this run's only credential source.

Every consumer - Docker Compose interpolation, psql, Alembic, pytest and the ARQ
worker - reads the values from this file, so no part of the system can end up
with an independently constructed (and conflicting) credential. Values are
random per run and are never printed here; the summary printed to stdout
contains only hosts, ports and names.
"""
import os
import secrets
import socket
import sys

path = sys.argv[1]
env = os.environ


def pick_ports(specs: list[tuple[str, str]]) -> list[int]:
    """Allocate one free loopback port per (kind, preferred) pair.

    Every probe socket stays bound while the others are chosen: releasing one
    first lets the allocator hand the same ephemeral port back twice, which would
    make two containers fight over one host port. A requested port is validated
    instead of replaced, so the run cannot silently move off a firewall rule.

    No SO_REUSEADDR here: it would let a probe 'succeed' against a port another
    process is already holding (Windows allows it), hiding the collision until the
    containers fail to publish.
    """
    probes: list[socket.socket] = []
    ports: list[int] = []
    try:
        for kind, preferred in specs:
            target = int(preferred) if preferred else 0
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", target))
            except OSError as exc:
                raise SystemExit(
                    f"host port {target} requested for {kind} is not usable ({exc}); "
                    "unset that port variable to let the verifier choose a free one"
                ) from exc
            probes.append(probe)
            ports.append(probe.getsockname()[1])
    finally:
        for probe in probes:
            probe.close()
    if len(set(ports)) != len(ports):
        raise SystemExit(f"the verifier allocated duplicate host ports: {ports}")
    return ports


pg_port, redis_port, minio_api_port, minio_console_port = pick_ports(
    [
        ("PostgreSQL", env["WANT_PG_PORT"]),
        ("Redis", env["WANT_REDIS_PORT"]),
        ("MinIO API", env["WANT_MINIO_API_PORT"]),
        ("MinIO console", env["WANT_MINIO_CONSOLE_PORT"]),
    ]
)

# token_urlsafe() is [A-Za-z0-9_-] only: safe inside a URL, in a shell assignment
# and in Compose interpolation, with no quoting rules to remember.
pg_password = secrets.token_urlsafe(24)
minio_user = "verify" + secrets.token_hex(3)          # MinIO wants >= 8 chars
minio_secret = secrets.token_urlsafe(24)
session_secret = secrets.token_urlsafe(32)
csrf_secret = secrets.token_urlsafe(32)
admin_password = secrets.token_urlsafe(18)

verify_db = env["VERIFY_DB"]
values = {
    # --- infrastructure the compose file interpolates ---
    "POSTGRES_USER": env["PG_USER"],
    "POSTGRES_PASSWORD": pg_password,
    "POSTGRES_DB": env["PG_DB"],
    "POSTGRES_PORT": str(pg_port),
    "REDIS_PORT": str(redis_port),
    "MINIO_IMAGE": env["MINIO_IMAGE"],
    "MINIO_API_PORT": str(minio_api_port),
    "MINIO_CONSOLE_PORT": str(minio_console_port),
    "MINIO_ROOT_USER": minio_user,
    "MINIO_ROOT_PASSWORD": minio_secret,
    # --- application settings (read from the environment by pydantic-settings) ---
    "APP_ENV": "development",
    "DATABASE_URL": f"postgresql+asyncpg://{env['PG_USER']}:{pg_password}@127.0.0.1:{pg_port}/{verify_db}",
    "REDIS_URL": f"redis://127.0.0.1:{redis_port}/{env['REDIS_DB']}",
    "OBJECT_STORAGE_ENDPOINT": f"http://127.0.0.1:{minio_api_port}",
    "OBJECT_STORAGE_REGION": "us-east-1",
    "OBJECT_STORAGE_ACCESS_KEY": minio_user,
    "OBJECT_STORAGE_SECRET_KEY": minio_secret,
    "OBJECT_STORAGE_BUCKET": env["VERIFY_BUCKET"],
    "OBJECT_STORAGE_SECURE": "false",
    "SESSION_SECRET": session_secret,
    "CSRF_SECRET": csrf_secret,
    "COOKIE_SECURE": "false",
    "BOOTSTRAP_ADMIN_USERNAME": "admin",
    "BOOTSTRAP_ADMIN_PASSWORD": admin_password,
    "AI_PROVIDER": "none",
    "OCR_PROVIDER": "none",
    "STT_PROVIDER": "none",
    "TRANSLATION_PROVIDER": "none",
    "DICTIONARY_PROVIDER": "none",
}
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    handle.write("# Generated by scripts/verify_phase12.sh - throwaway credentials for one\n")
    handle.write("# verification run. Not the developer .env; safe to delete; never commit.\n")
    for key, value in values.items():
        handle.write(f"{key}={value}\n")
os.chmod(path, 0o600)

print(f"database user={env['PG_USER']} initial_db={env['PG_DB']} verify_db={verify_db}")
print(f"loopback ports postgres={pg_port} redis={redis_port} minio={minio_api_port} console={minio_console_port}")
print(f"redis db index={env['REDIS_DB']} bucket={env['VERIFY_BUCKET']}")
print("secrets: 5 throwaway values generated (postgres password, minio key pair,")
print("         session secret, csrf secret, bootstrap admin password) - not printed")
PYEOF
  )"
  status=$?
  if [ $status -ne 0 ]; then
    bad "could not generate the verification environment"
    printf '%s\n' "$gen_out" | indented
    return 1
  fi
  printf '%s\n' "$gen_out" | indented

  # Load every value once; from here on nothing re-derives a credential.
  set -a
  # shellcheck disable=SC1090
  source "$VERIFY_ENV"
  set +a
  info "application settings for this run come from $VERIFY_ENV (mode 600, deleted with the run)"
  return 0
}

prepare_isolated_project() {
  local existing
  existing="$(dc ps -q 2>/dev/null | tr -d '\r')"
  if [ -n "$existing" ]; then
    info "removing leftover verification containers and their volumes (project '$COMPOSE_PROJECT' only)"
    dc down --volumes --remove-orphans >/dev/null 2>&1
    # A previous volume initialised with a previous password would fail
    # authentication now; the verification volumes are disposable.
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# 3-5. Services, from the isolated project
# --------------------------------------------------------------------------- #
start_service() {
  local service="$1" out status
  out="$(dc up -d "$service" 2>&1)"
  status=$?
  if [ $status -ne 0 ]; then
    printf '%s\n' "$out" | indented
    if printf '%s\n' "$out" | grep -qiE 'pull access denied|error pulling image|manifest unknown|unauthorized|not found|no such host|Get "https'; then
      bad "could not pull/start $service - the image is unavailable from this machine"
      if [ "$service" = "minio" ]; then
        info "pinned image: $MINIO_IMAGE_DEFAULT (override with MINIO_IMAGE=<registry>/<repo>:<tag>)"
      fi
      return 1
    fi
    if printf '%s\n' "$out" | grep -qiE 'port is already allocated|address already in use|bind'; then
      bad "could not publish $service on its host port"
      info "a stale verification stack is the usual cause; the run starts with 'down --volumes' for its own project only"
      return 1
    fi
    bad "could not start $service"
    return 1
  fi
  info "$service container requested"
  return 0
}

# The first service started also clears the previous verification run's own
# containers and volumes, so a volume initialised with an earlier (now invalid)
# password can never cause an authentication failure in this run.
start_postgres() {
  prepare_isolated_project || return 1
  start_service postgres
}

# --------------------------------------------------------------------------- #
# 6. Real health: the container's own healthcheck, then the protocol itself
# --------------------------------------------------------------------------- #
health_status() {
  local cid="$1"
  [ -n "$cid" ] || { printf 'missing'; return 0; }
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$cid" 2>/dev/null \
    | tr -d '\r'
}

health_diagnosis() {
  local service="$1" cid="$2"
  [ "$service" = "minio" ] || return 0
  [ -n "$cid" ] || return 0
  # A licensed MinIO build (AIStor/EOS) starts and serves its health page, then
  # denies every S3 operation, so the readiness loop never finishes. The community
  # server also prints a 'License:' banner, so match the denial wording only.
  if docker logs --tail 60 "$cid" 2>&1 | tr -d '\r' \
      | grep -qiE 'no valid license|running in offline mode|all s3 operations are denied'; then
    bad "that MinIO image is a licensed build: it denies all S3 operations without a license"
    info "use the community AGPLv3 server - the compose file pins one proven against this app"
  fi
}

wait_healthy() {
  local service="$1" cid status waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    cid="$(dc ps -q "$service" 2>/dev/null | head -1 | tr -d '\r')"
    if [ -n "$cid" ]; then
      status="$(health_status "$cid")"
      case "$status" in
        healthy) info "$service: container healthcheck reports healthy"; return 0 ;;
        starting|"") : ;;
        no-healthcheck)
          bad "$service has no healthcheck defined, so its readiness cannot be proven"
          return 1 ;;
        exited|dead|removing)
          bad "$service container is '$status' - recent logs:"
          docker logs --tail 25 "$cid" 2>&1 | indented
          health_diagnosis "$service" "$cid"
          return 1 ;;
        unhealthy)
          bad "$service container reported unhealthy - recent health probe output:"
          docker inspect --format '{{json .State.Health}}' "$cid" 2>/dev/null | tr -d '\r' | tail -3 | indented
          docker logs --tail 25 "$cid" 2>&1 | indented
          health_diagnosis "$service" "$cid"
          return 1 ;;
        *) bad "$service container state is '$status'"; return 1 ;;
      esac
    fi
    sleep 2
    waited=$((waited + 2))
  done
  bad "$service did not become healthy within ${HEALTH_TIMEOUT}s"
  health_diagnosis "$service" "$(dc ps -q "$service" 2>/dev/null | head -1 | tr -d '\r')"
  return 1
}

probe_postgres_health() {
  if ! wait_healthy postgres; then return 1; fi
  if ! dc exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    bad "pg_isready refused connections"
    return 1
  fi
  if ! "$PY" - "$POSTGRES_PORT" <<'PYEOF' >/dev/null 2>&1
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5):
    pass
PYEOF
  then
    bad "PostgreSQL port 127.0.0.1:${POSTGRES_PORT} is not reachable from $(uname -s)"
    info "with Docker Desktop the published ports are reachable from WSL on 127.0.0.1; if your daemon is remote, export VERIFY_PG_HOST and adapt DATABASE_URL"
    return 1
  fi
  info "postgres: accepting connections on 127.0.0.1:${POSTGRES_PORT}"
  return 0
}

probe_redis_health() {
  if ! wait_healthy redis; then return 1; fi
  # PING through the application's own client and the application's own URL.
  if ! ( cd "$BACKEND" && "$PY" - <<'PYEOF'
import asyncio

from app.core.config import get_settings
from app.core.redis_client import close_redis, get_redis


async def main() -> None:
    redis = get_redis()
    pong = await redis.ping()
    info = await redis.info("server")
    await close_redis()
    if not pong:
        raise SystemExit("Redis PING did not return PONG")
    print(f"   .. redis: PING ok via {get_settings().redis_url} (server {info.get('redis_version')})")


asyncio.run(main())
PYEOF
  ); then
    bad "Redis did not answer PING through the application client at $REDIS_URL"
    return 1
  fi
  return 0
}

probe_minio_health() {
  if ! wait_healthy minio; then return 1; fi
  if ! ( cd "$BACKEND" && "$PY" - <<'PYEOF'
import urllib.request

from app.core.config import get_settings

settings = get_settings()
url = settings.object_storage_endpoint.rstrip("/") + "/minio/health/ready"
with urllib.request.urlopen(url, timeout=10) as response:
    if response.status != 200:
        raise SystemExit(f"{url} answered {response.status}")
print(f"   .. minio: {url} answered 200")
PYEOF
  ); then
    bad "MinIO did not answer its readiness endpoint at $OBJECT_STORAGE_ENDPOINT"
    return 1
  fi
  # ... and that the verifier's credentials are actually S3 credentials.
  if ! ( cd "$BACKEND" && "$PY" - <<'PYEOF'
from app.core.config import get_settings
from app.core.storage import ObjectStorage

settings = get_settings()
buckets = ObjectStorage()._client.list_buckets()["Buckets"]
print(
    f"   .. minio: authenticated list_buckets with access key "
    f"'{settings.object_storage_access_key}' ({len(buckets)} bucket(s))"
)
PYEOF
  ); then
    bad "the generated MinIO credentials were rejected over S3"
    return 1
  fi
  return 0
}

wait_for_services() {
  local failed=0
  probe_postgres_health || { failed=1; PG_OK=0; INFRA_FAILURES+=("step 6 - PostgreSQL not reachable"); }
  probe_redis_health || { failed=1; REDIS_OK=0; INFRA_FAILURES+=("step 6 - Redis not reachable"); }
  probe_minio_health || { failed=1; MINIO_OK=0; INFRA_FAILURES+=("step 6 - MinIO not reachable"); }
  [ $failed -eq 0 ] || return 1
  info "all three services are healthy AND answering their own protocol"
  return 0
}

# --------------------------------------------------------------------------- #
# 7. Isolated verification database + the real connection preflight
# --------------------------------------------------------------------------- #
psql_admin() {
  dc exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
}

create_verifier_database() {
  case "$VERIFY_DB" in
    "$PG_DB_WANT")
      bad "refusing to reset '$VERIFY_DB' - it is the application database (set VERIFY_TEST_DB)"
      return 1 ;;
    *_verify_phase12|*_test|test_*) : ;;
    *)
      bad "refusing to reset '$VERIFY_DB' - the verification database must be named *_test, test_* or *_verify_phase12"
      return 1 ;;
  esac

  psql_admin \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${VERIFY_DB}' AND pid <> pg_backend_pid()" \
    -c "DROP DATABASE IF EXISTS \"${VERIFY_DB}\" WITH (FORCE)" \
    -c "CREATE DATABASE \"${VERIFY_DB}\" OWNER ${POSTGRES_USER} TEMPLATE template1" || {
      bad "could not (re)create the isolated database '$VERIFY_DB'"
      return 1
    }
  info "dropped and recreated '$VERIFY_DB' (empty schema, owned by $POSTGRES_USER)"

  local objects
  objects="$(dc exec -T postgres psql -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$VERIFY_DB" \
    -c "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r','v','p')" 2>/dev/null | tr -d '\r')"
  if [ "$objects" != "0" ]; then
    bad "the fresh verification database already contains ${objects:-?} relation(s)"
    return 1
  fi
  info "verified: '$VERIFY_DB' contains no relations before the migration runs"

  # Preflight with the EXACT DATABASE_URL the suites will use. Without this a
  # credential mismatch would only surface as dozens of skipped tests.
  if ! ( cd "$BACKEND" && "$PY" - "$VERIFY_DB" <<'PYEOF'
import asyncio
import re
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

expected_db = sys.argv[1]


def redact(url: str) -> str:
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT version()"))).scalar()
            current = (await conn.execute(text("SELECT current_database()"))).scalar()
            user = (await conn.execute(text("SELECT current_user"))).scalar()
    finally:
        await engine.dispose()
    if current != expected_db:
        raise SystemExit(f"DATABASE_URL connects to '{current}', expected '{expected_db}'")
    print(f"   .. authenticated connection to {redact(settings.database_url)}")
    print(f"   .. server: {str(version).split(',')[0]} as user '{user}' in database '{current}'")


asyncio.run(main())
PYEOF
  ); then
    bad "could not authenticate to the verification database with DATABASE_URL"
    info "this is a credential problem, not a test problem: every value must come from $VERIFY_ENV"
    return 1
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# 8. The committed migration builds the schema
# --------------------------------------------------------------------------- #
# Alembic's own view of the chain tip, so this gate never asserts a stamp that a
# later phase has already moved past.
alembic_head() {
  ( cd "$BACKEND" && "$PY" -m alembic heads 2>/dev/null | tail -n 1 | awk '{print $1}' )
}

run_migrations() {
  local head
  head="$(alembic_head)"
  if [ -z "$head" ]; then
    bad "could not read the alembic head revision"
    return 1
  fi
  info "committed chain tip: $head"

  ( cd "$BACKEND" && "$PY" -m alembic upgrade head ) || { bad "alembic upgrade head failed"; return 1; }
  ( cd "$BACKEND" && "$PY" -m alembic upgrade head ) || { bad "second 'alembic upgrade head' was not a clean no-op"; return 1; }
  info "upgrade head applied, and applying it again is a no-op"

  # A brand new process reading the same database proves the DDL survived the
  # process that created it (schema-level restart persistence).
  ( cd "$BACKEND" && "$PY" - "$head" <<'PYEOF'
import asyncio
import sys

from sqlalchemy import text

from app.core.database import engine

HEAD_REVISION = sys.argv[1]


async def main() -> None:
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        tables = (
            await conn.execute(
                text("SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
            )
        ).scalar()
        columns = (
            await conn.execute(
                text("SELECT count(*) FROM information_schema.columns "
                     "WHERE table_schema = 'public'")
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
    print(
        f"   .. read back by a fresh process: alembic_version={version} tables={tables} "
        f"columns={columns} foreign_keys={fks} indexes={indexes}"
    )
    assert version == HEAD_REVISION, f"unexpected alembic_version: {version!r}"
    assert tables >= 45, f"expected at least 45 tables, found {tables}"
    assert fks >= 50, f"expected at least 50 foreign keys, found {fks}"
    assert indexes >= 91, f"expected at least 91 indexes, found {indexes}"
    await engine.dispose()


asyncio.run(main())
PYEOF
  ) || { bad "the migrated schema could not be read back by a fresh process"; return 1; }
  run_migration_round_trip || return 1
  return 0
}

# Downgrade to the revision Phase 1-3 left behind, then upgrade over the top of it
# with data in the tables. `upgrade head` on an empty database only ever exercises
# the last revision against nothing; this is the "previous state -> new state"
# transition every later phase has to survive.
run_migration_round_trip() {
  local head
  head="$(alembic_head)"
  if [ -z "$head" ]; then
    bad "could not read the alembic head revision"
    return 1
  fi

  ( cd "$BACKEND" && "$PY" -m alembic downgrade 0001_bootstrap ) \
    || { bad "alembic downgrade 0001_bootstrap failed"; return 1; }
  ( cd "$BACKEND" && "$PY" - <<'PYEOF'
import asyncio

from sqlalchemy import text

from app.core.database import engine


async def main() -> None:
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        index = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_vocabulary_word_language'")
            )
        ).scalar()
        kept_table = (
            await conn.execute(
                text("SELECT count(*) FROM information_schema.tables "
                     "WHERE table_schema = 'public' AND table_name = 'vocabulary_entry'")
            )
        ).scalar()
        columns = (
            await conn.execute(
                text("SELECT count(*) FROM information_schema.columns "
                     "WHERE table_schema = 'public' AND table_name = 'vocabulary_entry'")
            )
        ).scalar()
    assert version == "0001_bootstrap", f"downgrade left the stamp at {version!r}"
    assert index is None, f"0002's index survived its own downgrade: {index!r}"
    # A granular revision must not take the table or its columns with it: a teacher
    # who rolls back a phase must get the word bank back, not lose it.
    assert kept_table == 1, "downgrade dropped the vocabulary_entry table"
    assert columns >= 10, f"downgrade dropped vocabulary_entry columns: {columns}"
    await engine.dispose()


asyncio.run(main())
PYEOF
  ) || { bad "the downgraded schema did not match the pre-Phase-4 state"; return 1; }
  info "downgrade 0001_bootstrap removed only the vocabulary word index"

  ( cd "$BACKEND" && "$PY" - <<'PYEOF'
import asyncio

from sqlalchemy import text

from app.core.database import engine

INSERT = (
    "INSERT INTO vocabulary_entry (id, created_at, updated_at, word, learning_language, "
    "synonyms, antonyms, status) VALUES (gen_random_uuid(), now(), now(), 'roundtrip', 'en', "
    "'[]'::jsonb, '[]'::jsonb, 'READY') RETURNING id"
)


async def main() -> None:
    async with engine.connect() as conn:
        staged = (await conn.execute(text(INSERT))).scalars().all()
        # Committed, not rolled back: the point of the round trip is that 0002
        # rebuilds its index over rows that were already written.
        await conn.commit()
    assert staged, "could not stage a row for the round trip"
    await engine.dispose()


asyncio.run(main())
PYEOF
  ) || { bad "could not stage a vocabulary row before re-upgrading"; return 1; }

  ( cd "$BACKEND" && "$PY" -m alembic upgrade head ) \
    || { bad "'alembic upgrade head' from 0001_bootstrap failed"; return 1; }
  ( cd "$BACKEND" && "$PY" - "$head" <<'PYEOF'
import asyncio
import sys

from sqlalchemy import text

from app.core.database import engine

HEAD_REVISION = sys.argv[1]


async def main() -> None:
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        definition = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_vocabulary_word_language'")
            )
        ).scalar()
        kept = (
            await conn.execute(text("SELECT count(*) FROM vocabulary_entry WHERE word = 'roundtrip'"))
        ).scalar()
        # The rebuilt index has to be usable, not merely present: two live copies of
        # the staged word must now be impossible. `ON CONFLICT DO NOTHING` reports a
        # partial-index collision as a skipped row, so an id coming back means failure.
        # The parentheses matter: `await x.execute(...).scalars()` awaits the *chain*,
        # which leaves the coroutine un-awaited and this check silently un-run.
        duplicate = (
            await conn.execute(
                text(
                    "INSERT INTO vocabulary_entry (id, created_at, updated_at, word, learning_language, "
                    "synonyms, antonyms, status) VALUES (gen_random_uuid(), now(), now(), 'roundtrip', 'en', "
                    "'[]'::jsonb, '[]'::jsonb, 'READY') ON CONFLICT DO NOTHING RETURNING id"
                )
            )
        ).scalars().all()
        await conn.execute(text("DELETE FROM vocabulary_entry WHERE word = 'roundtrip'"))
        await conn.commit()
    assert version == HEAD_REVISION, f"re-upgrade stamped {version!r}"
    assert definition is not None, "the vocabulary word index did not come back"
    assert "UNIQUE" in definition.upper(), definition
    # Postgres rewrites the predicate in its own spelling - `WHERE (deleted_at IS NULL)`,
    # with parentheses - so this compares the normalised form rather than the exact text
    # the migration happened to be written with.
    normalised = " ".join(definition.lower().replace("(", " ").replace(")", " ").split())
    assert "where deleted_at is null" in normalised, definition
    assert kept == 1, f"the round trip lost or duplicated staged rows: {kept}"
    assert not duplicate, "the rebuilt index accepted a duplicate live word"
    await engine.dispose()


asyncio.run(main())
PYEOF
  ) || { bad "re-applying 0002 over existing data failed"; return 1; }
  info "upgrade from 0001_bootstrap rebuilt the index over existing rows, and it rejects duplicates"
  return 0
}

# --------------------------------------------------------------------------- #
# 9. Seed, twice, and prove the second run changed nothing
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
# 10-12. The suites. A skip in a mandatory suite is a failure.
# --------------------------------------------------------------------------- #
# Paths handed to Python are always relative to $BACKEND: an absolute WSL path
# would be meaningless to a Windows interpreter and vice versa.
# `--basetemp` keeps pytest's scratch directory inside this run's report folder
# instead of the host temp dir, whose permissions on some Windows setups make
# pytest's end-of-session cleanup fail after an otherwise green run.
run_pytest_suite() {
  local label="$1" report_rel="$2"
  shift 2
  local pytest_status gate_status summary
  ( cd "$BACKEND" && "$PY" -m pytest "$@" -p no:cacheprovider \
      --basetemp="$REPORT_REL/pytest-tmp" --junitxml="$report_rel" -q --tb=short -rA )
  pytest_status=$?
  summary="$( ( cd "$BACKEND" && "$PY" "$GATE_REL" "$report_rel" "$label" ) )"
  gate_status=$?
  printf '%s\n' "$summary"
  SUITE_SUMMARY+=("$summary")
  if [ $gate_status -ne 0 ]; then
    return 1
  fi
  if [ $pytest_status -ne 0 ]; then
    info "pytest itself exited $pytest_status while the report looked clean - see the output above"
    return 1
  fi
  return 0
}

run_unit_suite() {
  run_pytest_suite "unit/static" "$REPORT_REL/junit-unit.xml" \
    tests --ignore=tests/integration || return 1
  return 0
}

run_integration_suite() {
  run_pytest_suite "integration (Postgres+Redis+MinIO)" "$REPORT_REL/junit-integration.xml" \
    tests/integration -v || return 1
  return 0
}

exercise_minio_storage() {
  run_pytest_suite "MinIO storage round-trip" "$REPORT_REL/junit-storage.xml" \
    tests/integration/test_storage_minio.py -v || return 1
  # Independent confirmation, over S3, that the bucket the application created
  # through its own abstraction is really there now.
  ( cd "$BACKEND" && "$PY" - <<'PYEOF'
from app.core.config import get_settings
from app.core.storage import ObjectStorage

settings = get_settings()
storage = ObjectStorage()
assert storage.bucket_exists(), f"bucket {settings.object_storage_bucket!r} is missing"
print(f"   .. bucket present after the round-trip: {settings.object_storage_bucket}")
PYEOF
  ) || { bad "the verification bucket is not visible over S3"; return 1; }
  return 0
}

# --------------------------------------------------------------------------- #
# 13. ARQ worker: starts with every optional adapter disabled, and its
#     server-authoritative cron actually completes against live Postgres.
# --------------------------------------------------------------------------- #
# How many times the worker mentioned the attempt-expiry cron, excluding the
# startup banner that simply lists the registered function names. arq logs one
# line when a job starts and one when it finishes, so 2 means it really ran.
cron_evidence() {
  grep -E 'expire_attempts' "$1" 2>/dev/null | grep -vc 'Starting worker' || true
}

check_worker() {
  local log="$REPORT_DIR/worker.log" waited=0
  mkdir -p "$REPORT_DIR"
  : > "$log"
  # AI/OCR/STT providers are all 'none' here on purpose: a disabled optional
  # adapter must not stop the worker from starting.
  ( cd "$BACKEND" && exec "$PY" -m arq app.workers.main.WorkerSettings ) >>"$log" 2>&1 &
  local pid=$!

  stop_worker() {
    kill "$pid" 2>/dev/null
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 15 ]; do sleep 1; i=$((i + 1)); done
    kill -9 "$pid" 2>/dev/null
  }

  while [ "$waited" -lt "$WORKER_TIMEOUT" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      bad "the ARQ worker process exited during startup (log follows)"
      tail -25 "$log" | indented
      return 1
    fi
    if grep -qiE 'Traceback|CRITICAL|AddressError|ConnectionError' "$log"; then
      bad "the ARQ worker logged an error during startup (log follows)"
      tail -25 "$log" | indented
      stop_worker
      return 1
    fi
    if grep -q 'Starting worker for' "$log"; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if ! grep -q 'Starting worker for' "$log"; then
    bad "the ARQ worker did not report a startup banner within ${WORKER_TIMEOUT}s"
    tail -25 "$log" | indented
    stop_worker
    return 1
  fi
  info "worker registered its functions (redis $REDIS_URL)"

  # The attempt-expiry cron fires every 15 seconds; a completed run proves the
  # worker reaches live Postgres, not just Redis.
  local cron_waited=0
  while [ "$cron_waited" -lt "$WORKER_TIMEOUT" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      bad "the ARQ worker stopped after coming up (log follows)"
      tail -25 "$log" | indented
      return 1
    fi
    if [ "$(cron_evidence "$log")" -ge 2 ]; then
      break
    fi
    sleep 1
    cron_waited=$((cron_waited + 1))
  done
  if [ "$(cron_evidence "$log")" -lt 2 ]; then
    bad "the worker's attempt-expiry cron never completed within ${WORKER_TIMEOUT}s"
    tail -25 "$log" | indented
    stop_worker
    return 1
  fi
  if grep -qiE 'Traceback|CRITICAL|AddressError|ConnectionError' "$log"; then
    bad "the worker logged an error while running its cron (log follows)"
    tail -25 "$log" | indented
    stop_worker
    return 1
  fi
  info "cron executed against PostgreSQL:"
  grep -E 'expire_attempts' "$log" | grep -v 'Starting worker' | tail -2 | indented
  stop_worker
  info "worker stopped"
  return 0
}

# --------------------------------------------------------------------------- #
# Cleanup: only resources this run owns - never a development volume.
# --------------------------------------------------------------------------- #
CLEANED=0
cleanup() {
  [ "$CLEANED" -eq 1 ] && return 0
  CLEANED=1
  if [ "$KEEP" -eq 1 ]; then
    say ""
    warn "--keep: verification containers, volumes and $VERIFY_ENV were left in place"
    info "tear them down yourself with:"
    info "  $COMPOSE -p $COMPOSE_PROJECT --env-file $VERIFY_ENV -f $COMPOSE_FILE down --volumes --remove-orphans"
    return 0
  fi
  if [ -n "$COMPOSE" ] && [ -f "$VERIFY_ENV" ]; then
    dc down --volumes --remove-orphans >/dev/null 2>&1 && \
      info "verification containers and their volumes removed (project '$COMPOSE_PROJECT' only)"
  fi
  # The throwaway credentials go with the run.
  rm -f "$VERIFY_ENV" 2>/dev/null
}

# --------------------------------------------------------------------------- #
# Drive the steps
# --------------------------------------------------------------------------- #
say "=========================================================================="
say " Phase 1-2 mandatory acceptance check"
say "=========================================================================="

trap 'cleanup' EXIT
trap 'say ""; say "interrupted"; exit 130' INT TERM

infra_step 1 "Docker and a native verification interpreter" TOOL_OK check_toolchain
if [ "$TOOL_OK" -eq 0 ]; then
  say ""
  bad "cannot continue: the toolchain prerequisite failed, so no mandatory check could run."
  printf '  - %s\n' "${INFRA_FAILURES[@]}"
  say ""
  say "Phase 1-2 is NOT accepted (infrastructure)."
  exit 1
fi

infra_step 2 "Generate throwaway credentials and an isolated compose project" TOOL_OK generate_verifier_environment
if [ "$TOOL_OK" -eq 0 ]; then
  say ""
  bad "cannot continue: this run's environment could not be created."
  printf '  - %s\n' "${INFRA_FAILURES[@]}"
  say ""
  say "Phase 1-2 is NOT accepted (infrastructure)."
  exit 1
fi

infra_step 3 "Start PostgreSQL in the isolated project"            PG_OK    start_postgres
infra_step 4 "Start Redis in the isolated project"                 REDIS_OK start_service redis
infra_step 5 "Start MinIO in the isolated project"                 MINIO_OK start_service minio
infra_step 6 "Wait for real service health (healthcheck + protocol)" HEALTH_OK wait_for_services

if [ ${#INFRA_FAILURES[@]} -gt 0 ]; then
  say ""
  warn "infrastructure is incomplete - the mandatory checks below are reported as blocked, not passed"
fi

work_step 7  "Create the isolated database + DATABASE_URL auth preflight" "pg"  create_verifier_database
work_step 8  "alembic upgrade head + downgrade/upgrade round trip" "pg"     run_migrations
work_step 9  "Run the seed twice and prove idempotency" "pg"                     run_seed_twice
work_step 10 "Backend unit/static suite" "tool"                                  run_unit_suite
work_step 11 "Backend integration suite (zero skips allowed)" "pg redis minio"   run_integration_suite
work_step 12 "MinIO object storage through the application abstraction" "minio"  exercise_minio_storage
work_step 13 "ARQ worker startup + cron against live Postgres" "pg redis"         check_worker

cleanup

# --------------------------------------------------------------------------- #
# Final report
# --------------------------------------------------------------------------- #
say ""
say "=========================================================================="
say " Result"
say "=========================================================================="
say "passed steps: ${#PASSED_STEPS[@]} / $NUM_STEPS"
if [ ${#SUITE_SUMMARY[@]} -gt 0 ]; then
  say ""
  say "suites:"
  printf '  - %s\n' "${SUITE_SUMMARY[@]}"
fi

if [ ${#INFRA_FAILURES[@]} -gt 0 ]; then
  say ""
  say "INFRASTRUCTURE FAILURES (services, images or credentials - not test results):"
  printf '  - %s\n' "${INFRA_FAILURES[@]}"
fi
if [ ${#BLOCKED_STEPS[@]} -gt 0 ]; then
  say ""
  say "MANDATORY CHECKS THAT COULD NOT RUN (blocked by the infrastructure above):"
  printf '  - %s\n' "${BLOCKED_STEPS[@]}"
fi
if [ ${#TEST_FAILURES[@]} -gt 0 ]; then
  say ""
  say "TEST / VERIFICATION FAILURES:"
  printf '  - %s\n' "${TEST_FAILURES[@]}"
fi

if [ ${#INFRA_FAILURES[@]} -eq 0 ] && [ ${#TEST_FAILURES[@]} -eq 0 ] && [ ${#BLOCKED_STEPS[@]} -eq 0 ]; then
  say ""
  say "PHASE 1-2 ACCEPTED: every mandatory check passed against live PostgreSQL,"
  say "Redis and MinIO, with zero skipped mandatory tests."
  exit 0
fi

say ""
say "Phase 1-2 is NOT accepted. Fix the items above and re-run: bash scripts/verify_phase12.sh"
exit 1
