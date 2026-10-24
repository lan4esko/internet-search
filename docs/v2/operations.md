# Эксплуатация

## Backup и restore

Завершите все TASK, затем:

```bash
python3 scripts/backup.py backups/snapshot
```

Backup проверяет отсутствие активных v1/v2 задач, останавливает producers, сохраняет pg_dumpall, scope и Redis, записывает SHA-256 и возвращает исходное состояние сервисов. `.env` храните отдельно — его нет в backup и в скачиваемом ZIP.

Restore допускается только в пустой отдельный Compose project. Он проверяет checksum/размеры и tar traversal до запуска, создаёт временного admin, импортирует SQL, задаёт пароль app-role из защищённого `.env`, восстанавливает scope/Redis и отключает login временного admin:

```bash
.venv/bin/python scripts/restore.py backups/snapshot --project-name internet-search-restored --api-port 28211 --web-port 28281
```

Нужны уже собранные исходные образы и зависимости из API requirements. Команда следующего запуска сохраняется в `<project>.start.txt` внутри backup. Исходные volumes не удаляются. Не используйте `down --volumes` для обновления рабочей установки.

## Snapshots

Export читает одну `REPEATABLE READ` транзакцию. Observation и endpoint watermarks отдельные. CSV: `record_type,id,payload`, payload — JSON. `matches` содержит только HTTP-confirmed, кандидаты доступны в tls/all. Заголовки: `X-SHA256`, `X-Watermark`, `X-Endpoint-Watermark`, `X-Row-Count`, `X-Export-ID`. Сопутствующий manifest сохраняет scope hash. Сканирование не останавливается.

## Мониторинг и диагностика

```bash
docker compose ps
docker compose logs --tail=100 api task-runner
curl --fail http://127.0.0.1:28210/v2/ready
docker compose --profile monitoring up -d cadvisor prometheus grafana
```

API не имеет Docker socket; только metrics-proxy читает известные сервисы своего Compose project. Панель показывает CPU, RAM/RSS, RX/TX, I/O, health/restarts/uptime. Prometheus читает API и cAdvisor. Семь dashboards показывают Executive Scan, Pipeline, Containers, Egress, TLS Research, Database, Safety. Egress и Database dashboards используют pipeline counters, а не несуществующие proxy/SQL latency exporters.

При нехватке диска остановите новые TASK и освободите только неиспользуемый build cache; базы и scope volumes не удаляйте. Порты по умолчанию привязаны к loopback. Для удалённого доступа используйте аутентифицированный reverse proxy.

## Ограничения

Не заявлены внешняя нагрузочная приёмка на миллионах endpoints, precision/recall GlobalProtect, enterprise proxy matrix, независимый packet-capture leakage audit, health-aware rotation pools, распределение shards одного TASK между scanner nodes и multi-tenant authentication. Текущая версия — рабочий MVP. Текущие проверки восстановленного архива описаны в VALIDATION.md; результаты утраченной среды не подменяют их.
