#!/usr/bin/env bash
# Stop a running mesh-vnode started by start.sh.
# Exits 0 whether or not anything was running, so start.sh can call it blindly.
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"
PIDFILE="data/vnode.pid"

wait_for_exit() {   # pid, seconds
    local pid=$1 deadline=$((SECONDS + $2))
    while kill -0 "$pid" 2>/dev/null; do
        ((SECONDS >= deadline)) && return 1
        sleep 0.2
    done
    return 0
}

# Is this pid one of our own ancestors? Killing the shell that invoked us would
# be a rude way to fail.
is_ancestor() {
    local target=$1 pid=$$
    while [[ -n $pid && $pid != 0 ]]; do
        [[ $pid == "$target" ]] && return 0
        pid=$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null) || return 1
    done
    return 1
}

# Does this pid actually RUN the server, rather than merely mention it?
#
# `pgrep -f mesh-vnode` matches any command line containing the string -
# an editor, a grep, a shell script that echoes the name. Worse, `uv run
# mesh-vnode fakenode` contains both "mesh-vnode" and "run" without
# being the server at all. So read the arguments and require that the subcommand
# sits immediately after the executable: "<...>/mesh-vnode run".
is_server() {
    local pid=$1 arg
    local -a args=()
    [[ -r /proc/$pid/cmdline ]] || return 1
    while IFS= read -r -d '' arg; do
        args+=("$arg")
    done < "/proc/$pid/cmdline"
    local i
    for ((i = 0; i < ${#args[@]} - 1; i++)); do
        if [[ ${args[i]##*/} == "mesh-vnode" && ${args[i + 1]} == "run" ]]; then
            return 0
        fi
    done
    return 1
}

stopped=0

if [[ -f $PIDFILE ]]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [[ -n ${pid:-} ]] && kill -0 "$pid" 2>/dev/null; then
        echo "stopping mesh-vnode (pid $pid)"
        # Negative pid signals the whole process group: uv spawns python as a
        # child, and killing only uv would leave the server holding the ports.
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
        if wait_for_exit "$pid" 10; then
            echo "stopped"
        else
            echo "did not exit in 10s, sending KILL"
            kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
        fi
        stopped=1
    fi
    rm -f "$PIDFILE"
fi

# Catch an instance started some other way, or one whose pidfile was lost.
strays=()
for pid in $(pgrep -f "mesh-vnode" 2>/dev/null || true); do
    [[ $pid == "$$" ]] && continue
    is_ancestor "$pid" && continue
    is_server "$pid" || continue
    strays+=("$pid")
done

if ((${#strays[@]})); then
    echo "stopping stray instances: ${strays[*]}"
    kill -TERM "${strays[@]}" 2>/dev/null || true
    sleep 2
    for pid in "${strays[@]}"; do
        kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    done
    stopped=1
fi

((stopped)) || echo "mesh-vnode was not running"
exit 0
