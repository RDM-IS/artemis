"""CARDIO-LOC — which cardio a session runs on, per location.

CONFIG, NOT CODE. Everything here is data Ryan can edit: the inventory, the
order modalities are preferred in, and the order devices are preferred in
within a modality. **The rower's possible move to MSP (~2026-10-04) is one line
in `INVENTORY`** — no deploy of logic, no code change, no migration.

Shape:

    INVENTORY[location_key] = ((modality, device), …)   # preference is NOT here
    MODALITY_ORDER                                      # preference IS here
    DEVICE_ORDER[modality]                              # tie-break within one

A location with an empty tuple has no cardio. That is an EXPLICIT state — see
`resolve()` — never a silent fall through to another location's equipment, the
same discipline as a plan row with no `load_config` (LOCATION-1).

Modalities are `row`, `bike`, `treadmill`, `elliptical`. The DEVICE (water,
indoor trainer, upright, recumbent) is an attribute, not a modality: one
progression per modality, the device captured for reference. Recumbent stays a
variant of `bike` (Ryan, 2026-09-25) — with `device` on every log, the heart-rate
data can revisit that once there are a few weeks of both.
"""

from __future__ import annotations

MODALITIES = ("row", "bike", "treadmill", "elliptical")

#: Preference order, most preferred first (Ryan, 2026-09-25). Rowing is primary:
#: it is the modality that carries a real progression; everything else is a
#: substitute logged against the same session target. REORDER THIS FREELY — it
#: is a preference, not a fact, and changing it changes resolution with no code
#: change (tests/test_cardio_loc.py proves that).
MODALITY_ORDER: tuple[str, ...] = ("row", "bike", "elliptical", "treadmill")

#: Tie-break within one modality, for a location that has two of the same kind.
#: The office has both bikes; upright comes first (Ryan, 2026-09-25). A device
#: not listed here sorts after the listed ones, in inventory order.
DEVICE_ORDER: dict[str, tuple[str, ...]] = {
    "bike": ("upright", "recumbent", "indoor trainer"),
}

#: What each location actually has, as (modality, device) pairs.
#: EDIT THIS, not the code, when equipment moves.
INVENTORY: dict[str, tuple[tuple[str, str], ...]] = {
    "richfield": (("row", "water"), ("bike", "indoor trainer")),
    "brown_deer": (("treadmill", "treadmill"),),
    "office": (("treadmill", "treadmill"), ("elliptical", "elliptical"),
               ("bike", "upright"), ("bike", "recumbent")),
    #: MSP home has none. The empty tuple is the point: it resolves to the
    #: explicit no-equipment state rather than borrowing the office's.
    "msp_home": (),
    "outside": (),
}

#: How a device reads on a plan row. The keys above are short and stable; these
#: are what a human sees ("upright bike", not "upright").
DEVICE_LABELS: dict[str, str] = {
    "water": "water rower",
    "indoor trainer": "road bike on indoor trainer",
    "upright": "upright bike",
    "recumbent": "recumbent bike",
    "treadmill": "treadmill",
    "elliptical": "elliptical",
}


def label_for(device: str | None) -> str:
    return DEVICE_LABELS.get(device or "", device or "")


#: Named in the no-equipment message, so the reply says what IS available
#: somewhere rather than only what is missing.
REFERENCE_LOCATION = "office"


def _device_rank(modality: str, device: str, inventory_index: int) -> tuple[int, int]:
    order = DEVICE_ORDER.get(modality, ())
    return (order.index(device) if device in order else len(order), inventory_index)


def available(location_key: str | None) -> tuple[tuple[str, str], ...]:
    """Everything this location has, as (modality, device)."""
    return INVENTORY.get(location_key or "", ())


def modalities_at(location_key: str | None) -> tuple[str, ...]:
    """The distinct modalities a location has, in preference order."""
    have = {m for m, _ in available(location_key)}
    return tuple(m for m in MODALITY_ORDER if m in have)


def device_for(location_key: str | None, modality: str) -> str | None:
    """The preferred device for a modality at a location, or None."""
    candidates = [(d, i) for i, (m, d) in enumerate(available(location_key)) if m == modality]
    if not candidates:
        return None
    return min(candidates, key=lambda c: _device_rank(modality, c[0], c[1]))[0]


def resolve(location_key: str | None) -> dict:
    """What a cardio session runs on here.

    Returns either::

        {"modality": "row", "device": "water", "available": [...],
         "is_substitute": False}

    or, for a location with no cardio, the EXPLICIT state::

        {"modality": None, "device": None, "available": [],
         "reason": "no cardio equipment at this location",
         "elsewhere": ["treadmill", "elliptical", "bike"]}

    `is_substitute` is True whenever the resolved modality is not `row`:
    a substitute runs the same session target but never advances the rowing
    baseline (see artemis/cardio_baseline.py).
    """
    have = modalities_at(location_key)
    all_here = [{"modality": m, "device": d} for m, d in available(location_key)]
    if not have:
        return {
            "modality": None,
            "device": None,
            "available": all_here,
            "reason": "no cardio equipment at this location",
            "elsewhere": list(modalities_at(REFERENCE_LOCATION)),
            "elsewhere_location": REFERENCE_LOCATION,
        }
    modality = have[0]
    return {
        "modality": modality,
        "device": device_for(location_key, modality),
        "available": all_here,
        "is_substitute": modality != "row",
    }


def describe(resolved: dict) -> str:
    """One line for a plan row's notes or a chat reply."""
    if not resolved.get("modality"):
        others = ", ".join(resolved.get("elsewhere") or []) or "nothing on record"
        return (f"{resolved.get('reason', 'no cardio equipment')} — "
                f"at the {resolved.get('elsewhere_location', 'office')}: {others}")
    device = resolved.get("device")
    shown = (resolved["modality"] if device in (None, resolved["modality"])
             else f"{resolved['modality']} ({label_for(device)})")
    return shown if not resolved.get("is_substitute") else f"{shown} — substitute for rowing"
