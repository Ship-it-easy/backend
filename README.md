# Введение
Проект, реализующий работу с:
1. Web sessions
2. RBAC
3. Дневное планирование маршрутов выездных инженеров

# Проект
## Технологический стек
- **Python**: `3.12`
- **Production**: `alembic`, `dishka`, `fastapi`, `psycopg`, `sqlalchemy[async]`, `gunicorn`, `ortools`, `httpx`
- **Development**: `isort`, `ruff`, `pre-commit`


## API
<p align="center">
  <img src="docs/API.png" />
  <br><em>Handlers</em>
</p>

### General
- '/': Открыт для **всех**
   - Перенаправляет на Swagger документацию

### Auth (`/auth`)

- 'signup' (POST): Открыт для **всех**
  - Регистрация аккаунта
- 'login' (POST): Открыт для **всех**
  - Вход в аккаунт
- 'logout' (DELETE): Открыт для **всех**
  - Выход с аккаунта
- 'verification/{user_id}': Открыт для **всех**
  - Верификация аккаунта, проходя по ссылке из письма на почте

### Hello world (`/hello_world`)
- 'user' (GET): Открыт для **user**
  - return: Hello world by user
- 'admin' (GET): Открыт для **admin**
  - return: Hello world by admin

### Planning (`/api/projects/{project_id}/planning`)

- `runs` (POST): запуск расчёта текущего дня, доступен **admin**
- `runs/{run_id}` (GET): результат расчёта, доступен **user/admin**
- `runs` (GET): история расчётов, доступна **user/admin**
- `batches` (POST): асинхронный запуск управляемого каскада на 7–30 дней;
- `batches/{batch_id}` (GET): прогресс, рассчитанные дни и остаток;
- `batches/{batch_id}/stop` (POST): идемпотентная безопасная остановка;
- `batches/{batch_id}/validate-current-day` (POST): проверка актуальности перед
  публикацией текущего дня.
- `context` (GET): текущая дата и IANA timezone проекта для интерфейса;
- `runs/{run_id}/publish` (POST): публикация текущего дня из batch владельцем
  или диспетчером выбранного проекта.

Тело запуска:

```json
{
  "planning_date": "2026-09-12",
  "timezone": "Asia/Yekaterinburg"
}
```

Timezone должен совпадать с timezone проекта. Все абсолютные времена в ответах
возвращаются в UTC. Интерактивная документация доступна в `/docs`.

Для запуска batch обязателен заголовок `Idempotency-Key`, а
`requested_start_date` должна совпадать с текущей датой в timezone проекта:

```json
{
  "requested_start_date": "2026-09-14"
}
```

Каждая дата каскада создаёт отдельный `planning_run` и отдельную модель OR-Tools.
Завершённые дни сохраняются атомарно, а после перезапуска worker обработка
продолжается с первой незавершённой даты.

## Файловая структура

```
.
├── conf # конфиги
├── docs # документация
└── src
    ├── auth
        ├── application # логика приложения и интерфейсы
        ├── domain # модели
        ├── entrypoint # настройка запуска
        ├── infrastructure # адаптеры
        └── presentation # внешнее общение
    └── planning # дневное планирование в тех же слоях

```

## Описание схем реляционной базы данных
Использован императивный подход. С помощью `map_imperatively` была смаплена доменная модель в представление базы данных.

## Зависимости
Приложение разделено на слои:
1. Domain
2. Application
3. Infrastructure
4. Presentation

<p align="center">
  <img src="docs/CA.jpg" alt="Correct Dependency with DI" />
  <br><em>Чистая архитектура, Роберт Мартин</em>
</p>

- Соблюден принцип инверсии зависимотей
- Зависимости доставляются при помощи инъекции зависимостей, используя di-framework Dishka

## Переменные окружения
Для работы проекта необходимо настроить переменные окружения. В корне проекта подготовлены шаблоны:
- `.env.example` — используйте для локального запуска сервисов (localhost).
- `.env.docker.example` — используйте для запуска всего стека через Docker Compose.

Скопируйте нужный шаблон в соответствующий файл (`.env` или `.env.docker`) и заполните актуальные значения перед запуском.

## Как запустить
1. Склонируй проект
2. Заполни переменные окружения
3. Подними проект ``docker compose up --build``. Первый импорт OSM-данных для
   Valhalla и Nominatim может занять продолжительное время.
4. Проведи миграции. Либо напрямую в контейнере, либо ``make migrate`` в терминале
5. При необходимости создай демонстрационные данные Перми:
   ``make seed-planning-demo``.

Для быстрого запуска solver без геосервисов укажи в `planning_config.travel_provider`
значение `STATIC_TEST`. Для дорожных матриц OpenStreetMap используется
`VALHALLA_LOCAL`.

## Полезные материалы
1. Web sessions - https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
2. OAuth2 - https://auth0.com/docs и https://oauth.net/
