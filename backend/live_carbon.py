
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

API_URL = (
    "https://api.electricitymaps.com/v4/carbon-intensity/latest"
)

# API key .env file se read hogi
API_KEY = os.getenv("ELECTRICITY_MAPS_API_KEY")

# Electricity Maps grid zone IDs
ZONES = {
    "IN": "India",
    "DE": "Germany",
    "US-MIDA-PJM": "US Mid-Atlantic (PJM)",
}


def get_carbon_intensity(zone: str) -> dict:
    if not API_KEY:
        raise RuntimeError(
            "ELECTRICITY_MAPS_API_KEY missing hai. "
            "Backend/.env check karo."
        )

    response = httpx.get(
        API_URL,
        params={"zone": zone},
        headers={"auth-token": API_KEY},
        timeout=20.0,
    )
    response.raise_for_status()

    data = response.json()
    intensity = data.get("carbonIntensity")

    if intensity is None:
        raise RuntimeError(
            f"{zone} ke response mein carbonIntensity nahi hai."
        )

    return {
        "zone": data.get("zone", zone),
        "carbon_intensity_gco2_kwh": float(intensity),
        "datetime": data.get("datetime"),
        "is_estimated": data.get("isEstimated"),
        "source": "Electricity Maps API",
    }


def main():
    readings = []

    for zone, name in ZONES.items():
        try:
            result = get_carbon_intensity(zone)
            result["name"] = name
            readings.append(result)
            print(result)

        except httpx.HTTPStatusError as exc:
            print(
                f"{name}: API HTTP {exc.response.status_code}"
            )
            print(exc.response.text[:300])

        except (httpx.RequestError, RuntimeError, ValueError) as exc:
            print(f"{name}: live data unavailable: {exc}")

    if not readings:
        print("\nNo live readings. No region selected.")
        return

    cleanest = min(
        readings,
        key=lambda item: item["carbon_intensity_gco2_kwh"],
    )

    print("\nLowest reported carbon intensity:")
    print(cleanest)


if __name__ == "__main__":
    main()
