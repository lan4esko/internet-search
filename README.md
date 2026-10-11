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

Панель: `http://127.0.0.1:28280`. API: `http://127.0.0.1:28210`. Конфигурация создаётся в `.env` с правами `0600` и не перезаписывается.

Создание TASK выполняется в панели: укажите имя, CIDR-файл, размеры чанков, порты, rate и число workers. API также доступен через `POST /api/tasks`.

Профили поиска сохраняются кнопкой «Сохранить шаблон» в control-БД и могут быть
загружены перед созданием TASK. Кнопка «Тест конфигурации» открывает dry-run:
параметры, argv и выбранный CIDR-файл проверяются без запуска Masscan/HTTPX и без
отправки сетевых пакетов. API этих операций: `/api/templates` и
`/api/templates/validate`.
В том же окне есть явная кнопка реального ограниченного теста: она сначала
выбирает указанное число корректных подсетей, затем создаёт TASK с префиксом
`[TEST]` и запускает штатный Masscan → HTTPX конвейер.

Каждый TASK открывается отдельной вкладкой. Внутри вкладки доступны прогресс
этапов, открытые `IP:PORT`, Fortinet-совпадения, IP-чанки, нагрузка контейнеров
сводка пакетов Masscan, ответов HTTPX, совпадений и сохранённые argv-команды. Формат Masscan и дополнительные аргументы обоих
инструментов задаются до запуска; аргументы передаются напрямую процессу без
shell, а флаги, которыми управляет сам конвейер, отклоняются.

Для отдельного мониторинга Prometheus и Grafana предусмотрен профиль Compose:
`docker compose --profile monitoring up -d prometheus grafana`. Prometheus
забирает `/api/operations/prometheus`, а Grafana автоматически получает готовый
дашборд CPU, памяти, сети контейнеров и количества TASK по состояниям. Порты по
умолчанию: Prometheus `29090`, Grafana `23000`; bootstrap генерирует пароль
Grafana в локальном `.env`.

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
