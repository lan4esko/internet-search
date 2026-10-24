import os,shutil,unittest
from tests.v2.test_integration import request,wait,CIDR
@unittest.skipUnless(CIDR,'Set V2_TEST_CIDR for real browser lab tests')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright=sync_playwright().start();cls.browser=cls.playwright.chromium.launch(executable_path=shutil.which('chromium') or cls.playwright.chromium.executable_path,headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.playwright.stop()
    def setUp(self):
        self.page=self.browser.new_page(viewport={'width':1440,'height':1080});self.page.set_default_timeout(15000);self.errors=[];self.page.on('pageerror',lambda e:self.errors.append(str(e)));self.page.goto(os.getenv('V2_WEB_URL','http://127.0.0.1:28280'));self.page.locator('#template-options input').first.wait_for()
    def tearDown(self):self.page.close();self.assertEqual(self.errors,[])
    def test_dry_run(self):
        count=request('/tasks')['total'];self.page.locator('[name=file]').set_input_files({'name':'lab.txt','mimeType':'text/plain','buffer':CIDR.encode()});self.page.locator('#validate-scope').click();self.page.locator('#scope-preview').wait_for(state='visible');self.assertIn('"target_count": 1',self.page.locator('#scope-preview').inner_text());self.assertEqual(request('/tasks')['total'],count)
    def test_mobile_width(self):
        self.page.set_viewport_size({'width':390,'height':844});self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
    def test_real_creation_export(self):
        p=self.page;p.locator('[name=name]').fill('browser-v2-restored');p.locator('[name=file]').set_input_files({'name':'lab.txt','mimeType':'text/plain','buffer':CIDR.encode()});p.locator('[name=ports]').fill('8443');p.locator('[name=sni]').fill('lab.invalid');p.locator('summary').filter(has_text='Дополнительные параметры').click()
        if os.getenv('V2_TEST_GATEWAY_MAC'):p.locator('[name=gateway_mac]').fill(os.environ['V2_TEST_GATEWAY_MAC'])
        p.locator('[name=authorized]').check();p.locator('#create').click();p.locator('#task-id').filter(has_text='TASK #').wait_for();task_id=int(p.locator('#task-id').inner_text().split('#')[1].split()[0])
        try:
            p.locator('#task-status').filter(has_text='Завершён').wait_for(timeout=60000);p.locator('#observations .tier-http_confirmed').wait_for();self.assertIn('paloalto-globalprotect',p.locator('#observations').inner_text())
            with p.expect_download() as download:p.locator('#export').click()
            self.assertTrue(download.value.suggested_filename.endswith('.jsonl'))
        finally:wait(task_id);request(f'/tasks/{task_id}','DELETE')
