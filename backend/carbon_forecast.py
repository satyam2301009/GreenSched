
from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import RandomForestRegressor


class CarbonForecaster:
    """
    Demonstration Random Forest model for carbon-intensity forecasting.

    The bundled training data is synthetic, not historical grid data.
    Predictions must therefore be labelled DEMO_FORECAST.
    """

    def __init__(self):
        rng = np.random.default_rng(42)

        # Synthetic hourly patterns for reproducible demonstration only.
        hours = np.arange(24)
        samples = []

        for day in range(30):
            for hour in hours:
                daily_pattern = (
                    300
                    + 65 * np.cos((hour - 15) * 2 * np.pi / 24)
                    + 18 * np.sin(day * 2 * np.pi / 7)
                )
                intensity = max(30, daily_pattern + rng.normal(0, 12))
                samples.append([hour, day % 7, intensity])

        data = np.asarray(samples, dtype=float)
        features = data[:, :2]
        targets = data[:, 2]

        self.model = RandomForestRegressor(
            n_estimators=100,
            random_state=42,
            min_samples_leaf=2,
        )
        self.model.fit(features, targets)

    def forecast(self, hours_ahead: int = 1) -> dict:
        if not 1 <= hours_ahead <= 24:
            raise ValueError("hours_ahead must be between 1 and 24")

        now = datetime.now(timezone.utc)
        target_hour = (now.hour + hours_ahead) % 24
        target_day = (now.weekday() + (now.hour + hours_ahead) // 24) % 7

        prediction = float(
            self.model.predict([[target_hour, target_day]])[0]
        )

        return {
            "forecast_intensity_gco2_kwh": round(max(0, prediction), 2),
            "hours_ahead": hours_ahead,
            "generated_at": now.isoformat(),
            "model": "RandomForestRegressor",
            "source": "DEMO_FORECAST",
            "warning": (
                "Trained on synthetic demonstration data; "
                "not a real-world carbon forecast."
            ),
        }


forecaster = CarbonForecaster()
