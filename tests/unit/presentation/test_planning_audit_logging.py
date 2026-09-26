import json
import logging

from auth.entrypoint.setup import PlanningAuditFormatter


def test_planning_audit_event_is_structured_without_personal_data():
    record = logging.makeLogRecord(
        {
            "name": "planning.audit",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "baseline_calculation_ready",
            "project_id": 4,
            "planning_run_id": 17,
            "planning_date": "2030-01-01",
            "plan_version": 3,
            "algorithm_version": "FIFO_V2",
            "input_hash": "hash",
            "duration_ms": 14,
            "jobs_count": 8,
            "engineers_count": 2,
            "address": "DO NOT LOG THIS",
        }
    )
    value = json.loads(PlanningAuditFormatter().format(record))
    assert value["event"] == "baseline_calculation_ready"
    assert value["jobs_count"] == 8
    assert value["engineers_count"] == 2
    assert "DO NOT LOG THIS" not in json.dumps(value)


def test_ordinary_log_retains_ordinary_format():
    record = logging.makeLogRecord(
        {"name": "app", "levelno": logging.INFO, "msg": "ordinary message"}
    )
    assert PlanningAuditFormatter("%(message)s").format(record) == "ordinary message"


def test_persistence_failure_event_does_not_expose_exception_message():
    record = logging.makeLogRecord(
        {
            "name": "planning.audit",
            "levelno": logging.ERROR,
            "levelname": "ERROR",
            "msg": "baseline_persistence_failed",
            "project_id": 4,
            "planning_run_id": 17,
            "input_hash": "hash",
            "error_type": "IntegrityError",
            "error_message": "DO NOT LOG SQL PARAMETERS OR ADDRESSES",
        }
    )
    value = json.loads(PlanningAuditFormatter().format(record))
    assert value["event"] == "baseline_persistence_failed"
    assert value["error_type"] == "IntegrityError"
    assert "DO NOT LOG" not in json.dumps(value)
