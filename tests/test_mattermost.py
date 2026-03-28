#!/usr/bin/env python3
"""Mattermost connection test — validates bot credentials and channel access via Secrets Manager."""

import sys
import os

# Ensure repo root is on the path so knowledge/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests
from knowledge.secrets import get_mattermost_credentials


def main():
    creds = get_mattermost_credentials()
    url = creds["url"].rstrip("/")
    token = creds["token"]
    channel_id = creds["channel_id"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    results = []

    # ── Test 1: Server reachable ──────────────────────────────────────
    test_name = "Mattermost server reachable (ping)"
    try:
        resp = requests.get(f"{url}/api/v4/system/ping", timeout=10)
        if resp.status_code == 200:
            results.append((test_name, True))
            print(f"  PASS  {test_name}")
        else:
            results.append((test_name, False))
            print(f"  FAIL  {test_name} — HTTP {resp.status_code}")
    except Exception as exc:
        results.append((test_name, False))
        print(f"  FAIL  {test_name} — {exc}")

    # ── Test 2: Bot token valid ───────────────────────────────────────
    test_name = "Bot token valid (users/me)"
    try:
        resp = requests.get(f"{url}/api/v4/users/me", headers=headers, timeout=10)
        if resp.status_code == 200:
            username = resp.json().get("username", "unknown")
            results.append((test_name, True))
            print(f"  PASS  {test_name} — logged in as @{username}")
        else:
            results.append((test_name, False))
            print(f"  FAIL  {test_name} — HTTP {resp.status_code}")
    except Exception as exc:
        results.append((test_name, False))
        print(f"  FAIL  {test_name} — {exc}")

    # ── Test 3: Channel accessible ────────────────────────────────────
    test_name = "Channel accessible"
    try:
        resp = requests.get(
            f"{url}/api/v4/channels/{channel_id}", headers=headers, timeout=10
        )
        if resp.status_code == 200:
            ch_name = resp.json().get("display_name", channel_id)
            results.append((test_name, True))
            print(f"  PASS  {test_name} — #{ch_name}")
        else:
            results.append((test_name, False))
            print(f"  FAIL  {test_name} — HTTP {resp.status_code}")
    except Exception as exc:
        results.append((test_name, False))
        print(f"  FAIL  {test_name} — {exc}")

    # ── Test 4: Post message ──────────────────────────────────────────
    test_name = "Post message to channel"
    try:
        payload = {
            "channel_id": channel_id,
            "message": (
                "ACOS Phase 1 complete. Mattermost connection confirmed. "
                "AWS infrastructure live."
            ),
        }
        resp = requests.post(
            f"{url}/api/v4/posts", headers=headers, json=payload, timeout=10
        )
        if resp.status_code in (200, 201):
            post_id = resp.json().get("id", "unknown")
            results.append((test_name, True))
            print(f"  PASS  {test_name} — post_id={post_id}")
        else:
            results.append((test_name, False))
            print(f"  FAIL  {test_name} — HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        results.append((test_name, False))
        print(f"  FAIL  {test_name} — {exc}")

    # ── Summary ───────────────────────────────────────────────────────
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'=' * 40}")
    print(f"  {passed}/{total} passed")
    print(f"{'=' * 40}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
