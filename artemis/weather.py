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
