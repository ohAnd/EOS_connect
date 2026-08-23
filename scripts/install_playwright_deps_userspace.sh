#!/usr/bin/env bash
# Make Playwright's Chromium runnable without root.
#
# `playwright install-deps` needs sudo to apt-install Chromium's shared libraries.
# Where that is not available, this fetches the same packages and unpacks them into
# ~/.cache/ms-playwright-deps/lib, which tests/web/conftest.py adds to LD_LIBRARY_PATH
# on its own. Nothing outside your home directory is touched.
#
# Usage:  scripts/install_playwright_deps_userspace.sh
set -euo pipefail

PREFIX="${HOME}/.cache/ms-playwright-deps"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# Chromium headless shell needs these beyond a base Ubuntu install. If a future build
# reports another missing .so, add its package here — `ldd <chrome-binary>` names them.
PACKAGES=(libnspr4 libnss3 libasound2t64)

echo "Downloading: ${PACKAGES[*]}"
( cd "${WORK}" && apt-get download "${PACKAGES[@]}" )

echo "Unpacking into ${PREFIX}"
mkdir -p "${PREFIX}/lib"
for deb in "${WORK}"/*.deb; do
    dpkg -x "${deb}" "${WORK}/root"
done
cp -r "${WORK}"/root/usr/lib/*/. "${PREFIX}/lib/"

echo "Done. ${PREFIX}/lib now holds:"
ls "${PREFIX}/lib" | sed 's/^/  /' | head -20
echo
echo "Now run:  pytest tests/web"
