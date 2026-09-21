#!/usr/bin/env bash
# Start mesh-vnode, stopping any instance that is already running.
#
#   ./start.sh              background, logs to data/vnode.log
#   ./start.sh --fg         foreground, Ctrl-C to quit
#   ./start.sh --node IP    use this node for this run, whatever the web ui says
#   ./start.sh --dev        local testing: fake node + vite, no hardware needed
#
# Any VNODE_* variable set in the environment or in .env still applies.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

FOREGROUND=0
DEV=0
NODE_ARG=""
ARGS=()
while (($#)); do
    case $1 in
        --fg|--foreground) FOREGROUND=1 ;;
        # Everything needed to look at the web UI without a radio: a fake node
        # to talk to, a throwaway store, and vite in front for hot reload.
        --dev) DEV=1 ;;
        # Passed as a flag, not as VNODE_UPSTREAM_HOST, so it also wins over
        # an address saved in the web UI.
        --node) shift; NODE_ARG="$1"; ARGS+=(--node "$1") ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

FAKE_PORT="${VNODE_FAKE_PORT:-4403}"
FAKE_PIDFILE="data/fakenode.pid"

if ((DEV)); then
    # A store of its own: a dev session invents nodes and messages that have no
    # business in the real one.
    ARGS+=(--db data/dev.sqlite3)
    if [[ -z $NODE_ARG ]]; then
        NODE_ARG="127.0.0.1"
        ARGS+=(--node 127.0.0.1 --node-port "$FAKE_PORT")
    fi
    echo "starting the fake node on port $FAKE_PORT"
    setsid uv run mesh-vnode fakenode --port "$FAKE_PORT" --interval 3 \
        >"data/fakenode.log" 2>&1 &
    echo $! >"$FAKE_PIDFILE"
    # The service retries the connection anyway, but waiting here keeps the
    # first log lines from being a failure that then fixes itself.
    sleep 1
fi

# Build the web UI on first run. Without it the API still works, it just has no
# pages to serve.
if ((DEV)); then
    if [[ ! -d webui/node_modules ]]; then
        echo "installing web ui dependencies (first run only)"
        npm --prefix webui install
    fi
elif [[ ! -d webui/dist ]]; then
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

if ((FOREGROUND)) && ((!DEV)); then
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
        if ((DEV)); then
            echo "  fake node     port ${FAKE_PORT}, log: data/fakenode.log"
            echo "  store         data/dev.sqlite3 (throwaway, safe to delete)"
            echo
            echo "starting vite - Ctrl-C stops the fake node and the service too"
            # vite proxies /api to the service, so this is the real UI against a
            # real store; only the page is rebuilt on save. --host also puts it
            # on the LAN, which is how the phone layout gets tested.
            trap './stop.sh >/dev/null 2>&1' EXIT INT TERM
            npm --prefix webui run dev -- --host
            exit 0
        fi
        echo "  stop          ./stop.sh"
        exit 0
    fi
    sleep 0.5
done

echo "started (pid $pid) but the web ui did not answer within 20s; check $LOGFILE"
