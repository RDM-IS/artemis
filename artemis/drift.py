"""DRIFT-ALARM — is what is RUNNING what `main` says it should be?

Three components, three independent checks:

    box           the running sha in ~/artemis          vs origin/main
    lambda        `sha=` in the Lambda's Description    vs origin/main
    gym_display   gym-display.pages.dev/version.json    vs the sha CI wrote to SSM

Written 2026-09-25 after the Pages build had been failing silently since
2026-09-23: the tests were green, the site was two merges stale, and nothing
said so. The lesson is that a check which cannot fail is not a check — so a
target this module cannot reach is reported as `unknown`, never as "no drift".

Every check returns a result instead of raising. `check_all()` wraps each one,
so a single broken check still reports the other two.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

OK = "ok"
DRIFT = "drift"
UNKNOWN = "unknown"

REPO_DIR = "/home/ec2-user/artemis"
LAMBDA_NAME = "rdmis-crm-api"
LAMBDA_REGION = "us-east-1"
GYM_DISPLAY_VERSION_URL = "https://gym-display.pages.dev/version.json"
# gym.rdm.is sits behind Cloudflare Access and answers 302 to anything
# unauthenticated, so the check reads the Pages origin, which is public.

# CI-1: gym-display's CI writes main's head here on every push to main, over
# GitHub OIDC. The box reads it with ssm:GetParameter on this one parameter —
# no GitHub credential, no PAT to expire. (Both stored GitHub tokens were dead
# when this was written, which is exactly the failure mode being designed out.)
MAIN_SHA_PARAMETER = "/artemis/gym-display/main-sha"
HTTP_TIMEOUT = 15


def _short(sha: str | None) -> str:
    return (sha or "")[:7] or "—"


def _result(component: str, state: str, detail: str,
            live: str | None = None, expected: str | None = None) -> dict:
    return {"component": component, "state": state, "detail": detail,
            "live": live, "expected": expected}


def _git(*args: str, cwd: str = REPO_DIR) -> str:
    """git, with a timeout. Raises on failure — callers turn that into UNKNOWN."""
    out = subprocess.run(("git", *args), cwd=cwd, capture_output=True, text=True,
                         timeout=HTTP_TIMEOUT)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def origin_main_sha(cwd: str = REPO_DIR) -> str:
    """The sha `origin/main` points at RIGHT NOW, read from the remote.

    `git rev-parse origin/main` would answer from the last fetch, which is
    exactly the staleness this alarm exists to catch.
    """
    line = _git("ls-remote", "origin", "main", cwd=cwd)
    if not line:
        raise RuntimeError("ls-remote returned nothing for main")
    return line.split()[0]


def gym_display_main_sha(parameter: str = MAIN_SHA_PARAMETER) -> str:
    """`main`'s head for gym-display, as its CI last published it to SSM.

    A parameter that does not exist yet means CI has not pushed to main since
    this was set up — that is UNKNOWN (the caller says so), never "no drift".
    """
    import boto3
    client = boto3.client("ssm", region_name=LAMBDA_REGION)
    value = client.get_parameter(Name=parameter)["Parameter"]["Value"].strip()
    if len(value) < 7:
        raise RuntimeError(f"{parameter} holds {value!r}, not a sha")
    return value


def fetch_version_json(url: str = GYM_DISPLAY_VERSION_URL) -> dict:
    """The served /version.json, PARSED.

    Before the 2026-09-25 build fix this path returned the SPA fallback — HTML
    with a 200 — so a status check alone would have called a stale site healthy.
    The body has to parse as JSON and carry a sha, or this raises.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "artemis-drift-alarm"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read(64_000).decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except ValueError:
        raise RuntimeError(f"{url} did not return JSON (first bytes: {body[:40]!r})") from None
    if not isinstance(data, dict) or not data.get("sha"):
        raise RuntimeError(f"{url} returned JSON without a sha: {body[:60]!r}")
    return data


def parse_lambda_sha(description: str | None) -> str | None:
    """The `sha=<40 hex>` token deploy.sh writes into the Description."""
    for part in (description or "").split():
        if part.startswith("sha=") and len(part) > 4:
            return part[4:]
    return None


def lambda_description() -> str | None:
    import boto3
    client = boto3.client("lambda", region_name=LAMBDA_REGION)
    return client.get_function_configuration(FunctionName=LAMBDA_NAME).get("Description")


# ── the three checks ───────────────────────────────────────────────────────

def check_box(expected: str) -> dict:
    live = _git("rev-parse", "HEAD")
    if live == expected:
        return _result("box", OK, f"on {_short(live)}", live, expected)
    return _result("box", DRIFT, f"running {_short(live)}, main is {_short(expected)}",
                   live, expected)


def check_lambda(expected: str) -> dict:
    description = lambda_description()
    live = parse_lambda_sha(description)
    if not live:
        return _result("lambda", UNKNOWN,
                       f"Description carries no sha= token ({(description or '')[:40]!r})",
                       None, expected)
    if live == expected:
        return _result("lambda", OK, f"deployed from {_short(live)}", live, expected)
    return _result("lambda", DRIFT, f"deployed from {_short(live)}, main is {_short(expected)}",
                   live, expected)


def check_gym_display() -> dict:
    expected = gym_display_main_sha()
    live = fetch_version_json()["sha"]
    if live == expected:
        return _result("gym_display", OK, f"serving {_short(live)}", live, expected)
    return _result("gym_display", DRIFT, f"serving {_short(live)}, main is {_short(expected)}",
                   live, expected)


def check_all() -> list[dict]:
    """All three checks. One that blows up becomes UNKNOWN; the others still run."""
    results: list[dict] = []
    try:
        artemis_main = origin_main_sha()
    except Exception as exc:                                   # noqa: BLE001
        reason = f"could not read origin/main ({type(exc).__name__}: {exc})"
        return [_result("box", UNKNOWN, reason), _result("lambda", UNKNOWN, reason),
                _safe(check_gym_display, "gym_display")]
    results.append(_safe(check_box, "box", artemis_main))
    results.append(_safe(check_lambda, "lambda", artemis_main))
    results.append(_safe(check_gym_display, "gym_display"))
    return results


def _safe(fn, component: str, *args) -> dict:
    try:
        return fn(*args)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning("Drift check %s failed: %s: %s", component, type(exc).__name__, exc)
        return _result(component, UNKNOWN, f"{type(exc).__name__}: {str(exc)[:120]}")


# ── reporting ──────────────────────────────────────────────────────────────

def signature(results: list[dict]) -> str:
    """The once-per-STATE guard key.

    Keyed on what is wrong, not on the date: the same drift must not be posted
    every hour, but a NEW drift (or a recovery) must be posted the hour it
    appears. Shas are included, so redeploying the wrong commit still reports.
    """
    return ";".join(
        f"{r['component']}={r['state']}:{_short(r.get('live'))}>{_short(r.get('expected'))}"
        for r in sorted(results, key=lambda r: r["component"]))


def all_clear(results: list[dict]) -> bool:
    return all(r["state"] == OK for r in results)


def format_message(results: list[dict]) -> str:
    """The Mattermost post. Data only — what is live, what main says."""
    icon = {OK: "✅", DRIFT: "⚠️", UNKNOWN: "❔"}
    lines = ["**Deploy drift**"]
    for r in sorted(results, key=lambda r: r["component"]):
        lines.append(f"{icon.get(r['state'], '❔')} `{r['component']}` — {r['detail']}")
    if any(r["state"] == UNKNOWN for r in results):
        lines.append("_`unknown` means the check could not reach its target — "
                     "not that the component is current._")
    return "\n".join(lines)
