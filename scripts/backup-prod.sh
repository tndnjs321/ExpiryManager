#!/usr/bin/env bash
set -euo pipefail
umask 077

# Read-only snapshot and explicitly enabled cleanup; default is rollback.
[[ "${GITHUB_REF:-}" == "refs/heads/main" ]] || { echo "Refusing non-main execution"; exit 1; }
CLEANUP_MODE="${CLEANUP_MODE:-rollback}"
case "$CLEANUP_MODE" in rollback|commit) ;; *) echo "Invalid cleanup mode"; exit 1 ;; esac
: "${PGHOST:?Missing DB_HOST}" "${PGUSER:?Missing DB_USER}" "${PGPASSWORD:?Missing DB_PASSWORD}"
: "${AWS_ACCESS_KEY_ID:?Missing R2_ACCESS_KEY_ID}" "${AWS_SECRET_ACCESS_KEY:?Missing R2_SECRET_ACCESS_KEY}"
: "${R2_ENDPOINT:?Missing R2_ENDPOINT}" "${R2_BUCKET_NAME:?Missing R2_BUCKET_NAME}"
[[ "$PGUSER" == "bakery_backup.lfkxwylieimqmusjnxpx" ]] || { echo "Unexpected database user"; exit 1; }
[[ "$PGHOST" =~ ^[a-zA-Z0-9.-]+\.pooler\.supabase\.com$ ]] || { echo "Unexpected database host format"; exit 1; }
[[ "$R2_ENDPOINT" =~ ^https://[a-zA-Z0-9.-]+\.r2\.cloudflarestorage\.com/?$ ]] || { echo "Unexpected R2 endpoint format"; exit 1; }
[[ "$R2_BUCKET_NAME" == "personal-backups" ]] || { echo "Unexpected R2 bucket"; exit 1; }
export PGPORT=5432 PGDATABASE=postgres PGSSLMODE=verify-full
cert_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/certs" && pwd)"
export PGSSLROOTCERT="$cert_dir/prod-ca-2021.crt" PGCONNECT_TIMEOUT=20
[[ -s "$PGSSLROOTCERT" ]] || { echo "Supabase CA certificate missing"; exit 1; }
openssl x509 -in "$PGSSLROOTCERT" -noout -checkend 0 >/dev/null
export AWS_DEFAULT_REGION=auto AWS_EC2_METADATA_DISABLED=true AWS_PAGER=""
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
command -v psql >/dev/null
command -v aws >/dev/null
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
file="$tmp/backup.json"
echo "Checking DB role and creating read-only snapshot"
psql -X -qAt -v ON_ERROR_STOP=1 > "$file" <<'SQL'
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = pg_catalog;
DO $check$
BEGIN
    IF current_user <> 'bakery_backup' THEN
        RAISE EXCEPTION 'Unexpected effective database role';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = current_user
          AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole)
    ) THEN
        RAISE EXCEPTION 'Unexpected privileged database role';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('bakery_dev','bakery','bakery_dev_private','bakery_private','wage','common','auth')
          AND c.relkind IN ('r','p','v','m','f')
          AND (
              (n.nspname <> 'bakery' OR c.relname NOT IN ('stores','products','expiry_items'))
              AND has_table_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR
              n.nspname = 'bakery' AND c.relname IN ('stores','products','expiry_items')
              AND (has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                   OR NOT row_security_active(c.oid))
          )
    ) THEN
        RAISE EXCEPTION 'Unexpected effective table privileges or inactive RLS';
    END IF;
END
$check$;
SELECT jsonb_build_object(
    'format','expiry-manager-db-backup',
    'version',1,
    'environment','production',
    'snapshotAt',transaction_timestamp(),
    'tables',jsonb_build_object(
        'stores',(SELECT coalesce(jsonb_agg(to_jsonb(s) ORDER BY s.id),'[]'::jsonb) FROM bakery.stores s),
        'products',(SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.id),'[]'::jsonb) FROM bakery.products p),
        'expiry_items',(SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY e.store_id,e.id),'[]'::jsonb) FROM bakery.expiry_items e)
    )
);
COMMIT;
SQL
python3 - "$file" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    backup = json.load(f)
assert backup["format"] == "expiry-manager-db-backup"
assert backup["environment"] == "production"
tables = backup["tables"]
assert set(tables) == {"stores", "products", "expiry_items"}
assert all(isinstance(v, list) for v in tables.values())
stores = {s["id"] for s in tables["stores"]}
assert stores, "No stores visible to backup role"
products = {(p["store_id"], p["id"]) for p in tables["products"]}
assert all(p[0] in stores for p in products)
assert all(e["store_id"] in stores and (e["store_id"], e["product_id"]) in products for e in tables["expiry_items"])
print("DB snapshot validated")
PY
date_kst="$(TZ=Asia/Seoul date +%F)"
key="bakery/production/$date_kst/${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}.json"
digest="$(sha256sum "$file" | cut -d ' ' -f 1)"
echo "Uploading private R2 backup"
aws s3api put-object --endpoint-url "$R2_ENDPOINT" --bucket "$R2_BUCKET_NAME" --key "$key" --body "$file" --content-type application/json --if-none-match '*' --metadata "sha256=$digest" >/dev/null
aws s3api get-object --endpoint-url "$R2_ENDPOINT" --bucket "$R2_BUCKET_NAME" --key "$key" "$tmp/verified.json" >/dev/null
cmp -s "$file" "$tmp/verified.json" || { echo "R2 read-back integrity check failed"; exit 1; }
[[ "$(sha256sum "$tmp/verified.json" | cut -d ' ' -f 1)" == "$digest" ]]
echo "R2 upload and SHA-256 verification succeeded: $key"
# Only publish counts from the verified snapshot. Avoid cross-store counts in pushes.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    python3 - "$tmp/verified.json" <<'PY'
import json, os, sys, uuid
with open(sys.argv[1], encoding="utf-8") as f:
    tables = json.load(f)["tables"]
with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
    out.write("backup_verified=true\n")
    if len(tables["stores"]) == 1:
        store = str(uuid.UUID(tables["stores"][0]["id"]))
        out.write(f"snapshot_store={store}\n")
        out.write(f"products_count={len(tables['products'])}\n")
        out.write(f"items_count={len(tables['expiry_items'])}\n")
PY
fi
# Use only the verified object bytes, never a fresh DB query, for cleanup targets.
echo "Preparing cleanup from verified backup (mode: $CLEANUP_MODE)"
python3 - "$tmp/verified.json" "$tmp/cleanup.sql" "$tmp/count" "$CLEANUP_MODE" <<'PY'
import base64
import datetime as dt
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    backup = json.load(f)
snapshot = dt.datetime.fromisoformat(backup["snapshotAt"].replace("Z", "+00:00"))
if snapshot.tzinfo is None:
    raise ValueError("Snapshot timestamp must include a timezone")
cutoff = snapshot.astimezone(dt.timezone.utc) - dt.timedelta(hours=360)
targets = [
    e for e in backup["tables"]["expiry_items"]
    if e["status"] in ("sold", "discarded")
    and e["processed_at"] is not None
    and dt.datetime.fromisoformat(e["processed_at"].replace("Z", "+00:00")) <= cutoff
]
if len(targets) > 50000:
    raise ValueError("Cleanup target limit exceeded")
payload = base64.b64encode(json.dumps({
    "snapshotAt": backup["snapshotAt"],
    "targets": targets
}, ensure_ascii=False).encode("utf-8")).decode("ascii")
# Base64 has no SQL quoting characters. The SQL contains no raw record values.
sql = """BEGIN;
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = pg_catalog;
WITH payload AS (
    SELECT convert_from(decode('%s', 'base64'), 'UTF8')::jsonb AS data
)
SELECT bakery.cleanup_history_auto(
    (data->>'snapshotAt')::timestamptz,
    data->'targets'
) FROM payload;
%s;
""" % (payload, {"rollback": "ROLLBACK", "commit": "COMMIT"}[sys.argv[4]])
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write(sql)
with open(sys.argv[3], "w", encoding="utf-8") as f:
    f.write(str(len(targets)))
PY
echo "Eligible backed-up records: $(cat "$tmp/count")"
# Only the selected mode can commit. The production test workflow defaults to rollback.
if ! psql -X -qAt -v ON_ERROR_STOP=1 -v VERBOSITY=verbose \
    < "$tmp/cleanup.sql" > "$tmp/cleanup-result" 2> "$tmp/cleanup-error"; then
    echo "Cleanup execution failed; inspect database and verified backup before retrying."
    grep -oE 'ERROR:  [0-9A-Z]{5}:' "$tmp/cleanup-error" | head -n 1 || true
    exit 1
fi
count="$(cat "$tmp/cleanup-result")"
[[ "$count" =~ ^[0-9]+$ ]] && [[ "$count" -le "$(cat "$tmp/count")" ]] || { echo "Unexpected cleanup result"; exit 1; }
unset PGPASSWORD
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf 'cleanup_mode=%s\ncleanup_count=%s\n' "$CLEANUP_MODE" "$count" >> "$GITHUB_OUTPUT"
fi
if [[ "$CLEANUP_MODE" == "commit" ]]; then
    echo "COMMIT completed. Deleted records: $count"
else
    echo "Cleanup integration test succeeded; matched records: $count"
    echo "ROLLBACK completed. No database records were deleted."
fi

