#!/usr/bin/env bash
# The writer side: dump one library and upload it to the fixed blob of its replica, when the library changed.
# The env file (phdcode-publish@<name>.service) sets PG_CONTAINER, PG_USER, PG_DB (the Postgres container of
# the repo's docker-compose.yml), AZ_ACCOUNT, AZ_CONTAINER, AZ_BLOB. The upload uses the az login of the machine.
# The dump leaves out the data of tool_calls (the calls of the writer's own users).
set -euo pipefail
state=${XDG_STATE_HOME:-$HOME/.local/state}/phdcode/publish-$PG_DB
mkdir -p "$(dirname "$state")"
q() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atc "$1"; }
# ponytail: a change that touches none of these counts and times is not seen; hash the tables if that matters
mark=$(q "select (select count(*) from papers) || ' ' || (select max(updated_at) from papers) || ' ' || (select count(*) from chunks) || ' ' || (select count(*) from paper_keywords)")
if [ -f "$state" ] && [ "$(cat "$state")" = "$mark" ]; then echo "no change: $mark"; exit 0; fi
out=$(mktemp); trap 'rm -f "$out"' EXIT
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc --exclude-table-data=tool_calls >"$out"
az storage blob upload --auth-mode login --account-name "$AZ_ACCOUNT" -c "$AZ_CONTAINER" -n "$AZ_BLOB" \
  -f "$out" --overwrite --no-progress --only-show-errors >/dev/null
echo "$mark" >"$state"
echo "published $(stat -c %s "$out") bytes: $mark"
