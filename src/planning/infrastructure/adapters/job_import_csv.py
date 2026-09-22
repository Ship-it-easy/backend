"""Durable CSV validation and atomic application of job import batches."""

import asyncio
import hashlib
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from planning.application.access import ProjectAccess
from planning.application.errors import (
    ConflictError,
    InvalidPlanningRequest,
    ObjectNotFoundError,
    PlanningUnavailable,
)
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
)
from planning.application.services.job_import_csv import issue, parse_csv
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.geocoder_factory import (
    address_provider_version,
    create_address_search_provider,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    job_import_audit,
    job_import_batches,
    job_import_rows,
    job_planning_state,
    jobs,
    planning_events,
    work_types,
)

logger = logging.getLogger(__name__)
RULES_VERSION = "csv-v1"


def _batch_dict(row):
    data = dict(row)
    data.pop("source_bytes", None)
    data["batch_id"] = data["id"]
    data["needs_warning_acknowledgement"] = data["warning_count"] > 0
    data["entity_type"] = "JOBS_CSV"
    return data


class JobImportExecutor:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: PlanningServiceConfig,
    ):
        self._sessionmaker = sessionmaker
        self._config = config
        self._tasks = {}
        self._project_locks = {}
        self._maintenance_task = None

    @property
    def address_provider_version(self) -> str:
        return address_provider_version(self._config)

    def schedule(self, batch_id: int, project_id: int):
        task = self._tasks.get(batch_id)
        if task and not task.done():
            return
        task = asyncio.create_task(
            self._validate(batch_id, project_id), name=f"job-import-{batch_id}"
        )
        self._tasks[batch_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(batch_id, None))

    async def recover(self):
        async with self._sessionmaker() as session:
            await self._expire(session)
            ids = (
                await session.execute(
                    select(
                        job_import_batches.c.id, job_import_batches.c.project_id
                    ).where(job_import_batches.c.status.in_(("UPLOADED", "VALIDATING")))
                )
            ).all()
            await session.commit()
        for batch_id, project_id in ids:
            self.schedule(int(batch_id), int(project_id))
        self._maintenance_task = asyncio.create_task(
            self._maintain(), name="job-import-maintenance"
        )

    async def _maintain(self):
        while True:
            await asyncio.sleep(3600)
            try:
                async with self._sessionmaker() as session:
                    await self._expire(session)
                    ids = (
                        await session.execute(
                            select(
                                job_import_batches.c.id, job_import_batches.c.project_id
                            ).where(
                                job_import_batches.c.status.in_(
                                    ("UPLOADED", "VALIDATING")
                                )
                            )
                        )
                    ).all()
                    await session.commit()
                for batch_id, project_id in ids:
                    self.schedule(int(batch_id), int(project_id))
            except Exception:
                logger.exception("job_import_maintenance_failed")

    async def shutdown(self):
        if self._maintenance_task:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        if self._tasks:
            for task in self._tasks.values():
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _expire(self, session):
        expired = (
            await session.execute(
                select(job_import_batches.c.id, job_import_batches.c.project_id).where(
                    job_import_batches.c.source_file_expires_at
                    <= datetime.now(timezone.utc),
                    job_import_batches.c.source_bytes.is_not(None),
                )
            )
        ).all()
        await session.execute(
            update(job_import_batches)
            .where(
                job_import_batches.c.source_file_expires_at
                <= datetime.now(timezone.utc),
                job_import_batches.c.source_bytes.is_not(None),
            )
            .values(
                source_bytes=None,
                status=case(
                    (job_import_batches.c.status == "APPLIED", "APPLIED"),
                    else_="EXPIRED",
                ),
            )
        )
        for batch_id, project_id in expired:
            await session.execute(
                insert(job_import_audit).values(
                    batch_id=batch_id,
                    project_id=project_id,
                    event_type="JOB_IMPORT_SOURCE_DELETED",
                    details={},
                )
            )

    async def _validate(self, batch_id, project_id):
        lock = self._project_locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            await self._validate_inner(batch_id)

    async def _validate_inner(self, batch_id):
        try:
            async with self._sessionmaker() as session:
                async with session.begin():
                    batch = (
                        (
                            await session.execute(
                                select(job_import_batches)
                                .where(job_import_batches.c.id == batch_id)
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if batch is None or batch["status"] in ("APPLIED", "EXPIRED"):
                        return
                    if batch["source_bytes"] is None:
                        await session.execute(
                            update(job_import_batches)
                            .where(job_import_batches.c.id == batch_id)
                            .values(status="EXPIRED")
                        )
                        return
                    content = batch["source_bytes"]
                    project_id = batch["project_id"]
                    await session.execute(
                        update(job_import_batches)
                        .where(job_import_batches.c.id == batch_id)
                        .values(
                            status="VALIDATING",
                            stage="PARSING",
                            validation_started_at=datetime.now(timezone.utc),
                            processed_rows=0,
                            error_count=0,
                            warning_count=0,
                            package_issues=[],
                        )
                    )
                    await session.execute(
                        insert(job_import_audit).values(
                            batch_id=batch_id,
                            project_id=project_id,
                            event_type="JOB_IMPORT_VALIDATION_STARTED",
                            details={},
                        )
                    )
                    await session.execute(
                        delete(job_import_rows).where(
                            job_import_rows.c.batch_id == batch_id
                        )
                    )
            encoding, delimiter, parsed_rows, package_issues = parse_csv(content)
            if not parsed_rows and not package_issues:
                package_issues = [issue("MALFORMED_CSV")]
            if parsed_rows:
                async with self._sessionmaker() as session:
                    catalog = (
                        (
                            await session.execute(
                                select(work_types).where(
                                    work_types.c.project_id == project_id
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    provider = create_address_search_provider(self._config)
                    cache = {}
                    for row in parsed_rows:
                        raw = row["raw_required_values_json"]
                        name = raw["Тип заявки ВК"].strip().casefold()
                        matches = [
                            x for x in catalog if x["name"].strip().casefold() == name
                        ]
                        if name:
                            if not matches:
                                row["issues_json"].append(
                                    issue(
                                        "UNKNOWN_WORK_TYPE",
                                        row["row_number"],
                                        "Тип заявки ВК",
                                        raw["Тип заявки ВК"],
                                    )
                                )
                            elif len(matches) > 1:
                                row["issues_json"].append(
                                    issue(
                                        "AMBIGUOUS_WORK_TYPE",
                                        row["row_number"],
                                        "Тип заявки ВК",
                                        raw["Тип заявки ВК"],
                                    )
                                )
                            elif not matches[0]["active"]:
                                row["issues_json"].append(
                                    issue(
                                        "INACTIVE_WORK_TYPE",
                                        row["row_number"],
                                        "Тип заявки ВК",
                                        raw["Тип заявки ВК"],
                                    )
                                )
                            elif not matches[0]["default_service_duration_min"]:
                                row["issues_json"].append(
                                    issue(
                                        "UNKNOWN_WORK_TYPE",
                                        row["row_number"],
                                        "Тип заявки ВК",
                                        raw["Тип заявки ВК"],
                                    )
                                )
                            else:
                                row["work_type_id"] = matches[0]["id"]
                                row["work_type_duration_min"] = matches[0][
                                    "default_service_duration_min"
                                ]
                        address = raw["Адрес"]
                        if address:
                            if address not in cache:
                                for attempt in range(3):
                                    try:
                                        cache[address] = await provider.search(address)
                                        break
                                    except PlanningUnavailable:
                                        if attempt == 2:
                                            raise
                                        await asyncio.sleep(0.5 * (2**attempt))
                            found = cache[address]
                            if not found:
                                row["issues_json"].append(
                                    issue(
                                        "ADDRESS_NOT_FOUND",
                                        row["row_number"],
                                        "Адрес",
                                        address,
                                    )
                                )
                            elif len(found) > 1:
                                row["issues_json"].append(
                                    issue(
                                        "ADDRESS_AMBIGUOUS",
                                        row["row_number"],
                                        "Адрес",
                                        address,
                                    )
                                )
                            else:
                                item = found[0]
                                details = item.get("address") or {}
                                if details.get("country_code") != "ru":
                                    row["issues_json"].append(
                                        issue(
                                            "ADDRESS_OUTSIDE_RUSSIA",
                                            row["row_number"],
                                            "Адрес",
                                            address,
                                        )
                                    )
                                elif not any(
                                    details.get(k)
                                    for k in ("city", "town", "village", "hamlet")
                                ) or not any(
                                        details.get(k)
                                        for k in (
                                            "road",
                                            "pedestrian",
                                            "street",
                                            "locality",
                                        )
                                ):
                                    row["issues_json"].append(
                                        issue(
                                            "ADDRESS_INCOMPLETE",
                                            row["row_number"],
                                            "Адрес",
                                            address,
                                        )
                                    )
                                elif not (
                                    -90 <= item["latitude"] <= 90
                                    and -180 <= item["longitude"] <= 180
                                ):
                                    row["issues_json"].append(
                                        issue(
                                            "ADDRESS_NOT_FOUND",
                                            row["row_number"],
                                            "Адрес",
                                            address,
                                        )
                                    )
                                else:
                                    row["normalized_address"] = item["display_name"]
                                    row["canonical_address_key"] = (
                                        item.get("address_key")
                                        or item["display_name"].casefold()
                                    )
                                    row["latitude"] = item["latitude"]
                                    row["longitude"] = item["longitude"]
                        await session.execute(
                            update(job_import_batches)
                            .where(job_import_batches.c.id == batch_id)
                            .values(
                                stage="VALIDATING_ROWS",
                                total_rows=len(parsed_rows),
                                processed_rows=row["row_number"] - 1,
                            )
                        )
                        await session.commit()
                    groups = defaultdict(list)
                    for row in parsed_rows:
                        if all(
                            row.get(key) is not None
                            for key in (
                                "canonical_address_key",
                                "sla_date",
                                "time_window_start",
                                "time_window_end",
                                "work_type_id",
                            )
                        ):
                            groups[
                                (
                                    row["canonical_address_key"],
                                    row["sla_date"],
                                    row["time_window_start"],
                                    row["time_window_end"],
                                    row["work_type_id"],
                                )
                            ].append(row)
                    for group in groups.values():
                        if len(group) > 1:
                            for row in group:
                                row["issues_json"].append(
                                    issue(
                                        "DUPLICATE_IN_FILE",
                                        row["row_number"],
                                        value=", ".join(
                                            str(x["row_number"]) for x in group
                                        ),
                                    )
                                )
                    for row in parsed_rows:
                        if row.get("canonical_address_key") and row.get("work_type_id"):
                            existing = await session.scalar(
                                select(jobs.c.id)
                                .where(
                                    jobs.c.project_id == project_id,
                                    jobs.c.status.notin_(("CANCELLED", "COMPLETED")),
                                    jobs.c.address == row["normalized_address"],
                                    jobs.c.sla_date == row["sla_date"],
                                    jobs.c.time_window_start
                                    == row["time_window_start"],
                                    jobs.c.time_window_end == row["time_window_end"],
                                    jobs.c.work_type_id == row["work_type_id"],
                                )
                                .limit(1)
                            )
                            if existing:
                                row["issues_json"].append(
                                    issue(
                                        "POSSIBLE_EXISTING_DUPLICATE",
                                        row["row_number"],
                                        severity="WARNING",
                                        value=existing,
                                    )
                                )
                    for row in parsed_rows:
                        errors = any(
                            x["severity"] == "ERROR" for x in row["issues_json"]
                        )
                        warnings = any(
                            x["severity"] == "WARNING" for x in row["issues_json"]
                        )
                        row["severity"] = (
                            "ERROR" if errors else "WARNING" if warnings else None
                        )
                        await session.execute(
                            insert(job_import_rows).values(batch_id=batch_id, **row)
                        )
                    await session.commit()
            errors = len(package_issues) + sum(
                x["severity"] == "ERROR"
                for row in parsed_rows
                for x in row["issues_json"]
            )
            warnings = sum(
                x["severity"] == "WARNING"
                for row in parsed_rows
                for x in row["issues_json"]
            )
            async with self._sessionmaker() as session:
                await session.execute(
                    update(job_import_batches)
                    .where(job_import_batches.c.id == batch_id)
                    .values(
                        status="HAS_ERRORS" if errors else "READY_TO_APPLY",
                        stage="DONE",
                        source_encoding=encoding,
                        source_delimiter=delimiter,
                        validation_rules_version=RULES_VERSION,
                        address_provider_version=self.address_provider_version,
                        total_rows=len(parsed_rows),
                        processed_rows=len(parsed_rows),
                        error_count=errors,
                        warning_count=warnings,
                        package_issues=package_issues,
                        validation_finished_at=datetime.now(timezone.utc),
                    )
                )
                await session.execute(
                    insert(job_import_audit).values(
                        batch_id=batch_id,
                        project_id=project_id,
                        event_type="JOB_IMPORT_HAS_ERRORS"
                        if errors
                        else "JOB_IMPORT_VALIDATED",
                        details={
                            "rows": len(parsed_rows),
                            "errors": errors,
                            "warnings": warnings,
                        },
                    )
                )
                await session.commit()
        except PlanningUnavailable:
            await self._technical_error(batch_id, "ADDRESS_SERVICE_UNAVAILABLE")
        except Exception:
            logger.exception("job_import_validation_failed batch_id=%s", batch_id)
            await self._technical_error(batch_id, "VALIDATION_FAILED")

    async def _technical_error(self, batch_id, category):
        async with self._sessionmaker() as session:
            project_id = await session.scalar(
                select(job_import_batches.c.project_id).where(
                    job_import_batches.c.id == batch_id
                )
            )
            await session.execute(
                update(job_import_batches)
                .where(job_import_batches.c.id == batch_id)
                .values(
                    status="TECHNICAL_ERROR",
                    stage="DONE",
                    technical_error_category=category,
                    package_issues=[issue(category)],
                    validation_finished_at=datetime.now(timezone.utc),
                )
            )
            if project_id:
                await session.execute(
                    insert(job_import_audit).values(
                        batch_id=batch_id,
                        project_id=project_id,
                        event_type="JOB_IMPORT_TECHNICAL_ERROR",
                        details={"category": category},
                    )
                )
            await session.commit()


class JobImportService:
    def __init__(
        self,
        session: AsyncSession,
        access: ProjectAccess,
        executor: JobImportExecutor,
        planning_executor: PlanningBatchExecutor,
    ):
        self.session = session
        self.access = access
        self.executor = executor
        self.planning_executor = planning_executor

    async def _authorized(self, project_id, write=False):
        return await self.access.project(project_id, write=write)

    async def ensure_access(self, project_id, write=False):
        await self._authorized(project_id, write=write)

    async def _batch(self, project_id, batch_id, lock=False):
        query = select(job_import_batches).where(
            job_import_batches.c.id == batch_id,
            job_import_batches.c.project_id == project_id,
        )
        if lock:
            query = query.with_for_update()
        batch = (await self.session.execute(query)).mappings().one_or_none()
        if batch is None:
            raise ObjectNotFoundError("Import batch not found")
        return batch

    async def upload(self, project_id, filename, content):
        user = await self._authorized(project_id, write=True)
        if Path(filename or "").suffix.lower() != ".csv":
            raise InvalidPlanningRequest("Требуется файл CSV", code="INVALID_FILE_TYPE")
        if len(content) > 10 * 1024 * 1024:
            raise InvalidPlanningRequest("Файл больше 10 МБ", code="FILE_TOO_LARGE")
        digest = hashlib.sha256(content).hexdigest()
        existing = (
            (
                await self.session.execute(
                    select(job_import_batches)
                    .where(
                        job_import_batches.c.project_id == project_id,
                        job_import_batches.c.content_sha256 == digest,
                    )
                    .order_by(job_import_batches.c.id.desc())
                )
            )
            .mappings()
            .all()
        )
        if any(row["status"] == "APPLIED" for row in existing):
            raise ConflictError("Этот файл уже применён", code="FILE_ALREADY_APPLIED")
        reusable = next((row for row in existing if row["status"] != "EXPIRED"), None)
        if reusable:
            # A previous validation may have been performed by an older
            # application image (for example before a parser compatibility
            # fix). Re-run failed reusable batches when the same file is
            # uploaded instead of returning stale issues forever.
            if reusable["status"] in (
                "HAS_ERRORS",
                "FAILED_VALIDATION",
                "TECHNICAL_ERROR",
                "STALE_VALIDATION",
            ):
                await self.session.execute(
                    update(job_import_batches)
                    .where(job_import_batches.c.id == reusable["id"])
                    .values(status="UPLOADED")
                )
                await self.session.commit()
                self.executor.schedule(int(reusable["id"]), project_id)
                return _batch_dict(await self._batch(project_id, reusable["id"]))
            return _batch_dict(reusable)
        try:
            batch_id = await self.session.scalar(
                insert(job_import_batches)
                .values(
                    project_id=project_id,
                    created_by=user.id,
                    original_filename=Path(filename).name[:255],
                    content_sha256=digest,
                    source_bytes=content,
                    status="UPLOADED",
                    source_file_expires_at=datetime.now(timezone.utc)
                    + timedelta(days=30),
                )
                .returning(job_import_batches.c.id)
            )
            await self.session.execute(
                insert(job_import_audit).values(
                    batch_id=batch_id,
                    project_id=project_id,
                    actor_user_id=user.id,
                    event_type="JOB_IMPORT_UPLOADED",
                    details={"sha256": digest},
                )
            )
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            row = (
                (
                    await self.session.execute(
                        select(job_import_batches).where(
                            job_import_batches.c.project_id == project_id,
                            job_import_batches.c.content_sha256 == digest,
                            job_import_batches.c.status != "EXPIRED",
                        )
                    )
                )
                .mappings()
                .one()
            )
            if row["status"] == "APPLIED":
                raise ConflictError(
                    "Этот файл уже применён", code="FILE_ALREADY_APPLIED"
                ) from error
            return _batch_dict(row)
        self.executor.schedule(int(batch_id), project_id)
        return _batch_dict(await self._batch(project_id, batch_id))

    async def get(self, project_id, batch_id):
        await self._authorized(project_id)
        data = _batch_dict(await self._batch(project_id, batch_id))
        summary = (
            (
                await self.session.execute(
                    select(
                        func.min(job_import_rows.c.sla_date).label("sla_date_from"),
                        func.max(job_import_rows.c.sla_date).label("sla_date_to"),
                        func.count(func.distinct(job_import_rows.c.work_type_id)).label(
                            "work_type_count"
                        ),
                        func.count(
                            func.distinct(job_import_rows.c.canonical_address_key)
                        ).label("address_count"),
                    ).where(job_import_rows.c.batch_id == batch_id)
                )
            )
            .mappings()
            .one()
        )
        data["summary"] = dict(summary)
        return data

    async def list(self, project_id, limit=50, offset=0):
        await self._authorized(project_id)
        rows = (
            (
                await self.session.execute(
                    select(job_import_batches)
                    .where(job_import_batches.c.project_id == project_id)
                    .order_by(job_import_batches.c.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return [_batch_dict(row) for row in rows]

    async def issues(self, project_id, batch_id, severity=None, query="", page=1):
        await self._authorized(project_id)
        batch = await self._batch(project_id, batch_id)
        values = list(batch["package_issues"] or [])
        rows = (
            (
                await self.session.execute(
                    select(job_import_rows.c.issues_json).where(
                        job_import_rows.c.batch_id == batch_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            values.extend(row)
        if severity:
            values = [x for x in values if x["severity"] == severity]
        if query:
            q = query.casefold()
            values = [
                x
                for x in values
                if q in str(x.get("row_number") or "").casefold()
                or q in x["code"].casefold()
            ]
        values.sort(key=lambda x: (x["severity"] != "ERROR", x.get("row_number") or 0))
        return {
            "items": values[(page - 1) * 50 : page * 50],
            "total": len(values),
            "page": page,
        }

    async def revalidate(self, project_id, batch_id):
        await self._authorized(project_id, write=True)
        batch = await self._batch(project_id, batch_id)
        if (
            batch["status"]
            not in (
                "TECHNICAL_ERROR",
                "STALE_VALIDATION",
                "HAS_ERRORS",
                "READY_TO_APPLY",
            )
            or batch["source_bytes"] is None
        ):
            raise ConflictError(
                "Повторная проверка недоступна", code="IMPORT_NOT_REVALIDATABLE"
            )
        await self.session.execute(
            update(job_import_batches)
            .where(job_import_batches.c.id == batch_id)
            .values(status="UPLOADED")
        )
        await self.session.commit()
        self.executor.schedule(batch_id, project_id)
        return await self.get(project_id, batch_id)

    async def apply(
        self, project_id, batch_id, acknowledge_warnings=False, idempotency_key=None
    ):
        user = await self._authorized(project_id, write=True)
        batch = await self._batch(project_id, batch_id, lock=True)
        if batch["status"] == "APPLIED":
            if idempotency_key and batch["apply_idempotency_key"] == idempotency_key:
                return _batch_dict(batch)
            raise ConflictError("Пакет уже применён", code="IMPORT_ALREADY_APPLIED")
        if batch["status"] != "READY_TO_APPLY":
            raise ConflictError("Пакет не готов к применению", code="IMPORT_HAS_ERRORS")
        if (
            batch["source_bytes"] is None
            or hashlib.sha256(batch["source_bytes"]).hexdigest()
            != batch["content_sha256"]
        ):
            raise ConflictError("Исходный файл изменился", code="STALE_VALIDATION")
        if (batch["validation_rules_version"], batch["address_provider_version"]) != (
            RULES_VERSION,
            self.executor.address_provider_version,
        ):
            await self.session.execute(
                update(job_import_batches)
                .where(job_import_batches.c.id == batch_id)
                .values(status="STALE_VALIDATION")
            )
            await self.session.commit()
            raise ConflictError("Проверка устарела", code="STALE_VALIDATION")
        if batch["warning_count"] and not acknowledge_warnings:
            raise ConflictError(
                "Подтвердите возможные дубли", code="WARNINGS_NOT_ACKNOWLEDGED"
            )
        if batch["warning_count"]:
            await self.session.execute(
                insert(job_import_audit).values(
                    batch_id=batch_id,
                    project_id=project_id,
                    actor_user_id=user.id,
                    event_type="JOB_IMPORT_WARNINGS_ACKNOWLEDGED",
                    details={"warnings": batch["warning_count"]},
                )
            )
        await self.session.execute(
            insert(job_import_audit).values(
                batch_id=batch_id,
                project_id=project_id,
                actor_user_id=user.id,
                event_type="JOB_IMPORT_APPLY_STARTED",
                details={},
            )
        )
        duplicate = await self.session.scalar(
            select(job_import_batches.c.id)
            .where(
                job_import_batches.c.project_id == project_id,
                job_import_batches.c.content_sha256 == batch["content_sha256"],
                job_import_batches.c.status == "APPLIED",
                job_import_batches.c.id != batch_id,
            )
            .limit(1)
        )
        if duplicate:
            raise ConflictError("Этот файл уже применён", code="FILE_ALREADY_APPLIED")
        rows = (
            (
                await self.session.execute(
                    select(job_import_rows)
                    .where(job_import_rows.c.batch_id == batch_id)
                    .order_by(job_import_rows.c.row_number)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) != batch["total_rows"] or any(
            any(i["severity"] == "ERROR" for i in row["issues_json"]) for row in rows
        ):
            raise ConflictError("Проверка устарела", code="STALE_VALIDATION")
        ids = {row["work_type_id"] for row in rows}
        catalog = (
            (
                await self.session.execute(
                    select(work_types).where(
                        work_types.c.project_id == project_id,
                        work_types.c.id.in_(ids),
                        work_types.c.active.is_(True),
                    )
                )
            )
            .mappings()
            .all()
        )
        current = {x["id"]: x for x in catalog}
        if len(current) != len(ids) or any(
            current[row["work_type_id"]]["default_service_duration_min"]
            != row["work_type_duration_min"]
            for row in rows
            if row["work_type_id"] in current
        ):
            await self.session.execute(
                update(job_import_batches)
                .where(job_import_batches.c.id == batch_id)
                .values(status="STALE_VALIDATION")
            )
            await self.session.commit()
            raise ConflictError(
                "Справочник изменился, повторите проверку", code="STALE_VALIDATION"
            )
        inserted = []
        for row in rows:
            job_id = await self.session.scalar(
                insert(jobs)
                .values(
                    project_id=project_id,
                    import_batch_id=batch_id,
                    import_row_number=row["row_number"],
                    external_id=None,
                    internal_code=f"JOB-{uuid.uuid4().hex[:12].upper()}",
                    status="NEW",
                    priority_type="NORMAL",
                    address=row["normalized_address"],
                    address_hash=hashlib.sha256(
                        row["canonical_address_key"].encode("utf-8")
                    ).hexdigest(),
                    latitude=row["latitude"],
                    longitude=row["longitude"],
                    sla_date=row["sla_date"],
                    time_window_start=row["time_window_start"],
                    time_window_end=row["time_window_end"],
                    work_type_id=row["work_type_id"],
                    service_duration_min=current[row["work_type_id"]][
                        "default_service_duration_min"
                    ],
                )
                .returning(jobs.c.id)
            )
            inserted.append(int(job_id))
            await self.session.execute(
                insert(job_planning_state).values(
                    job_id=job_id, project_id=project_id, state="UNASSIGNED"
                )
            )
            await self.session.execute(
                update(job_import_rows)
                .where(job_import_rows.c.id == row["id"])
                .values(created_job_id=job_id)
            )
        event_id = await self.session.scalar(
            insert(planning_events)
            .values(
                project_id=project_id,
                event_type="JOBS_IMPORTED",
                job_ids=inserted,
                event_payload={"import_batch_id": batch_id},
                initiator="USER",
                actor_user_id=user.id,
                idempotency_key=f"job-import:{batch_id}",
                state="PENDING",
            )
            .returning(planning_events.c.id)
        )
        await self.session.execute(
            update(job_import_batches)
            .where(job_import_batches.c.id == batch_id)
            .values(
                status="APPLIED",
                created_count=len(inserted),
                planning_event_id=event_id,
                apply_idempotency_key=idempotency_key,
                warnings_acknowledged_by=user.id if batch["warning_count"] else None,
                warnings_acknowledged_at=datetime.now(timezone.utc)
                if batch["warning_count"]
                else None,
                applied_at=datetime.now(timezone.utc),
            )
        )
        await self.session.execute(
            insert(job_import_audit).values(
                batch_id=batch_id,
                project_id=project_id,
                actor_user_id=user.id,
                event_type="JOB_IMPORT_APPLIED",
                details={
                    "created_count": len(inserted),
                    "planning_event_id": int(event_id),
                },
            )
        )
        await self.session.execute(
            insert(job_import_audit).values(
                batch_id=batch_id,
                project_id=project_id,
                actor_user_id=user.id,
                event_type="PLANNING_EVENT_CREATED_FROM_IMPORT",
                details={"planning_event_id": int(event_id)},
            )
        )
        await self.session.commit()
        self.planning_executor.schedule_project(project_id)
        return await self.get(project_id, batch_id)
