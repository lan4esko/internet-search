# Диагностика реального TASK-конвейера

Диагностика должна идти по трём отдельным уровням, чтобы нулевой результат
не маскировал ошибку следующего этапа:

1. `task_chunks(stage='masscan')` и raw JSON показывают запуск Masscan,
   статус процесса, хвост интерактивного вывода, размер файла и `found_count`.
2. `task_open_ports` показывает, какие `IP:PORT` действительно прошли TCP
   этап. Только эти строки передаются HTTPX.
3. `task_chunks(stage='httpx')`, `httpx_results` и raw JSONL показывают HTTP
   ответы, ошибки процесса и совпавшие Fortinet признаки.

Полезные команды:

```bash
docker compose logs --tail=200 task-runner
docker compose exec postgres psql -U app -d internet_search \
  -c 'select id,name,status,database_name,target_count,open_port_count,result_count from task_registry order by id desc;'
```

После получения имени физической БД TASK:

```bash
docker compose exec postgres psql -U app -d task_<uuid> \
  -c 'select stage,status,item_count,found_count,response_count,matched_count,status_tail,error_message from task_chunks order by stage,chunk_no;'
```

Если Masscan дал `found_count=0`, HTTPX запускаться не должен. Если найден
открытый порт, но нет HTTPX ответа, проверяются схема, маршрут, TLS и сетевой
доступ; изменение fingerprint в этот момент не исправляет транспортную ошибку.
