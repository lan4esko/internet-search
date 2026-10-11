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

В панели также доступны два профиля Palo Alto. Они разделены, потому что
GlobalProtect и административная Web UI PAN-OS используют разные страницы:

```text
Palo Alto GlobalProtect:
/global-protect/login.esp
GlobalProtect Portal
id="login_form"
name="prot"
/global-protect/portal/css/login.css
/global-protect/portal/images/logo-pan-

Palo Alto PAN-OS Admin:
/php/login.php
window.Pan
Panos.browser
panos-panos-login.js
/styles/login/css/login-admin.css
/login/images/panw_new_logo_302_53.png
id="login_form"
name="authType"
name="challengeCookie"
```

Для Palo Alto PAN-OS Admin все перечисленные признаки должны присутствовать
в ответе HTTP со статусом `200`; обычные браузерные заголовки и `PHPSESSID`
в fingerprint не входят, потому что они не являются уникальными.

Подробный контракт: [task-pipeline.md](task-pipeline.md).
