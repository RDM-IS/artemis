"""Weather wrapper — pre-departure forecast and the outdoor-walk decision.

Uses the OpenWeatherMap FREE endpoints (the key is not subscribed to One Call
3.0, which answered 401 on every call):

    /data/2.5/weather    current conditions
    /data/2.5/forecast   5 days in 3-hour slots → today's high/low/precip

API key in Secrets Manager at rdmis/dev/openweather-api-key. Every function
falls back to a safe value on any error so the wake post still goes out.

Location follows Ryan: with a timezone override active, the override's city
(geocoded, cached); otherwise the configured home location
(config.HOME_LAT / HOME_LON, default West Bend, WI).

"Today" is the ACTIVE timezone's local date (quiet_hours.local_today).

Public API (signatures unchanged for callers):
    get_current_conditions(lat=None, lon=None) → {
        "temp_f": float, "precip_next_90min": bool, "fetched_at": datetime | None}
    get_today_forecast(lat=None, lon=None) → {
        "available": bool, "high_f": int | None, "low_f": int | None,
        "precip_chance": int | None, "precip_in": float | None, "summary": str | None}
    geocode(place) → (lat, lon) | None
    active_location() → (lat, lon) | None

Never log a request URL or a requests exception message: both carry
`appid=<key>`. Failures log the endpoint name and HTTP status only (and the
process-wide RedactingFilter scrubs anything that slips through).
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from artemis import config

logger = logging.getLogger(__name__)

HOME_LAT = config.HOME_LAT
HOME_LON = config.HOME_LON
# Back-compat aliases.
WEST_BEND_LAT = HOME_LAT
WEST_BEND_LON = HOME_LON

_OWM_ROOT = "https://api.openweathermap.org"
_OWM_WEATHER = f"{_OWM_ROOT}/data/2.5/weather"
_OWM_FORECAST = f"{_OWM_ROOT}/data/2.5/forecast"
_OWM_GEOCODE = f"{_OWM_ROOT}/geo/1.0/direct"
_OWM_TIMEOUT = 5  # seconds — fast fail; the post must go out regardless

# OWM reports precipitation in mm regardless of `units`.
_MM_PER_IN = 25.4
# Current-conditions weather groups that mean it's precipitating now.
_PRECIP_GROUPS = {"Rain", "Drizzle", "Thunderstorm", "Snow"}
# A forecast slot at/above this probability counts as "rain soon".
_PRECIP_SOON_POP = 0.5

# Safe defaults when the API is unreachable. 50°F + no rain → outdoor.
_FALLBACK = {
    "temp_f": 50.0,
    "precip_next_90min": False,
    "fetched_at": None,
}

_UNAVAILABLE = {"available": False, "high_f": None, "low_f": None,
                "precip_chance": None, "precip_in": None, "summary": None}


def _get_api_key() -> Optional[str]:
    """Lazy-load the OpenWeatherMap API key. None when not configured."""
    try:
        from knowledge.secrets import get_openweather_api_key
        return get_openweather_api_key()
    except Exception:
        logger.warning("OpenWeatherMap API key unavailable — using fallback weather")
        return None


def _owm_get(url: str, label: str, params: dict) -> Optional[dict | list]:
    """GET an OWM endpoint. Returns parsed JSON, or None on any failure.

    Logs only `label` and the HTTP status — never the URL or the exception
    text, which both contain the appid.
    """
    try:
        resp = requests.get(url, params=params, timeout=_OWM_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("OpenWeatherMap %s request failed (%s)", label, type(exc).__name__)
        return None
    if not resp.ok:
        logger.warning("OpenWeatherMap %s returned HTTP %s", label, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("OpenWeatherMap %s returned non-JSON", label)
        return None


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def geocode(place: str) -> tuple[float, float] | None:
    """Resolve a place name to (lat, lon) via the OWM geocoder, cached in
    acos.system_state. None when unavailable."""
    key = (place or "").strip().lower()
    if not key:
        return None
    cache_key = f"geocode:{key}"
    try:
        from artemis.quiet_hours import get_system_value
        cached = get_system_value(cache_key)
        if cached:
            lat_s, _, lon_s = cached.partition(",")
            return float(lat_s), float(lon_s)
    except Exception:
        logger.debug("geocode cache read failed", exc_info=True)

    api_key = _get_api_key()
    if not api_key:
        return None
    rows = _owm_get(_OWM_GEOCODE, "geocode", {"q": place, "limit": 1, "appid": api_key})
    if not rows:
        return None
    lat, lon = float(rows[0]["lat"]), float(rows[0]["lon"])
    try:
        from artemis.quiet_hours import set_system_value
        set_system_value(cache_key, f"{lat},{lon}")
    except Exception:
        logger.debug("geocode cache write failed", exc_info=True)
    return lat, lon


def active_location() -> tuple[float, float] | None:
    """Where Ryan is: the timezone override's city, else home.

    Returns None when an override is active but its city can't be geocoded —
    callers omit weather rather than show home weather for the wrong place.
    """
    try:
        from artemis.quiet_hours import get_timezone_override
        row = get_timezone_override()
    except Exception:
        logger.debug("timezone override lookup failed — using home", exc_info=True)
        row = None
    if not row:
        return HOME_LAT, HOME_LON
    label = row.get("city_name") or (row.get("timezone") or "").split("/")[-1].replace("_", " ")
    coords = geocode(label)
    if coords is None:
        logger.info("weather: no coordinates for %r — omitting weather", label)
    return coords


def _resolve(lat: Optional[float], lon: Optional[float]) -> tuple[float, float] | None:
    if lat is not None and lon is not None:
        return lat, lon
    return active_location()


def _local_tz():
    from artemis.quiet_hours import local_tz
    return local_tz()


# ---------------------------------------------------------------------------
# Forecast — today's high / low / precipitation
# ---------------------------------------------------------------------------

def summarize_today(forecast: dict, tz, today) -> dict:
    """Reduce a /data/2.5/forecast payload to today's numbers (pure; tested
    against recorded fixtures).

    Uses only the 3-hour slots whose local date (in `tz`) is `today`, so at
    04:30 the high/low cover the rest of the day.
    """
    slots = [
        s for s in (forecast.get("list") or [])
        if datetime.fromtimestamp(s["dt"], tz).date() == today
    ]
    if not slots:
        return dict(_UNAVAILABLE)

    highs = [s["main"]["temp_max"] for s in slots if s.get("main", {}).get("temp_max") is not None]
    lows = [s["main"]["temp_min"] for s in slots if s.get("main", {}).get("temp_min") is not None]
    pops = [float(s.get("pop") or 0) for s in slots]
    precip_mm = sum(
        float((s.get("rain") or {}).get("3h") or 0) + float((s.get("snow") or {}).get("3h") or 0)
        for s in slots
    )

    # Summary: the wettest slot's description when rain is likely, else the
    # most common description (earliest wins a tie).
    max_pop = max(pops)
    if max_pop >= _PRECIP_SOON_POP:
        wettest = slots[pops.index(max_pop)]
        summary = (wettest.get("weather") or [{}])[0].get("description")
    else:
        descs = [(s.get("weather") or [{}])[0].get("description") for s in slots]
        descs = [d for d in descs if d]
        summary = Counter(descs).most_common(1)[0][0] if descs else None

    return {
        "available": True,
        "high_f": round(max(highs)) if highs else None,
        "low_f": round(min(lows)) if lows else None,
        "precip_chance": round(max_pop * 100),
        "precip_in": round(precip_mm / _MM_PER_IN, 2),
        "summary": summary,
    }


def get_today_forecast(lat: Optional[float] = None, lon: Optional[float] = None) -> dict:
    """Today's high/low and precipitation for the pre-departure line.
    available=False when the API or the location is unavailable."""
    where = _resolve(lat, lon)
    api_key = _get_api_key()
    if where is None or not api_key:
        return dict(_UNAVAILABLE)
    data = _owm_get(_OWM_FORECAST, "forecast",
                    {"lat": where[0], "lon": where[1], "units": "imperial", "appid": api_key})
    if not isinstance(data, dict):
        return dict(_UNAVAILABLE)
    from artemis.quiet_hours import local_today
    return summarize_today(data, _local_tz(), local_today())


# ---------------------------------------------------------------------------
# Current conditions — the outdoor-walk decision
# ---------------------------------------------------------------------------

def precip_soon(current: dict, forecast: dict | None, now: datetime) -> bool:
    """True when it's precipitating now, or a forecast slot starting within the
    next 3 hours is likely wet. The free API has no minute-level data, so the
    old "next 90 minutes" is approximated by the next 3-hour slot."""
    groups = {w.get("main") for w in (current.get("weather") or [])}
    if groups & _PRECIP_GROUPS:
        return True
    if float((current.get("rain") or {}).get("1h") or 0) > 0.1:
        return True
    if float((current.get("snow") or {}).get("1h") or 0) > 0.1:
        return True
    horizon = now + timedelta(hours=3)
    for s in (forecast or {}).get("list") or []:
        start = datetime.fromtimestamp(s["dt"], timezone.utc)
        if now <= start <= horizon and float(s.get("pop") or 0) >= _PRECIP_SOON_POP:
            return True
    return False


def get_current_conditions(lat: Optional[float] = None, lon: Optional[float] = None) -> dict:
    """Current temp + near-term precipitation. Always returns a usable dict."""
    where = _resolve(lat, lon)
    api_key = _get_api_key()
    if where is None or not api_key:
        return dict(_FALLBACK)
    params = {"lat": where[0], "lon": where[1], "units": "imperial", "appid": api_key}
    current = _owm_get(_OWM_WEATHER, "weather", params)
    if not isinstance(current, dict):
        return dict(_FALLBACK)
    forecast = _owm_get(_OWM_FORECAST, "forecast", params)
    now = datetime.now(timezone.utc)
    temp = (current.get("main") or {}).get("temp")
    return {
        "temp_f": float(temp) if temp is not None else _FALLBACK["temp_f"],
        "precip_next_90min": precip_soon(current, forecast if isinstance(forecast, dict) else None, now),
        "fetched_at": now,
    }
