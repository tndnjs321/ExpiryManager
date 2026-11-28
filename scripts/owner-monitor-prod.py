"""Low-frequency production monitoring; never log credentials or response bodies."""
import json
import os
import pathlib
import re
import runpy
import sys
import urllib.error
import urllib.request
import uuid

URL = "https://lfkxwylieimqmusjnxpx.supabase.co/functions/v1/bakery-owner-monitor-job"
METRICS = {
    "db_bytes", "workers_requests", "r2_bytes", "r2_class_a", "r2_class_b",
    "github_token", "supabase_token", "db_query", "workers_query",
    "r2_storage_query", "r2_analytics_query",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(key, payload):
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"x-owner-monitor-key": key, "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=40) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise RuntimeError("Monitor response too large")
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Monitor endpoint HTTP {exc.code}") from None
    except (OSError, ValueError):
        raise RuntimeError("Monitor request or response failed") from None
    if not isinstance(data, dict):
        raise RuntimeError("Invalid monitor response")
    return data


def validate_alerts(alerts):
    if not isinstance(alerts, list) or len(alerts) > 20:
        raise RuntimeError("Invalid alert list")
    seen = set()
    for a in alerts:
        if not isinstance(a, dict) or a.get("metric") not in METRICS:
            raise RuntimeError("Invalid alert metric")
        event = str(uuid.UUID(a["event_id"]))
        if event in seen or type(a.get("level")) is not int or not 1 <= a["level"] <= 5:
            raise RuntimeError("Invalid or duplicate alert")
        seen.add(event)
        if not all(isinstance(a.get(k), str) and 0 < len(a[k]) <= limit
                   for k, limit in (("title", 150), ("body", 1000))):
            raise RuntimeError("Invalid alert message")
    return alerts


def alert_path():
    return pathlib.Path(os.environ["RUNNER_TEMP"]) / "expiry-owner-monitor-alerts.json"


def check(key, preview=False):
    data = request(key, {"mode": "preview", "force_r2": True} if preview else {"mode": "check"})
    if data.get("mode") != ("preview" if preview else "check"):
        raise RuntimeError("Invalid monitor mode")
    rows = data.get("observations")
    failures = data.get("failed_metrics")
    if (not isinstance(rows, list) or not rows or len(rows) > 20
            or not isinstance(failures, list)
            or not all(isinstance(m, str) and m in METRICS for m in failures)
            or type(data.get("r2_checked")) is not bool):
        raise RuntimeError("Invalid monitor observations")
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or row.get("metric") not in METRICS
                or row["metric"] in seen or type(row.get("level")) is not int
                or not 0 <= row["level"] <= 5):
            raise RuntimeError("Invalid observation")
        seen.add(row["metric"])
        print(f"Monitor {row['metric']}: level={row['level']}", flush=True)
    required = {"github_token", "supabase_token", "db_query", "workers_query"}
    if data["r2_checked"]:
        required |= {"r2_storage_query", "r2_analytics_query"}
    if not required <= seen or (preview and not data["r2_checked"]):
        raise RuntimeError("Incomplete monitor response")
    print(f"Monitor R2 checked={data['r2_checked']}; query failures={len(failures)}", flush=True)
    if preview:
        print("Preview only: no state change, no FCM send", flush=True)
        return int(bool(failures))
    alerts = validate_alerts(data.get("alerts"))
    # Runner-local file, never an artifact or cache.
    path = alert_path()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w",
                   encoding="utf-8") as f:
        json.dump(alerts, f, ensure_ascii=False)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write(f"alert_count={len(alerts)}\nfailed_count={len(failures)}\n")
    print(f"Reserved warnings: {len(alerts)}", flush=True)
    return 0


def deliver(key, token):
    path = alert_path()
    with path.open(encoding="utf-8") as f:
        alerts = validate_alerts(json.load(f))
    results = []
    system = None
    rows = []
    if token:
        try:
            # Credentials were removed from os.environ before loading these modules.
            system = runpy.run_path(str(pathlib.Path(__file__).with_name("notify-system-prod.py")))
            system["notify"]["validate_environment"]()
            rows = system["owner_devices"]()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            system = None
            print("Owner device lookup failed", flush=True)
    for alert in alerts:
        if not token or system is None:
            status = "failed"
        else:
            sent = failed = unknown = 0
            seen = set()
            for row in rows:
                try:
                    device = str(uuid.UUID(row["device_id"]))
                    if device in seen:
                        continue
                    seen.add(device)
                    # Recheck owner role and current enabled token before every send.
                    if row not in system["owner_devices"]():
                        continue
                    outcome = system["send"](row, alert["title"], alert["body"], token)
                    if outcome == "sent":
                        sent += 1
                    elif outcome != "unregistered":
                        failed += 1
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    unknown += 1
            status = ("partial" if sent and (failed or unknown) else "sent" if sent
                      else "unknown" if unknown else "failed" if failed else "no_device")
        results.append({"event_id": alert["event_id"], "status": status})
        print(f"Owner monitor alert {alert['metric']}: {status}", flush=True)
    if results:
        data = request(key, {"mode": "finish", "results": results})
        if data.get("recorded") != len(results) or data.get("failed") != 0:
            raise RuntimeError("Alert status recording incomplete")
    path.unlink(missing_ok=True)
    return int(any(r["status"] != "sent" for r in results))


def main():
    if os.getenv("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Refusing non-main execution")
    key = os.environ.pop("OWNER_MONITOR_JOB_KEY", "")
    token = os.environ.pop("FCM_ACCESS_TOKEN", "")
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise RuntimeError("Missing or invalid monitoring key")
    mode = sys.argv[1:]
    if mode == ["preview"]:
        return check(key, preview=True)
    if mode == ["check"]:
        return check(key)
    if mode == ["send"]:
        return deliver(key, token)
    raise RuntimeError("Invalid mode")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        # RuntimeError messages above contain only fixed text or HTTP status.
        print(str(exc) if type(exc) is RuntimeError else "Monitor processing failed",
              file=sys.stderr, flush=True)
        sys.exit(1)
