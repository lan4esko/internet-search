# Техническое задание: TASK pipeline

## Цель

Приложение находит разрешённые IPv4-цели с открытым TCP-портом и подтверждает Fortinet по реальному HTTP-ответу. Моковые результаты в runtime отсутствуют.

```text
CIDR TXT → TASK database → IP rows → Masscan → open IP:PORT
                                      → HTTPX → Fortinet fingerprint
                                      → protocol://ip:port
```

## Изоляция TASK

Контрольная база `internet_search` хранит реестр TASK и конфигурацию. При создании TASK PostgreSQL создаёт отдельную физическую базу `task_<uuid>`. Внутри неё находятся `task_meta`, `task_cidrs`, `task_targets`, `task_chunks`, `task_open_ports`, `httpx_results` и `task_events`.

`task_targets.ip` — настоящий `PRIMARY KEY`; дубли IP внутри TASK невозможны. В `task_open_ports` ключом является `(ip, port, protocol)`.

## Этап 0 — загрузка и подготовка

Пользователь создаёт TASK и загружает один TXT-файл. Файл сохраняется с SHA-256 и после создания не изменяется. Некорректный CIDR отклоняет загрузку.

Параметры TASK задаются независимо: `masscan_chunk_size`, `httpx_chunk_size`, `masscan_rate`, список Masscan-портов, формат вывода (`json`, `list`, `xml`), `masscan_workers`, `httpx_workers` и ограничение HTTPX `rate_limit`. Дополнительные argv-токены Masscan и HTTPX можно передать массивом; input/output-флаги, которыми управляет конвейер, отклоняются валидатором. Все значения сохраняются в конфигурации конкретного запуска.

Все адреса CIDR разворачиваются, включая network и broadcast. Каждому IP назначается `source_chunk_no`. До Masscan используются `ready_for_masscan=true` и `masscan_status=pending`.

## Этап 1 — Masscan

Worker выбирает свободные чанки, формирует временный файл IP и запускает реальный Masscan через `create_subprocess_exec` с `--includefile`, `--ports`, `--rate`, выбранным форматом output и raw-файлом. Дополнительные argv передаются отдельными аргументами процесса, без shell.

Raw-файл сохраняется в каталоге TASK. Открытые порты записываются в `task_open_ports` с `masscan_open=true`, `httpx_ready=true` и `httpx_status=pending`. IP без открытого порта получает `masscan_status=closed` и не участвует в HTTPX. Закрытые порты не сохраняются.

## Этап 2 — HTTPX

Worker выбирает строки, где `masscan_open=true`, `httpx_ready=true` и `httpx_status='pending'`. Формируется файл `IP:PORT`. Количество параллельных процессов задаётся `httpx_workers`, число потоков — профилем HTTPX, а `rate_limit` ограничивает частоту запросов; дополнительные HTTPX argv позволяют включать поддерживаемые опции без изменения кода; размер файла — `httpx_chunk_size`.

## Fortinet fingerprint

Fingerprint перенесён из `/home/kali/turbo/debug_scan_2.0_.sh` без расширения логики:

```text
paths: /remote/login, /remote/index, /ssl-vpn/login.html
match strings: ftnt-fortinet-grid, name="magic", SVPNCOOKIE
GET, timeout 5, follow redirects, 500 threads
```

Результат нормализуется к `protocol://ip:port`; путь удаляется.

## Результат

`httpx_results` хранит URL, статус, title, webserver, заголовки, тело ответа, найденные признаки и номер HTTPX-чанка. Экспорт отдаёт уникальные результаты с `matched=true`, например `https://192.0.2.10:10443`.

Redis является сигналом пробуждения. Реестр control DB остаётся источником истины: task-runner подбирает `queued` TASK после потери события. Ошибка чанка не создаёт результат; чанк остаётся `failed` и может быть повторён.
