"""MACHINE-SETUP — named machine positions in set notes (pure, no deps).

One definition shared by the debrief parser (artemis/health.py), the report
export and the Lambda (which bundles knowledge/, not artemis/). gym-display's
src/lib/set-notes.ts writes and reads the same tokens.

Notes tokens, in fixed order: ``seat=4; pad=3; range=2``. A legacy
``setting=N`` (the old single number) reads as the seat when no seat is given.

Debrief phrases are extracted deterministically — no LLM decides a number:
  "seat 4" / "seat at 4" / "seat #4"           → seat
  "pad 3" / "back pad 3" / "chest pad 3"       → pad
  "range 2" / "ROM 2" / "pin 7" / "range pin 7" → range
  "setting 4"                                   → seat (legacy single number)
"""

import re

SETUP_FIELDS = ("seat", "pad", "range")

_NUM = r"(\d+(?:\.\d+)?)"
_SEP = r"\s*(?:#|at|on|=|:|to|position|pos\.?)?\s*#?\s*"

# Longest words first so "range pin 7" is one match, not "pin 7" alone.
_PHRASES = (
    ("range", r"(?<!rep )(?<!reps )(?:range(?:\s+of\s+motion)?(?:\s+pin)?|rom|pin)"),
    ("pad", r"(?:(?:back|chest|thigh|knee|shin|leg|ankle|foot)\s+)?pad"),
    ("seat", r"seat(?:\s+height)?"),
    ("setting", r"setting"),
)
_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(f"(?P<{k}>{p})" for k, p in _PHRASES) + r")" + _SEP + _NUM + r"\b",
    re.I,
)


def _num(s: str) -> float | int:
    f = float(s)
    return int(f) if f.is_integer() else f


def extract_setup(text: str | None) -> dict:
    """Named positions spoken in free text. First mention of each field wins."""
    out: dict = {}
    legacy = None
    for m in _PHRASE_RE.finditer(text or ""):
        field = next(k for k, _ in _PHRASES if m.group(k))
        val = _num(m.group(len(_PHRASES) + 1))
        if field == "setting":
            legacy = val if legacy is None else legacy
        else:
            out.setdefault(field, val)
    if legacy is not None and "seat" not in out:
        out["seat"] = legacy
    return {f: out[f] for f in SETUP_FIELDS if f in out}


def strip_setup(text: str | None) -> str:
    """Free text with the setup phrases removed (and leftover separators tidied)."""
    rest = _PHRASE_RE.sub("", text or "")
    rest = re.sub(r"\s*([,;])\s*(?:[,;]\s*)*", r"\1 ", rest)
    return rest.strip(" ,;.-")


def setup_tokens(setup: dict | None) -> str:
    """``{"seat": 4, "range": 7}`` → ``"seat=4; range=7"`` (fixed order)."""
    setup = setup or {}
    return "; ".join(f"{f}={_fmt(setup[f])}" for f in SETUP_FIELDS if setup.get(f) is not None)


def _fmt(v) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def _token_re(key: str) -> re.Pattern:
    return re.compile(r"(?:^|;)\s*" + key + r"\s*=\s*(-?\d+(?:\.\d+)?)\s*(?=;|$)", re.I)


_TOKEN_RES = {f: _token_re(f) for f in (*SETUP_FIELDS, "setting")}


def parse_setup(notes: str | None) -> dict:
    """Setup tokens in a set's notes. Legacy ``setting=N`` → seat."""
    out: dict = {}
    for f in SETUP_FIELDS:
        m = _TOKEN_RES[f].search(notes or "")
        if m:
            out[f] = _num(m.group(1))
    if "seat" not in out:
        m = _TOKEN_RES["setting"].search(notes or "")
        if m:
            out["seat"] = _num(m.group(1))
    return {f: out[f] for f in SETUP_FIELDS if f in out}


def format_setup(setup: dict | None) -> str:
    """``{"seat": 5, "pad": 2}`` → ``"seat 5 · pad 2"`` (empty string if none)."""
    setup = setup or {}
    return " · ".join(f"{f} {_fmt(setup[f])}" for f in SETUP_FIELDS if setup.get(f) is not None)


def with_setup_tokens(notes: str | None, source_text: str | None) -> str | None:
    """Debrief row notes with the setup spoken in ``source_text`` (plus any the
    LLM copied into ``notes``) moved into tokens in front. Idempotent: notes that
    already carry tokens are left as they are."""
    if parse_setup(notes):
        return notes
    setup = {**extract_setup(notes), **extract_setup(source_text)}
    if not setup:
        return notes
    rest = strip_setup(notes)
    return "; ".join(p for p in (setup_tokens(setup), rest) if p)
