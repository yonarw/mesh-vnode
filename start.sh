#!/usr/bin/env bash
# Start mesh-vnode, stopping any instance that is already running.
#
#   ./start.sh              background, logs to data/vnode.log
#   ./start.sh --fg         foreground, Ctrl-C to quit
#   ./start.sh --node IP    use this node for this run, whatever the web ui says
#
# Any VNODE_* variable set in the environment or in .env still applies.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

FOREGROUND=0
NODE_ARG=""
ARGS=()
while (($#)); do
    case $1 in
        --fg|--foreground) FOREGROUND=1 ;;
        # Passed as a flag, not as VNODE_UPSTREAM_HOST, so it also wins over
        # an address saved in the web UI.
        --node) shift; NODE_ARG="$1"; ARGS+=(--node "$1") ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

./stop.sh

mkdir -p data
PIDFILE="data/vnode.pid"
LOGFILE="data/vnode.log"          # rotating, written by the service itself
CRASHLOG="data/vnode.stderr.log"  # only what escapes logging: startup errors, crashes

echo "syncing python dependencies"
uv sync --quiet

# Build the web UI on first run. Without it the API still works, it just has no
# pages to serve.
if [[ ! -d webui/dist ]]; then
    if command -v npm >/dev/null 2>&1; then
        echo "building the web ui (first run only)"
        [[ -d webui/node_modules ]] || npm --prefix webui install
        npm --prefix webui run build
    else
        echo "note: npm not found, so the web ui will not be served (the API still works)"
    fi
fi

WEB_PORT="${VNODE_WEB_PORT:-8080}"
if [[ -n $NODE_ARG ]]; then
    echo "upstream node: $NODE_ARG (--node, overrides the web ui setting)"
else
    echo "upstream node: as set in the web ui, else VNODE_UPSTREAM_HOST"
fi

if ((FOREGROUND)); then
    exec uv run mesh-vnode run "${ARGS[@]+"${ARGS[@]}"}"
fi

# setsid gives the server its own process group, so stop.sh can signal uv and
# the python process it spawns together.
# The service writes and rotates the log itself (8 MB x 3 by default), so a Pi
# left running for months does not fill its card. The console copy is off;
# stdout/stderr only catch what happens before logging is up, or a crash.
VNODE_LOG_FILE="${VNODE_LOG_FILE:-$LOGFILE}" VNODE_LOG_CONSOLE=false \
    setsid uv run mesh-vnode run "${ARGS[@]+"${ARGS[@]}"}" >"$CRASHLOG" 2>&1 &
pid=$!
echo "$pid" >"$PIDFILE"

for _ in $(seq 40); do
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "failed to start. Last lines of $CRASHLOG and $LOGFILE:"
        tail -20 "$CRASHLOG"
        [[ -f $LOGFILE ]] && tail -20 "$LOGFILE"
        rm -f "$PIDFILE"
        exit 1
    fi
    if curl -fsS -o /dev/null "http://127.0.0.1:${WEB_PORT}/api/status" 2>/dev/null; then
        echo
        echo "mesh-vnode is up (pid $pid)"
        echo "  web ui        http://localhost:${WEB_PORT}"
        echo "  virtual node  port ${VNODE_LISTEN_PORT:-4404}  (add this in the Meshtastic app under Network)"
        echo "  logs          tail -f $LOGFILE"
        echo "  stop          ./stop.sh"
        exit 0
    fi
    sleep 0.5
done

echo "started (pid $pid) but the web ui did not answer within 20s; check $LOGFILE"
