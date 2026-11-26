"""Production-only FCM dispatch. Claims are committed before any send."""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

ROLE = "bakery_notify"
PROJECT = "personal-notifications-ed12b"
LINK = "https://tndnjs321.github.io/ExpiryManager/"
DB_ENV = {k: v for k, v in os.environ.items() if k != "FCM_ACCESS_TOKEN"}


def db(sql):
    guard = """
    BEGIN;
    SET LOCAL statement_timeout = '20s';
    SET LOCAL search_path = pg_catalog;
    DO $guard$
    BEGIN
        IF current_user <> 'bakery_notify'
           OR session_user <> 'bakery_notify' THEN
            RAISE EXCEPTION 'Unexpected database role';
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user
                   AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)) THEN
            RAISE EXCEPTION 'Unexpected privileged database role';
        END IF;
    END
    $guard$;
    """
    try:
        result = subprocess.run(
            ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
            input=guard + sql + "\nCOMMIT;\n", text=True,
            capture_output=True, env=DB_ENV, timeout=50,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Database connection or execution failed") from None
    if result.returncode:
        # Never print raw SQL output, server error detail or FCM tokens.
        raise RuntimeError("Database command failed; check notify functions, role and TLS")
    return result.stdout.strip()


def claim():
    rows = json.loads(db("""
        SELECT coalesce(jsonb_agg(to_jsonb(d)), '[]'::jsonb)
        FROM bakery.claim_due_notifications() d;
    """))
    if not isinstance(rows, list) or len(rows) > 100:
        raise RuntimeError("Unexpected claim response")
    return rows


def uuid_value(value):
    return str(uuid.UUID(value))


def still_enabled(row):
    device = uuid_value(row["device_id"])
    store = uuid_value(row["store_id"])
    # Compare the current token in memory, never interpolate it into SQL.
    current = db(f"""
        SELECT coalesce(jsonb_agg(p.fcm_token), '[]'::jsonb)
        FROM bakery.push_devices p
        WHERE p.id = '{device}'::uuid AND p.store_id = '{store}'::uuid
          AND p.enabled IS TRUE;
    """)
    return json.loads(current) == [row["fcm_token"]]


def finish(row, status):
    if status not in ("sent", "failed", "unknown"):
        raise RuntimeError("Invalid delivery status")
    delivery = uuid_value(row["delivery_id"])
    result = db(f"SELECT bakery.finish_notification('{delivery}'::uuid, '{status}');")
    if result != "t":
        raise RuntimeError("Delivery status was not updated")


def send(row, access_token):
    counts = [row[k] for k in ("expired_count", "today_count", "upcoming_count")]
    if any(type(n) is not int or n < 0 for n in counts) or not sum(counts):
        raise ValueError("Invalid notification counts")
    body = " · ".join(f"{label} {count}건" for label, count in
                      zip(("기한 경과", "오늘까지", "2일 이내"), counts) if count)
    payload = {"message": {
        "token": row["fcm_token"],
        "notification": {"title": "유통기한 확인", "body": body},
        "webpush": {"headers": {"TTL": "300"}, "fcm_options": {"link": LINK}},
    }}
    request = urllib.request.Request(
        f"https://fcm.googleapis.com/v1/projects/{PROJECT}/messages:send",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + access_token,
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
            return "sent" if isinstance(data, dict) and data.get("name") else "unknown"
    except urllib.error.HTTPError as error:
        # No automatic retry, even after a definite rejection.
        code = error.code
        print(f"FCM HTTP {code}", flush=True)
        return "failed" if 400 <= code < 500 and code not in (408, 429) else "unknown"
    except (OSError, ValueError):
        return "unknown"


def validate_environment():
    if os.getenv("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Unexpected branch")
    if os.getenv("PGUSER") != ROLE + ".lfkxwylieimqmusjnxpx":
        raise RuntimeError("Unexpected database account")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+\.pooler\.supabase\.com", os.getenv("PGHOST", "")):
        raise RuntimeError("Unexpected database host")
    if os.getenv("PGSSLMODE") != "verify-full" or not os.path.isfile(os.getenv("PGSSLROOTCERT", "")):
        raise RuntimeError("TLS verification required")



def cleanup_preview():
    validate_environment()
    data = json.loads(db("SELECT bakery.cleanup_notification_history(true);"))
    if not isinstance(data, dict) or data.get("dry_run") is not True:
        raise RuntimeError("Unexpected cleanup response")
    for key in ("deliveries", "devices"):
        if type(data.get(key)) is not int or data[key] < 0:
            raise RuntimeError("Unexpected cleanup count")
    print("Cleanup preview: " + json.dumps(data), flush=True)
    return 0


def main():
    validate_environment()
    if sys.argv[1:] == ["--cleanup-preview"]:
        return cleanup_preview()
    if sys.argv[1:]:
        raise RuntimeError("Unexpected argument")
    access_token = os.environ.pop("FCM_ACCESS_TOKEN", "")
    if not access_token:
        raise RuntimeError("Missing FCM access token")

    rows = claim()
    print(f"New deliveries claimed: {len(rows)}", flush=True)
    results = {"sent": 0, "failed": 0, "unknown": 0, "record_errors": 0}
    for row in rows:
        status = "unknown"
        try:
            if not still_enabled(row):
                status = "failed"
                print("Device disabled or token changed; skipped", flush=True)
            else:
                status = send(row, access_token)
        except (ValueError, KeyError, TypeError, RuntimeError):
            print("Delivery precheck or processing failed", flush=True)
        results[status] += 1
        try:
            finish(row, status)
        except (RuntimeError, ValueError, KeyError, TypeError):
            results["record_errors"] += 1
            print("Could not record final status; claim remains reserved", flush=True)
    print("Delivery summary: " + json.dumps(results), flush=True)
    return 1 if any(results[k] for k in ("failed", "unknown", "record_errors")) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, KeyError, TypeError):
        # Avoid a traceback containing secret-bearing inputs.
        print("Dispatch stopped; check configuration and database functions.", file=sys.stderr)
        sys.exit(1)
