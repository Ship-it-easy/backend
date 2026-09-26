#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env.demo ]]; then
  echo "Создайте .env.demo из .env.demo.example" >&2
  exit 1
fi
if grep -Eq '^(POSTGRES_PASS=REPLACE_WITH|YANDEX_GEOCODER_API_KEY=REPLACE_WITH)' .env.demo; then
  echo "Замените шаблонные значения в .env.demo" >&2
  exit 1
fi

compose=(docker compose --env-file .env.demo -f compose.demo.yml)
"${compose[@]}" config -q

# Import the Moscow graph before image builds compete for memory.
"${compose[@]}" up -d --wait --wait-timeout 7200 postgres valhalla
"${compose[@]}" stop

# Build one image at a time on a 2 GB VPS.
"${compose[@]}" build backend
"${compose[@]}" build frontend
"${compose[@]}" up -d --wait --wait-timeout 900

echo "Сервисы запущены. Проверьте сайт и состояние: docker compose --env-file .env.demo -f compose.demo.yml ps"
