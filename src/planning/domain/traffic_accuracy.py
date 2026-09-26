"""Evaluate independent completed trips; appointment windows are not observations."""

import math
from statistics import mean, median


def evaluate_accuracy(observations: list[dict]) -> dict:
    """Input pairs must describe driving only, excluding service and waiting.

    No scalar 'accuracy' without a definition: report error and explicit tolerance.
    Callers must provide out-of-sample observations, not fitted/training durations.
    """
    if not observations:
        return {
            "status": "not_validated",
            "observed_trip_count": 0,
            "mae_minutes": None,
            "p90_absolute_error_minutes": None,
            "within_5_minutes_percent": None,
            "mape_percent": None,
        }
    errors, relative, signed = [], [], []
    seen = set()
    for row in observations:
        identifier = str(row["trip_id"])
        if identifier in seen:
            raise ValueError("Duplicate trip_id in accuracy evaluation")
        seen.add(identifier)
        actual, predicted = (
            float(row["actual_driving_seconds"]),
            float(row["predicted_driving_seconds"]),
        )
        if (
            not (math.isfinite(actual) and math.isfinite(predicted))
            or actual <= 0
            or predicted < 0
        ):
            raise ValueError("Driving times must be finite, actual > 0, predicted >= 0")
        signed.append(predicted - actual)
        errors.append(abs(predicted - actual))
        relative.append(abs(predicted - actual) / actual)
    p90 = sorted(errors)[math.ceil(0.90 * len(errors)) - 1]
    return {
        "status": "evaluated",
        "observed_trip_count": len(errors),
        "mae_minutes": round(mean(errors) / 60, 2),
        "median_absolute_error_minutes": round(median(errors) / 60, 2),
        "p90_absolute_error_minutes": round(p90 / 60, 2),
        "bias_minutes": round(mean(signed) / 60, 2),
        "within_5_minutes_percent": round(
            sum(e <= 300 for e in errors) / len(errors) * 100, 2
        ),
        "mape_percent": round(mean(relative) * 100, 2),
    }
