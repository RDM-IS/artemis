"""MEAL-NUDGE — one evening line when nothing has been logged for the day.

Capture is the bottleneck: 3 of 14 days in the first fortnight had any intake,
and an off day is empty unless Ryan logs it (pre-fill is work-days-only and an
unpicked off day correctly records `no_plan`). So: one nudge, once, late, and
only when the day is genuinely empty.

Three rules this module keeps, and they are the whole design:

* **HUMAN-GATED ACTIVATION.** This is standing automation, so it ships OFF and
  only Ryan turns it on (`meal nudge on`). Nothing decides to start nudging by
  itself — descendant of the Brad Spaits rule.
* **FAIL-CLOSED means SILENCE here**, which is the opposite of a resolver. An
  unreadable nutrition row returns None and the caller posts nothing: a false
  nudge on a day he already logged is noise he has to correct, a missed nudge
  costs nothing.
* **It never says what to eat and never guesses what was eaten.** One line,
  naming the form of a reply. No content, no estimate.
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

#: acos.system_state key. Absent or anything but "on" means OFF.
FLAG_KEY = "meal_nudge_enabled"

TEXT = ("Nothing logged for today yet — reply like `for dinner I had …` "
        "and I'll total it.")


def enabled() -> bool:
    """True only when the flag is explicitly on.

    Any read failure is False: an automation that cannot confirm it was switched
    on does not run.
    """
    try:
        from artemis.quiet_hours import get_system_value
        return (get_system_value(FLAG_KEY) or "").strip().lower() == "on"
    except Exception:
        logger.warning("MEAL-NUDGE: could not read %s — treating as off",
                       FLAG_KEY, exc_info=True)
        return False


def set_enabled(on: bool) -> None:
    from artemis.quiet_hours import set_system_value
    set_system_value(FLAG_KEY, "on" if on else "off")


def entry_count(cur, day: date) -> int | None:
    """Entries logged for `day`, or **None when the row could not be read**.

    None is not zero. The caller must treat it as "do not post" — see the
    module docstring.
    """
    try:
        cur.execute("SELECT count(*) FROM nutrition.entry WHERE day_date = %s", (day,))
        row = cur.fetchone()
        if row is None:
            return None
        return int(row[0] if not isinstance(row, dict) else list(row.values())[0])
    except Exception:
        logger.warning("MEAL-NUDGE: nutrition.entry unreadable for %s", day, exc_info=True)
        return None
