"""SSL cert, domain expiry, and uptime webhook receiver."""

import logging
import socket
import ssl
from datetime import datetime, timezone

from artemis import config

logger = logging.getLogger(__name__)


def check_ssl_expiry(domain: str) -> dict:
    """Check SSL certificate expiry for a domain.

    Returns dict with 'domain', 'expiry_date', 'days_remaining', 'status'.
    """
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as sock:
            sock.settimeout(10)
            sock.connect((domain, 443))
            cert = sock.getpeercert()

        expiry_str = cert["notAfter"]  # e.g. 'Mar 15 12:00:00 2026 GMT'
        expiry_date = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc
        )
        days_remaining = (expiry_date - datetime.now(timezone.utc)).days

        status = "ok"
        if days_remaining < 7:
            status = "critical"
        elif days_remaining < 30:
            status = "warning"

        return {
            "domain": domain,
            "expiry_date": expiry_date.isoformat(),
            "days_remaining": days_remaining,
            "status": status,
        }
    except Exception as e:
        logger.exception("SSL check failed for %s", domain)
        return {
            "domain": domain,
            "expiry_date": None,
            "days_remaining": -1,
            "status": "error",
            "error": str(e),
        }


def check_all_ssl() -> list[dict]:
    """Check SSL for all monitored domains."""
    results = []
    for domain in config.MONITORED_DOMAINS:
        results.append(check_ssl_expiry(domain))
    return results


def check_domain_expiry() -> list[dict]:
    """Check domain registration expiry from config."""
    results = []
    today = datetime.now(timezone.utc).date()

    for domain, expiry_str in config.DOMAIN_EXPIRY_DATES.items():
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            days_remaining = (expiry_date - today).days

            status = "ok"
            if days_remaining < 14:
                status = "critical"
            elif days_remaining < 60:
                status = "warning"

            results.append({
                "domain": domain,
                "expiry_date": expiry_str,
                "days_remaining": days_remaining,
                "status": status,
            })
        except ValueError:
            logger.error("Invalid expiry date for %s: %s", domain, expiry_str)
            results.append({
                "domain": domain,
                "expiry_date": expiry_str,
                "days_remaining": -1,
                "status": "error",
                "error": "Invalid date format",
            })

    return results


def format_ssl_alerts(results: list[dict]) -> str:
    """Format SSL check results into alert text. Returns empty string if all ok."""
    alerts = [r for r in results if r["status"] in ("warning", "critical", "error")]
    if not alerts:
        return ""

    lines = []
    for r in alerts:
        if r["status"] == "error":
            lines.append(f"- **{r['domain']}**: SSL check failed — {r.get('error', 'unknown')}")
        else:
            emoji = "\u26a0\ufe0f" if r["status"] == "warning" else "\ud83d\udea8"
            lines.append(
                f"- {emoji} **{r['domain']}**: SSL expires in {r['days_remaining']} days"
            )
    return "\n".join(lines)


def format_domain_alerts(results: list[dict]) -> str:
    """Format domain expiry results into alert text. Returns empty string if all ok."""
    alerts = [r for r in results if r["status"] in ("warning", "critical", "error")]
    if not alerts:
        return ""

    lines = []
    for r in alerts:
        if r["status"] == "error":
            lines.append(f"- **{r['domain']}**: domain expiry check error")
        else:
            emoji = "\u26a0\ufe0f" if r["status"] == "warning" else "\ud83d\udea8"
            lines.append(
                f"- {emoji} **{r['domain']}**: domain expires in {r['days_remaining']} days ({r['expiry_date']})"
            )
    return "\n".join(lines)
