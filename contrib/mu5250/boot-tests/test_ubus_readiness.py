"""Execute the actual readiness helper against controlled boot timing fixtures."""
from pathlib import Path
import json
import os
import socket
import subprocess
import tempfile
import shutil

ROOT=Path(__file__).resolve().parent
BASH=Path(os.environ.get('MU_OC_TEST_BASH') or (r'C:\Program Files\Git\bin\bash.exe' if os.name=='nt' else shutil.which('bash')))
def unix(path):
    value=str(path.resolve()).replace('\\','/')
    return '/'+value[0].lower()+value[2:] if len(value)>1 and value[1]==':' else value
results=[]
def run(name,phase,expected=0,elapsed=0,**settings):
    with tempfile.TemporaryDirectory(prefix='ubus-test-',dir=ROOT) as directory:
        folder=Path(directory)
        (folder/'proc').mkdir()
        fixture='preexisting-path'
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                sock.bind(str(folder/'ubus.sock'))
            fixture='stale-unix-socket'
        except (AttributeError,OSError):
            (folder/'ubus.sock').touch(exist_ok=True)
        env=os.environ.copy()
        env.update({'TEST_ROOT':unix(folder),'MU_OC_PROC_ROOT':unix(folder/'proc'),
                    'TEST_SCRIPT':unix(ROOT.parent/'wait-private-ubus.sh'),'TEST_PHASE':phase})
        env.update({'TEST_'+k.upper():str(v) for k,v in settings.items()})
        result=subprocess.run([str(BASH),'--noprofile','--norc','--posix',unix(ROOT/'ubus-launcher.sh')],env=env,capture_output=True,text=True,timeout=15)
        assert result.returncode==expected,(name,result.returncode,result.stdout,result.stderr)
        actual=int((folder/'proc/uptime').read_text().split('.')[0])
        assert actual==elapsed,(name,actual,elapsed)
        calls=(folder/'calls').read_text() if (folder/'calls').exists() else ''
        assert calls,(name,'No real connection probe was attempted')
        results.append({'name':name,'pass':True,'fixture':fixture,'elapsed':actual})
        print('PASS',name)

for name in ['wait-private-ubus.sh','private-init.sh','ubus-wrapper.sh']:
    subprocess.run([str(BASH),'-n',unix(ROOT.parent/name)],check=True,capture_output=True)
run('Stale socket cannot make bus immediately ready','bus',bus_at=4,elapsed=4)
run('Permanently stale socket times out','bus',expected=1,bus_at=99,limit=4,elapsed=4)
run('Connected bus passes immediately','bus')
run('RPCD must register session object','rpcd',session_at=3,elapsed=3)
run('RPCD session alone is insufficient without board data','rpcd',board_at=5,elapsed=5)
run('Missing board data is a bounded failure','rpcd',board_at=99,expected=1,limit=4,elapsed=4)
run('Empty successful list output does not mean session exists','rpcd',empty_session=1,expected=1,limit=4,elapsed=4)
run('RPCD must wait for both connection and objects','rpcd',bus_at=2,session_at=4,board_at=6,elapsed=6)
(ROOT/'ubus-results.json').write_text(json.dumps({'passed':len(results),'failed':0,'tests':results},indent=2),encoding='utf-8')
print(f'{len(results)} passed; 0 failed')
