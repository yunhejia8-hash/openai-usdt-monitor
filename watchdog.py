import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LATEST_PATH = Path("output/latest.json")
STALE_TRIGGER_MINUTES = 50
MONITOR_WORKFLOW = "monitor.yml"


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def snapshot_age_minutes(latest, now):
    ts = latest.get("generated_at_utc")
    if not ts:
        return None
    return (now - parse_ts(ts)).total_seconds() / 60


def should_dispatch(age_minutes, active_runs, force=False):
    stale = age_minutes is None or age_minutes >= STALE_TRIGGER_MINUTES
    return (force or stale) and active_runs == 0


def api_request(url, token, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "openai-usdt-monitor-watchdog",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def main():
    latest = {}
    try:
        latest = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    now = datetime.now(timezone.utc)
    age = snapshot_age_minutes(latest, now)
    print(f"snapshot_age_minutes={age if age is not None else 'missing'}")

    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")

    runs = api_request(f"{api}/repos/{repo}/actions/workflows/{MONITOR_WORKFLOW}/runs?per_page=20", token)
    active = sum(1 for run in runs.get("workflow_runs", []) if run.get("status") in {"queued", "in_progress"})
    print(f"active_monitor_runs={active}")

    force = os.environ.get("WATCHDOG_FORCE", "false").lower() == "true"
    print(f"watchdog_force={force}")
    if not should_dispatch(age, active, force=force):
        print("watchdog_action=none")
        return

    api_request(
        f"{api}/repos/{repo}/actions/workflows/{MONITOR_WORKFLOW}/dispatches",
        token,
        method="POST",
        payload={"ref": "main", "inputs": {"trigger_source": "watchdog"}},
    )
    print("watchdog_action=dispatched")


if __name__ == "__main__":
    main()
