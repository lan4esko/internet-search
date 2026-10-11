# Развёртывание

## Требования

Linux, Docker Engine с Compose v2, Git, Python 3.10+, доступ к Docker daemon и
маршрутизация к разрешённым сетям. Для Masscan task-runner получает `NET_RAW` и
`NET_ADMIN`. Только task-runner запускает сетевые инструменты; Web и API Docker
socket не получают.

## Чистая установка

```bash
git clone https://github.com/lan4esko/internet-search.git
cd internet-search
python3 scripts/bootstrap.py
docker compose up -d --wait
```

Bootstrap создаёт `.env`, собирает API, Web, PostgreSQL, Redis, metrics services
и task-runner, применяет контрольные миграции и проверяет health endpoints.
Старые volumes не являются совместимым хранилищем нового TASK-конвейера;
для новой архитектуры используйте чистый Compose project.

## PostgreSQL

Контрольная БД задаётся через `DATABASE_URL` и обычно называется
`internet_search`. Пользователь PostgreSQL должен иметь право `CREATEDB`, потому
что API создаёт физическую `task_<uuid>` базу на каждый TASK. На чистом Compose
роль, заданная `POSTGRES_USER`, создаётся владельцем PostgreSQL.

Остановка TASK-базы выполняется только через API удаления завершённого TASK.
Не удаляйте её вручную, пока запись `task_registry` существует.

## Доступ

Порты Web и API по умолчанию привязаны к localhost. Для удалённой работы:

```bash
ssh -N -L 28280:127.0.0.1:28280 user@server
```

Встроенной аутентификации нет; при внешнем доступе используйте reverse proxy с
HTTPS и аутентификацией для панели и `/api/`.

## Обновление

Остановите task-runner после завершения активных TASK, создайте резервную копию,
соберите новые образы и примените миграции control DB:

```bash
docker compose stop task-runner
docker compose build
docker compose run --rm --no-deps api alembic upgrade head
docker compose up -d --wait
```

Физические базы TASK не мигрируются старой схемой. Изменения task-схемы должны
иметь отдельную версию и проверяться на новой тестовой TASK-базе.
