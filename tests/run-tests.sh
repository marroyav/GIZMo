#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
temporary=$(mktemp -d /tmp/gizmo-tests.XXXXXX)
control_pid=

cleanup()
{
    if [ -n "$control_pid" ]; then
        kill "$control_pid" 2>/dev/null || true
        wait "$control_pid" 2>/dev/null || true
    fi
    rm -rf -- "$temporary"
}
trap cleanup EXIT HUP INT TERM

for script in "$repo_root"/scripts/* "$repo_root"/packaging/*.sh "$repo_root"/packaging/maintainer-scripts/*; do
    sh -n "$script"
done
echo "ok   shell syntax"

PYTHONPYCACHEPREFIX="$temporary/pycache" python3 -m compileall -q \
    "$repo_root/src/python" "$repo_root/tests/test_zmq_commands.py"
echo "ok   Python syntax"

PYTHONPYCACHEPREFIX="$temporary/pycache" python3 "$repo_root/tests/test_zmq_commands.py"
echo "ok   command compatibility tests"

cat > "$temporary/network.env" <<'EOF'
GIZMO_NETWORK_MODE=none
EOF
GIZMO_NETWORK_CONFIG="$temporary/network.env" "$repo_root/scripts/gizmo-network-setup" \
    > "$temporary/network.log"
grep -q 'externally managed' "$temporary/network.log"
echo "ok   externally managed network mode"

if command -v systemd-socket-activate >/dev/null 2>&1; then
    socket_path="$temporary/control.sock"
    systemd-socket-activate -l "$socket_path" "$repo_root/build/gizmo-control" \
        >"$temporary/control.log" 2>&1 &
    control_pid=$!
    attempt=0
    while [ ! -S "$socket_path" ] && [ "$attempt" -lt 50 ]; do
        sleep 0.1
        attempt=$((attempt + 1))
    done
    python3 - "$socket_path" <<'PY'
import socket
import sys

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.connect(sys.argv[1])
    client.sendall(b"ping\n")
    response = client.recv(256)
if response != b"OK pong\n":
    raise SystemExit(f"unexpected control response: {response!r}")
PY
    kill "$control_pid" 2>/dev/null || true
    wait "$control_pid" 2>/dev/null || true
    control_pid=
    echo "ok   socket-activated privileged helper"
fi

if command -v rg >/dev/null 2>&1; then
    legacy_matches=$(
        rg -n '/home/ubuntu/Software|/etc/rc\.local' \
            "$repo_root/src" "$repo_root/scripts" "$repo_root/packaging/systemd" || true
    )
else
    legacy_matches=$(
        grep -R -n -E '/home/ubuntu/Software|/etc/rc\.local' \
            "$repo_root/src" "$repo_root/scripts" "$repo_root/packaging/systemd" || true
    )
fi
if [ -n "$legacy_matches" ]; then
    printf '%s\n' "$legacy_matches"
    echo "Maintained runtime still contains a legacy absolute path" >&2
    exit 1
fi
echo "ok   maintained path ownership"

verify_log="$temporary/systemd-verify.log"
set +e
systemd-analyze verify "$repo_root"/packaging/systemd/* >"$verify_log" 2>&1
verify_status=$?
set -e

# On a development host, verify reports that target-only executables such as
# xmutil and /usr/bin/gizmo-zmon are absent. Treat every other diagnostic as a
# unit-file failure.
sed -E '/Command .* is not executable: No such file or directory/d' "$verify_log" > "$temporary/systemd-unexpected.log"
if [ -s "$temporary/systemd-unexpected.log" ]; then
    cat "$temporary/systemd-unexpected.log" >&2
    exit "$verify_status"
fi
echo "ok   systemd unit syntax"

echo "All host-side tests passed"
