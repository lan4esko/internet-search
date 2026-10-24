#!/usr/bin/env python3
"""Real local acceptance; removes temporary lab scope exception in finally."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def run(*args,env=None,capture=False):return subprocess.run(args,cwd=ROOT,env=env,check=True,text=True,capture_output=capture)
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--browser',action='store_true');parser.add_argument('--restart-test',action='store_true');args=parser.parse_args()
    run('docker','compose','--profile','lab','up','-d','--wait','--wait-timeout','60','lab');container=run('docker','compose','ps','-q','lab',capture=True).stdout.strip()
    networks=json.loads(run('docker','inspect',container,capture=True).stdout)[0]['NetworkSettings']['Networks']
    if len(networks)!=1:raise RuntimeError('Fixture must have one network')
    peer=next(iter(networks.values()));cidr=peer['IPAddress']+'/32';environment=dict(os.environ,V2_LAB_CIDRS=cidr,V2_TEST_CIDR=cidr,V2_TEST_GATEWAY_MAC=peer['MacAddress'])
    if args.restart_test:environment['V2_TEST_RESTART']='1'
    environment['PYTHONPATH']=':'.join(str(ROOT/p) for p in ['shared','services/api','services/metrics-agent'])
    try:
        run('docker','compose','-f','docker-compose.yml','-f','tests/v2/compose.lab.yml','up','-d','--wait','--wait-timeout','120','api','task-runner','web',env=environment)
        suites=['tests.unit.test_container_metrics','tests.unit.test_masscan_formats','tests.v2.test_core','tests.v2.test_backup','tests.v2.test_integration']
        if args.browser:suites.append('tests.v2.test_browser')
        run(sys.executable,'-m','unittest',*suites,'-v',env=environment)
    finally:
        run('docker','compose','up','-d','--wait','--wait-timeout','120','api','web');run('docker','compose','--profile','lab','stop','lab')
if __name__=='__main__':main()
