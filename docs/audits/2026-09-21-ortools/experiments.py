"""Read-only solver audit; synthetic inputs, no DB or network access.

Run from repository root: .venv/bin/python docs/audits/2026-09-21-ortools/experiments.py
"""

import asyncio
import itertools
import json
import math
import random
import time as clock
import types
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from planning.application.interfaces.travel_matrix_provider import TravelMatrix
from planning.application.services.dynamic_today_planning import DynamicTodayPlanningService
from planning.application.services.planning_input_normalizer import PlanningInputNormalizer
from planning.application.services.today_route_protection import TodayRouteProtectionService
from planning.application.validators.planning_result import PlanningValidator
from planning.domain.entities.coordinate import Coordinate
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job
from planning.domain.entities.planning import PlanningConfig, PlanningInput
from planning.domain.enums import TransportType
from planning.infrastructure.adapters.planning_solver_ortools import OrToolsPlanningSolver, _objective_ranges

D = date(2030, 1, 1)
NOW = datetime(2030, 1, 1, 7, tzinfo=timezone.utc)


def config(**overrides):
    values = dict(id=1, version=1, sla_overdue_base=10000, sla_overdue_per_day=1000,
                  sla_today=9000, sla_tomorrow=4000, sla_2_3_days=2000, sla_later=500,
                  skill_one_engineer=500, skill_two_engineers=250, equipment_one_unit=500,
                  equipment_two_units=250, window_30=300, window_60=150, window_120=50,
                  travel_cost_per_minute=1, solver_time_limit_sec=2,
                  max_jobs_per_run=1000, travel_provider="TEST")
    return PlanningConfig(**(values | overrides))


def job(i, **overrides):
    return replace(Job(i, D, 30, Coordinate(0, i / 1000), 480, 1080, None,
                       frozenset(), frozenset(), NOW, 10000), **overrides)


def engineer(i=1, **overrides):
    return replace(Engineer(i, TransportType.CAR, Coordinate(0, i), 480, 1080,
                            frozenset()), **overrides)


def data(jobs, engineers=None, **overrides):
    return replace(PlanningInput(1, D, "UTC", config(), jobs,
                                 engineers or [engineer()], {}, [], len(jobs),
                                 frozenset(j.id for j in jobs), {}), **overrides)


class Provider:
    def __init__(self, minutes, distances=None):
        self.seconds = [[None if x is None else x * 60 for x in r] for r in minutes]
        self.distances = distances or [[None if x is None else x * 100 for x in r] for r in minutes]

    async def get_matrix(self, coordinates, profile, cache_ttl_days=None):
        return TravelMatrix(self.seconds, self.distances, profile, "AUDIT")


async def solve(d, minutes, distances=None):
    return await OrToolsPlanningSolver(Provider(minutes, distances)).solve(d)


def brief(d, r):
    return dict(routes=[dict(engineer=x.engineer_id, jobs=[j.job_id for j in x.jobs],
                              distance=x.total_distance_meters, travel=x.total_travel_min)
                        for x in r.routes], dropped=[j.job_id for j in r.unassigned],
                status=r.solver_status, objective=r.objective,
                strategy=r.objective_metrics.get("solve_strategy"),
                solver_ms=r.solver_time_ms, validation=PlanningValidator().validate(d, r))


def exhaustive_one_vehicle(d, minutes, distances):
    """Independent enumeration of every subset/order, latest TZ objective tuple."""
    n = len(d.jobs)
    best = None
    best_order = None
    for count in range(n + 1):
        for route in itertools.permutations(range(n), count):
            minute, prev, valid = d.engineers[0].shift_start_min, n, True
            dist_units, time_units = 0, 0
            for i in route:
                j = d.jobs[i]
                start = max(minute + minutes[prev][i], j.window_start_min)
                if start > j.window_end_min or start + j.duration_min > d.engineers[0].shift_end_min:
                    valid = False
                    break
                minute = start + j.duration_min
                dist_units += math.ceil(distances[prev][i] / d.config.distance_unit_meters)
                time_units += math.ceil(minutes[prev][i] * 60 / d.config.time_unit_seconds)
                prev = i
            if not valid:
                continue
            drop = sum(j.drop_penalty for i, j in enumerate(d.jobs) if i not in route)
            score = (drop, int(bool(route)), dist_units, dist_units, time_units)
            if best is None or score < best:
                best, best_order = score, [d.jobs[i].id for i in route]
    return best, best_order


async def quality_case():
    # Metric, symmetric matrices from integer grid coordinates, no equipment.
    for seed in range(1):
        rng = random.Random(seed)
        points = [(rng.randrange(30), rng.randrange(30)) for _ in range(7)]
        minutes = [[abs(a[0]-b[0]) + abs(a[1]-b[1]) for b in points] for a in points]
        distances = [[v * 100 for v in row] for row in minutes]
        jobs = [job(i+1, duration_min=rng.choice([15, 30, 45]),
                    window_start_min=480 + rng.choice([0, 30, 60, 120]),
                    window_end_min=480 + rng.choice([180, 240, 300]),
                    drop_penalty=rng.choice([1000, 2000, 4000])) for i in range(6)]
        d = data(jobs, [engineer(shift_end_min=780)])
        r = await solve(d, minutes, distances)
        exact, order = exhaustive_one_vehicle(d, minutes, distances)
        rs = r.routes[0] if r.routes else None
        units = sum(math.ceil(j.distance_from_previous_meters/10) for j in rs.jobs) if rs else 0
        actual = (r.drop_cost, int(bool(rs)), units, units, rs.total_travel_min if rs else 0)
        return dict(found_counterexample=actual > exact, seed=seed, points=points, jobs=[dict(id=j.id, duration=j.duration_min,
                        window=[j.window_start_min,j.window_end_min], penalty=j.drop_penalty) for j in jobs],
                        minutes=minutes, result=brief(d,r), actual_score=actual,
                        exhaustive_score=exact, exhaustive_route=order,
                        guided_local_search=await guided_comparison(d,minutes,distances),
                        exhaustive_timetable=route_times(d,minutes,order))


def route_times(d, minutes, route_ids):
    index_by_id = {j.id:i for i,j in enumerate(d.jobs)}
    minute, prev, times = d.engineers[0].shift_start_min, len(d.jobs), []
    for job_id in route_ids:
        i = index_by_id[job_id]
        j = d.jobs[i]
        start = max(minute+minutes[prev][i],j.window_start_min)
        minute = start+j.duration_min
        times.append(dict(job=job_id,start=start,finish=minute))
        prev = i
    return times


async def guided_comparison(d,minutes,distances):
    import planning.infrastructure.adapters.planning_solver_ortools as original
    source = Path(original.__file__).read_text()
    assert source.count("LocalSearchMetaheuristic.GREEDY_DESCENT") == 1
    # Isolated in-memory copy for an A/B experiment; production file is unchanged.
    variant = types.ModuleType("audit_gls_solver")
    exec(compile(source.replace("LocalSearchMetaheuristic.GREEDY_DESCENT",
                                "LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH"),
                 "<audit_gls_solver>","exec"),variant.__dict__)
    started=clock.perf_counter()
    result = await variant.OrToolsPlanningSolver(Provider(minutes,distances)).solve(d)
    return brief(d,result) | {"wall_ms":round((clock.perf_counter()-started)*1000)}


async def scaling():
    from planning.application.services.multi_day_planning import _enforce_sla_hierarchy
    scenarios=[]
    for n,e in ((10,3),(100,10),(100,30)):
        rng=random.Random(42)
        points=[(rng.randrange(20),rng.randrange(20)) for _ in range(n+e)]
        minutes=[[abs(a[0]-b[0])+abs(a[1]-b[1]) for b in points] for a in points]
        distances=[[v*500 for v in row] for row in minutes]
        groups=["OVERDUE","DUE_TODAY","DUE_IN_1_DAY","DUE_IN_2_3_DAYS","DUE_LATER_IN_CURRENT_BLOCK","RESERVE"]
        jobs=[job(i+1,duration_min=30,drop_penalty=[12000,9000,4000,2000,500,500][i%6]+(i%3)*50) for i in range(n)]
        d=data(jobs,[engineer(i+1,shift_end_min=960) for i in range(e)])
        decisions={j.id:{"priority_group":groups[i%6]} for i,j in enumerate(jobs)}
        d=replace(d,jobs=_enforce_sla_hierarchy(jobs,decisions,d))
        started=clock.perf_counter()
        ranges=_objective_ranges(d,{"auto":[[v*60 for v in row] for row in minutes]},{"auto":distances})
        row=dict(jobs=n,engineers=e,pmax=ranges["pmax"],maximum_objective=ranges["maximum_objective"],
                 composite_maximum=ranges["composite_maximum_objective"],strategy=ranges["solve_strategy"])
        try:
            r=await solve(d,minutes,distances)
            row.update(assigned=sum(len(route.jobs) for route in r.routes),used_engineers=len(r.routes),
                       solver_ms=r.solver_time_ms,validation=PlanningValidator().validate(d,r),status=r.solver_status)
        except Exception as exc:
            row["error"]=str(exc)
        row["wall_ms"]=round((clock.perf_counter()-started)*1000)
        scenarios.append(row)
    return scenarios


class CoordinateProvider:
    async def get_matrix(self, coordinates, profile, cache_ttl_days=None):
        seconds = [[0 if a == b else (1800 if a.longitude == 1 else 300)
                    for b in coordinates] for a in coordinates]
        return TravelMatrix(seconds, [[v*10 for v in r] for r in seconds], profile, "AUDIT")


async def candidate_failure():
    raw_job = dict(id=1, sla_date=D.isoformat(), work_type_id=1,
                   service_duration_min=60, default_service_duration_min=60,
                   required_transport=None, time_window_start="08:00:00", time_window_end="08:20:00",
                   latitude=0, longitude=0, address="job", created_at=NOW.isoformat(), status="NEW")
    source = dict(project={"timezone":"UTC"}, config=asdict(config()), jobs=[raw_job],
                  engineers=[dict(id=i, transport_type="CAR", start_address="base",
                                  start_latitude=0, start_longitude=i) for i in (1,2)],
                  schedules=[dict(engineer_id=i, work_date=D.isoformat(), shift_start="08:00:00",
                                  shift_end="10:00:00") for i in (1,2)],
                  engineer_qualifications={}, required_qualifications={}, required_equipment={}, equipment_units={})
    repo = AsyncMock()
    class Factory:
        def create(self, provider): return OrToolsPlanningSolver(CoordinateProvider())
    service = DynamicTodayPlanningService(PlanningInputNormalizer(AsyncMock()), Factory(), PlanningValidator(), repo)
    context = dict(source=source, maximum_end=D+timedelta(days=29), snapshot_time=NOW)
    try:
        result = await service._insert_one(project_id=1,event_id=1,planning_date=D,
            context=context,assignments=[],new_job=raw_job,jobs_by_id={1:raw_job})
        outcome = {"result":str(result)}
    except Exception as exc:
        outcome = {"error_type":type(exc).__name__, "error":str(exc)}
    outcome["saved_candidates"] = repo.save_candidate.await_count
    outcome["progress_engineers"] = [c.kwargs.get("current_engineer_id") for c in repo.set_candidate_progress.await_args_list]
    feasible_data = data([job(1,coordinate=Coordinate(0,0),duration_min=60,window_end_min=500,mandatory=True)],
                         [engineer(2,shift_end_min=600)])
    feasible_result = await OrToolsPlanningSolver(CoordinateProvider()).solve(feasible_data)
    outcome["engineer_2_alone"] = brief(feasible_data,feasible_result)
    return outcome


async def cache_statement_sizes():
    """Capture actual provider SQL with fake HTTP and session; no external calls."""
    from sqlalchemy.dialects.postgresql.psycopg import PGDialectAsync_psycopg
    from planning.entrypoint.config import PlanningServiceConfig
    from planning.infrastructure.adapters.travel_matrix_provider_valhalla import ValhallaTravelMatrixProvider
    counts=[]
    class Rows:
        def mappings(self): return []
    class Session:
        async def execute(self,statement):
            compiled=statement.compile(dialect=PGDialectAsync_psycopg(),compile_kwargs={"render_postcompile":True})
            counts.append(dict(operation="INSERT" if statement.is_insert else "SELECT",bind_parameters=len(compiled.params)))
            return Rows()
        async def commit(self): pass
    class Response:
        def __init__(self,payload): self.payload=payload
        def raise_for_status(self): pass
        def json(self): return self.payload
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,json):
            assert url=="/sources_to_targets"
            return Response({"sources_to_targets":[[{"time":60,"distance":1} for _ in json["targets"]] for _ in json["sources"]]})
    cfg=PlanningServiceConfig("http://unused","http://unused","",15,40)
    with patch("planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",Client):
        await ValhallaTravelMatrixProvider(Session(),cfg).get_matrix(
            [Coordinate(58+i/1000,56) for i in range(110)],"auto")
    return dict(unique_points=110,cold_cache=True,statements=counts,postgres_parameter_limit=65535,
                note="SQL compiled from actual provider; not sent to PostgreSQL")


async def main():
    results = {}
    d = data([job(1, window_end_min=500),job(2, window_start_min=540, window_end_min=600)])
    matrix = [[0,10,10],[None,0,10],[5,10,0]]
    try:
        results["missing_irrelevant_arc"] = brief(d, await solve(d,matrix))
    except Exception as exc:
        results["missing_irrelevant_arc"] = {"error":str(exc),"feasible_route":[1,2],
                                             "starts":[485,540],"missing_arc":"2 -> 1"}

    d = data([job(1)], [engineer(shift_end_min=600)])
    r = await solve(d,[[0,20],[20,0]])
    route = r.routes[0]
    delta = timedelta(minutes=10)
    bad_route = replace(route,planned_start=route.planned_start-delta,
                        planned_finish=route.planned_finish-delta,
                        jobs=[replace(j,planned_arrival=j.planned_arrival-delta,
                                      planned_start=j.planned_start-delta,
                                      planned_finish=j.planned_finish-delta) for j in route.jobs])
    bad = replace(r,routes=[bad_route])
    results["validator_early_departure"] = dict(shift_start="08:00", route_start=bad_route.planned_start.isoformat(),
                                                  first_job_start=bad_route.jobs[0].planned_start.isoformat(),
                                                  validation=PlanningValidator().validate(d,bad))
    d = data([job(1,duration_min=60,window_start_min=590,window_end_min=600)], [engineer(shift_end_min=600)])
    results["last_service_respected"] = brief(d,await solve(d,[[0,1],[1,0]]))

    d = data([job(1,required_equipment=frozenset({1}),allowed_engineer_ids=frozenset({1})),
              job(2,required_equipment=frozenset({1}),allowed_engineer_ids=frozenset({2}))],
             [engineer(1),engineer(2)],equipment_units={1:1})
    results["equipment_one_unit_two_engineers"] = brief(d,await solve(d,[[0 if i==j else 1 for j in range(4)] for i in range(4)]))
    results["candidate_failure"] = await candidate_failure()
    d = data([job(1,duration_min=10),job(2,duration_min=10)],
             [engineer(shift_end_min=540)],config=config(time_unit_seconds=3600))
    r = await solve(d,[[0,1,1],[1,0,1],[1,1,0]])
    results["coarse_time_unit"] = brief(d,r) | {"tmax":r.objective_metrics["tmax"],
                                                  "actual_time_units":sum(len(route.jobs) for route in r.routes)}

    from planning.application.services.multi_day_planning import _enforce_sla_hierarchy
    from planning.domain.enums import JobPriorityType
    jobs = [job(1,duration_min=100,drop_penalty=1009000,priority_type=JobPriorityType.EMERGENCY),
            job(2,duration_min=50,drop_penalty=9000),job(3,duration_min=50,drop_penalty=9000)]
    d = data(jobs,[engineer(shift_end_min=582)])
    decisions={j.id:{"priority_group":"DUE_TODAY"} for j in jobs}
    adjusted=_enforce_sla_hierarchy(jobs,decisions,d)
    matrix=[[0 if i==j else 1 for j in range(4)] for i in range(4)]
    raw_result=await solve(d,matrix)
    adjusted_data=replace(d,jobs=adjusted)
    adjusted_result=await solve(adjusted_data,matrix)
    results["emergency_vs_cardinality"] = dict(original_penalties=[j.drop_penalty for j in jobs],
        adjusted_penalties=[j.drop_penalty for j in adjusted],raw=brief(d,raw_result),
        adjusted=brief(adjusted_data,adjusted_result),
        exact_adjusted=exhaustive_one_vehicle(adjusted_data,matrix,[[v*100 for v in row] for row in matrix]))
    results["quality_vs_exhaustive"] = await quality_case()
    example=results["quality_vs_exhaustive"]
    jobs=[job(j["id"],duration_min=j["duration"],window_start_min=j["window"][0],
              window_end_min=j["window"][1],drop_penalty=9000) for j in example["jobs"]]
    d=data(jobs,[engineer(shift_end_min=780)])
    d=replace(d,jobs=_enforce_sla_hierarchy(jobs,{j.id:{"priority_group":"DUE_TODAY"} for j in jobs},d))
    r=await solve(d,example["minutes"])
    results["quality_with_sla_hierarchy"] = brief(d,r) | {
        "equal_original_penalty":9000,
        "exact":exhaustive_one_vehicle(d,example["minutes"],[[v*100 for v in row] for row in example["minutes"]])}
    results["synthetic_scaling"] = await scaling()
    results["cache_statement_sizes"] = await cache_statement_sizes()
    out = Path(__file__).with_name("results.json")
    out.write_text(json.dumps(results,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(results,ensure_ascii=False,indent=2),flush=True)


if __name__ == "__main__":
    asyncio.run(main())
