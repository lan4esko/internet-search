# Архитектура

```mermaid
flowchart TD
  UI[Web] --> API[FastAPI]
  API --> C[(control DB)]
  API --> R[Redis]
  API --> F[(scope_data)]
  R --> W[task-runner]
  W --> C
  W --> T[(task_uuid DB)]
  W --> M[Masscan]
  W --> H[HTTPX]
  M --> T
  H --> T
```

`control DB` содержит только `task_registry`: имя TASK, имя физической базы,
исходный файл, конфигурацию и сводные счётчики. Данные одного запуска никогда
не смешиваются с данными другого запуска.

`task-runner` — рабочий Docker-сервис. Он получает сигнал из Redis, затем сам
читает реестр и подбирает потерянные `queued` TASK. Для TASK он подключается к
его физической базе, расширяет CIDR, создаёт IP-чанки, запускает Masscan,
сохраняет открытые порты, запускает HTTPX и записывает fingerprint.

Внешние сервисы не создают фиктивные наблюдения. API отображает только строки,
которые появились после реального subprocess и SQL-транзакции.

Control database содержит только реестр TASK. Развёртывание выполняется на
чистом PostgreSQL volume; физические базы TASK создаются worker-ом через
`CREATE DATABASE` и удаляются вместе с TASK.
