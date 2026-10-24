# Internet Search 2.0 — восстановленная сборка

Это исходники v2, восстановленные из изменений в переписке после утраты предыдущей облачной среды. Архив не является побайтовой копией прежней среды. Он содержит код и инструкции запуска; пароли, базы, результаты сканирования, Git metadata и Python/Docker caches не включены. Результаты проверок этой конкретной сборки находятся в `VALIDATION.md`.

Основной runtime: **CIDR → ScopeCompiler → ZMap → PostgreSQL outbox → Redis Streams → ZGrab2 → TLS/HTTP normalization → YAML evidence → SSE/UI/exports**. Masscan и HTTPX больше не запускаются новым worker. CIDR не разворачивается в миллионы IP-строк.

## Запуск после скачивания

1. Распакуйте ZIP.
2. Откройте терминал в распакованной папке `internet-search-v2.0`.
3. На Linux с установленными Docker Engine, Docker Compose v2 и Python 3.10+ выполните:

```bash
python3 scripts/bootstrap.py
```

Первичная сборка скачивает зависимости. Bootstrap создаёт локальный `.env` с правами 0600, применяет миграции и запускает сервисы. Пароли в исходники вводить не нужно.

На своём компьютере откройте `http://127.0.0.1:28280`. API: `http://127.0.0.1:28210`, OpenAPI: `/docs` на порту API.

Загрузите разрешённый IPv4 CIDR TXT, проверьте scope без трафика, затем создайте TASK. Для реального ZMap discovery машине нужны raw IPv4 networking и корректные interface/source IP/gateway. Обычный HTTP/SOCKS proxy не заменяет raw-сеть ZMap.

## Реализовано

- Immutable source, allowlist, mandatory/user denylist, SHA-256 manifest и повторная pre-probe проверка.
- Физическая PostgreSQL-база на TASK, ledger `PORT × SHARD`, уникальные endpoints `IP:PORT:SNI`.
- Spool на диске, transactional outbox, consumer groups, retries/DLQ и идемпотентное восстановление после restart.
- ZMap 2.1.1 и ZGrab2 v0.2.0; TLS/X.509/HTTP normalization с ограниченным размером тела.
- YAML rules: AND/OR/NOT, datasets, regex timeout, положительные/отрицательные признаки, score и maximum tier.
- Криптографические совпадения ограничены `crypto_candidate`; HTTP подтверждается отдельным правилом. SPKI dataset GlobalProtect пустой до независимой калибровки.
- Direct/source-IP/CONNECT/SOCKS5 egress. Прокси получает числовой проверенный адрес; нет fallback на direct и следования redirects.
- Панель задач, SSE, evidence, pagination и live CSV/JSONL snapshots с watermark/SHA-256.
- Container telemetry; дополнительный стек cAdvisor/Prometheus/Grafana и семь dashboards.
- Backup/restore, безопасная проверка tar/checksum и CI с локальной лабораторией.

## Повторный запуск и обновление

```bash
docker compose up -d --wait
```

Для обновления сначала завершите TASK, сделайте backup и остановите application services:

```bash
python3 scripts/backup.py backups/before-update
docker compose stop web api task-runner metrics-agent metrics-proxy
python3 scripts/bootstrap.py
```

Не удаляйте volumes при обновлении. Миграция `0003_v2` сохраняет v1 registry и базы. Старый UI доступен на `/legacy.html`; создание/повтор TASK v1 возвращают HTTP 410.

## Тесты

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/api/requirements.txt -r services/task-runner/requirements.txt -r tests/browser/requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python scripts/test_v2.py --restart-test --browser
python3 scripts/benchmark_v2.py
```

`test_v2.py` использует только локальный Docker fixture. Он временно разрешает его точный `/32`, затем удаляет исключение в `finally`. Не запускайте при активных рабочих TASK: тест перезапускает worker/API.

## Мониторинг

```bash
docker compose --profile monitoring up -d cadvisor prometheus grafana
```

Prometheus: локальный порт 29090; Grafana: 23000, admin password генерируется в `.env`. Docker Engine socket доступен только узкому readonly metrics-proxy; API его не получает.

Подробности: [архитектура, API и DSL](docs/v2/implementation.md), [backup/restore](docs/v2/operations.md), [границы проверки](VALIDATION.md). Старые документы вне `docs/v2` описывают v1.
