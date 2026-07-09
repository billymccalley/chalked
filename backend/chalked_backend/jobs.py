from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def settle() -> int:
    return call_system_job("settle", "ChalkedSettlementJob/0.1")


def backup() -> int:
    return call_system_job("backup", "ChalkedBackupJob/0.1")


def call_system_job(name: str, user_agent: str) -> int:
    public_url = os.environ.get("CHALKED_PUBLIC_URL", "").rstrip("/")
    secret = os.environ.get("CHALKED_CRON_SECRET", "")
    if not public_url or not secret:
        print("CHALKED_PUBLIC_URL and CHALKED_CRON_SECRET are required", file=sys.stderr)
        return 2
    request = urllib.request.Request(
        f"{public_url}/api/system/{name}",
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
        },
        data=b"{}",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"{name.title()} job failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    command = args[0] if args else ""
    if command == "settle":
        return settle()
    if command == "backup":
        return backup()
    print("Usage: python -m backend.chalked_backend.jobs settle|backup", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
