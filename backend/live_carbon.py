
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load .env from the backend directory
load_dotenv(Path(__file__).resolve().parent / ".env")

LATEST_URL = (
    "https://api.electricitymaps.com/v4/carbon-intensity/latest"
)
FORECAST_URL = (
    "https://api.electricitymaps.com/v4/carbon-intensity/forecast"
)

ZONES = {
    "IN": "India",
    "DE": "Germany",
    "US-MIDA-PJM": "US Mid-Atlantic (PJM)",
}


def _get_api_key():
    key = os.getenv("ELECTRICITY_MAPS_API_KEY")
    if not key:
        raise RuntimeError(
            "ELECTRICITY_MAPS_API_KEY missing from backend/.env"
        )
    return key


def _get_json(url, params):
    try:
        response = httpx.get(
            url,
            params=params,
            headers={"auth-token": _get_api_key()},
            timeout=20.0,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        # Do not print request headers or the API key.
        raise RuntimeError(
            f"Electricity Maps API returned HTTP "
            f"{exc.response.status_code}"
        ) from None
    except httpx.RequestError:
        raise RuntimeError(
            "Could not connect to Electricity Maps API"
        ) from None


def get_carbon_intensity(zone="IN"):
    data = _get_json(
        LATEST_URL,
        {"zone": zone},
    )

    intensity = data.get("carbonIntensity")
    if intensity is None:
        raise RuntimeError(
            f"No carbon intensity returned for zone {zone}"
        )

    return {
        "zone": zone,
        "carbon_intensity_gco2_kwh": float(intensity),
        "datetime": data.get("datetime"),
        "is_estimated": data.get("isEstimated"),
        "source": "Electricity Maps API",
        "data_type": "latest",
    }


def get_carbon_forecast(zone="IN", horizon_hours=24):
    if not 1 <= horizon_hours <= 72:
        raise ValueError("horizon_hours must be between 1 and 72")

    data = _get_json(
        FORECAST_URL,
        {
            "zone": zone,
            "horizonHours": horizon_hours,
        },
    )

    raw_forecast = data.get("forecast")
    if not isinstance(raw_forecast, list) or not raw_forecast:
        raise RuntimeError(
            f"No forecast points returned for zone {zone}"
        )

    updated_at = data.get("updatedAt")
    if not updated_at:
        raise RuntimeError(
            "Forecast response is missing updatedAt"
        )

    try:
        updated_dt = datetime.fromisoformat(
            updated_at.replace("Z", "+00:00")
        )
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        raise RuntimeError(
            "Forecast updatedAt timestamp is invalid"
        ) from None

    now = datetime.now(timezone.utc)
    if updated_dt.astimezone(timezone.utc) < now - timedelta(hours=6):
        raise RuntimeError(
            "Carbon forecast is older than 6 hours; "
            "refusing to schedule using stale data"
        )

    points = []

    for item in raw_forecast:
        try:
            timestamp = datetime.fromisoformat(
                item["datetime"].replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)

            intensity = float(item["carbonIntensity"])

            if intensity < 0:
                continue

            points.append({
                "datetime": timestamp.astimezone(
                    timezone.utc
                ),
                "carbon_intensity_gco2_kwh": intensity,
            })
        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    points.sort(key=lambda point: point["datetime"])

    if not points:
        raise RuntimeError(
            "Forecast contains no valid carbon intensity points"
        )

    return {
        "zone": zone,
        "updated_at": updated_dt.astimezone(
            timezone.utc
        ).isoformat(),
        "temporal_granularity": data.get(
            "temporalGranularity", "unknown"
        ),
        "source": "Electricity Maps API",
        "data_type": "forecast",
        "forecast": points,
    }


def main():
    print("=== Latest Carbon Intensity ===")

    for zone, name in ZONES.items():
        try:
            result = get_carbon_intensity(zone)
            print(
                f"{name}: "
                f"{result['carbon_intensity_gco2_kwh']} "
                f"gCO2/kWh | "
                f"time={result['datetime']} | "
                f"estimated={result['is_estimated']}"
            )
        except (RuntimeError, ValueError) as exc:
            print(f"{name}: ERROR - {exc}")

    print("\n=== India Carbon Forecast ===")

    try:
        result = get_carbon_forecast("IN", 24)
        print(f"Updated at: {result['updated_at']}")
        print(f"Granularity: {result['temporal_granularity']}")
        print(f"Data type: {result['data_type']}")

        for point in result["forecast"][:5]:
            print(
                f"{point['datetime'].isoformat()} -> "
                f"{point['carbon_intensity_gco2_kwh']} "
                "gCO2/kWh"
            )
    except (RuntimeError, ValueError) as exc:
        print(f"Forecast ERROR - {exc}")


if __name__ == "__main__":
    main()
