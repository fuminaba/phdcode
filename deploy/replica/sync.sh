#!/usr/bin/env bash
# Pull the newest dump of the writer (publish.sh) and swap it in. The dump is restored into phdcode_next first;
# the live database changes only if that restore worked and the paper count did not fall by more than 10 %.
# Exit 0: swapped, or no new dump (HTTP 304). Run as root (phdcode-replica-sync.service). README.md in this folder.
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE=${REPLICA_ENV:-/etc/phdcode-replica/replica.env}
DATA=${REPLICA_DATA:-/var/lib/phdcode-replica}
DUMP_URL=$(sed -n 's/^DUMP_URL=//p' "$ENV_FILE")   # the blob URL with its read SAS: never printed
dc() { docker compose --env-file "$ENV_FILE" "$@"; }
sql() { dc exec -T db psql -U phdcode -d "$1" -v ON_ERROR_STOP=1 -Atc "$2" </dev/null; }   # no stdin: exec -T reads it
mkdir -p "$DATA"; chmod 700 "$DATA"
touch "$DATA/etag"
code=$(curl -sS -o "$DATA/new.dump" -w '%{http_code}' --etag-compare "$DATA/etag" --etag-save "$DATA/etag.new" "$DUMP_URL")
if [ "$code" = 304 ]; then echo "no new dump"; exit 0; fi
if [ "$code" != 200 ]; then echo "the download answered HTTP $code" >&2; exit 1; fi
dc exec -T ollama ollama pull nomic-embed-text >/dev/null 2>&1 </dev/null
sql postgres "DROP DATABASE IF EXISTS phdcode_next"
sql postgres "CREATE DATABASE phdcode_next"
dc exec -T db pg_restore -U phdcode -d phdcode_next --no-owner --no-privileges --single-transaction <"$DATA/new.dump"
new=$(sql phdcode_next "select count(*) from papers")
old=$(sql phdcode "select count(*) from papers" 2>/dev/null || echo 0)   # 0 on the first run: no table yet
if [ "$new" -lt 1 ] || [ "$new" -lt $((old * 9 / 10)) ]; then
  echo "refused: the new dump has $new papers, the live copy $old" >&2; exit 1
fi
dc stop mcp
sql postgres "DROP DATABASE IF EXISTS phdcode_old"
sql postgres "ALTER DATABASE phdcode RENAME TO phdcode_old"
sql postgres "ALTER DATABASE phdcode_next RENAME TO phdcode"
dc start mcp
mv "$DATA/etag.new" "$DATA/etag"
rm -f "$DATA/new.dump"
echo "swapped: $new papers (was $old)"
