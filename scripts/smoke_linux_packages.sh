#!/bin/bash
# Install the built .deb and .rpm in stock distro containers and check that the
# packaged CLI actually runs from /usr/bin after a real install.
#
# Two lanes, because they carry different risk:
#
#   official  Docker Hub images. This lane gates the release, so every package
#             — including the RED OS one, which is an ordinary rpm — has to
#             prove it installs and runs here.
#   vendor    Astra Linux and RED OS images from the vendors' own registries.
#             This lane confirms the packages on the OS they target, but its
#             registries are third-party and their reachability from CI is
#             outside our control, so it runs informationally and must never
#             hold back a tagged release.
#
# Docker Hub official images are tag-pinned on purpose: they are the smoke
# environment, not a build input, and the point is to track what users actually
# run. The vendor images are pinned by digest instead.
#
# The single .deb covers Astra Linux too: Astra is Debian-based
# (ID_LIKE=debian, dpkg 1.21) and installs the same package, so it needs a
# smoke target rather than a build of its own.
#
# Usage: bash smoke_linux_packages.sh <official|vendor> <deb> <rpm> <red80-rpm>
set -euo pipefail

LANE="$1"
DEB="$2"
RPM="$3"
RPM_RED80="$4"
ASTRA_IMAGE="registry.astralinux.ru/library/astra/ubi18@sha256:694fcfd48cf152ec833caeb63dba416e7ea55d8491bf5b46dd6c29d6fbf0ede3"
RED80_IMAGE="registry.red-soft.ru/ubi8/ubi@sha256:cae37cb16daadfecae09e854471592f27bcd6aefb4b44da1e5b22bba57b1e9cd"

smoke_package() {
    local image="$1" package="$2" install_cmd="$3"
    local name
    name="$(basename "$package")"
    echo "--- Smoking $name in $image ---"
    docker run --rm \
        --volume "$(cd "$(dirname "$package")" && pwd)/$name:/tmp/$name:ro" \
        "$image" sh -c "
            set -eu
            $install_cmd /tmp/$name
            test -f /usr/share/applications/ouroboros.desktop
            test -f /usr/share/pixmaps/ouroboros.png
            ouroboros --help >/dev/null
        "
}

case "$LANE" in
    official)
        smoke_package ubuntu:24.04 "$DEB" "dpkg --install"
        smoke_package fedora:42 "$RPM" "rpm --install"
        smoke_package fedora:42 "$RPM_RED80" "rpm --install"
        ;;
    vendor)
        smoke_package "$ASTRA_IMAGE" "$DEB" "dpkg --install"
        smoke_package "$RED80_IMAGE" "$RPM_RED80" "rpm --install"
        ;;
    *)
        echo "ERROR: unknown lane: $LANE (expected 'official' or 'vendor')" >&2
        exit 2
        ;;
esac

echo "=== Linux package smoke passed ($LANE lane) ==="
