#!/bin/sh
# Back up a Compose deployment of MediaFlow (docs/DEPLOYMENT.md, "Backups").
#
#   scripts/backup.sh [directory]          # default: backups/<UTC timestamp>
#
# Writes database.dump (pg_dump, custom format: a consistent snapshot taken while the app runs),
# uploads.tar (the originals, when MEDIA_STORAGE=local; with S3, back up the bucket instead) and
# SHA256SUMS. Redis is not backed up: PostgreSQL is the record of truth, and after a restore the
# reconciler re-dispatches any job that was still in progress (docs/JOB_LIFECYCLE.md).
# Uses the Compose files and project of the current directory; set COMPOSE_FILE /
# COMPOSE_PROJECT_NAME as for `docker compose` to target another.
set -eu

out=${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$out"
umask 077  # backups hold everything: keep them private

echo "database -> $out/database.dump"
docker compose exec -T postgres pg_dump -U mediaflow -d mediaflow --format=custom >"$out/database.dump"

storage=$(docker compose exec -T worker sh -c 'echo "${MEDIA_STORAGE:-local}"')
if [ "$storage" = "local" ]; then
    echo "uploads  -> $out/uploads.tar"
    docker compose exec -T worker tar -C /app/uploads -cf - . >"$out/uploads.tar"
else
    echo "uploads: MEDIA_STORAGE=$storage, so originals live in the bucket; back that up with" \
         "the provider's tools (versioning or replication), not here."
fi

(cd "$out" && sha256sum ./* >SHA256SUMS)
echo "done: $(du -sh "$out" | cut -f1) in $out"
