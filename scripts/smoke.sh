#!/usr/bin/env bash
# Spin up the BrowserBuddy smoke-test stack for manual testing:
#   - the ingest backend (uv run buddy serve) on 127.0.0.1:8765
#   - Firefox with the extension loaded (web-ext run)
#
# Uses an isolated data dir and a PERSISTENT Firefox profile (both under
# .smoke/, gitignored) so pairing survives across runs. Closing Firefox — or
# Ctrl-C in this terminal — tears the backend down.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_DIR="$REPO/.smoke"
DATA_DIR="$SMOKE_DIR/data"
PROFILE_DIR="$SMOKE_DIR/firefox-profile"
LOG="$SMOKE_DIR/serve.log"
HOST="127.0.0.1"
PORT="${BROWSERBUDDY_PORT:-8765}"
WEB_EXT="$REPO/extension/node_modules/.bin/web-ext"
DIST="$REPO/extension/dist/firefox"

BUILD=0
FRESH=0

usage() {
  cat <<EOF
Usage: scripts/smoke.sh [options]

Spins up the BrowserBuddy smoke stack:
  - backend ingest server (uv run buddy serve) on $HOST:$PORT
  - Firefox with the extension loaded (web-ext run)

Isolated data dir:   $DATA_DIR
Persistent FF profile: $PROFILE_DIR

Options:
  -b, --build   Rebuild the extension first (default: build only if
                dist/firefox is missing)
      --fresh   Wipe the smoke data dir AND Firefox profile first, for a
                clean first-pairing test
  -h, --help    Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -b|--build) BUILD=1 ;;
    --fresh) FRESH=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

log() { printf '\033[1;34m[smoke]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[smoke]\033[0m %s\n' "$*" >&2; }

# --- preflight ------------------------------------------------------------
command -v uv >/dev/null 2>&1 || { err "uv not found — install from https://docs.astral.sh/uv/"; exit 1; }
[ -x "$WEB_EXT" ] || { err "web-ext missing — run 'npm install' in extension/ first"; exit 1; }

mkdir -p "$SMOKE_DIR" "$DATA_DIR"

if [ "$FRESH" = 1 ]; then
  log "Fresh run: wiping smoke data dir and Firefox profile"
  rm -rf "$DATA_DIR" "$PROFILE_DIR"
  mkdir -p "$DATA_DIR"
fi

# --- build ----------------------------------------------------------------
if [ "$BUILD" = 1 ] || [ ! -d "$DIST" ]; then
  log "Building extension -> dist/firefox"
  ( cd "$REPO/extension" && npm run build )
fi

# True when something is listening on HOST:PORT. Prefer curl (a 404 still
# means the server answered); fall back to nc, then bash /dev/tcp — some
# shells disable /dev/tcp, so it is the last resort, not the first.
port_open() {
  if command -v curl >/dev/null 2>&1; then
    curl -s -o /dev/null --max-time 2 "http://$HOST:$PORT/" 2>/dev/null
  elif command -v nc >/dev/null 2>&1; then
    nc -z -w2 "$HOST" "$PORT" >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null
  fi
}

# --- backend --------------------------------------------------------------
OWN_BACKEND=0
BACKEND_PID=""
if port_open; then
  log "Backend already listening on $HOST:$PORT — reusing it (leaving its lifecycle alone)"
else
  log "Starting backend (logs -> $LOG)"
  ( cd "$REPO/backend" \
      && BROWSERBUDDY_DATA_DIR="$DATA_DIR" BROWSERBUDDY_HOST="$HOST" BROWSERBUDDY_PORT="$PORT" \
         exec uv run buddy serve ) >"$LOG" 2>&1 &
  BACKEND_PID=$!
  OWN_BACKEND=1
fi

cleanup() {
  if [ "$OWN_BACKEND" = 1 ] && [ -n "$BACKEND_PID" ]; then
    log "Stopping backend (pid $BACKEND_PID)"
    kill "$BACKEND_PID" 2>/dev/null || true
    pkill -P "$BACKEND_PID" 2>/dev/null || true   # reap the uvicorn child too
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# --- wait for readiness ---------------------------------------------------
if [ "$OWN_BACKEND" = 1 ]; then
  log "Waiting for backend on $HOST:$PORT ..."
  for _ in $(seq 1 30); do
    port_open && break
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      err "Backend exited early. Last log lines:"; tail -n 20 "$LOG" >&2; exit 1
    fi
    sleep 0.5
  done
  if ! port_open; then
    err "Backend did not come up within 15s. Last log lines:"; tail -n 20 "$LOG" >&2; exit 1
  fi
  log "Backend is up."
fi

# --- secret + instructions ------------------------------------------------
SECRET="$( cd "$REPO/backend" && BROWSERBUDDY_DATA_DIR="$DATA_DIR" uv run buddy secret )"
INSPECT="cd $REPO/backend && BROWSERBUDDY_DATA_DIR=$DATA_DIR uv run buddy"

cat <<EOF

  ────────────────────────────────────────────────────────────
  BrowserBuddy smoke stack is UP.

  Firefox is launching with the extension loaded.

  1. Pair it (first run only): open the popup -> "⚙ Settings" ->
     paste the secret below into the options page -> Save.

         shared secret:  $SECRET

  2. Start a session from the popup, browse a couple of pages, Stop.
     Watch the badge: REC while recording, II when paused.

  3. Inspect from another terminal:

         $INSPECT sessions
         $INSPECT search "<something you read>"

  Data dir: $DATA_DIR
  Log:      $LOG

  Close Firefox (or press Ctrl-C here) to shut everything down.
  ────────────────────────────────────────────────────────────

EOF

# --- browser (foreground; blocks until Firefox is closed) -----------------
log "Launching Firefox via web-ext ..."
"$WEB_EXT" run \
  --source-dir "$DIST" \
  --firefox-profile "$PROFILE_DIR" \
  --profile-create-if-missing \
  --keep-profile-changes

log "Firefox closed."
