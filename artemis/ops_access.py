"""OPS-2 — Cloudflare Access enforcement for the ops API (the security gate).

The ops UI (ops.rdm.is) is a Cloudflare Pages SPA behind Cloudflare Access (OTP/SSO
locked to ryan@rdm.is — the same pattern as gym.rdm.is). The box API it calls sits
behind the SAME Access application (via a cloudflared tunnel), so every browser
request carries a Cloudflare-signed identity JWT in the `Cf-Access-Jwt-Assertion`
header (or the `CF_Authorization` cookie). This module validates that JWT on the
API side, so a request that did not pass Access is rejected even if it reached the
origin directly — closing the old exposure (a static API key embedded in the client
bundle, readable by anyone). No secret ships in the browser bundle anymore.

Validation: RS256 signature against the Access team's JWKS
(`https://<team>/cdn-cgi/access/certs`), plus `aud` == the Application Audience tag
and `iss` == the team domain, plus exp/nbf. Service-token access (machine callers)
presents the same Access JWT shape, so this one code path covers both.

Config (env, set on the box — see the OPS-2 deploy runbook):
    CF_ACCESS_TEAM_DOMAIN   e.g. rdmis.cloudflareaccess.com
    CF_ACCESS_AUD           the Access Application Audience (AUD) tag

FAIL CLOSED: if either is unset, or the token is missing/invalid/expired, the
request is denied. There is no anonymous read of any panel and no anonymous mutation.
"""

import functools
import logging
import os

from flask import g, jsonify, request

logger = logging.getLogger(__name__)

# Cloudflare injects the identity JWT under this header at the edge. The cookie is
# the fallback the browser sets on the Access-protected origin.
_JWT_HEADER = "Cf-Access-Jwt-Assertion"
_JWT_COOKIE = "CF_Authorization"


class AccessError(Exception):
    """Raised when an Access token is missing, malformed, or fails validation.

    `status` distinguishes a missing credential (401) from a present-but-invalid one
    (403). A configuration gap is treated as 403 (deny) — never as allow."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def _team_domain() -> str:
    return (os.environ.get("CF_ACCESS_TEAM_DOMAIN") or "").strip().rstrip("/")


def _aud() -> str:
    return (os.environ.get("CF_ACCESS_AUD") or "").strip()


def is_configured() -> bool:
    """True when both Access env vars are present. When False the API fails closed."""
    return bool(_team_domain() and _aud())


# PyJWKClient is cached per team domain so we don't refetch JWKS every request; the
# client itself caches signing keys and refreshes on an unknown `kid`.
_jwks_clients: dict = {}


def _jwks_client(team_domain: str):
    import jwt  # lazy: keeps the bot importable even if PyJWT isn't installed yet

    client = _jwks_clients.get(team_domain)
    if client is None:
        url = f"https://{team_domain}/cdn-cgi/access/certs"
        client = jwt.PyJWKClient(url)
        _jwks_clients[team_domain] = client
    return client


def verify_access_token(token: str) -> dict:
    """Validate a Cloudflare Access JWT and return its claims, or raise AccessError.

    Isolated (not a Flask concern) so tests can exercise it directly and the
    decorator can be monkeypatched. Signature + aud + iss + exp are all enforced by
    jwt.decode; a bad token raises."""
    if not is_configured():
        raise AccessError("Cloudflare Access is not configured (CF_ACCESS_* unset)", status=403)
    try:
        import jwt
    except Exception as exc:  # PyJWT missing on the box → deny, don't allow
        raise AccessError(f"Access verification unavailable: {exc}", status=403)

    team_domain = _team_domain()
    try:
        signing_key = _jwks_client(team_domain).get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_aud(),
            issuer=f"https://{team_domain}",
        )
    except AccessError:
        raise
    except Exception as exc:
        # jwt.* raises many subclasses (ExpiredSignature, InvalidAudience, …); collapse
        # to a single deny so we never leak which check failed.
        raise AccessError(f"invalid Access token: {exc}", status=403)


def _extract_token() -> str | None:
    tok = request.headers.get(_JWT_HEADER)
    if tok:
        return tok.strip()
    return request.cookies.get(_JWT_COOKIE)


def require_access(fn):
    """Flask decorator: reject any request that did not pass Cloudflare Access.

    401 when no token is presented; 403 when a token is present but invalid (or Access
    is misconfigured). On success the verified identity (email) is stashed on
    flask.g.access_email for audit attribution."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # CORS preflight carries no credentials by design — let it through so the
        # browser can learn the allowed methods/headers (the after_request CORS hook
        # answers it). The actual GET/POST that follows still requires a valid token.
        if request.method == "OPTIONS":
            return ("", 204)
        token = _extract_token()
        if not token:
            return jsonify({"error": "unauthorized", "detail": "no Cloudflare Access token"}), 401
        try:
            claims = verify_access_token(token)
        except AccessError as exc:
            logger.warning("ops API access denied: %s", exc)
            return jsonify({"error": "forbidden", "detail": str(exc)}), exc.status
        g.access_email = claims.get("email") or claims.get("common_name") or "unknown"
        g.access_claims = claims
        return fn(*args, **kwargs)

    return wrapper


def current_actor() -> str:
    """The verified caller identity for audit rows, or 'ops-ui' as a safe default."""
    return getattr(g, "access_email", None) or "ops-ui"
