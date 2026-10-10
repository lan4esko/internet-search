# Scope ingestion в TASK-конвейере

Старый поток `scope source → snapshot → scan plan → shard` удалён из рабочего
контракта. Сейчас загрузка CIDR является атомарной операцией создания TASK.

API сохраняет исходный файл и SHA-256 в control database, создаёт физическую
БД `task_<uuid>`, записывает канонические IPv4 CIDR в `task_cidrs`, после чего
публикует событие `tasks.created`. Task-runner разворачивает сети в `task_targets`
и создаёт Masscan-чанки. Список CIDR после создания TASK неизменяем.

Подробная схема этапов и таблиц находится в [task-pipeline.md](../task-pipeline.md).
