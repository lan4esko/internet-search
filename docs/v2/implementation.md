# Internet Search v2: архитектура и API

Приложенный PDF содержит семистраничное архитектурное резюме, а не упомянутые в нём 31 страницу полного контракта. Эта восстановленная сборка конкретизирует доступные требования; не является заявлением о полной промышленной приёмке.

## Структура

- `shared/scanlib/v2/scope.py`: нормализованные IPv4 CIDR без перечисления адресов, вычитание reserved/private/user exclusions и SHA-256. Лабораторное исключение `V2_LAB_CIDRS` задаётся только администратором, не параметром TASK.
- `database.py`: task database schema: `scope_manifest`, `coverage`, `endpoints`, `outbox`, `observations`, `exports`, `events`.
- `services/task-runner/app/v2.py`: scheduler, ZMap discovery, outbox publisher, Redis consumer group, параллельные L7 workers и recovery. Один runner исполняет один TASK; advisory lock запрещает двух владельцев. Разные runner-ы могут брать разные TASK.
- `collect.py`: реальные subprocess argv без shell. В Debian ZMap allow/block flags называются `--whitelist-file`/`--blacklist-file`. Spool пишется на диск независимо от backpressure; ingestion ждёт свободного места в L7 queue. Sent берётся из metadata, неизвестное значение отображается как неизвестное.
- `transport.py`: проверенный numeric destination открывается relay, а ZGrab подключается к случайному loopback IP с исходным портом. CSV domain задаёт Host, SNI — отдельный контекст. Пустой native ZGrab blocklist относится к внутреннему relay; исходный destination повторно проверяется перед каждым исходящим соединением. Redirects отключены.
- `normalize.py`: TLS version с учётом TLS 1.3 supported_versions, cipher, ALPN, certificate/SPKI SHA-256, Subject/Issuer/SAN, validity, algorithms/chain, HTTP headers/status и bounded body/hash. Сбор сертификата не означает PKI validation (`certificate_trust_evaluated=false`).
- `rules.py`: bounded YAML DSL, boolean operators, datasets, evidence, scoring и regex timeout. Правила фиксируются snapshot-ом при создании TASK.

Delivery — at least once с идемпотентной записью, не exactly-once сетевые пробы. Crash может повторить SYN/HTTP, но уникальные `(ip,port,sni)` и `(endpoint,module,path,scheme)` предотвращают дублирование данных. ACK следует после сохранения observations. Outbox publish/mark защищены row lock; `XAUTOCLAIM` и повторная публикация позволяют восстановить pending jobs. Файлы spool и raw observations сохраняются на общем volume.

## API

Nginx удаляет `/api`; прямые API paths:

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/v2/ready` | PostgreSQL, Redis, worker heartbeat |
| POST | `/v2/scope/validate` | JSON `{cidr_text, deny_cidrs}`, без проб |
| GET | `/v2/egress` | Pool names/modes, без секретов |
| GET / PUT | `/v2/templates`, `/v2/templates/{id}` | Чтение правил / сохранение YAML |
| POST | `/v2/tasks` | multipart `name`, `file`, JSON-строка `config` |
| GET | `/v2/tasks` | Список, limit/offset |
| GET | `/v2/tasks/{id}` | Counters, coverage, events |
| GET | `/v2/tasks/{id}/events` | SSE `event: task`, каждые 2 секунды |
| GET | `/v2/tasks/{id}/observations` | limit/offset, module=tls|http, matches=true |
| POST | `/v2/tasks/{id}/cancel`, `/retry` | Остановка / повтор ошибок |
| DELETE | `/v2/tasks/{id}` | Удаление terminal TASK |
| GET | `/v2/tasks/{id}/export` | stage=discovery|tls|http|matches|all, format=csv|jsonl |
| GET | `/v2/tasks/{id}/exports` | Журнал snapshots |

```json
{"ports":[443],"rate_pps":100,"shards":1,"seed":1,"sni":["vpn.example.org"],"schemes":["https"],"paths":["/global-protect/login.esp"],"l7_workers":2,"timeout":10,"max_body_kb":64,"egress":"direct","template_ids":["globalprotect-http","globalprotect-crypto"]}
```

Опциональны `interface`, `source_ip`, `gateway_mac`; значения должны соответствовать реальному scanner node. Gateway MAC нужен при отсутствии доступного ARP. Система не выдумывает его. Shards не должны превышать число адресов scope.

## YAML DSL

```yaml
version: 1
id: example-http
classification:
  product: example
  on_match: http_confirmed
  maximum_tier: http_confirmed
threshold: 2
match:
  all:
    - {field: http.status_code, op: equals, value: 200}
    - {field: http.body, op: contains, value: Example Service}
negative: {field: http.body, op: contains, value: Known decoy}
```

Предикаты: equals, contains, regex, in, exists, sha256. Boolean nodes: all, any, not. Вес предиката `weight` по умолчанию 1; порог — `threshold`. Для `in` допускается `dataset: name` из `datasets`. Поля — dot paths по нормализованному наблюдению; поддержан обход списков (certificate chains). HTTP header keys нормализованы к lowercase с дефисами. Regex timeout даёт unknown. Crypto-only ceiling — crypto_candidate; HTTP confirmation требует положительного HTTP evidence и HTTP status. Формирование собственной калиброванной выборки SPKI — отдельная исследовательская работа.

## Egress

ZMap использует raw network и собственный interface/source IP. L7 выбирает member pool детерминированно по IP:PORT:SNI. Настройка сервером:

```dotenv
V2_EGRESS_POOLS={"office":[{"mode":"http_connect","url_env":"OFFICE_PROXY_URL"}],"bound":[{"mode":"source_ip","source_ip":"192.0.2.20"}]}
```

Реальный `OFFICE_PROXY_URL` задаётся защищённо в окружении API и runner; не в Git. SOCKS5: `mode=socks5`. CONNECT поддерживает HTTP/HTTPS proxy и Basic auth. Proxy получает только проверенный IPv4:port; SNI не разрешается proxy в другой IP. Нет silent fallback. Source IP должен реально принадлежать scanner node. Допускается серверный JSON-файл `V2_EGRESS_FILE`, доступный обоим сервисам.
