"""Owner-only operational notifications; no tokens or raw errors in logs."""
import datetime as dt
import json
import os
import pathlib
import runpy
import sys
import urllib.error
import urllib.parse
import urllib.request

notify = runpy.run_path(str(pathlib.Path(__file__).with_name("notify-prod.py")))


def owner_devices():
    rows = json.loads(notify["db"]("""
        SELECT coalesce(jsonb_agg(to_jsonb(d)), '[]'::jsonb)
        FROM bakery.get_owner_system_devices() d;
    """))
    if not isinstance(rows, list):
        raise RuntimeError("Invalid device response")
    return rows


def is_automatic_run():
    return (os.getenv("GITHUB_EVENT_NAME") == "schedule"
            or os.getenv("AUTOMATIC_RUN", "").lower() == "true")


def should_report_dispatch_failure():
    # Best-effort suppression of repeating failures within the same UTC hour.
    # A new failure following a successful run is always reported.
    if not is_automatic_run():
        return True
    token = os.getenv("GH_TOKEN")
    if not token:
        return True
    try:
        url = ("https://api.github.com/repos/tndnjs321/ExpiryManager/actions/"
               "workflows/notify-prod.yml/runs?"
               + urllib.parse.urlencode({"branch": "main", "status": "completed",
                                         "per_page": 30}))
        request = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request, timeout=10) as response:
            runs = json.load(response)["workflow_runs"]
        current_id = int(os.environ["GITHUB_RUN_ID"])
        prior = next((r for r in runs if r["id"] < current_id
                      and (r.get("event") == "schedule"
                           or (r.get("event") == "workflow_dispatch"
                               and r.get("display_title") == "Cloudflare automatic - notify-prod"))), None)
        if prior and prior["conclusion"] == "failure":
            then = dt.datetime.fromisoformat(prior["run_started_at"].replace("Z", "+00:00"))
            now = dt.datetime.now(dt.timezone.utc)
            if then.replace(minute=0, second=0, microsecond=0) == now.replace(minute=0, second=0, microsecond=0):
                return False
    except (OSError, ValueError, KeyError, TypeError):
        print("Failure suppression unavailable; attempting alert", flush=True)
    return True


def message(mode, store):
    if mode == "dispatch-failure":
        return "유통기한 알림 처리 오류", "일부 발송 또는 시스템 처리가 실패했습니다. GitHub Actions를 확인하세요."
    if mode == "cleanup-failure":
        return "알림 이력 정리 오류", "정리 작업이 실패했거나 결과를 확인하지 못했습니다. GitHub Actions를 확인하세요."
    if mode != "backup":
        raise ValueError("Invalid notification mode")
    verified = os.getenv("BACKUP_VERIFIED") == "true"
    if os.getenv("BACKUP_OUTCOME") != "success":
        if verified:
            return "백업 완료 · 후속 처리 오류", "R2 저장 및 무결성 검증 완료. 자동 정리 또는 결과 확인에 실패했습니다."
        return "백업 오류", "R2 백업 저장 또는 검증 완료를 확인하지 못했습니다. GitHub Actions를 확인하세요."
    if not verified:
        return "백업 결과 확인 오류", "백업 검증 결과가 누락되었습니다. GitHub Actions를 확인하세요."
    mode = os.getenv("CLEANUP_MODE")
    if mode not in ("commit", "rollback"):
        return "백업 결과 확인 오류", "백업 검증 완료. 정리 결과를 확인하지 못했습니다."
    manual = not is_automatic_run()
    title = "수동 백업 완료" if manual else "자동 백업 완료"
    body = "R2 저장 · 무결성 검사 정상"
    if store == os.getenv("SNAPSHOT_STORE"):
        products, items, count = [int(os.environ[k]) for k in
                                  ("PRODUCTS_COUNT", "ITEMS_COUNT", "CLEANUP_COUNT")]
        if min(products, items, count) < 0:
            raise ValueError("Invalid backup summary")
        body += f" · 상품 {products}건 · 기록 {items}건"
        body += f" · 자동 정리 {count}건" if mode == "commit" else f" · 정리 미적용(대상 {count}건)"
    else:
        body += " · 자동 정리 완료" if mode == "commit" else " · 정리 미적용"
    return title, body


def send(row, title, body, token):
    payload = {"message": {
        "token": row["fcm_token"],
        "notification": {"title": title, "body": body},
        "webpush": {"headers": {"TTL": "3600"},
                    "fcm_options": {"link": notify["LINK"]}},
    }}
    req = urllib.request.Request(
        "https://fcm.googleapis.com/v1/projects/" + notify["PROJECT"] + "/messages:send",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            result = json.load(response)
        return "sent" if isinstance(result, dict) and result.get("name") else "failed"
    except urllib.error.HTTPError as error:
        print(f"System FCM HTTP {error.code}", flush=True)
        if notify["is_unregistered"](error):
            notify["disable_unregistered"](row)
            return "unregistered"
        return "failed"


def main():
    notify["validate_environment"]()
    if len(sys.argv) != 2 or sys.argv[1] not in ("backup", "dispatch-failure", "cleanup-failure"):
        raise ValueError("Invalid mode")
    mode = sys.argv[1]
    if mode == "dispatch-failure" and not should_report_dispatch_failure():
        print("Repeated scheduled failure suppressed within current hour", flush=True)
        return 0
    token = os.environ.pop("FCM_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("Missing access token")
    # Do not pass GitHub API credentials to psql.
    notify["DB_ENV"].pop("GH_TOKEN", None)
    rows = owner_devices()
    seen = set()
    sent = failed = skipped = unregistered = 0
    for row in rows:
        device = row["device_id"]
        if device in seen:
            continue
        seen.add(device)
        try:
            # Recheck the owner role, enabled state and token just before sending.
            if row not in owner_devices():
                skipped += 1
                continue
            title, body = message(mode, row["store_id"])
            outcome = send(row, title, body, token)
            if outcome == "sent":
                sent += 1
            elif outcome == "unregistered":
                unregistered += 1
            else:
                failed += 1
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            failed += 1
    print(f"Owner system alerts: sent={sent}, failed={failed}, skipped={skipped}, unregistered={unregistered}", flush=True)
    if not rows:
        print("No enabled owner devices", flush=True)
    return int(failed > 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        print("Owner alert processing failed; inspect configuration and workflow status.", file=sys.stderr)
        sys.exit(1)
