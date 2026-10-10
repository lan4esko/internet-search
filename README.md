# Internet Search

Рабочий конвейер идентификации разрешённых IPv4-целей:

```text
CIDR TXT → отдельная БД TASK → IP-чанки → Masscan
                              → открытые IP:PORT → HTTPX
                              → Fortinet fingerprint → protocol://ip:port
```

Приложение использует Docker Compose, PostgreSQL, Redis, Masscan и ProjectDiscovery HTTPX. Runtime не создаёт моковых результатов.

## TASK и физическая база

Каждый запуск создаётся из одного неизменяемого CIDR-файла. В control-базе `internet_search` хранится реестр, после чего PostgreSQL создаёт отдельную физическую базу `task_<uuid>`.

В task-базе:

- `task_targets` — все IP из CIDR, включая network и broadcast; `ip` является primary key;
- `task_chunks` — состояние Masscan и HTTPX чанков;
- `task_open_ports` — только порты, подтверждённые Masscan;
- `httpx_results` — реальные HTTPX-ответы и совпадения Fortinet.

Параметры `masscan_chunk_size` и `httpx_chunk_size` задаются отдельно. Числа Masscan и HTTPX workers задаются при создании TASK. Подробная спецификация: [docs/task-pipeline.md](docs/task-pipeline.md).

## Запуск

Требуются Docker Engine с Compose v2, Python 3.10+ и разрешённая сеть.

```bash
python3 scripts/bootstrap.py
docker compose up -d --wait
```

Панель: `http://127.0.0.1:8080`. Конфигурация создаётся в `.env` с правами `0600` и не перезаписывается.

Создание TASK выполняется в панели: укажите имя, CIDR-файл, размеры чанков, порты, rate и число workers. API также доступен через `POST /api/tasks`.

## Fortinet fingerprint

В первой рабочей версии используется проверенная логика `/home/kali/turbo/debug_scan_2.0_.sh`:

```text
/remote/login,/remote/index,/ssl-vpn/login.html
ftnt-fortinet-grid
name="magic"
SVPNCOOKIE
```

HTTPX получает только Masscan-открытые `IP:PORT`. Экспорт TASK содержит уникальные строки `http://ip:port` или `https://ip:port`.

## Документация

- [Техническое ТЗ и схема TASK](docs/task-pipeline.md)
- [Эксплуатация и API](docs/task-operations.md)
- [Развёртывание](docs/deployment.md)
- [Архитектура](docs/architecture.md)
- [Диагностика](docs/scanner-diagnosis.md)
