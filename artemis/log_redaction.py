"""Scrub credentials out of log output (HARDEN-1 item 3).

`requests` puts the full request URL — query string included — into
HTTPError messages, and a `logger.exception(...)` then writes that URL to the
journal. OpenWeatherMap authenticates with `?appid=<key>`, so every failed
weather call leaked the key into `journalctl -u acos`.

RedactingFilter rewrites each record before any handler formats it: the
message, its args, the formatted traceback, and stack info. It is attached to
the ROOT HANDLERS (not the root logger) because logger-level filters do not
run for records propagated up from child loggers.

    from artemis.log_redaction import install
    logging.basicConfig(...)
    install()
"""

import logging
import re

REDACTED = "REDACTED"

# name=value where name is a credential-looking query/form parameter. The
# leading \b keeps "monkey=" or "turnkey=" from matching "key=".
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(appid|api_?key|apikey|key|access_token|auth_token|token|secret|password)"
    r"=([^&\s'\"<>]+)"
)


def redact_secrets(text: str) -> str:
    """Replace the value of every credential-looking `name=value` pair."""
    if not text:
        return text
    return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


class RedactingFilter(logging.Filter):
    """Redacts secrets from a record's message and exception text in place."""

    _formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken %-format must not drop the record
            message = str(record.msg)
        record.msg = redact_secrets(message)
        record.args = None

        if record.exc_info and not record.exc_text:
            # Formatter.format() reuses a pre-set exc_text, so the redacted
            # traceback is what every handler prints.
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


def install(logger: logging.Logger | None = None) -> RedactingFilter:
    """Attach one RedactingFilter to every handler of `logger` (default: root).
    Idempotent — a handler never gets two."""
    target = logger or logging.getLogger()
    flt = RedactingFilter()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(flt)
    return flt
