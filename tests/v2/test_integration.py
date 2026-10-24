import hashlib,json,os,subprocess,time,unittest,uuid
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from tests.integration.test_task_api import multipart
API=os.getenv('V2_API_URL','http://127.0.0.1:28210')+'/v2'
CIDR=os.getenv('V2_TEST_CIDR')
def request(path,method='GET',body=None,content_type='application/json'):
    if isinstance(body,dict):body=json.dumps(body).encode()
    with urlopen(Request(API+path,data=body,method=method,headers={'Content-Type':content_type}),timeout=30) as response:
        data=response.read();return json.loads(data) if data else None
def create(config=None,name=None):
    cfg={'ports':[8443],'rate_pps':10,'timeout':3,'sni':['lab.invalid'],'paths':['/global-protect/login.esp','/redirect']}
    if os.getenv('V2_TEST_GATEWAY_MAC'):cfg['gateway_mac']=os.environ['V2_TEST_GATEWAY_MAC']
    if config:cfg.update(config)
    body,kind=multipart({'name':name or 'v2-lab-'+uuid.uuid4().hex[:8],'config':json.dumps(cfg)},'authorized-lab.txt',(CIDR+'\n').encode())
    return request('/tasks','POST',body,kind)
def wait(task_id,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        task=request(f'/tasks/{task_id}')
        if task['status'] in {'completed','completed_with_errors','failed','cancelled'}:return task
        time.sleep(.3)
    raise AssertionError('TASK timeout: '+json.dumps(task))
@unittest.skipUnless(CIDR,'Set V2_TEST_CIDR to the local Docker fixture /32')
class PipelineTests(unittest.TestCase):
    def setUp(self):self.ids=[]
    def tearDown(self):
        for task_id in self.ids:
            row=request(f'/tasks/{task_id}')
            if row['status'] not in {'completed','completed_with_errors','failed','cancelled'}:request(f'/tasks/{task_id}/cancel','POST');wait(task_id)
            for attempt in range(30):
                try:request(f'/tasks/{task_id}','DELETE');break
                except HTTPError as error:
                    if error.code!=409:raise
                    time.sleep(.1)
            else:raise AssertionError('Worker lock not released')
    def test_real_pipeline_and_redirect_guard(self):
        task=create();self.ids.append(task['id']);final=wait(task['id']);self.assertEqual(final['status'],'completed',final)
        self.assertEqual(final['stats']['endpoints']['total'],1);self.assertEqual(final['stats']['coverage']['sent'],1)
        rows=request(f"/tasks/{task['id']}/observations")['items'];self.assertEqual(len(rows),3)
        tls=next(r for r in rows if r['module']=='tls');self.assertEqual(tls['observation']['status'],'success');self.assertIn('lab.invalid',tls['observation']['tls']['san']);self.assertEqual(len(tls['observation']['tls']['spki_sha256']),64)
        confirmed=[r for r in rows if any(e['tier']=='http_confirmed' for e in r['evidence'])];self.assertEqual(len(confirmed),1)
        redirect=next(r for r in rows if r['path']=='/redirect');self.assertEqual(redirect['observation']['http']['status_code'],302);self.assertFalse(redirect['observation']['http']['redirect_followed'])
    def test_live_snapshot_checksum(self):
        task=create();self.ids.append(task['id'])
        with urlopen(API+f"/tasks/{task['id']}/export?stage=all&format=jsonl") as response:
            body=response.read();self.assertEqual(hashlib.sha256(body).hexdigest(),response.headers['X-SHA256'])
            for line in body.splitlines():
                r=json.loads(line);key='X-Endpoint-Watermark' if r['record_type']=='discovery' else 'X-Watermark';self.assertLessEqual(r['id'],int(response.headers[key]))
        self.assertEqual(wait(task['id'])['status'],'completed')
        with urlopen(API+f"/tasks/{task['id']}/export?stage=matches&format=csv") as response:self.assertEqual(response.headers['X-Row-Count'],'1')
    def test_cancel_and_invalid_scope(self):
        with self.assertRaises(HTTPError) as error:request('/scope/validate','POST',{'cidr_text':'127.0.0.0/8'})
        self.assertEqual(error.exception.code,422)
        task=create();self.ids.append(task['id']);request(f"/tasks/{task['id']}/cancel",'POST');self.assertEqual(wait(task['id'])['status'],'cancelled')
    def test_sse_and_yaml_validation(self):
        task=create();self.ids.append(task['id'])
        with urlopen(API+f"/tasks/{task['id']}/events",timeout=10) as response:
            self.assertEqual(response.readline().decode().strip(),'event: task');self.assertEqual(json.loads(response.readline().decode()[6:])['id'],task['id'])
        with self.assertRaises(HTTPError) as error:request('/templates/bad','PUT',b'version: [bad','application/yaml')
        self.assertEqual(error.exception.code,422)
    @unittest.skipUnless(os.getenv('V2_TEST_RESTART')=='1','Set V2_TEST_RESTART=1 for restart test')
    def test_restart_without_duplicates(self):
        task=create({'paths':['/slow','/global-protect/login.esp'],'timeout':10});self.ids.append(task['id'])
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if request(f"/tasks/{task['id']}")['stats']['endpoints']['running']:break
            time.sleep(.1)
        else:self.fail('No active collector')
        subprocess.run(['docker','compose','restart','--timeout','1','task-runner'],check=True,capture_output=True)
        final=wait(task['id']);self.assertEqual(final['status'],'completed',final);self.assertEqual(final['stats']['endpoints']['total'],1)
        rows=request(f"/tasks/{task['id']}/observations")['items'];self.assertEqual(len(rows),3);self.assertEqual(len({(r['endpoint_id'],r['module'],r['path'],r['scheme']) for r in rows}),3)
