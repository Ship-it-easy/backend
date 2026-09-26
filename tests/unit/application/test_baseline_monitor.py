from datetime import datetime, timedelta, timezone

from scripts.baseline_monitor import percentile, summarize


def sample(status: str = "READY", **changes):
    published = datetime(2030, 1, 1, tzinfo=timezone.utc)
    return {
        "status": status,
        "calculation_time_ms": 10,
        "input_jobs_count": 8,
        "engineers_count": 2,
        "coverage_comparable": True if status == "READY" else None,
        "finished_at": published + timedelta(seconds=2),
        "published_at": published,
        "input_hash": "same-input",
        "algorithm_version": "FIFO_V2",
        "travel_matrix_hash": "same-road-data",
        "result_hash": "same-result" if status == "READY" else None,
        "attempt_count": 1,
        "failure_code": None,
        **changes,
    }


def test_percentile_handles_empty_and_nearest_rank():
    assert percentile([], 0.95) is None
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5


def test_monitor_reports_required_metrics_and_hash_alerts():
    rows = [
        sample(),
        sample(
            result_hash="different-result",
            coverage_comparable=False,
            calculation_time_ms=20,
        ),
        sample(
            "FAILED",
            failure_code="BASELINE_INPUT_HASH_MISMATCH",
            attempt_count=3,
        ),
        sample(
            "FAILED",
            failure_code="BASELINE_DISTANCE_DATA_NOT_READY",
            attempt_count=2,
        ),
    ]
    result = summarize(rows, [])
    assert result["status_share"]["READY"] == 0.5
    assert result["duration_ms"]["p95"] == 20
    assert result["input_size"]["jobs_max"] == 8
    assert result["retry_count"] == 3
    assert result["exhausted_retries"] == 1
    assert result["matrix_errors"] == 1
    assert result["input_hash_mismatches"] == 1
    assert result["coverage_mismatch_share"] == 0.5
    assert result["result_hash_divergences"] == 1
    assert result["publication_delay_ms"]["p50"] == 2000
    assert "BASELINE_INPUT_HASH_MISMATCH" in result["alerts"]
    assert "BASELINE_RESULT_HASH_DIVERGENCE" in result["alerts"]


def test_monitor_alerts_when_failed_rate_rises():
    current = [sample("FAILED") for _ in range(3)] + [sample()]
    previous = [sample() for _ in range(4)]
    assert "BASELINE_FAILED_RATE_INCREASED" in summarize(current, previous)["alerts"]


def test_duration_percentiles_measure_full_attempt_not_only_fifo_cpu_time():
    started = datetime(2030, 1, 1, tzinfo=timezone.utc)
    result = summarize(
        [
            sample(
                started_at=started,
                finished_at=started + timedelta(milliseconds=2300),
                calculation_time_ms=1,
            )
        ],
        [],
    )
    assert result["duration_ms"] == {"p50": 2300.0, "p95": 2300.0, "p99": 2300.0}
