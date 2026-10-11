# Состояние проекта

Этот файл фиксирует рабочую архитектуру, которую следует переносить на другой
сервер. Источником истины для кода является Git-коммит, а не содержимое текущих
Docker volumes.

## Рабочий конвейер

```text
CIDR TXT → физическая БД task_<uuid> → IP-чанки → Masscan
                                      → открытые IP:PORT → HTTPX
                                      → Fortinet fingerprint → protocol://ip:port
```

Каждый TASK получает отдельную PostgreSQL-базу. Redis используется как очередь
сигналов для worker-а. Masscan и HTTPX работают на реальных данных, результаты
сохраняются в таблицах TASK и экспортируются через API или веб-панель.

## Панель и API

- Web: `http://127.0.0.1:28280`
- API: `http://127.0.0.1:28210`
- `GET /api/tasks` — список TASK;
- `GET /api/tasks/{id}` — прогресс и статистика TASK;
- `GET /api/tasks/{id}/events` — поток Masscan/HTTPX;
- `POST /api/tasks/preview-cidr` — предварительный подсчёт CIDR;
- `GET /api/tasks/{id}/export?matched_only=true` — экспорт найденных URL.

В панели проверены выбор TASK, обновление списка и метрик, открытие TASK,
четыре вкладки результата, переходы по `IP:PORT`, экспорт, создание и удаление
завершённого TASK. Сетевые GET-запросы панели повторяются при кратковременном
сбое.

## Проверенное состояние

Контрольный TASK #8 завершился со следующими показателями:

- 20 входных адресов;
- 20 открытых портов `10443`;
- 4/4 Masscan-чанка;
- 4/4 HTTPX-чанка;
- 10 подтверждённых Fortinet-результатов;
- экспорт строк `https://ip:10443`.

Для переноса приложения TASK #8 и другие текущие базы не считаются частью Git:
они находятся в Docker volumes и требуют отдельной резервной копии.

## Чистое развёртывание

```bash
git clone https://github.com/lan4esko/internet-search.git
cd internet-search
python3 scripts/bootstrap.py
docker compose up -d --wait
```

`scripts/bootstrap.py` создаёт приватный `.env` из `.env.example`, генерирует
пароль PostgreSQL, собирает образы, применяет миграции control DB и проверяет
health endpoints. Секреты, raw scan-артефакты и Docker volumes в Git не входят.
