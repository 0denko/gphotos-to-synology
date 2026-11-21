#!/usr/bin/env bash
set -euo pipefail

PUID="${PUID:-1026}"
PGID="${PGID:-100}"

# Create/update runtime group and user to match Synology DSM ownership
if ! getent group appgroup >/dev/null 2>&1; then
    groupadd -o -g "${PGID}" appgroup
else
    groupmod -o -g "${PGID}" appgroup
fi

if ! id -u appuser >/dev/null 2>&1; then
    useradd -o -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin appuser
else
    usermod -o -u "${PUID}" -g "${PGID}" appuser
fi

# Ensure writable application directories exist
mkdir -p "${DATA_DIR:-/data/config}" "${WORK_DIR:-/data/work}" "${INBOX_DIR:-/data/inbox}" "${PHOTOS_DIR:-/data/photos}"
chown -R "${PUID}:${PGID}" "${DATA_DIR:-/data/config}" "${WORK_DIR:-/data/work}" "${INBOX_DIR:-/data/inbox}" || true

exec gosu "${PUID}:${PGID}" "$@"
