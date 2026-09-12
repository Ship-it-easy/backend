# План реализации backend дневного планирования

## 1. Архитектура

Добавить отдельный bounded context `src/planning` в той же Clean Architecture, что и
`auth`:

```text
src/planning/
├── domain/          # Job, Engineer, PlanningRun, Route, enum и правила
├── application/     # orchestration, normalizer, penalty, validator, interfaces
├── infrastructure/  # PostgreSQL, OR-Tools, Valhalla, Nominatim
├── presentation/    # FastAPI handlers и DTO
└── entrypoint/       # Dishka providers и подключение router
```

`PlanningService` выполняет один pipeline:

```text
create run + snapshot -> normalize -> compatibility + penalty -> travel matrix
-> OR-Tools -> reason resolver -> independent validator -> atomic save
```

Расчёт остаётся синхронным, как требует ТЗ. RabbitMQ и отдельный worker для первого
этапа не нужны.

## 2. Технологии

- Оставить текущие Python 3.12, FastAPI, Dishka, async SQLAlchemy, PostgreSQL и
  Alembic.
- Добавить `ortools` с зафиксированной версией — solver VRPTW и optional jobs.
- Добавить `httpx` — клиенты локальных геосервисов с явным timeout; повторный
  запуск безопасен благодаря кешу координат и дорожных дуг.
- Использовать стандартные `zoneinfo`, `datetime` и целочисленные минуты от начала
  дня в переданном IANA timezone внутри solver. Все datetime на входе/выходе API и
  в БД — UTC (`Z` в API, `timestamptz` в PostgreSQL).
- `STATIC_TEST` оставить как детерминированный локальный провайдер для ручной
  проверки без сетевых запросов.
- Использовать обычные структурированные логи без отдельной системы метрик в
  первой итерации.

## 3. Бесплатная локальная замена Yandex

- `Valhalla + OpenStreetMap` — локальный Docker-сервис матриц времени. Профили:
  `CAR -> auto`, `NONE -> pedestrian`.
- `Nominatim + OpenStreetMap` — локальное геокодирование адресов.
- В `docker-compose.yml` добавить оба сервиса, healthcheck и persistent volumes;
  для первой итерации загружать готовый OSM-экстракт Пермского края и ограничивать
  геокодирование его границами, а не загружать весь федеральный округ или планету.
- В приложении оставить интерфейсы `TravelMatrixProvider` и `Geocoder`, реализации
  `STATIC_TEST`, `VALHALLA_LOCAL` и `NOMINATIM_LOCAL`.
- В demo seed заявки Перми хранятся с реальными адресами и пустыми `latitude`/
  `longitude`; перед расчётом `NOMINATIM_LOCAL` геокодирует их, после чего
  `VALHALLA_LOCAL` строит дорожную матрицу по полученным координатам.
- Матрицу запрашивать блоками и кешировать по координатам, профилю и версии OSM.
  Недоступную дугу хранить как запрещённую, а не как нулевое время.
- У локального стека нет live-traffic: `traffic_reference_time` сохраняется в
  snapshot, но на стоимость дуг не влияет. Yandex-провайдер можно добавить позже
  без изменения solver.

## 4. База данных

Одной Alembic-цепочкой добавить таблицы из ТЗ:

- исходные: `projects`, `jobs`, `engineers`, `engineer_schedules`, `work_types`,
  qualifications/equipment и их M:N-связи;
- настройки: версионируемая `planning_config`;
- результаты: `planning_runs`, `planning_routes`, `planning_route_jobs`,
  `planning_equipment_assignments`, `planning_unassigned_jobs`;
- геокеш: адрес, `address_hash`, координаты, provider/version, `geocoded_at`;
- кеш матриц/дуг или отдельный snapshot матрицы с hash.

Обязательные ограничения БД: проектные FK/unique, уникальность заявки внутри run,
уникальность sequence и partial unique index на один активный run для
`(project_id, planning_date)`. Исходный snapshot читать в короткой транзакции
`REPEATABLE READ`, затем коммитить и запускать solver без удержания транзакции.

## 5. Solver и правила

1. Нормализовать заявки строго по reason codes из ТЗ; ошибка одной заявки не
   останавливает run.
   Для инженера входной контракт гарантирует полную пару стартовых координат либо
   `start_address`; адрес геокодируется до построения матрицы.
2. Посчитать `BaseCompatible`, SLA/drop penalty и детерминированно применить
   `max_jobs_per_run`.
3. Построить `RoutingModel`: engineer = vehicle, job = optional node, отдельные
   start и dummy end, vehicle-specific transit для `auto/pedestrian`.
4. Добавить Time Dimension: service + travel, ожидание через slack, окна начала и
   границы смены; возврат на базу имеет нулевую стоимость.
5. Связать `VehicleVar(job)` с boolean `Assigned[j,e]`, затем добавить
   `UsesEquipment[e,r]` и дневные лимиты оборудования.
6. После решения сформировать assigned/unassigned, посчитать метрики и запустить
   независимый `PlanningValidator`. Только валидный результат становится SUCCESS.

Целевая функция реализуется без дополнительных скрытых коэффициентов:

```text
DropCost = sum(DropPenalty[j] for j in Unassigned)
TravelCost = sum(TravelTime[arc] for used arc)
Objective = DropCost + TravelCost
```

Для просроченной заявки SLA-часть считается один раз:
`10_000 + 1_000 * OverdueDays`. После этого один раз добавляются бонусы дефицита
инженеров, оборудования и узкого окна.

Переход от последней заявки к dummy end имеет нулевые travel time и arc cost, но
учитывает service duration последней заявки. Поэтому фактическое завершение
последней работы обязательно находится внутри смены; возвращение на базу не
рассчитывается.

Ограничения оборудования сразу реализовать через boolean constraints OR-Tools.
Нагрузочную проверку объёмов 50/100/250/1000 заявок перенести на следующую итерацию:
матрица растёт как O(n²), поэтому значение 1000 до замеров считается защитным
конфигом, а не подтверждённой производительностью.

## 6. API и доступ

Реализовать три endpoint из ТЗ:

- `POST /api/projects/{project_id}/planning/runs`;
- `GET /api/projects/{project_id}/planning/runs/{run_id}`;
- `GET /api/projects/{project_id}/planning/runs`.

Тело запуска первой итерации:

```json
{
  "planning_date": "2026-09-12",
  "timezone": "Asia/Yekaterinburg"
}
```

`timezone` обязателен и валидируется как IANA timezone. В первой итерации
`planning_date` должен совпадать с текущей датой в переданном timezone. Смены и
окна интерпретируются в нём, затем все рассчитанные datetime переводятся в UTC.
Для запуска на текущий день нижняя граница маршрута — следующая полная минута после
текущего времени. В snapshot сохраняются и исходный timezone, и UTC-моменты.

Подключить существующую session-auth: глобальный `ADMIN` запускает расчёт,
`USER`/`ADMIN` читают результат и историю. Проектные membership-роли в текущей
модели данных отсутствуют и при необходимости добавляются отдельной итерацией.
Все ответы и ошибки описать Pydantic-схемами в OpenAPI. Timeout backend установить
выше `solver_time_limit_sec` плюс запас на подготовку.

## 7. Порядок реализации

1. Миграции, доменные модели, enums и seed тестового проекта/config.
2. Репозитории, snapshot и блокировка конкурентного запуска.
3. Normalizer, compatibility, penalty и детерминированные reason codes.
4. `STATIC_TEST` для ручной проверки, затем локальные Nominatim/Valhalla и кеш.
5. OR-Tools solver и извлечение маршрутов/оборудования.
6. Независимый Validator и атомарное сохранение результата.
7. API, RBAC и структурированные логи.
8. Ручной acceptance-прогон основных сценариев из ТЗ и demo seed.

Автотесты, performance/equipment spike, нагрузочные замеры и технические метрики
не входят в согласованный объём реализации.

## Definition of Done для хакатона

- Один `docker compose up` поднимает backend, PostgreSQL, Valhalla и Nominatim.
- Контрольный набор строит SUCCESS-план без hard-constraint нарушений.
- Повторный запуск создаёт новую неизменяемую версию; параллельный получает 409.
- Невалидные и неназначенные заявки имеют точные reason codes.
- Validator и API history работают; внешние платные API и ключи не нужны.

## Декомпозиция и статус

### Проектирование

- [x] Проанализировать исходное ТЗ и текущую архитектуру репозитория.
- [x] Выбрать локальную замену Yandex: Valhalla + Nominatim + OpenStreetMap.
- [x] Зафиксировать UTC-контракт API и IANA timezone для календарных правил.
- [x] Зафиксировать Objective, обработку последней заявки и географию Перми.
- [x] Зафиксировать финальные DTO запросов/ответов и перечень error codes.

### Данные и каркас модуля

- [x] Добавить зависимости и конфигурацию planning/geoservices.
- [x] Создать структуру `src/planning` и зарегистрировать её через Dishka/FastAPI.
- [x] Реализовать доменные сущности, enum и value objects.
- [x] Добавить таблицы, ограничения, индексы и Alembic-миграцию.
- [x] Добавить начальную `planning_config` и demo seed для Перми.
  Demo-заявки используют адреса без координат, чтобы проверять полный путь через
  Nominatim.

### Подготовка расчёта

- [x] Реализовать создание run, snapshot и защиту от параллельного запуска.
- [x] Реализовать нормализацию заявок, смен, окон и UTC-преобразования.
- [x] Реализовать `BaseCompatible` и расчёт `DropPenalty`.
- [x] Реализовать `max_jobs_per_run` и предварительные reason codes.

### Геоданные

- [x] Добавить Valhalla и Nominatim в Docker Compose с OSM-данными региона Перми.
- [x] Реализовать `STATIC_TEST`, `NOMINATIM_LOCAL` и `VALHALLA_LOCAL`.
- [x] Реализовать address hash, геокеш и кеш дорожных дуг.
- [x] Реализовать блочное построение матриц `auto` и `pedestrian`.
- [x] Завершить и проверить чистый первый импорт OSM в оба локальных геосервиса.
  Valhalla и Nominatim healthy; использован отдельный extract Пермского края.

### Оптимизация и результат

- [x] Построить OR-Tools RoutingModel, Time Dimension и optional jobs.
- [x] Добавить ограничения квалификаций, транспорта, смен и окон.
- [x] Добавить дневное закрепление и лимиты оборудования.
- [x] Извлечь маршруты, UTC-времена, оборудование и неназначенные заявки.
- [x] Реализовать детерминированный `UnassignedReasonResolver`.
- [x] Реализовать независимый runtime `PlanningValidator`.
- [x] Реализовать атомарное сохранение результата и статусов ошибок.

### API и готовность демо

- [x] Реализовать POST запуска, GET результата и GET истории.
- [x] Подключить существующую авторизацию и согласованные глобальные роли.
- [x] Добавить OpenAPI-схемы, безопасные ошибки и timeout-конфигурацию.
- [x] Добавить минимальные application logs начала, результата и ошибки run.
- [x] Провести ручной acceptance-прогон и подготовить сценарий демонстрации.
