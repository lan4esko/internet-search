"""Smoke test for the TASK-centric panel using a deterministic API fixture."""
import json
import shutil
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest import SkipTest, TestCase
from urllib.parse import parse_qs, urlsplit


STATIC = Path(__file__).resolve().parents[2] / "services" / "web" / "static"


class TaskPanelBrowserTests(TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SkipTest("Для браузерной проверки нужен Python playwright")
        chromium = shutil.which("chromium")
        if not chromium:
            with sync_playwright() as paths:
                candidate=paths.chromium.executable_path
                chromium=candidate if Path(candidate).is_file() else None
        if not chromium:
            raise SkipTest("Для браузерной проверки нужен Chromium")
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(executable_path=chromium, headless=True, args=["--no-sandbox"])
        except Exception as error:
            raise SkipTest(f"Playwright не запустился: {error}") from error

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.created_tasks = []
        self.create_requests = []
        self.hold_create = False
        self.pending_create = None
        self.create_failure = None
        self.no_results = False
        self.many_results = False
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.page.set_default_timeout(10000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.route)
        self.page.goto("http://panel.test/")
        self.page.locator("#tasks-table tr").wait_for(state="attached")

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        parsed = urlsplit(route.request.url)
        if not parsed.path.startswith("/api/"):
            files = {"/": "legacy.html", "/app.js": "app.js", "/styles.css": "styles.css"}
            filename = files.get(parsed.path)
            if not filename:
                route.fulfill(status=404, body="missing")
                return
            mime = "text/html" if filename.endswith("html") else "application/javascript" if filename.endswith("js") else "text/css"
            route.fulfill(content_type=mime, body=(STATIC / filename).read_text())
            return
        path = parsed.path.removeprefix("/api")
        task = {"id": 7, "name": "Fortinet test", "database_name": "task_" + "a" * 32,
                "source_filename": "scope.txt", "source_sha256": "b" * 64, "status": "completed",
                "config": {"masscan_ports": "10443"}, "target_count": 4, "open_port_count": 1,
                "result_count": 1, "error_message": None, "created_at": "2026-10-10T00:00:00Z",
                "started_at": "2026-10-10T00:00:01Z", "finished_at": "2026-10-10T00:01:00Z",
                "progress_percent": 100, "masscan_chunks": {"total": 2, "completed": 2, "running": 0, "failed": 0},
                "httpx_chunks": {"total": 1, "completed": 1, "running": 0, "failed": 0}}
        if self.no_results:
            task["result_count"] = 0
        elif self.many_results:
            task["result_count"] = 201
        if path == "/templates":
            payload = {"items": [], "total": 0}
        elif path == "/operations/container-metrics":
            payload = {"containers": [], "status": "ok"}
        elif path == "/tasks/preview-cidr":
            payload = {"filename": "scope.txt", "sha256": "b" * 64,
                       "subnet_count": 1, "address_count": 1, "duplicate_count": 0, "invalid_count": 0}
        elif path == "/tasks" and route.request.method == "POST":
            content_type = route.request.headers["content-type"]
            body = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + route.request.post_data_buffer
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
                      for part in BytesParser(policy=default).parsebytes(body).iter_parts()}
            self.create_requests.append(fields)
            if self.create_failure:
                route.fulfill(status=422, content_type="application/json", body=json.dumps({"detail": self.create_failure}))
                return
            created = {**task, "id": 8, "name": fields["name"], "status": "queued", "progress_percent": 0}
            if self.hold_create:
                self.pending_create = (route, created)
                return
            self.fulfill_create(route, created)
            return
        elif path == "/tasks":
            payload = {"items": self.created_tasks + [task], "total": len(self.created_tasks) + 1, "limit": 100, "offset": 0}
        elif path == "/tasks/7":
            payload = task
        elif path == "/tasks/8":
            payload = self.created_tasks[0]
        elif path == "/tasks/7/open-ports":
            payload = {"items": [{"ip": "192.0.2.10", "port": 10443, "protocol": "tcp", "masscan_open": True, "httpx_ready": False, "httpx_status": "completed", "httpx_chunk_no": 1}], "total": 1, "limit": 200, "offset": 0}
        elif path == "/tasks/7/results":
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            if self.many_results:
                items = [{"id": i + 1, "ip": f"192.0.2.{i + 1}", "port": 10443, "protocol": "https", "url": f"https://192.0.2.{i + 1}:10443", "status_code": 200, "title": "Please Login", "matched": True}
                         for i in range(offset, min(offset + 200, 201))]
                total = 201
            else:
                items = [] if self.no_results or offset else [{"id": 1, "ip": "192.0.2.10", "port": 10443, "protocol": "https", "url": "https://192.0.2.10:10443", "status_code": 200, "title": "Please Login", "matched": True}]
                total = 0 if self.no_results else 1
            payload = {"items": items, "total": total, "limit": 200, "offset": offset}
        elif path == "/tasks/7/targets":
            payload = {"items": [{"ip": "192.0.2.10", "source_chunk_no": 1, "masscan_status": "open", "ready_for_masscan": False}], "total": 1, "limit": 200, "offset": 0}
        elif path == "/tasks/7/events":
            payload = {"items": [
                {"id": 1, "stage": "masscan", "level": "info", "message": "Masscan завершил чанк 1", "payload": {"chunk": 1, "open_ports": 1}, "created_at": "2026-10-10T00:00:03Z"},
                {"id": 2, "stage": "httpx", "level": "info", "message": "HTTPX завершил чанк 1", "payload": {"chunk": 1, "responses": 1, "matched": 1}, "created_at": "2026-10-10T00:00:04Z"},
            ] if parsed.query.find("after_id=0") >= 0 else [], "last_id": 2}
        elif path == "/tasks/7/export":
            payload = {"task_id": 7, "count": 0 if self.no_results else 1,
                       "targets": [] if self.no_results else ["https://192.0.2.10:10443"]}
        else:
            payload = {"task_id": 7, "count": 1, "targets": ["https://192.0.2.10:10443"]}
        route.fulfill(content_type="application/json", body=json.dumps(payload))

    def fulfill_create(self, route, created):
        self.created_tasks.append(created)
        route.fulfill(status=202, content_type="application/json", body=json.dumps(created))

    def fill_task(self, fingerprint="paloalto-panos-admin"):
        self.page.locator("#task-name").fill("Palo Alto creation test")
        self.page.locator("#fingerprint-id").select_option(fingerprint)
        self.page.locator("#task-file").set_input_files({"name": "scope.txt", "mimeType": "text/plain", "buffer": b"192.0.2.1/32\n"})

    def test_create_task_after_async_response_opens_details(self):
        self.fill_task()
        self.page.locator(".create-button").click()
        self.page.wait_for_function("document.querySelector('#detail-name').textContent === 'Palo Alto creation test'")
        self.assertEqual(len(self.create_requests), 1)
        self.assertIn("TASK #8 создан", self.page.locator("#task-form-message").inner_text())
        self.assertNotIn("reset", self.page.locator("#task-form-message").inner_text())
        self.assertEqual(self.page.locator("#task-name").input_value(), "")
        self.assertFalse(self.page.locator(".create-button").is_disabled())
        self.assertEqual(self.page.locator(".task-tab.active strong").inner_text(), "TASK #8 · Palo Alto creation test")
        fields = self.create_requests[0]
        self.assertEqual(json.loads(fields["httpx_paths"]), ["/php/login.php"])
        self.assertEqual(json.loads(fields["httpx_schemes"]), ["https"])
        self.assertIn("window.Pan", json.loads(fields["httpx_match_strings"]))

    def test_globalprotect_submission_keeps_its_paths(self):
        self.fill_task("paloalto-globalprotect-portal")
        self.page.locator(".create-button").click()
        self.page.wait_for_function("document.querySelector('#detail-name').textContent === 'Palo Alto creation test'")
        self.assertEqual(json.loads(self.create_requests[0]["httpx_paths"]), ["/global-protect/login.esp"])
        self.assertEqual(json.loads(self.create_requests[0]["httpx_schemes"]), ["https"])

    def test_pending_creation_blocks_duplicate_submissions(self):
        self.hold_create = True
        self.fill_task()
        self.page.locator(".create-button").click()
        self.page.wait_for_function("document.querySelector('.create-button').disabled")
        self.page.evaluate("document.querySelector('#task-form').requestSubmit()")
        self.assertEqual(len(self.create_requests), 1)
        self.assertIsNotNone(self.pending_create)
        self.fulfill_create(*self.pending_create)
        self.page.wait_for_function("document.querySelector('#detail-name').textContent === 'Palo Alto creation test'")
        self.page.wait_for_function("!document.querySelector('.create-button').disabled")
        self.assertFalse(self.page.locator(".create-button").is_disabled())
        self.assertEqual(len(self.create_requests), 1)

    def test_failed_creation_keeps_inputs_and_allows_retry(self):
        self.create_failure = "Некорректный CIDR"
        self.fill_task()
        self.page.locator(".create-button").click()
        self.page.wait_for_function("document.querySelector('#task-form-message').textContent.includes('Некорректный CIDR')")
        self.assertEqual(self.page.locator("#task-name").input_value(), "Palo Alto creation test")
        self.assertEqual(self.page.locator("#task-file").evaluate("input => input.files.length"), 1)
        self.assertFalse(self.page.locator(".create-button").is_disabled())
        self.assertEqual(self.created_tasks, [])

    def test_task_progress_and_result_tabs(self):
        self.page.locator(".task-directory > summary").click()
        self.page.get_by_role("button", name="Открыть", exact=True).click()
        self.assertIn("100.0%", self.page.locator("#detail-progress-label").inner_text())
        self.assertIn("100.0%", self.page.locator("#task-stats").inner_text())
        self.page.locator("#detail-content .result-link").get_by_text("https://192.0.2.10:10443", exact=True).wait_for()
        self.assertEqual(self.page.locator("#detail-content .endpoint-link").count(), 0)
        self.assertFalse(self.page.locator("#task-diagnostics").evaluate("element => element.open"))
        self.page.get_by_text("Masscan завершил чанк 1").wait_for()
        self.page.get_by_text("HTTPX завершил чанк 1").wait_for()
        self.assertNotIn("HTTPX завершил", self.page.locator("#masscan-log").inner_text())
        self.assertNotIn("Masscan завершил", self.page.locator("#httpx-log").inner_text())
        self.page.locator("#task-diagnostics > summary").click()
        self.page.get_by_role("button", name="IP и чанки · IP(0)", exact=True).click()
        self.page.locator("#diagnostic-content").get_by_text("192.0.2.10", exact=True).wait_for()

    def test_no_ip2_shows_empty_result_instead_of_masscan_list(self):
        self.no_results = True
        self.page.locator(".task-directory > summary").click()
        self.page.get_by_role("button", name="Открыть", exact=True).click()
        self.page.locator("#detail-content").get_by_text("Подтверждённых IP(2) пока нет").wait_for()
        self.assertEqual(self.page.locator("#detail-content table").count(), 0)
        self.assertFalse(self.page.locator("#task-diagnostics").evaluate("element => element.open"))
        self.page.locator("#export-results").click()
        self.page.get_by_text("Подтверждённых IP(2) пока нет: экспортировать нечего.").wait_for()

    def test_ip2_list_can_page_beyond_first_200_results(self):
        self.many_results = True
        self.page.locator(".task-directory > summary").click()
        self.page.get_by_role("button", name="Открыть", exact=True).click()
        self.page.get_by_text("Показано 1–200 из 201").wait_for()
        self.page.get_by_role("button", name="Следующие", exact=True).click()
        self.page.get_by_text("Показано 201–201 из 201").wait_for()
        self.page.locator("#detail-content .result-link").get_by_text("https://192.0.2.201:10443", exact=True).wait_for()
        self.page.get_by_role("button", name="Назад", exact=True).click()
        self.page.get_by_text("Показано 1–200 из 201").wait_for()
