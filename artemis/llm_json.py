"""Reading JSON out of a model response, in one place.

The same three-regex fence strip was copy-pasted into `briefs`, `health`,
`dossier` and `intent`, and all four shared the same two blind spots. One of
them cost 20 email triages in five days.

**What actually went wrong, because it was not what the log said.** Triage
logged `Failed to parse triage response: ```json` 20 times, which reads like a
fencing bug -- and the fence strip was already correct. Correlating each failure
against the `response_length` in `acos.audit_log` settled it:

    failed to parse : n=20  median 2820 chars  max 2909
    parsed fine     : n=69  median  511 chars  max 2660

The failures were the LONG responses. `max_tokens=1000` cut a pretty-printed
JSON array off mid-value, so there was no closing fence and no closing bracket.
A truncated response is not a malformed one, and reporting it as "failed to
parse" is what made the cause look like fencing for five days.

So this module does two things: it strips fences (including a trailing fence
with prose after it, which the old regex missed because it was anchored to the
end of the string), and it **names truncation as truncation**.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

#: A leading fence with an optional language tag: ```json, ```JSON, ``` .
_OPEN = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]*\r?\n?")
#: A closing fence, and anything after it -- "Hope that helps!" is not JSON.
#: The old regex was `\s*```$`, anchored to the end, so any trailing prose
#: defeated it and json.loads then failed with "Extra data".
_CLOSE = re.compile(r"\r?\n?[ \t]*```[\s\S]*$")


class LlmJsonError(ValueError):
    """A model response that could not be read as JSON.

    `truncated` is the part worth having: it lets a caller log "the response was
    cut off at max_tokens" instead of "failed to parse", which are different
    problems with different fixes.
    """

    def __init__(self, message: str, *, raw: str = "", truncated: bool = False):
        super().__init__(message)
        self.raw = raw
        self.truncated = truncated


def strip_fences(text: str) -> str:
    """Remove one leading fence (with optional language tag) and the trailing
    fence with anything following it. Deliberately nothing cleverer: guessing at
    a balanced JSON value inside prose would turn a truncated response into a
    plausible partial answer, and a partial triage is worse than none."""
    if not text:
        return ""
    out = _OPEN.sub("", text.strip(), count=1)
    out = _CLOSE.sub("", out, count=1)
    return out.strip()


def looks_truncated(raw: str) -> bool:
    """A heuristic for "the model was cut off", used only when the API did not
    tell us. Opening a fence and never closing it is the clearest sign; so is
    ending mid-structure."""
    if not raw:
        return False
    body = raw.strip()
    # Opened a fence and never closed it.
    if body.startswith("```") and body.count("```") < 2:
        return True
    # Started a JSON structure and never closed it. The "started" half matters:
    # "I cannot do that." also fails to end in `}` or `]`, and calling a refusal
    # a truncation would send the next reader looking for the wrong fix.
    inner = strip_fences(body)
    if inner[:1] in ("{", "["):
        return inner[-1:] not in ("}", "]")
    return False


def parse(raw: str, *, stop_reason: str | None = None, what: str = "response"):
    """Parse a model response as JSON, or raise `LlmJsonError`.

    `stop_reason` is the API's own answer and is believed over the heuristic:
    `max_tokens` means truncated whether or not the text looks it.
    """
    if not raw or not raw.strip():
        raise LlmJsonError(f"{what}: empty", raw=raw or "",
                           truncated=(stop_reason == "max_tokens"))
    truncated = stop_reason == "max_tokens" or (stop_reason is None and looks_truncated(raw))
    try:
        return json.loads(strip_fences(raw))
    except json.JSONDecodeError as exc:
        if truncated:
            raise LlmJsonError(
                f"{what}: the model was cut off (max_tokens) after {len(raw)} chars — "
                f"raise max_tokens rather than changing the parser",
                raw=raw, truncated=True) from exc
        raise LlmJsonError(f"{what}: {exc.msg} at char {exc.pos} of {len(raw)}",
                           raw=raw, truncated=False) from exc


def stop_reason_of(response) -> str | None:
    """The API's stop_reason, defensively — an SDK without it must not crash the
    caller that is only trying to explain a failure."""
    return getattr(response, "stop_reason", None)
