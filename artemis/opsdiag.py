"""OPS-1 — deterministic operational self-diagnosis.

Self-diagnosis is deterministic CLASSIFICATION, never LLM troubleshooting. A KNOWN
failure class maps to a runbook rendered verbatim: what failed, the literal error,
and the exact remediation commands. An UNKNOWN failure surfaces the raw error
labeled `unclassified` — never guessed at, never narrated around. This is the
no-fabrication gate applied to operational state.

Remediation commands are machine-labeled `Mac:` / `EC2 (ssh rdmis):` per repo
convention so the reader knows exactly where to run each one. `classify()` is pure
(string/type matching over the exception + a small context dict) and `Runbook.render`
is pure; the only side effect — an `acos.audit_log` row per failure — is written by
`report_failure()`, the single entry point callers use, so failures are queryable
history rather than just chat scroll.

Adding a class is: write a matcher regex, add a `Runbook`, append the pair to
`_RUNBOOKS`. Order matters — the FIRST matcher that fires wins, so put the more
specific patterns ahead of the generic ones (a Postgres "connection refused" must
classify as rds-unreachable before a bare network matcher could grab it).
"""

import logging
import re
import subprocess
from dataclasses import dataclass

from knowledge.db import log_audit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runbook model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Runbook:
    failure_class: str
    summary: str
    remediation: str  # exact commands, machine-labeled Mac: / EC2 (ssh rdmis):

    def render(self, raw_error: str) -> str:
        """Verbatim runbook: what failed · the literal error · exact remediation."""
        return (
            f"⚠️ **{self.summary}** (`{self.failure_class}`)\n"
            f"Error: `{raw_error}`\n\n"
            f"**Fix:**\n{self.remediation}"
        )


# ---------------------------------------------------------------------------
# Seeded runbooks (remediation text is the source of truth for the brief too)
# ---------------------------------------------------------------------------

VAULT_PAT_AUTH = Runbook(
    failure_class="vault-pat-auth",
    summary="Vault git auth failed — PAT invalid, expired, or org-approval pending",
    remediation=(
        "1. Regenerate the token: GitHub → Settings → Developer settings → "
        "Fine-grained tokens → `acos-vault-readonly` "
        "(repo RDM-IS/vault, Contents: read-only). Confirm org approval isn't pending.\n"
        "2. Mac: aws secretsmanager put-secret-value --profile rdmis-admin "
        "--secret-id acos/vault-repo --secret-string "
        "'{\"clone_url\":\"https://github.com/RDM-IS/vault.git\",\"token\":\"github_pat_NEW\"}'\n"
        "3. Retry `vault sync`."
    ),
)

VAULT_SECRET_MISSING = Runbook(
    failure_class="vault-secret-missing",
    summary="Vault repo secret missing or malformed (needs clone_url + token)",
    remediation=(
        "1. Create the secret with the correct shape (use `put-secret-value` if it "
        "already exists):\n"
        "   Mac: aws secretsmanager create-secret --profile rdmis-admin "
        "--name acos/vault-repo --secret-string "
        "'{\"clone_url\":\"https://github.com/RDM-IS/vault.git\",\"token\":\"github_pat_XXX\"}'\n"
        "2. Retry `vault sync`."
    ),
)

VAULT_CLONE_NETWORK = Runbook(
    failure_class="vault-clone-network",
    summary="Vault clone/fetch network failure (DNS / timeout / unreachable)",
    remediation=(
        "1. Check the box has egress and GitHub is up (https://www.githubstatus.com):\n"
        "   EC2 (ssh rdmis): curl -sS -o /dev/null -w '%{http_code}\\n' https://github.com\n"
        "2. Retry `vault sync` once connectivity is confirmed."
    ),
)

GOOGLE_OAUTH_REFRESH = Runbook(
    failure_class="google-oauth-refresh",
    summary="Google (Gmail/Calendar) OAuth token refresh failed",
    remediation=(
        "1. Re-mint tokens with the OAuth setup flow:\n"
        "   EC2 (ssh rdmis): cd ~/artemis && /usr/bin/python3.11 setup_oauth.py\n"
        "2. Gmail/Calendar API access also depends on the Google Workspace "
        "subscription being active — confirm the Workspace account isn't suspended "
        "for billing before assuming a token problem."
    ),
)

RDS_UNREACHABLE = Runbook(
    failure_class="rds-unreachable",
    summary="RDS unreachable (connection refused / timeout)",
    remediation=(
        "1. Confirm the RDS instance is available (not stopped/rebooting) in the "
        "RDS console.\n"
        "2. Check the security group allows the box, and that `.env` RDS_HOST points "
        "at the live endpoint:\n"
        "   EC2 (ssh rdmis): grep RDS_HOST .env"
    ),
)

_RUNBOOKS_BY_CLASS = {
    rb.failure_class: rb for rb in (
        VAULT_PAT_AUTH, VAULT_SECRET_MISSING, VAULT_CLONE_NETWORK,
        GOOGLE_OAUTH_REFRESH, RDS_UNREACHABLE,
    )
}


def runbook_for(failure_class: str) -> Runbook | None:
    """Fetch a seeded runbook by class (the morning brief reuses the vault-pat-auth
    remediation block for its expiry warning — one source of truth)."""
    return _RUNBOOKS_BY_CLASS.get(failure_class)


# ---------------------------------------------------------------------------
# Matchers — the FIRST that fires wins; specific before generic
# ---------------------------------------------------------------------------

_PAT_AUTH_RE = re.compile(
    r"authentication failed|invalid username or password|could not read username|"
    r"terminal prompts disabled|http (?:401|403)|\b401 unauthorized|\b403 forbidden|"
    r"permission denied \(publickey|remote: (?:repository not found|invalid|permission)",
    re.IGNORECASE,
)
_SECRET_MISSING_RE = re.compile(
    r"resourcenotfound|secretsmanager.*(?:can.?t find|not.*found)|"
    r"can.?t find the specified secret|missing (?:clone_url|token)|"
    r"no such secret|could not find secret",
    re.IGNORECASE,
)
_CLONE_NETWORK_RE = re.compile(
    r"could ?n.?t resolve host|could not resolve host|temporary failure in name resolution|"
    r"network is unreachable|connection timed out|operation timed out|timed out|"
    r"failed to connect to github|no route to host|ssl.*handshake|"
    r"could not resolve proxy",
    re.IGNORECASE,
)
_OAUTH_RE = re.compile(
    r"invalid_grant|token has been expired or revoked|refresherror|invalid_client|"
    r"invalid_rapt|reauth related error|deleted_client",
    re.IGNORECASE,
)
_RDS_RE = re.compile(
    r"could not connect to server|connection refused|operationalerror|"
    r"could not translate host name|psycopg2|password authentication failed for user|"
    r"the database system is (?:starting up|shutting down)|remaining connection slots",
    re.IGNORECASE,
)


def _match(pattern: re.Pattern, text: str) -> bool:
    return bool(pattern.search(text or ""))


# order is load-bearing (see module docstring): specific → generic.
_RUNBOOKS: list[tuple] = [
    (lambda t, e, c: _match(_SECRET_MISSING_RE, t), VAULT_SECRET_MISSING),
    (lambda t, e, c: _match(_PAT_AUTH_RE, t), VAULT_PAT_AUTH),
    (lambda t, e, c: _match(_OAUTH_RE, t), GOOGLE_OAUTH_REFRESH),
    (lambda t, e, c: _match(_RDS_RE, t), RDS_UNREACHABLE),
    # network last: it must not shadow an RDS "connection refused" / a Postgres timeout.
    (lambda t, e, c: _match(_CLONE_NETWORK_RE, t) or isinstance(e, subprocess.TimeoutExpired),
     VAULT_CLONE_NETWORK),
]


# ---------------------------------------------------------------------------
# Error-text extraction (with secret scrubbing) + classification
# ---------------------------------------------------------------------------

_PAT_SCRUB_RE = re.compile(r"github_pat_[A-Za-z0-9_]+")


def _scrub(text: str) -> str:
    """Never let a token reach chat or the audit log, even if a subprocess echoed
    one (it shouldn't — the vault mirror keeps the PAT out of argv/URL — but be safe)."""
    return _PAT_SCRUB_RE.sub("github_pat_***", text or "")


def _error_text(exc: BaseException, context: dict | None) -> str:
    """Full haystack for matching: the exception, its subprocess stderr/stdout, and
    any explicit context fields a caller passed (stderr/stdout/detail)."""
    parts = [str(exc)]
    if isinstance(exc, subprocess.CalledProcessError):
        parts.append(exc.stderr or "")
        parts.append(exc.stdout or "")
    for key in ("stderr", "stdout", "detail"):
        val = (context or {}).get(key)
        if val:
            parts.append(str(val))
    return _scrub("\n".join(p for p in parts if p))


def _raw_error(exc: BaseException, context: dict | None) -> str:
    """The literal error to show the operator — prefer the informative subprocess
    stderr over the opaque `returned non-zero exit status N`."""
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        return _scrub(exc.stderr.strip())
    detail = (context or {}).get("detail")
    if detail:
        return _scrub(str(detail).strip())
    return _scrub(str(exc).strip()) or exc.__class__.__name__


def classify(exception: BaseException, context: dict | None = None) -> Runbook | None:
    """Map an exception (+ optional context) to a Runbook, or None if unknown. Pure:
    no I/O, no audit write. Callers that want the audit trail use report_failure()."""
    text = _error_text(exception, context)
    for matcher, rb in _RUNBOOKS:
        try:
            if matcher(text, exception, context):
                return rb
        except Exception:  # a matcher must never turn a diagnosis into a crash
            logger.debug("opsdiag matcher raised for %s", rb.failure_class, exc_info=True)
    return None


def report_failure(exception: BaseException, context: dict | None = None,
                   *, agent: str = "ops") -> str:
    """Classify → write an acos.audit_log row → return the runbook message (or the
    unclassified raw-error message). The single entry point: every failure it sees
    is BOTH surfaced to chat and recorded as queryable history. The audit write is
    best-effort (a logging failure must not swallow the operational reply)."""
    context = context or {}
    raw = _raw_error(exception, context)
    rb = classify(exception, context)
    failure_class = rb.failure_class if rb else "unclassified"
    try:
        log_audit(
            agent=agent, action="failure", domain="ops",
            outcome="classified" if rb else "unclassified",
            metadata={
                "failure_class": failure_class,
                "stage": context.get("stage"),
                "error": raw[:1000],
            },
        )
    except Exception:
        logger.debug("opsdiag: audit write failed", exc_info=True)

    if rb:
        return rb.render(raw)
    return (
        "⚠️ **Unclassified failure**\n"
        f"Error: `{raw}`\n\n"
        "_no runbook for this class yet._"
    )
