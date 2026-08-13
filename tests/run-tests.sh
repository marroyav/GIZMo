#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
temporary=$(mktemp -d /tmp/gizmo-tests.XXXXXX)
control_pid=
test_python=${PYTHON:-python3}

cleanup()
{
    if [ -n "$control_pid" ]; then
        kill "$control_pid" 2>/dev/null || true
        wait "$control_pid" 2>/dev/null || true
    fi
    rm -rf -- "$temporary"
}
trap cleanup EXIT HUP INT TERM

for script in \
    "$repo_root"/scripts/* \
    "$repo_root"/packaging/*.sh \
    "$repo_root"/packaging/maintainer-scripts/* \
    "$repo_root"/deploy/offboard/run-component \
    "$repo_root"/deploy/offboard/start \
    "$repo_root"/deploy/offboard/status
do
    case "$(sed -n '1p' "$script")" in
        *python*) ;;
        *bash*) bash -n "$script" ;;
        *) sh -n "$script" ;;
    esac
done
echo "ok   shell syntax"

PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" -m compileall -q \
    "$repo_root/src/python" "$repo_root/tests"
PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" -m py_compile \
    "$repo_root/scripts/gizmo-opcua-client"
echo "ok   Python syntax"

PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" \
    "$repo_root/tests/test_publication_safety.py"
echo "ok   public-tree safety checks"

PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" "$repo_root/tests/test_zmq_commands.py"
echo "ok   command compatibility tests"

PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" \
    "$repo_root/tests/test_opcua_model.py"
echo "ok   OPC UA semantic model tests"

PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" \
    "$repo_root/tests/test_dashboard.py"
echo "ok   live dashboard contract tests"

PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" \
    "$repo_root/tests/test_historian.py"
echo "ok   persistent historian tests"

if "$test_python" -c 'import numpy, opcua, zmq' >/dev/null 2>&1; then
    PYTHONPYCACHEPREFIX="$temporary/pycache" "$test_python" \
        "$repo_root/tests/test_opcua_address_space.py"
    echo "ok   OPC UA address-space integration tests"
else
    echo "skip OPC UA integration tests (runtime modules not installed on host)"
fi

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

if ! grep -q '^Wants=.*dfx-mgr\.service' \
        "$repo_root/packaging/systemd/gizmo-hardware.service" ||
    ! grep -q '^After=.*dfx-mgr\.service' \
        "$repo_root/packaging/systemd/gizmo-hardware.service" ||
    ! grep -q 'wait_for_device.*GIZMO_DFX_SOCKET' \
        "$repo_root/scripts/gizmo-hardware-setup"; then
    echo "FPGA overlay startup is not ordered after Xilinx dfx-mgr readiness" >&2
    exit 1
fi
echo "ok   Xilinx DFX startup ordering"

if ! grep -q '^TimeoutStartSec=90$' \
        "$repo_root/packaging/systemd/gizmo-opcua.service"; then
    echo "OPC UA cold-boot startup allowance regressed below the tested value" >&2
    exit 1
fi
echo "ok   OPC UA cold-boot startup allowance"

if ! grep -q 'gizmo-dashboard\.service' \
        "$repo_root/packaging/systemd/gizmo.target" ||
    ! grep -q '^ExecStart=.*/gizmo_dashboard\.py$' \
        "$repo_root/packaging/systemd/gizmo-dashboard.service" ||
    ! grep -q 'web/dashboard/\\*' "$repo_root/Makefile"; then
    echo "Live dashboard is not fully owned by the runtime package" >&2
    exit 1
fi
echo "ok   dashboard lifecycle and asset ownership"

if ! grep -q 'gizmo-historian\.service' \
        "$repo_root/packaging/systemd/gizmo.target" ||
    ! grep -q '^ExecStart=.*/gizmo_historian\.py$' \
        "$repo_root/packaging/systemd/gizmo-historian.service" ||
    ! grep -q '/var/lib/gizmo/history' \
        "$repo_root/packaging/tmpfiles/gizmo.conf"; then
    echo "Persistent historian is not fully owned by the runtime package" >&2
    exit 1
fi
echo "ok   historian lifecycle and storage ownership"

if ! grep -q '^NTP=time-relay\.example\.invalid$' \
        "$repo_root/config/60-gizmo-timesyncd.conf.example" ||
    ! grep -q 'SITE_CONFIG_ROOT.*/60-gizmo-timesyncd\.conf' \
        "$repo_root/Makefile" ||
    ! grep -q 'enable --now systemd-timesyncd\.service' \
        "$repo_root/packaging/maintainer-scripts/postinst"; then
    echo "Site-configured Kria time synchronization is not package-owned" >&2
    exit 1
fi
echo "ok   site-configured time synchronization"

if grep -q '^Requires=' "$repo_root/packaging/systemd/gizmo.target" ||
    grep -q '^Requires=.*gizmo-zmon' \
        "$repo_root/packaging/systemd/gizmo-zmq.service"; then
    echo "A ZMon restart would propagate into another lifecycle owner" >&2
    exit 1
fi
echo "ok   isolated component restarts"

echo "All host-side tests passed"
