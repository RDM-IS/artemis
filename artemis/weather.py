"""Weather wrapper — indoor/outdoor decision for cardio sessions.

Uses OpenWeatherMap free tier (One Call API 3.0). API key in Secrets Manager
at rdmis/dev/openweather-api-key. Falls back to safe defaults on any error
so the morning/evening prompts still go out.

Public API:
    get_current_conditions(lat, lon) → {
        "temp_f": float,
        "precip_next_90min": bool,
        "fetched_at": datetime | None,
    }

Hardcoded location (West Bend, WI): 43.4253, -88.1834.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# West Bend, WI — Ryan's home gym location
WEST_BEND_LAT = 43.4253
WEST_BEND_LON = -88.1834

# OpenWeatherMap One Call API 3.0
_OWM_BASE = "https://api.openweathermap.org/data/3.0/onecall"
_OWM_TIMEOUT = 5  # seconds — fast fail; prompt must go out regardless

# Safe defaults when the API is unreachable. 50°F + no rain → outdoor (the
# "default" case). The prompt will note that weather data is unavailable.
_FALLBACK = {
    "temp_f": 50.0,
    "precip_next_90min": False,
    "fetched_at": None,
}


def _get_api_key() -> Optional[str]:
    """Lazy-load OpenWeatherMap API key from Secrets Manager.

    Returns None if not configured — caller falls back to defaults.
    """
    try:
        from knowledge.secrets import get_openweather_api_key
        return get_openweather_api_key()
    except Exception:
        logger.warning("OpenWeatherMap API key unavailable — using fallback weather", exc_info=False)
        return None


def geocode(place: str) -> tuple[float, float] | None:
    """Resolve a place name to (lat, lon) via the OWM geocoder, cached in
    acos.system_state. Returns None when unavailable — callers fall back to home.
    """
    key = (place or "").strip().lower()
    if not key:
        return None
    cache_key = f"geocode:{key}"
    try:
        from artemis.quiet_hours import get_system_value, set_system_value
        cached = get_system_value(cache_key)
        if cached:
            lat_s, _, lon_s = cached.partition(",")
            return float(lat_s), float(lon_s)
    except Exception:
        logger.debug("geocode cache read failed", exc_info=True)

    api_key = _get_api_key()
    if not api_key:
        return None
    try:
        resp = requests.get(
            "https://api.openweathermap.org/geo/1.0/direct",
            params={"q": place, "limit": 1, "appid": api_key},
            timeout=_OWM_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json() or []
    except Exception:
        logger.warning("Geocode lookup failed for %r", place, exc_info=True)
        return None
    if not rows:
        return None
    lat, lon = float(rows[0]["lat"]), float(rows[0]["lon"])
    try:
        from artemis.quiet_hours import set_system_value
        set_system_value(cache_key, f"{lat},{lon}")
    except Exception:
        logger.debug("geocode cache write failed", exc_info=True)
    return lat, lon


def get_today_forecast(lat: float = WEST_BEND_LAT, lon: float = WEST_BEND_LON) -> dict:
    """Today's high/low and precipitation chance for the pre-departure line.

    Returns {'high_f', 'low_f', 'precip_chance', 'summary', 'available'};
    available=False when the API is unreachable (caller omits the line).
    """
    api_key = _get_api_key()
    if not api_key:
        return {"available": False, "high_f": None, "low_f": None,
                "precip_chance": None, "summary": None}
    try:
        resp = requests.get(
            _OWM_BASE,
            params={
                "lat": lat, "lon": lon, "appid": api_key, "units": "imperial",
                "exclude": "minutely,hourly,alerts,current",
            },
            timeout=_OWM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning("OpenWeatherMap daily fetch failed", exc_info=True)
        return {"available": False, "high_f": None, "low_f": None,
                "precip_chance": None, "summary": None}

    days = data.get("daily") or []
    if not days:
        return {"available": False, "high_f": None, "low_f": None,
                "precip_chance": None, "summary": None}
    today = days[0]
    temp = today.get("temp") or {}
    weather = (today.get("weather") or [{}])[0]
    return {
        "available": True,
        "high_f": round(float(temp.get("max"))) if temp.get("max") is not None else None,
        "low_f": round(float(temp.get("min"))) if temp.get("min") is not None else None,
        "precip_chance": round(float(today.get("pop", 0)) * 100),
        "summary": weather.get("description"),
    }


def get_current_conditions(lat: float = WEST_BEND_LAT, lon: float = WEST_BEND_LON) -> dict:
    """Fetch current temp + 90-minute precipitation outlook.

    Returns {'temp_f', 'precip_next_90min', 'fetched_at'}.
    Always returns a usable dict — falls back to safe defaults on any error.
    """
    api_key = _get_api_key()
    if not api_key:
        return dict(_FALLBACK)

    try:
        resp = requests.get(
            _OWM_BASE,
            params={
                "lat": lat,
                "lon": lon,
                "appid": api_key,
                "units": "imperial",
                # exclude what we don't need to keep payload small
                "exclude": "daily,hourly,alerts",
            },
            timeout=_OWM_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning("OpenWeatherMap fetch failed — using fallback", exc_info=True)
        return dict(_FALLBACK)

    current = data.get("current") or {}
    temp_f = float(current.get("temp", _FALLBACK["temp_f"]))

    # Minutely precipitation: list of {dt, precipitation} entries (mm/h).
    # Treat any minutely entry > 0.1 mm/h within the next 90 min as rain.
    minutely = data.get("minutely") or []
    precip_next_90min = any(
        (m.get("precipitation") or 0) > 0.1
        for m in minutely[:90]
    )

    return {
        "temp_f": temp_f,
        "precip_next_90min": precip_next_90min,
        "fetched_at": datetime.now(timezone.utc),
    }
