# Маршрут — backend

## Быстрый запуск

Нужен только Docker Desktop. Ключ Яндекс Геокодера уже добавлен в шаблон.

### 1. Создайте env-файл

```bash
cp .env.docker.example .env.docker
```

Файл уже содержит ключ Яндекс Геокодера и готов к запуску. Ничего изменять не
нужно. `.env.docker` добавлен в `.gitignore` и останется локальным.

### 2. Запустите всё одной командой

```bash
docker compose --env-file .env.docker up -d --build
```

При первом запуске Valhalla скачивает и индексирует карту ЦФО, поэтому подготовка
может занять некоторое время. Проверить состояние:

```bash
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker logs -f backend
```

Готово, когда `postgres` и `valhalla` имеют статус `healthy`, а `backend` — `Up`.

- API: <http://localhost:8000>
- Swagger: <http://localhost:8000/docs>

После запуска база чистая: справочники, оборудование, инженеры, смены, заявки и
демо-проекты не создаются.

### 3. Задайте пароль владельца

```bash
docker compose --env-file .env.docker exec backend \
  python scripts/set_owner_password.py
```

Введите пароль длиной не менее 12 символов. Для входа используйте логин `owner`
и заданный пароль.

Геокодирование всегда выполняется через Яндекс. Valhalla рассчитывает время,
расстояние и геометрию маршрутов. Nominatim в обычном запуске не используется.

## Остановка и повторный запуск

```bash
docker compose --env-file .env.docker down
docker compose --env-file .env.docker up -d
```

Чтобы удалить все локальные данные и создать чистую базу заново:

```bash
docker compose --env-file .env.docker down -v
docker compose --env-file .env.docker up -d --build
```

`down -v` безвозвратно удаляет локальные данные.

## Nominatim — только при необходимости

Он сохранён для разработки и запускается только явно:

```bash
docker compose --env-file .env.docker --profile nominatim up -d nominatim
```
