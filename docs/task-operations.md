# Эксплуатация TASK

## Запуск

```bash
python3 scripts/bootstrap.py
docker compose up -d --wait
```

Панель: `http://127.0.0.1:28280`. API: `http://127.0.0.1:28210`. Создание TASK выполняется через `POST /api/tasks` и передаёт имя, CIDR-файл, два размера чанка, параметры Masscan и количество воркеров. В форме доступны формат Masscan и дополнительные argv для обоих инструментов; запрещённые конвейером input/output-флаги валидируются API.

Шаблоны поиска:

- `GET /api/templates` — список сохранённых конфигураций;
- `POST /api/templates` и `PUT /api/templates/{id}` — сохранить или обновить шаблон;
- `DELETE /api/templates/{id}` — удалить шаблон;
- `POST /api/templates/validate` — dry-run проверки argv и CIDR без запуска сетевых инструментов.

Проверочный прогон возвращает `network_traffic=false`: он не создаёт TASK, не
создаёт физическую БД и не отправляет пакеты.

Для реального теста в панели используйте отдельную кнопку «Запустить реальный
тест». Она берёт только выбранное число валидных подсетей, создаёт обычный TASK
с именем `[TEST] ...` и отправляет его в worker. Это единственная кнопка тестового
окна, которая создаёт сетевой трафик; результат и события отображаются как у
обычного TASK и могут быть удалены после проверки.

## API

```text
GET    /api/tasks
POST   /api/tasks
GET    /api/tasks/{id}
DELETE /api/tasks/{id}
GET    /api/tasks/{id}/targets
GET    /api/tasks/{id}/open-ports
GET    /api/tasks/{id}/results
GET    /api/tasks/{id}/events
GET    /api/tasks/{id}/export
POST   /api/tasks/preview-cidr
POST   /api/tasks/{id}/retry
GET    /api/templates
POST   /api/templates
PUT    /api/templates/{id}
DELETE /api/templates/{id}
POST   /api/templates/validate
```

Удаление разрешено для `completed`, `failed` и `cancelled`. Активный TASK не удаляется. Удаление удаляет registry-запись и физическую `task_<uuid>` базу.

## Диагностика

```bash
docker compose logs --tail=200 task-runner
docker compose exec postgres psql -U app -d internet_search -c 'select id,name,status,database_name from task_registry order by id desc;'
```

Сырые файлы лежат в `scope_data/tasks/<task_id>/masscan` и `scope_data/tasks/<task_id>/httpx`. Источником результата остаются таблицы физической TASK-базы.

Если TASK остался в `queued`, task-runner подберёт его из control DB. Если чанк имеет `failed`, исправьте причину и вызовите `POST /api/tasks/{id}/retry`.

Поддерживается IPv4 и один PostgreSQL-сервис с отдельной физической базой на TASK. Панель по умолчанию доступна только localhost.
