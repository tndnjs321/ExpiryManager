"""Production-only FCM dispatch. Claims are committed before any send."""
import base64
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


def is_unregistered(error):
    # A generic HTTP 404 is not enough: require the typed FCM error.
    if error.code != 404:
        return False
    try:
        payload = json.loads(error.read(65536))
        details = payload["error"]["details"]
        return isinstance(details, list) and any(
            isinstance(d, dict)
            and d.get("@type") == "type.googleapis.com/google.firebase.fcm.v1.FcmError"
            and d.get("errorCode") == "UNREGISTERED"
            for d in details
        )
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return False


def disable_unregistered(row):
    device = uuid_value(row["device_id"])
    store = uuid_value(row["store_id"])
    token = row["fcm_token"]
    if not isinstance(token, str) or not token:
        raise ValueError("Invalid device token")
    # Encode arbitrary token characters safely; never print SQL or the token.
    encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
    result = db(f"""
        SELECT bakery.disable_invalid_push_device(
            '{store}'::uuid, '{device}'::uuid,
            convert_from(decode('{encoded}', 'base64'), 'UTF8')
        );
    """)
    if result not in ("t", "f"):
        raise RuntimeError("Unexpected device update response")
    print("Unregistered device disabled" if result == "t"
          else "Unregistered token already changed or disabled", flush=True)
    return result == "t"


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
        if is_unregistered(error):
            disable_unregistered(row)
            return "unregistered"
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



def cleanup_history(dry_run=True):
    validate_environment()
    if type(dry_run) is not bool:
        raise RuntimeError("Unexpected cleanup mode")
    sql_mode = "true" if dry_run else "false"
    # Both cleanup functions run in the same database transaction.
    result = json.loads(db(f"""
        SELECT jsonb_build_object(
            'notification', bakery.cleanup_notification_history({sql_mode}),
            'login', bakery.cleanup_login_attempts({sql_mode})
        );
    """))
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected cleanup response")
    data = {"dry_run": dry_run}
    for section, keys in (("notification", ("deliveries", "devices")),
                          ("login", ("login_attempts",))):
        part = result.get(section)
        if not isinstance(part, dict) or part.get("dry_run") is not dry_run:
            raise RuntimeError("Unexpected cleanup response")
        for key in keys:
            if type(part.get(key)) is not int or part[key] < 0:
                raise RuntimeError("Unexpected cleanup count")
            data[key] = part[key]
    print(("Cleanup preview: " if dry_run else "Cleanup committed: ") + json.dumps(data), flush=True)
    return 0


def main():
    validate_environment()
    if sys.argv[1:] == ["--cleanup-preview"]:
        return cleanup_history(True)
    if sys.argv[1:] == ["--cleanup-commit"]:
        return cleanup_history(False)
    if sys.argv[1:]:
        raise RuntimeError("Unexpected argument")
    access_token = os.environ.pop("FCM_ACCESS_TOKEN", "")
    if not access_token:
        raise RuntimeError("Missing FCM access token")

    rows = claim()
    print(f"New deliveries claimed: {len(rows)}", flush=True)
    results = {"sent": 0, "failed": 0, "unknown": 0, "unregistered": 0, "skipped": 0, "record_errors": 0}
    for row in rows:
        status = "unknown"
        try:
            if not still_enabled(row):
                status = "skipped"
                print("Device disabled or token changed; skipped", flush=True)
            else:
                status = send(row, access_token)
        except (ValueError, KeyError, TypeError, RuntimeError):
            print("Delivery precheck or processing failed", flush=True)
        results[status] += 1
        try:
            finish(row, "failed" if status in ("unregistered", "skipped") else status)
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
