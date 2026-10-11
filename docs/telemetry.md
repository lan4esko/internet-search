# Нагрузка и прогресс

Прогресс TASK считается по фактическим чанкам в его физической БД:

- количество IP в `task_targets`;
- завершённые, активные и ошибочные Masscan-чанки;
- открытые `IP:PORT` в `task_open_ports`;
- HTTPX-чанки и записи `httpx_results`;
- совпадения Fortinet fingerprint.

Отдельный metrics-agent продолжает собирать CPU, память, PIDs и сетевые байты
контейнеров `web`, `api`, `postgres`, `redis`, `task-runner` и `metrics-agent`.
Эти метрики показывают нагрузку инфраструктуры и не заменяют прогресс TASK.

API также предоставляет:

- `GET /api/operations/container-metrics/history` — ограниченную историю срезов;
- `GET /api/operations/prometheus` — текущий Prometheus-compatible срез для
  опционального Grafana-профиля.

История хранится в Redis с ограниченным размером и сроком жизни; PostgreSQL
остаётся источником данных о результатах TASK.
