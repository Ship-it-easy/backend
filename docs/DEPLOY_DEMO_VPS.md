# Демо на одном VPS (Москва, 4 ГБ RAM)

Этот способ поднимает фронтенд, backend, PostgreSQL, Valhalla, Nominatim и
Traefik одним Docker Compose-проектом. Снаружи доступны только HTTPS-сайт и
его `/api/` через Traefik. Прямых опубликованных портов у остальных сервисов
нет. Конфигурация использует экстракт Москвы; адреса и маршруты за её пределами
для такого демо не подходят.

## 0. Что подготовлено в репозиториях

- В backend: `compose.demo.yml`, `.env.demo.example`, этот документ и скрипты
  подготовки демонстрационных учётных записей.
- Во frontend: `Dockerfile`, `.dockerignore`, `nginx.conf`.

Перед клонированием на сервер **закоммитьте и отправьте эти изменения в оба
GitHub-репозитория**. Локальные незакоммиченные файлы команда `git clone` не
получит. В существующий `docker-compose.yml` для разработки изменения не
вносились.

## 1. Сервер и DNS

Рекомендуемый вариант для этого набора: VPS x86-64, Ubuntu 24.04, 2 vCPU,
4 ГБ RAM, SSD от 60 ГБ, публичный IPv4. Желательно 2 ГБ swap для кратких пиков
памяти. Если арендуется 8 ГБ RAM, можно запускать все сервисы сразу.

Создайте запись `A` для выбранного имени, например `demo.example.ru`, на IPv4
VPS. Удалите ошибочную запись `AAAA`, если IPv6 на VPS не настроен. В панели
провайдера разрешите входящие TCP `22` (SSH), `80` (сертификат и редирект) и
`443` (сайт). Остальные входящие порты не нужны. Серверу нужен исходящий
доступ к Docker Hub, GHCR, PyPI, npm, источнику OSM и Let's Encrypt.

## 2. Установить Docker и Git на чистой Ubuntu

Подключитесь по SSH и установите Docker Engine с Compose plugin из
официального репозитория Docker:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git openssl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker compose version
```

Официальная инструкция: https://docs.docker.com/engine/install/ubuntu/ .
Команды далее используют `sudo docker`, поэтому добавлять пользователя в группу
`docker` не требуется.

Проверьте swap командой `swapon --show`. Если вывода нет, для VPS с 4 ГБ RAM
можно добавить 2 ГБ:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Клонировать оба репозитория рядом

```bash
sudo mkdir -p /opt/ship-it
sudo chown "$USER":"$(id -gn)" /opt/ship-it
cd /opt/ship-it
git clone git@github.com:Ship-it-easy/backend.git ship-it-backend
git clone git@github.com:Ship-it-easy/frontend.git ship-it-frontend
cd ship-it-backend
```

Для приватных репозиториев заранее настройте на сервере GitHub SSH-ключ с
правом чтения. Если репозитории публичные, можно использовать HTTPS-адреса
`https://github.com/Ship-it-easy/backend.git` и
`https://github.com/Ship-it-easy/frontend.git`.

## 4. Заполнить конфигурацию

```bash
cp .env.demo.example .env.demo
chmod 600 .env.demo
openssl rand -hex 24
openssl rand -hex 24
nano .env.demo
```

В `.env.demo` замените `DEMO_DOMAIN`, `ACME_EMAIL`, `POSTGRES_PASS` и
`NOMINATIM_PASSWORD`. Для двух паролей используйте **разные** строки из
`openssl`. Остальные значения подходят для московского демо. Не копируйте
frontend `.env.example` в `.env`: он задаёт `VITE_API_URL=localhost`, тогда
браузер на другом компьютере не сможет обратиться к API.

Проверьте, что в конфигурации не осталось шаблонных значений:

```bash
grep -nE 'REPLACE_WITH|example\.ru' .env.demo
```

Эта команда не должна вывести строк.

Создайте хранилище сертификата и проверьте Compose:

```bash
mkdir -p letsencrypt
touch letsencrypt/acme.json
chmod 600 letsencrypt/acme.json
sudo docker compose --env-file .env.demo -f compose.demo.yml config -q
```

Файл `.env.demo` и каталог `letsencrypt` исключены из Git. Не отправляйте их в
репозиторий. Если команда `config -q` сообщит об ошибке, исправьте её до запуска.

## 5. Первый запуск на VPS с 4 ГБ RAM

На 4 ГБ импортируйте геоданные последовательно, чтобы Nominatim и Valhalla
не строили базы одновременно. Каждая команда `--wait` завершится после
готовности сервиса или ошибки/тайм-аута; импорт может занять время.

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml up -d --wait --wait-timeout 7200 postgres nominatim
sudo docker compose --env-file .env.demo -f compose.demo.yml up -d --wait --wait-timeout 7200 valhalla
sudo docker compose --env-file .env.demo -f compose.demo.yml up -d --build --wait --wait-timeout 900
```

Если ожидание прервалось, посмотрите состояние и логи. Не удаляйте тома с
данными, просто продолжите после устранения причины:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml ps
sudo docker compose --env-file .env.demo -f compose.demo.yml logs --tail=100 nominatim valhalla backend traefik
free -h
df -h
```

После первого импорта обычный запуск и обновление — одна команда:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml up -d --build
```

Для обновления сначала выполните `git pull` в **обоих** каталогах. Для сохранения
данных не запускайте `docker compose down -v`: ключ `-v` удалит тома PostgreSQL,
OSM и демо-манифестов.

## 6. Проверить сайт и подготовить данные для показа

Откройте `https://ВАШ_ДОМЕН`. Traefik должен выдать сертификат автоматически.
Для быстрой проверки с сервера:

```bash
curl -I https://ВАШ_ДОМЕН/
curl -i https://ВАШ_ДОМЕН/api/auth/me
```

Первый запрос должен вернуть `200`, второй без входа — `401`. Это нормальный
ответ API, подтверждающий маршрутизацию. Если сайт открывается без карты,
проверьте, что браузер может загружать тайлы с `tile.openstreetmap.org`.

Демо на 27 сентября 2026 года создаётся так:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml exec -T backend \
  python scripts/prepare_transport_demo.py --date 2026-09-27 --manifest-dir /demo-manifests
```

Скрипт выведет логин и пароль диспетчера. Сохраните их в надёжном месте. Его
манифест хранится в отдельном постоянном Docker volume, поэтому повторная
команда после пересоздания backend вернёт те же данные.

Для проверки расчёта **26 сентября** создайте отдельный набор на эту дату:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml exec -T backend \
  python scripts/prepare_transport_demo.py --date 2026-09-26 --manifest-dir /demo-manifests
```

27 сентября войдите в интерфейс диспетчером набора от 27-го, откройте
«Планирование» и запустите расчёт. Дата запуска должна быть текущей в timezone
демо-проекта (`Europe/Moscow`). Проверьте маршруты, карту, объяснения заявок и
повторный вход после перезагрузки страницы.

Если нужен кабинет `owner`, задайте пароль владельцу после запуска миграций:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml exec backend \
  python scripts/set_owner_password.py
```

Пароль вводится интерактивно и не попадает в историю команд. Логин — `owner`.

## Частые проблемы

- **Сертификат не выдался:** проверьте запись `A`, ошибочную `AAAA`, входящий
  порт `80` и `sudo docker compose ... logs traefik`.
- **Backend не стартует:** обычно ещё идёт импорт OSM либо один из геосервисов
  не прошёл healthcheck. Проверьте `ps` и логи `nominatim`/`valhalla`.
- **В браузере запросы идут на `localhost:8000`:** во frontend случайно попал
  `.env` с `VITE_API_URL`. Удалите его и пересоберите frontend.
- **Пустой интерфейс после входа:** новая база не содержит локальных данных
  разработчика; выполните команду подготовки демо-набора выше.
- **Нет памяти при импорте:** убедитесь, что используется московский PBF,
  поднимается по одному геосервису и настроен swap. Не меняйте PBF при уже
  созданных томах: существующий импорт сам не заменится.
