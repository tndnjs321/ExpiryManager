#!/usr/bin/env bash
set -euo pipefail
umask 077

# This test never mutates the database.
[[ "${GITHUB_REF:-}" == "refs/heads/develop" ]] || { echo "Refusing non-develop execution"; exit 1; }
: "${PGHOST:?Missing DB_HOST}" "${PGUSER:?Missing DB_USER}" "${PGPASSWORD:?Missing DB_PASSWORD}"
: "${AWS_ACCESS_KEY_ID:?Missing R2_ACCESS_KEY_ID}" "${AWS_SECRET_ACCESS_KEY:?Missing R2_SECRET_ACCESS_KEY}"
: "${R2_ENDPOINT:?Missing R2_ENDPOINT}" "${R2_BUCKET_NAME:?Missing R2_BUCKET_NAME}"
[[ "$PGUSER" == "bakery_dev_backup.lfkxwylieimqmusjnxpx" ]] || { echo "Unexpected database user"; exit 1; }
[[ "$PGHOST" =~ ^[a-zA-Z0-9.-]+\.pooler\.supabase\.com$ ]] || { echo "Unexpected database host format"; exit 1; }
[[ "$R2_ENDPOINT" =~ ^https://[a-zA-Z0-9.-]+\.r2\.cloudflarestorage\.com/?$ ]] || { echo "Unexpected R2 endpoint format"; exit 1; }
[[ "$R2_BUCKET_NAME" == "personal-backups" ]] || { echo "Unexpected R2 bucket"; exit 1; }
export PGPORT=5432 PGDATABASE=postgres PGSSLMODE=verify-full
export PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt PGCONNECT_TIMEOUT=20
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
unset PGPASSWORD
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
echo "No database records were deleted."
