"""Offline timing tests: execute the real shell scripts, never device commands."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import shutil

ROOT=Path(__file__).resolve().parent
SCRIPTS=ROOT.parent
BASH=Path(os.environ.get('MU_OC_TEST_BASH') or (r'C:\Program Files\Git\bin\bash.exe' if os.name=='nt' else shutil.which('bash')))
def unix(path):
    value=str(path.resolve()).replace('\\','/')
    return '/'+value[0].lower()+value[2:] if len(value)>1 and value[1]==':' else value

results=[]
def run(name,expected=0,contains='',script='core-ready.sh',**settings):
    with tempfile.TemporaryDirectory(prefix='boot-test-',dir=ROOT) as directory:
        folder=Path(directory)
        for child in ['app/run','proc/net','proc/123','sys']:
            (folder/child).mkdir(parents=True,exist_ok=True)
        env=os.environ.copy()
        env.update({'TEST_ROOT':unix(folder),'MU_OC_BASE':unix(folder/'app'),
                    'MU_OC_PROC_ROOT':unix(folder/'proc'),'MU_OC_SYS_ROOT':unix(folder/'sys'),
                    'TEST_SCRIPT':unix(SCRIPTS/script)})
        env.update({'TEST_'+key.upper():str(value) for key,value in settings.items()})
        if script=='start-enabled-core.sh':
            service=folder/'service.sh'
            service.write_text('#!/bin/bash\nn=0\n[ ! -r "$TEST_ROOT/count" ] || read -r n < "$TEST_ROOT/count"\nn=$((n+1)); echo "$n" > "$TEST_ROOT/count"\n[ "$n" -ge "${TEST_SUCCESS_AT:-99}" ]\n',encoding='utf-8',newline='\n')
            env['MU_OC_SERVICE']=unix(service)
        process=subprocess.run([str(BASH),'--noprofile','--norc','--posix',unix(ROOT/'launcher.sh')],env=env,capture_output=True,text=True,timeout=15)
        output=process.stdout+process.stderr
        assert process.returncode==expected,(name,process.returncode,output)
        assert contains in output,(name,output)
        if script=='core-ready.sh' and settings.get('mode')=='check':
            assert not (folder/'app/run/startup-state').exists(),name
        results.append({'name':name,'pass':True})
        print('PASS',name)

for source in ['core-ready.sh','start-enabled-core.sh','openclash-service.sh','private-init.sh']:
    subprocess.run([str(BASH),'-n',unix(SCRIPTS/source)],check=True,capture_output=True)
run('API is ready before proxy listeners and TUN',listen_at=3,tun_at=6,contains='waiting-for-tun')
run('Initial fork/exec delay must not abort startup',exec_at=3,listen_at=3,tun_at=4,contains='waiting-for-core-exec')
run('API alone is insufficient; TUN timeout is bounded',expected=1,tun_at=99,limit=5,contains='timed out at waiting-for-tun')
run('Controller readiness may lag behind process',api_at=4,contains='waiting-for-controller')
run('DNS UDP socket is required',expected=1,dns_at=99,limit=4,contains='waiting-for-dns-listeners')
run('TUN must be up, not merely present',expected=1,tun_flags='0x0',limit=4,contains='waiting-for-tun')
run('Established TCP sockets do not count as listeners',expected=1,tcp_state='01',limit=4,contains='waiting-for-proxy-listeners')
run('A core that exits before readiness fails promptly',expected=1,api_at=99,exit_at=2,contains='core exited')
run('One-shot ready check has no status-file side effect',mode='check')
run('Invalid port is rejected',expected=64,redir_port=65536)
run('Legacy UDP engine waits for the TPROXY socket',engine='tproxy',tproxy_at=3,tun_at=99,contains='waiting-for-tproxy-listener')
run('Boot retries succeed on the third attempt',script='start-enabled-core.sh',success_at=3,contains='startup completed')
run('Boot retries stop after three failures',script='start-enabled-core.sh',expected=1,contains='retries exhausted')
run('User disabling the service cancels boot retries',script='start-enabled-core.sh',disable_after_fail=1,contains='attempt failed')
(ROOT/'results.json').write_text(json.dumps({'passed':len(results),'failed':0,'tests':results},indent=2),encoding='utf-8')
print(f'{len(results)} passed; 0 failed')
