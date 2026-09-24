"""End-to-end local demo smoke; mutates only the explicitly supplied demo project."""

import argparse
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx


def run(manifest_path: Path, output: Path, full: bool = True):
    manifest = json.loads(manifest_path.read_text())
    checks = []

    def record(name, **details):
        item = {"check": name, "status": "PASS", **details}
        checks.append(item)
        output.write_text(json.dumps(checks, ensure_ascii=False, indent=2))
        print(json.dumps(item, ensure_ascii=False), flush=True)

    def request(client, method, path, **kwargs):
        r = client.request(method, path, **kwargs)
        if not r.is_success:
            raise AssertionError(f"{method} {path}: {r.status_code} {r.text[:1500]}")
        return r.json() if r.content else None

    project_id = manifest["project_id"]
    workspace = f"/api/projects/{project_id}/workspace"
    planning = f"/api/projects/{project_id}/planning"

    def wait_event(client, response):
        event_id = response.get("planning_event_id")
        if event_id is None:
            raise AssertionError(f"No event in response: {response}")
        deadline = time.monotonic() + 330
        while time.monotonic() < deadline:
            event = request(client, "GET", f"{planning}/events/{event_id}")
            state = event.get("state")
            if state == "PUBLISHED":
                return event
            if state in {"FAILED", "SUPERSEDED", "CANCELLED"}:
                raise AssertionError(json.dumps(event, ensure_ascii=False))
            time.sleep(1)
        raise AssertionError(f"Event {event_id} timed out")

    def plan(client):
        return request(client, "GET", f"{planning}/current")

    with httpx.Client(base_url="http://localhost:8000", timeout=30) as c:
        account = manifest["accounts"][0]
        request(
            c,
            "POST",
            "/api/auth/login",
            json={"login": account["login"], "password": account["password"]},
        )
        request(c, "GET", "/api/auth/me")
        available = request(c, "GET", "/api/project/available-projects")
        assert any(item["id"] == project_id for item in available), (
            "Refuse to mutate a different project"
        )
        assert account["login"].startswith("demo_routes_"), (
            "Use a dedicated demo account"
        )
        record("dispatcher_login", project_id=manifest["project_id"])
        readiness = request(
            c,
            "GET",
            f"{planning}/board",
            params={"from": manifest["planning_date"], "days": 7},
        )
        record("readiness", result=readiness)
        response = request(
            c,
            "POST",
            f"{planning}/events/manual",
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        event = wait_event(c, response)
        current = plan(c)
        assert current["assignments"], current
        record(
            "manual_plan",
            event_id=event["id"],
            assignments=len(current["assignments"]),
            version_id=(current.get("version") or {}).get("id"),
        )
        board = request(
            c,
            "GET",
            f"{planning}/board",
            params={"from": manifest["planning_date"], "days": 7},
        )
        day = request(
            c, "GET", f"{planning}/board/{manifest['planning_date']}"
        )
        record("board_and_day", board_keys=list(board), day_keys=list(day))
        if not full:
            return
        job = request(
            c,
            "POST",
            f"{workspace}/jobs",
            json={
                "address": "Пермь, Ленина, 50",
                "latitude": 58.011905,
                "longitude": 56.242396,
                "sla_date": manifest["planning_date"],
                "time_window_start": "08:00:00",
                "time_window_end": "15:00:00",
                "work_type_id": manifest["work_type_ids"][1],
            },
        )
        event = wait_event(c, job)
        current = plan(c)
        job_id = job.get("id", job.get("job_id"))
        assert any(
            x["job_id"] == job_id
            and str(x["planning_date"]) == manifest["planning_date"]
            for x in current["assignments"]
        ), (job, current)
        record(
            "due_today_insertion",
            event_id=event["id"],
            job_id=job_id,
            candidate_count=event.get("candidate_total"),
        )
        cancel = request(c, "POST", f"{workspace}/jobs/{job_id}/cancel")
        event = wait_event(c, cancel)
        assert all(x["job_id"] != job_id for x in plan(c)["assignments"])
        record("cancel_job", event_id=event["id"], job_id=job_id)
        today_assignments = [
            x
            for x in plan(c)["assignments"]
            if str(x["planning_date"]) == manifest["planning_date"]
        ]
        assert today_assignments
        engineer_id = today_assignments[0]["engineer_id"]
        response = request(
            c,
            "PATCH",
            f"{workspace}/engineers/{engineer_id}/availability",
            json={
                "entries": [{"work_date": manifest["planning_date"], "working": False}]
            },
        )
        event = wait_event(c, response)
        assert not any(
            x["engineer_id"] == engineer_id
            and str(x["planning_date"]) == manifest["planning_date"]
            for x in plan(c)["assignments"]
        )
        record("engineer_unavailable", event_id=event["id"], engineer_id=engineer_id)
        response = request(
            c,
            "PATCH",
            f"{workspace}/engineers/{engineer_id}/availability",
            json={
                "entries": [
                    {
                        "work_date": manifest["planning_date"],
                        "working": True,
                        "shift_start": "08:00:00",
                        "shift_end": "16:00:00",
                    }
                ]
            },
        )
        event = wait_event(c, response)
        record("engineer_restored", event_id=event["id"], engineer_id=engineer_id)
        request(c, "GET", f"{planning}/versions")
        record("plan_history")
        with httpx.Client(
            base_url="http://localhost:8000", timeout=30
        ) as engineer_client:
            account = manifest["accounts"][1]
            request(
                engineer_client,
                "POST",
                "/api/auth/login",
                json={"login": account["login"], "password": account["password"]},
            )
            rows = request(engineer_client, "GET", "/api/engineer/assignments")
            state = request(
                engineer_client, "GET", "/api/engineer/assignments/route-state"
            )
            record(
                "engineer_login_and_route",
                assignments=len(rows),
                route_state_keys=list(state),
            )
        record("smoke_complete", completed_at=datetime.now().isoformat())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("/private/tmp/routes-demo-smoke.json")
    )
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    run(args.manifest, args.output, not args.plan_only)
