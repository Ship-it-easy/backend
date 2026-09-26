# Демо на NAT-VPS с Nginx Proxy Manager провайдера

Схема для этого сервера:

```text
браузер → Nginx Proxy Manager провайдера → 192.168.8.29:8080 → frontend
                                                           └→ /api/ → backend
backend → PostgreSQL, Valhalla, API Геокодера Яндекса
SSH: 54.37.83.206:51440 → 192.168.8.29:22
```

NPM уже работает у провайдера: **не устанавливайте и не запускайте его на VPS**.
Traefik тоже не нужен. Docker Compose публикует только HTTP фронтенда на
внутреннем адресе `192.168.8.29:8080`. Backend, PostgreSQL и Valhalla не имеют
опубликованных портов. Nominatim не запускается: геокодирование делает API
Яндекса. Конфигурация Valhalla использует экстракт Москвы.

Порт `51440` относится только к SSH. По этой записи нельзя определить IP,
который нужен для DNS сайта: запись `A` должна вести на адрес **NPM**, который
указал провайдер. Он может совпадать с `54.37.83.206`, но это надо проверить
в панели или документации провайдера. Для работы NPM провайдера должен иметь
доступ к `192.168.8.29:8080` в своей внутренней сети. Если NPM выдаёт 502,
уточните у провайдера, какой внутренний адрес и порт он разрешает проксировать.

## 1. Сначала отправить подготовленные файлы в GitHub

На вашем компьютере есть локальные изменения, которых пока нет на GitHub.
Выполните на компьютере (не на VPS):

```bash
cd /Users/pavelepanov/ship-it-backend
git add .env.demo.example compose.demo.yml docs/DEPLOY_DEMO_VPS.md \
  scripts/check_demo_yandex.py scripts/demo_up_low_ram.sh
git commit -m "Prepare NAT VPS demo behind provider proxy"
git push origin main

cd /Users/pavelepanov/ship-it-frontend
git add nginx.conf
git commit -m "Proxy demo API through frontend nginx"
git push origin main
```

Если не выполнить этот шаг, `git clone` скачает старый Compose с Traefik и
Nominatim.

## 2. Войти на VPS и проверить ОС, Docker, память и диск

Используйте SSH-логин, выданный провайдером. Если это `root`:

```bash
ssh -p 51440 root@54.37.83.206
```

Если выдан `ubuntu`, замените `root` на `ubuntu`. Все команды ниже выполняются
уже внутри SSH-сеанса:

```bash
cat /etc/os-release
free -h
df -h /
ip -4 addr
sudo docker --version
sudo docker compose version
```

Убедитесь, что `192.168.8.29` действительно назначен VPS. Если Docker и
Compose уже работают, сразу переходите к шагу 3. Если Docker отсутствует и
ОС — Ubuntu 24.04, установите его из официального репозитория:

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
Для Debian или другой ОС используйте соответствующую инструкцию Docker.
Если Docker уже есть, а нет только Compose plugin, на Ubuntu с репозиторием
Docker достаточно `sudo apt install -y docker-compose-plugin`.

## 3. Создать swap при 2 ГБ RAM

```bash
swapon --show
df -h /
```

Если `swapon --show` ничего не вывел и на диске свободно минимум 4 ГБ:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
swapon --show
free -h
```

Строка в `/etc/fstab` включает swap после перезагрузки. На 1 vCPU сборка и
импорт карты могут идти долго даже со swap.

## 4. Клонировать оба репозитория рядом

Если Git и OpenSSL ещё не установлены, на Ubuntu выполните
`sudo apt install -y git openssl`. Для публичных репозиториев:

```bash
sudo mkdir -p /opt/ship-it
sudo chown "$USER":"$(id -gn)" /opt/ship-it
cd /opt/ship-it
git clone https://github.com/Ship-it-easy/backend.git ship-it-backend
git clone https://github.com/Ship-it-easy/frontend.git ship-it-frontend
cd ship-it-backend
```

Если репозитории приватные, настройте на VPS GitHub SSH-ключ с правом чтения
и используйте `git@github.com:Ship-it-easy/backend.git` и
`git@github.com:Ship-it-easy/frontend.git`.

## 5. Заполнить настройки приложения

```bash
cd /opt/ship-it/ship-it-backend
cp .env.demo.example .env.demo
chmod 600 .env.demo
openssl rand -hex 24
nano .env.demo
```

Замените `POSTGRES_PASS` на случайную строку из `openssl`, а
`YANDEX_GEOCODER_API_KEY` — на ключ API Геокодера Яндекса. Проверьте
`DEMO_HTTP_BIND=192.168.8.29` и `DEMO_HTTP_PORT=8080`. Если адрес или порт
отличаются в панели провайдера, поправьте их. Оставьте
`USE_YANDEX_GEOCODER=true`, `VALHALLA_THREADS=1` и московский `OSM_PBF_URL`.
Не загружайте `.env.demo` в GitHub. Frontend `.env.example` не копируйте в
`.env`: там адрес API localhost для локальной разработки.

```bash
grep -n 'REPLACE_WITH' .env.demo
sudo docker compose --env-file .env.demo -f compose.demo.yml config -q
```

Первая команда не должна ничего вывести. Если на порту 8080 уже что-то
работает (`sudo ss -ltnp | grep ':8080'`), выберите другой порт и укажите его
в `.env.demo` и Proxy Host.

## 6. Запустить приложение

На VPS с малой памятью подготовленный скрипт сначала импортирует карту
Valhalla, затем по одному собирает backend и frontend:

```bash
cd /opt/ship-it/ship-it-backend
sudo ./scripts/demo_up_low_ram.sh
sudo docker compose --env-file .env.demo -f compose.demo.yml ps
curl -I http://192.168.8.29:8080/
```

Последний запрос должен вернуть `200`. Если запуск прервался, смотрите:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml logs --tail=100 valhalla backend frontend
free -h
df -h /
```

Не выполняйте `docker compose down -v`: ключ `-v` удалит базу и карту.

## 7. Настроить готовый NPM провайдера

В панели NPM под выданными провайдером учётными данными откройте `Hosts` →
`Proxy Hosts` → `Add Proxy Host`:

| Поле | Значение |
| --- | --- |
| Domain Names | ваш домен или поддомен без `https://` |
| Scheme | `http` |
| Forward Hostname / IP | `192.168.8.29` |
| Forward Port | `8080` |

Во вкладке `SSL` запросите новый сертификат Let's Encrypt и включите
`Force SSL`. Отдельное правило `/api` в NPM не нужно: nginx фронтенда
передаст эти запросы бэкенду. DNS имени настройте на внешний адрес **NPM**
по инструкции провайдера, не на внутренний `192.168.8.29`. Дополнительное
NAT-правило для порта 8080 может не понадобиться, если NPM видит внутреннюю
сеть напрямую; если в панели провайдера требуется отдельное правило,
используйте предоставленный для HTTP порт и уточните у провайдера схему.

Для доступа снаружи нужны только SSH через выданный порт 51440 и HTTPS через
NPM. Не открывайте наружу 5432, 8000, 8002 или 8080. При наличии
фаервола провайдера ограничьте 8080 доступом только от NPM, если провайдер
сообщает его внутренний IP. Не включайте UFW вслепую: можно потерять SSH.

## 8. Проверить демонстрацию

Подставьте свой домен:

```bash
curl -I https://ВАШ_ДОМЕН/
curl -i https://ВАШ_ДОМЕН/api/auth/me
```

Ожидается `200` для главной и `401` для `/api/auth/me` без входа: это
нормальный ответ API. Проверьте доступ к API Геокодера Яндекса:

```bash
cd /opt/ship-it/ship-it-backend
sudo docker compose --env-file .env.demo -f compose.demo.yml exec -T backend \
  python scripts/check_demo_yandex.py
```

Подготовьте данные для показа 27 сентября 2026 года:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml exec -T backend \
  python scripts/prepare_transport_demo.py --date 2026-09-27 --manifest-dir /demo-manifests
```

Сохраните напечатанные логин и пароль диспетчера. Войдите в сайт и проверьте
авторизацию, карту, планирование, обновление страницы. Расчёт планирования
на 27-е запускайте 27 сентября по московскому времени. Если нужен кабинет
владельца:

```bash
sudo docker compose --env-file .env.demo -f compose.demo.yml exec backend \
  python scripts/set_owner_password.py
```

## Если что-то не работает

- `curl` к `192.168.8.29:8080` не отвечает: проверьте `docker compose ps`,
  `docker compose logs frontend backend` и `sudo ss -ltnp | grep ':8080'`.
- Локальный `curl` работает, но NPM выдаёт 502: уточните у провайдера, видит
  ли их NPM внутренний адрес `192.168.8.29:8080` и какой адрес нужен в поле
  Forward Hostname/IP.
- Сертификат не выпускается: проверьте, куда указывает DNS домена, и что
  входящий 80/443 обслуживает NPM провайдера.
- Адреса не находятся: проверьте ключ Яндекса командой
  `check_demo_yandex.py`.
- В браузере запросы идут на `localhost:8000`: в frontend попал `.env` с
  `VITE_API_URL`; удалите его и пересоберите frontend.
- Для обновления выполните `git pull` в **обоих** репозиториях и повторите
  `sudo ./scripts/demo_up_low_ram.sh`. Первый импорт Valhalla сохраняется в
  Docker volume.
