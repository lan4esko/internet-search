# Профиль TASK

Новый запуск не использует старый профиль snapshot/shard. Параметры TASK
передаются в `POST /api/tasks` и сохраняются в `control DB` и `task_meta`.

Обязательные параметры: `masscan_chunk_size`, `httpx_chunk_size`,
`masscan_ports`, `masscan_rate`, `masscan_workers`, `httpx_workers`.

HTTPX-пути и match strings первой версии фиксированы рабочим Fortinet-скриптом:

```text
/remote/login,/remote/index,/ssl-vpn/login.html
ftnt-fortinet-grid
name="magic"
SVPNCOOKIE
```

Подробный контракт: [task-pipeline.md](task-pipeline.md).
