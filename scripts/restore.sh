#!/bin/sh
# Restore a backup made by scripts/backup.sh into a Compose deployment (docs/DEPLOYMENT.md).
#
#   scripts/restore.sh <backup directory> --yes
#
# REPLACES the current database and uploaded originals. The API and worker are stopped while it
# runs and started again afterwards; PostgreSQL must be running. Checksums are verified first.
set -eu

dir=${1:?usage: scripts/restore.sh <backup directory> --yes}
[ "${2:-}" = "--yes" ] || { echo "This replaces all data. Re-run with --yes to confirm." >&2; exit 2; }
[ -f "$dir/database.dump" ] || { echo "no database.dump in $dir" >&2; exit 2; }
(cd "$dir" && sha256sum --check --quiet SHA256SUMS) || { echo "checksum mismatch in $dir" >&2; exit 1; }

running=$(docker compose ps --services --status running)
echo "stopping the app"
for service in proxy api worker; do
    if echo "$running" | grep -qx "$service"; then docker compose stop "$service"; fi
done

echo "database <- $dir/database.dump"
docker compose exec -T postgres pg_restore -U mediaflow -d mediaflow \
    --clean --if-exists --no-owner --exit-on-error <"$dir/database.dump"

if [ -f "$dir/uploads.tar" ]; then
    echo "uploads  <- $dir/uploads.tar"
    docker compose run --rm --no-deps -T --entrypoint sh worker \
        -c 'find /app/uploads -mindepth 1 -delete && tar -C /app/uploads -xf -' <"$dir/uploads.tar"
fi

echo "starting the app"
for service in api worker proxy; do
    if echo "$running" | grep -qx "$service"; then docker compose start "$service"; fi
done
echo "restored from $dir. Redis rebuilds itself: the reconciler re-dispatches active jobs."
