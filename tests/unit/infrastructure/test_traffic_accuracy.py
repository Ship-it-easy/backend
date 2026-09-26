import pytest

from planning.domain.traffic_accuracy import evaluate_accuracy


def test_no_data_does_not_mean_zero_error_or_perfect_accuracy():
    result = evaluate_accuracy([])
    assert result["status"] == "not_validated"
    assert result["observed_trip_count"] == 0
    assert result["mae_minutes"] is None
    assert result["within_5_minutes_percent"] is None


def test_observed_errors_include_tail_and_bias():
    result = evaluate_accuracy(
        [
            {
                "trip_id": "a",
                "actual_driving_seconds": 600,
                "predicted_driving_seconds": 480,
            },
            {
                "trip_id": "b",
                "actual_driving_seconds": 1200,
                "predicted_driving_seconds": 1200,
            },
            {
                "trip_id": "c",
                "actual_driving_seconds": 1800,
                "predicted_driving_seconds": 2400,
            },
        ]
    )
    assert result["mae_minutes"] == 4
    assert result["p90_absolute_error_minutes"] == 10
    assert result["within_5_minutes_percent"] == 66.67
    assert result["bias_minutes"] == 2.67


@pytest.mark.parametrize("actual", [0, -1, float("nan"), float("inf")])
def test_invalid_observations_are_not_silently_accepted(actual):
    with pytest.raises(ValueError):
        evaluate_accuracy(
            [
                {
                    "trip_id": "a",
                    "actual_driving_seconds": actual,
                    "predicted_driving_seconds": 60,
                }
            ]
        )


def test_duplicate_trip_cannot_inflate_accuracy_sample():
    row = {
        "trip_id": "a",
        "actual_driving_seconds": 60,
        "predicted_driving_seconds": 60,
    }
    with pytest.raises(ValueError):
        evaluate_accuracy([row, row])
