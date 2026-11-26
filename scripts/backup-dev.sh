#!/usr/bin/env bash
set -euo pipefail
umask 077

# Read-only snapshot, rollback test, and narrowly scoped fixture-only commit test.
[[ "${GITHUB_REF:-}" == "refs/heads/develop" ]] || { echo "Refusing non-develop execution"; exit 1; }
: "${PGHOST:?Missing DB_HOST}" "${PGUSER:?Missing DB_USER}" "${PGPASSWORD:?Missing DB_PASSWORD}"
: "${AWS_ACCESS_KEY_ID:?Missing R2_ACCESS_KEY_ID}" "${AWS_SECRET_ACCESS_KEY:?Missing R2_SECRET_ACCESS_KEY}"
: "${R2_ENDPOINT:?Missing R2_ENDPOINT}" "${R2_BUCKET_NAME:?Missing R2_BUCKET_NAME}"
[[ "$PGUSER" == "bakery_dev_backup.lfkxwylieimqmusjnxpx" ]] || { echo "Unexpected database user"; exit 1; }
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
    IF current_user <> 'bakery_dev_backup' THEN
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
        WHERE n.nspname IN ('bakery_dev','bakery','bakery_dev_private','wage','common','auth')
          AND c.relkind IN ('r','p','v','m','f')
          AND (
              (n.nspname <> 'bakery_dev' OR c.relname NOT IN ('stores','products','expiry_items'))
              AND has_table_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR
              n.nspname = 'bakery_dev' AND c.relname IN ('stores','products','expiry_items')
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
    'environment','develop',
    'snapshotAt',transaction_timestamp(),
    'tables',jsonb_build_object(
        'stores',(SELECT coalesce(jsonb_agg(to_jsonb(s) ORDER BY s.id),'[]'::jsonb) FROM bakery_dev.stores s),
        'products',(SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.id),'[]'::jsonb) FROM bakery_dev.products p),
        'expiry_items',(SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY e.store_id,e.id),'[]'::jsonb) FROM bakery_dev.expiry_items e)
    )
);
COMMIT;
SQL
python3 - "$file" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    backup = json.load(f)
assert backup["format"] == "expiry-manager-db-backup"
assert backup["environment"] == "develop"
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
key="bakery/develop/$date_kst/${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}.json"
digest="$(sha256sum "$file" | cut -d ' ' -f 1)"
echo "Uploading private R2 backup"
aws s3api put-object --endpoint-url "$R2_ENDPOINT" --bucket "$R2_BUCKET_NAME" --key "$key" --body "$file" --content-type application/json --if-none-match '*' --metadata "sha256=$digest" >/dev/null
aws s3api get-object --endpoint-url "$R2_ENDPOINT" --bucket "$R2_BUCKET_NAME" --key "$key" "$tmp/verified.json" >/dev/null
cmp -s "$file" "$tmp/verified.json" || { echo "R2 read-back integrity check failed"; exit 1; }
[[ "$(sha256sum "$tmp/verified.json" | cut -d ' ' -f 1)" == "$digest" ]]
echo "R2 upload and SHA-256 verification succeeded: $key"
# Use only the verified object bytes, never a fresh DB query, for cleanup targets.
echo "Preparing cleanup test from verified backup"
python3 - "$tmp/verified.json" "$tmp/cleanup.sql" "$tmp/count" <<'PY'
import base64
import datetime as dt
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    backup = json.load(f)
snapshot = dt.datetime.fromisoformat(backup["snapshotAt"].replace("Z", "+00:00"))
if snapshot.tzinfo is None:
    raise ValueError("Snapshot timestamp must include a timezone")
cutoff = snapshot - dt.timedelta(days=15)
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
SELECT bakery_dev.cleanup_history_auto(
    (data->>'snapshotAt')::timestamptz,
    data->'targets'
) FROM payload;
ROLLBACK;
""" % payload
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write(sql)
with open(sys.argv[3], "w", encoding="utf-8") as f:
    f.write(str(len(targets)))
PY
echo "Eligible backed-up records: $(cat "$tmp/count")"
# Test the actual function and its permissions, but never commit a deletion.
if ! psql -X -qAt -v ON_ERROR_STOP=1 -v VERBOSITY=verbose \
    < "$tmp/cleanup.sql" > "$tmp/cleanup-result" 2> "$tmp/cleanup-error"; then
    echo "Cleanup integration test failed; transaction was not committed."
    grep -oE 'ERROR:  [0-9A-Z]{5}:' "$tmp/cleanup-error" | head -n 1 || true
    exit 1
fi
count="$(cat "$tmp/cleanup-result")"
[[ "$count" =~ ^[0-9]+$ ]] && [[ "$count" -le "$(cat "$tmp/count")" ]] || { echo "Unexpected cleanup result"; exit 1; }
echo "Cleanup integration test succeeded; matched records: $count"
echo "ROLLBACK completed. No database records were deleted."

# One-time commit test: only the five explicitly created development fixtures.
# Never commit deletion of ordinary inventory in this test workflow.
python3 - "$tmp/verified.json" "$tmp/fixture-test.sql" <<'PY'
import base64
import datetime as dt
import json
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    backup = json.load(f)
store = "2c2fc998-349d-4633-9163-166033975a1c"
products = {
    p["id"] for p in backup["tables"]["products"]
    if p["store_id"] == store and p["name"] == "자동정리 검증용 상품"
}
fixtures = [
    e for e in backup["tables"]["expiry_items"]
    if e["store_id"] == store and e["product_id"] in products
    and e["id"].startswith("auto-test-")
]
expected_delete = {"16일 전 판매완료": "sold", "16일 전 폐기": "discarded"}
expected_keep = {"14일 전 판매완료": "sold", "14일 전 폐기": "discarded", "관리 중 유지": "active"}
actual = {e["note"]: e["status"] for e in fixtures}
if len(fixtures) == 3 and actual == expected_keep:
    print("Fixture commit test already completed; skipping")
    sys.exit(0)
if not fixtures:
    print("No test fixtures; skipping commit test")
    sys.exit(0)
if len(products) != 1 or len(fixtures) != 5 or actual != (expected_delete | expected_keep):
    raise ValueError("Unexpected test fixture set; no deletion committed")
snapshot = dt.datetime.fromisoformat(backup["snapshotAt"].replace("Z", "+00:00"))
cutoff = snapshot - dt.timedelta(hours=360)
targets = [e for e in fixtures if e["note"] in expected_delete]
keepers = [e for e in fixtures if e["note"] in expected_keep]
assert all(e["processed_at"] and dt.datetime.fromisoformat(e["processed_at"].replace("Z", "+00:00")) <= cutoff for e in targets)
assert all((e["status"] == "active" and e["processed_at"] is None) or (e["status"] != "active" and e["processed_at"] and dt.datetime.fromisoformat(e["processed_at"].replace("Z", "+00:00")) > cutoff) for e in keepers)
payload = base64.b64encode(json.dumps({
    "snapshotAt": backup["snapshotAt"], "targets": targets,
    "keepers": [e["id"] for e in keepers], "store": store
}, ensure_ascii=False).encode("utf-8")).decode("ascii")
sql = """BEGIN;
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = pg_catalog;
DO $fixture$
DECLARE
    b jsonb := convert_from(decode('%s', 'base64'), 'UTF8')::jsonb;
    n integer;
BEGIN
    n := bakery_dev.cleanup_history_auto((b->>'snapshotAt')::timestamptz,b->'targets');
    IF n <> 2 THEN
        RAISE EXCEPTION 'Expected exactly two fixture deletions';
    END IF;
    IF EXISTS (
        SELECT 1 FROM bakery_dev.expiry_items e
        WHERE e.store_id = (b->>'store')::uuid
          AND e.id IN (SELECT x->>'id' FROM jsonb_array_elements(b->'targets') x)
    ) THEN
        RAISE EXCEPTION 'Deleted fixture still exists';
    END IF;
    IF (SELECT count(*) FROM bakery_dev.expiry_items e
        WHERE e.store_id = (b->>'store')::uuid
          AND e.id IN (SELECT jsonb_array_elements_text(b->'keepers'))) <> 3 THEN
        RAISE EXCEPTION 'Expected three preserved fixtures';
    END IF;
END
$fixture$;
COMMIT;
""" % payload
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write(sql)
PY
if [[ -s "$tmp/fixture-test.sql" ]]; then
    echo "Running commit test restricted to two development fixtures"
    if ! psql -X -qAt -v ON_ERROR_STOP=1 -v VERBOSITY=verbose \
        < "$tmp/fixture-test.sql" > "$tmp/fixture-result" 2> "$tmp/fixture-error"; then
        echo "Fixture commit test failed; transaction was not committed."
        grep -oE 'ERROR:  [0-9A-Z]{5}:' "$tmp/fixture-error" | head -n 1 || true
        exit 1
    fi
    echo "Fixture COMMIT succeeded: 2 deleted, 3 preserved"
fi
unset PGPASSWORD
