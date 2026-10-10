# Проверочный запуск по материалам `turbo`

Каталог `/home/kali/turbo` содержит не CIDR, а промежуточные строки `IP:PORT`
в `chunks/*.txt`. Эти файлы нельзя загружать в TASK напрямую: стартовая точка
конвейера принимает только IPv4 CIDR. Для проверки был создан временный feed
без изменения исходного каталога: из каждой строки взята только IPv4-часть,
повторы удалены, каждый адрес записан как отдельный `/32`.

`merged_output.txt` в проверку не включается: это агрегат с чувствительными
данными. Порты для Masscan взяты из самих chunk-файлов и переданы явным
параметром TASK. Таким образом, БД получает полный набор адресов, а не старый
готовый результат HTTPX.

Пример подготовки безопасного временного feed:

```bash
python3 - <<'PY'
from pathlib import Path
import ipaddress

source = Path('/home/kali/turbo/chunks')
addresses = set()
for path in sorted(source.glob('*.txt')):
    for line in path.read_text(errors='replace').splitlines():
        candidate = line.strip().split(':', 1)[0]
        try:
            addresses.add(str(ipaddress.ip_address(candidate)))
        except ValueError:
            pass
Path('/tmp/turbo-task-scope.txt').write_text(
    ''.join(f'{address}/32\n' for address in sorted(addresses, key=ipaddress.ip_address)),
)
print(f'адресов: {len(addresses)}')
PY
```

Запуск через CLI проекта сохраняет параметры в конкретном TASK и ждёт полного
конвейера Masscan → HTTPX:

```bash
python3 scripts/run_authorized_scan.py /tmp/turbo-task-scope.txt \
  --name 'Turbo Fortinet catalog' \
  --masscan-chunk-size 5000 --httpx-chunk-size 200 \
  --masscan-ports '443,444,541,1443,1723,4433,5443,6434,7443,8083,8443,9943,10443' \
  --masscan-rate 1000 --masscan-workers 1 --httpx-workers 1 \
  --httpx-paths /remote/login /remote/index /ssl-vpn/login.html \
  --httpx-schemes https
```

В панели у TASK должны последовательно появиться: количество IP и Masscan-
чанков, потоковая скорость и процент текущего чанка, открытые `IP:PORT`,
HTTPX-чанки и результаты Fortinet fingerprint. Экспорт `Fortinet` содержит
только совпавшие строки вида `https://IP:PORT`.
