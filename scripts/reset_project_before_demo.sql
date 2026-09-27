-- Полный сброс операционных данных одного участка перед демонстрацией.
--
-- Удаляется:
--   * заявки и история их статусов;
--   * история CSV-импортов заявок;
--   * все запуски, пакеты, события, версии и результаты планирования;
--   * опубликованные планы, назначения и baseline/FIFO-аудит.
--
-- Сохраняется:
--   * участок и назначенные ему пользователи;
--   * инженеры, их графики и квалификации;
--   * типы работ, квалификации, оборудование и его доступность;
--   * настройки планирования;
--   * общие кэши геокодирования и времени в пути.
--
-- Скрипт рассчитан на psql и схему приложения на Alembic head a4d6f2b91c70.
-- Перед запуском остановите backend и сделайте pg_dump.
-- Обязательные переменные psql:
--   project_code         -- projects.internal_code очищаемого участка
--   confirm_project_code -- тот же код; защита от случайного запуска
--   confirm_cleanup      -- точная строка DELETE_ALL_JOBS_AND_PLANS
--
-- Пример:
--   psql ... \
--     -v project_code='DEMO_MOSCOW' \
--     -v confirm_project_code='DEMO_MOSCOW' \
--     -v confirm_cleanup='DELETE_ALL_JOBS_AND_PLANS' \
--     -f scripts/reset_project_before_demo.sql

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '5min';

-- Не продолжаем на другой версии схемы: новая таблица с FK на заявки или планы
-- должна быть явно добавлена в этот скрипт.
DO $guard$
DECLARE
    actual_heads text;
BEGIN
    SELECT string_agg(version_num, ',' ORDER BY version_num)
      INTO actual_heads
      FROM alembic_version;

    IF actual_heads IS DISTINCT FROM 'a4d6f2b91c70' THEN
        RAISE EXCEPTION
            'Неподдерживаемая версия схемы: %, ожидалась a4d6f2b91c70',
            coalesce(actual_heads, '<нет версии>');
    END IF;
END
$guard$;

CREATE TEMP TABLE demo_cleanup_target ON COMMIT DROP AS
SELECT
    p.id AS project_id,
    p.internal_code,
    p.name,
    (p.internal_code = :'confirm_project_code') AS project_confirmed,
    (:'confirm_cleanup' = 'DELETE_ALL_JOBS_AND_PLANS') AS cleanup_confirmed
FROM projects AS p
WHERE p.internal_code = :'project_code';

DO $guard$
DECLARE
    target_count integer;
    is_project_confirmed boolean;
    is_cleanup_confirmed boolean;
BEGIN
    SELECT
        count(*),
        bool_and(project_confirmed),
        bool_and(cleanup_confirmed)
      INTO target_count, is_project_confirmed, is_cleanup_confirmed
      FROM demo_cleanup_target;

    IF target_count <> 1 THEN
        RAISE EXCEPTION 'Участок с указанным project_code не найден';
    END IF;

    IF is_project_confirmed IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'confirm_project_code не совпал с project_code';
    END IF;

    IF is_cleanup_confirmed IS DISTINCT FROM true THEN
        RAISE EXCEPTION
            'Для удаления требуется confirm_cleanup=DELETE_ALL_JOBS_AND_PLANS';
    END IF;
END
$guard$;

-- Блокируем выбранную запись участка. Backend всё равно следует остановить:
-- его фоновые задачи не должны работать одновременно с очисткой.
SELECT p.id, p.internal_code, p.name
FROM projects AS p
JOIN demo_cleanup_target AS t ON t.project_id = p.id
FOR UPDATE;

-- Фиксируем количество сохраняемых данных и затем проверяем, что оно не изменилось.
CREATE TEMP TABLE demo_preserved_counts (
    entity text PRIMARY KEY,
    row_count bigint NOT NULL
) ON COMMIT DROP;

INSERT INTO demo_preserved_counts (entity, row_count)
SELECT 'projects', count(*)
FROM projects p JOIN demo_cleanup_target t ON t.project_id = p.id
UNION ALL
SELECT 'dispatcher_projects', count(*)
FROM dispatcher_projects x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'users', count(*)
FROM users x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'engineers', count(*)
FROM engineers x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'engineer_schedules', count(*)
FROM engineer_schedules x
JOIN engineers e ON e.id = x.engineer_id
JOIN demo_cleanup_target t ON t.project_id = e.project_id
UNION ALL
SELECT 'engineer_qualifications', count(*)
FROM engineer_qualifications x
JOIN engineers e ON e.id = x.engineer_id
JOIN demo_cleanup_target t ON t.project_id = e.project_id
UNION ALL
SELECT 'work_type_required_qualifications', count(*)
FROM work_type_required_qualifications x
JOIN work_types w ON w.id = x.work_type_id
JOIN demo_cleanup_target t ON t.project_id = w.project_id
UNION ALL
SELECT 'work_type_required_equipment', count(*)
FROM work_type_required_equipment x
JOIN work_types w ON w.id = x.work_type_id
JOIN demo_cleanup_target t ON t.project_id = w.project_id
UNION ALL
SELECT 'engineer_availability_events', count(*)
FROM engineer_availability_events x
JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'work_types', count(*)
FROM work_types x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'qualifications', count(*)
FROM qualifications x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'equipment_types', count(*)
FROM equipment_types x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'equipment_availability', count(*)
FROM equipment_availability x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'planning_config', count(*)
FROM planning_config x JOIN demo_cleanup_target t ON t.project_id = x.project_id;

\echo '=== Что будет сохранено ==='
TABLE demo_preserved_counts;

\echo '=== Что будет удалено ==='
SELECT
    (SELECT count(*) FROM jobs x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS jobs,
    (SELECT count(*) FROM job_import_batches x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS imports,
    (SELECT count(*) FROM planning_runs x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS planning_runs,
    (SELECT count(*) FROM planning_batches x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS planning_batches,
    (SELECT count(*) FROM planning_events x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS planning_events,
    (SELECT count(*) FROM project_plan_versions x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS project_plan_versions,
    (SELECT count(*) FROM daily_plans x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS legacy_daily_plans;

-- Сначала удаляем историю, которая ссылается на назначения без ON DELETE CASCADE.
DELETE FROM job_status_history AS h
USING jobs AS j, demo_cleanup_target AS t
WHERE h.job_id = j.id
  AND j.project_id = t.project_id;

DELETE FROM job_planning_state AS s
USING demo_cleanup_target AS t
WHERE s.project_id = t.project_id;

-- Разрываем циклические ссылки старой и новой моделей опубликованного плана.
UPDATE daily_plans AS d
SET current_version_id = NULL
FROM demo_cleanup_target AS t
WHERE d.project_id = t.project_id;

UPDATE planning_events AS e
SET published_plan_version_id = NULL
FROM demo_cleanup_target AS t
WHERE e.project_id = t.project_id;

-- planning_event_id в истории импорта намеренно не имеет внешнего ключа.
UPDATE job_import_batches AS b
SET planning_event_id = NULL
FROM demo_cleanup_target AS t
WHERE b.project_id = t.project_id;

-- Новая модель опубликованных планов. Каскадом удаляются результаты по дням,
-- назначения, изменения плана и снимки отменённых заявок.
DELETE FROM project_plan_versions AS v
USING demo_cleanup_target AS t
WHERE v.project_id = t.project_id;

-- Старая модель опубликованных планов. Каскадом удаляются версии и назначения.
DELETE FROM daily_plans AS d
USING demo_cleanup_target AS t
WHERE d.project_id = t.project_id;

-- События удаляют candidate_evaluations каскадом.
DELETE FROM planning_events AS e
USING demo_cleanup_target AS t
WHERE e.project_id = t.project_id;

-- Baseline удаляет маршруты, строки маршрутов, неназначенные заявки и сравнения.
DELETE FROM planning_baseline_results AS r
USING demo_cleanup_target AS t
WHERE r.project_id = t.project_id;

-- Эти таблицы ссылаются и на пакет, и на запуск без каскада по запуску.
DELETE FROM planning_batch_days AS d
USING planning_batches AS b, demo_cleanup_target AS t
WHERE d.planning_batch_id = b.id
  AND b.project_id = t.project_id;

DELETE FROM planning_batch_jobs AS j
USING planning_batches AS b, demo_cleanup_target AS t
WHERE j.planning_batch_id = b.id
  AND b.project_id = t.project_id;

-- Каскадом удаляются маршруты, заявки маршрутов, оборудование и неназначенные.
DELETE FROM planning_runs AS r
USING demo_cleanup_target AS t
WHERE r.project_id = t.project_id;

DELETE FROM planning_batches AS b
USING demo_cleanup_target AS t
WHERE b.project_id = t.project_id;

-- Удаляем строки/аудит импорта до заявок: строки импорта ссылаются на jobs.
DELETE FROM job_import_rows AS r
USING job_import_batches AS b, demo_cleanup_target AS t
WHERE r.batch_id = b.id
  AND b.project_id = t.project_id;

DELETE FROM job_import_audit AS a
USING demo_cleanup_target AS t
WHERE a.project_id = t.project_id;

DELETE FROM jobs AS j
USING demo_cleanup_target AS t
WHERE j.project_id = t.project_id;

DELETE FROM job_import_batches AS b
USING demo_cleanup_target AS t
WHERE b.project_id = t.project_id;

-- Проверка: ни одного операционного объекта участка не должно остаться.
DO $verify$
DECLARE
    leftovers bigint;
BEGIN
    SELECT
        (SELECT count(*) FROM jobs x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      + (SELECT count(*) FROM job_import_batches x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      + (SELECT count(*) FROM job_import_audit x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      + (SELECT count(*) FROM planning_runs x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      + (SELECT count(*) FROM planning_batches x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      + (SELECT count(*) FROM planning_events x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      + (SELECT count(*) FROM project_plan_versions x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      + (SELECT count(*) FROM daily_plans x
          JOIN demo_cleanup_target t ON t.project_id = x.project_id)
      INTO leftovers;

    IF leftovers <> 0 THEN
        RAISE EXCEPTION
            'После очистки осталось % операционных записей; транзакция отменена',
            leftovers;
    END IF;
END
$verify$;

CREATE TEMP TABLE demo_preserved_counts_after (
    entity text PRIMARY KEY,
    row_count bigint NOT NULL
) ON COMMIT DROP;

INSERT INTO demo_preserved_counts_after (entity, row_count)
SELECT 'projects', count(*)
FROM projects p JOIN demo_cleanup_target t ON t.project_id = p.id
UNION ALL
SELECT 'dispatcher_projects', count(*)
FROM dispatcher_projects x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'users', count(*)
FROM users x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'engineers', count(*)
FROM engineers x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'engineer_schedules', count(*)
FROM engineer_schedules x
JOIN engineers e ON e.id = x.engineer_id
JOIN demo_cleanup_target t ON t.project_id = e.project_id
UNION ALL
SELECT 'engineer_qualifications', count(*)
FROM engineer_qualifications x
JOIN engineers e ON e.id = x.engineer_id
JOIN demo_cleanup_target t ON t.project_id = e.project_id
UNION ALL
SELECT 'work_type_required_qualifications', count(*)
FROM work_type_required_qualifications x
JOIN work_types w ON w.id = x.work_type_id
JOIN demo_cleanup_target t ON t.project_id = w.project_id
UNION ALL
SELECT 'work_type_required_equipment', count(*)
FROM work_type_required_equipment x
JOIN work_types w ON w.id = x.work_type_id
JOIN demo_cleanup_target t ON t.project_id = w.project_id
UNION ALL
SELECT 'engineer_availability_events', count(*)
FROM engineer_availability_events x
JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'work_types', count(*)
FROM work_types x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'qualifications', count(*)
FROM qualifications x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'equipment_types', count(*)
FROM equipment_types x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'equipment_availability', count(*)
FROM equipment_availability x JOIN demo_cleanup_target t ON t.project_id = x.project_id
UNION ALL
SELECT 'planning_config', count(*)
FROM planning_config x JOIN demo_cleanup_target t ON t.project_id = x.project_id;

DO $verify$
DECLARE
    changed_entities text;
BEGIN
    SELECT string_agg(
               before.entity || ': ' || before.row_count || ' -> ' || after.row_count,
               ', ' ORDER BY before.entity
           )
      INTO changed_entities
      FROM demo_preserved_counts AS before
      JOIN demo_preserved_counts_after AS after USING (entity)
     WHERE before.row_count <> after.row_count;

    IF changed_entities IS NOT NULL THEN
        RAISE EXCEPTION
            'Изменились сохраняемые данные (%); транзакция отменена',
            changed_entities;
    END IF;
END
$verify$;

\echo '=== Проверка после очистки: всё должно быть 0 ==='
SELECT
    (SELECT count(*) FROM jobs x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS jobs,
    (SELECT count(*) FROM job_import_batches x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS imports,
    (SELECT count(*) FROM planning_runs x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS planning_runs,
    (SELECT count(*) FROM planning_batches x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS planning_batches,
    (SELECT count(*) FROM planning_events x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS planning_events,
    (SELECT count(*) FROM project_plan_versions x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS project_plan_versions,
    (SELECT count(*) FROM daily_plans x
      JOIN demo_cleanup_target t ON t.project_id = x.project_id) AS legacy_daily_plans;

COMMIT;

\echo 'Очистка успешно зафиксирована.'
