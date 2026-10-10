"""Smoke test for the TASK-centric panel using a deterministic API fixture."""
import json
import shutil
from pathlib import Path
from unittest import SkipTest, TestCase
from urllib.parse import urlsplit


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
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.route)
        self.page.goto("http://panel.test/")
        self.page.locator("#tasks-table tr").wait_for()

    def tearDown(self):
        self.page.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        parsed = urlsplit(route.request.url)
        if not parsed.path.startswith("/api/"):
            files = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
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
        if path == "/tasks":
            payload = {"items": [task], "total": 1, "limit": 100, "offset": 0}
        elif path == "/tasks/7":
            payload = task
        elif path == "/tasks/7/open-ports":
            payload = {"items": [{"ip": "192.0.2.10", "port": 10443, "protocol": "tcp", "masscan_open": True, "httpx_ready": False, "httpx_status": "completed", "httpx_chunk_no": 1}], "total": 1, "limit": 200, "offset": 0}
        elif path == "/tasks/7/results":
            payload = {"items": [{"id": 1, "ip": "192.0.2.10", "port": 10443, "protocol": "https", "url": "https://192.0.2.10:10443", "status_code": 200, "title": "Please Login", "matched": True}], "total": 1, "limit": 200, "offset": 0}
        elif path == "/tasks/7/targets":
            payload = {"items": [{"ip": "192.0.2.10", "source_chunk_no": 1, "masscan_status": "open", "ready_for_masscan": False}], "total": 1, "limit": 200, "offset": 0}
        else:
            payload = {"task_id": 7, "count": 1, "targets": ["https://192.0.2.10:10443"]}
        route.fulfill(content_type="application/json", body=json.dumps(payload))

    def test_task_progress_and_result_tabs(self):
        self.page.get_by_role("button", name="Открыть", exact=True).click()
        self.assertIn("100%", self.page.locator("#detail-progress-label").inner_text())
        self.page.get_by_role("button", name="Fortinet", exact=True).click()
        self.page.get_by_text("https://192.0.2.10:10443", exact=True).wait_for()
        self.page.get_by_role("button", name="IP и чанки", exact=True).click()
        self.page.get_by_text("192.0.2.10", exact=True).wait_for()
