# Проверка Masscan

Отдельный старый endpoint пробного snapshot-запуска больше не является частью
контракта. Тест выполняется созданием небольшого TASK с нужным
`masscan_chunk_size`, одним разрешённым CIDR и реальным task-runner.

Для проверки процесса смотрите `task_chunks`, raw-файлы в
`scope_data/tasks/<task_id>/masscan` и `task_open_ports`.
