#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
version=$(tr -d '[:space:]' < "$repo_root/VERSION")
architecture=$(dpkg --print-architecture)
package_root="$repo_root/build/package-root"
output="$repo_root/build/gizmo-runtime_${version}_${architecture}.deb"
python_for_build=${PYTHON_FOR_BUILD:-python3}
bundle_python_deps=${BUNDLE_PYTHON_DEPS:-1}

case "$package_root" in
    "$repo_root"/build/package-root) ;;
    *)
        echo "Refusing unexpected package root: $package_root" >&2
        exit 1
        ;;
esac

rm -rf -- "$package_root"
mkdir -p "$package_root/DEBIAN"

make -C "$repo_root" all
make -C "$repo_root" DESTDIR="$package_root" install

if [ "$bundle_python_deps" = 1 ]; then
    python_minor=$(
        "$python_for_build" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
    )
    if [ "$python_minor" != 3.10 ]; then
        echo "Bundled runtime dependencies require Python 3.10 (found $python_minor)." >&2
        echo "Build natively on Ubuntu 22.04/Kria, or use BUNDLE_PYTHON_DEPS=0 for structural tests." >&2
        exit 1
    fi

    mkdir -p "$package_root/usr/lib/gizmo/python"
    if [ -n "${WHEELHOUSE:-}" ]; then
        if [ "$architecture" = arm64 ]; then
            (
                cd "$WHEELHOUSE"
                sha256sum -c "$repo_root/packaging/wheelhouse-arm64.sha256"
            )
        fi
        PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
            "$python_for_build" -m pip install \
            --ignore-installed \
            --no-compile \
            --no-index \
            --find-links "$WHEELHOUSE" \
            --target "$package_root/usr/lib/gizmo/python" \
            -r "$repo_root/requirements-runtime.txt"
    else
        PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
            "$python_for_build" -m pip install \
            --ignore-installed \
            --no-compile \
            --target "$package_root/usr/lib/gizmo/python" \
            -r "$repo_root/requirements-runtime.txt"
    fi
fi

installed_size=$(du -sk "$package_root" | awk '{print $1}')
cat > "$package_root/DEBIAN/control" <<EOF
Package: gizmo-runtime
Version: $version
Section: science
Priority: optional
Architecture: $architecture
Maintainer: Manuel Arroyave <marroyav@users.noreply.github.com>
Depends: iproute2, python3 (>= 3.10), python3 (<< 3.11), systemd, udev
Description: GIZMo Kria slow-control runtime
 Owns FPGA overlay loading, PS-port network configuration, ZMon,
 the front-panel display, ZeroMQ, temperature, SDR, and OPC-UA services.
Installed-Size: $installed_size
EOF

cat > "$package_root/DEBIAN/conffiles" <<'EOF'
/etc/gizmo/hardware.env
/etc/gizmo/network.env
/etc/gizmo/runtime.env
EOF

install -m 0755 "$repo_root/packaging/maintainer-scripts/postinst" "$package_root/DEBIAN/postinst"
install -m 0755 "$repo_root/packaging/maintainer-scripts/prerm" "$package_root/DEBIAN/prerm"
install -m 0755 "$repo_root/packaging/maintainer-scripts/postrm" "$package_root/DEBIAN/postrm"

(
    cd "$package_root"
    find . -type f ! -path './DEBIAN/*' -printf '%P\0' \
        | sort -z \
        | xargs -0 -r md5sum
) > "$package_root/DEBIAN/md5sums"

dpkg-deb --root-owner-group --build "$package_root" "$output"
echo "$output"
