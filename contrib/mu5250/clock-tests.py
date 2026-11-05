"""Exercise the actual clock guard's ordering, retry and process ownership."""
from pathlib import Path
import os,subprocess,tempfile,json,shutil

WORK=Path(__file__).resolve().parent
ROOT=WORK/'clock-tests';ROOT.mkdir(exist_ok=True)
BASH=os.environ.get('MU_OC_TEST_BASH') or (r'C:\Program Files\Git\bin\bash.exe' if os.name=='nt' else shutil.which('bash'))
def unix(p):
    s=str(p.resolve()).replace('\\','/');return '/'+s[0].lower()+s[2:] if len(s)>1 and s[1]==':' else s
passed=[]
with tempfile.TemporaryDirectory(prefix='guard-',dir=ROOT) as directory:
    folder=Path(directory);assert folder.resolve().parent==ROOT.resolve()
    for name in ['bin','app/run','proc/123']:(folder/name).mkdir(parents=True,exist_ok=True)
    (folder/'proc/uptime').write_text('100.00 0.00\n')
    (folder/'secret').write_text('dummy-test-secret')
    (folder/'app/run/core.pid').write_text('123\n')
    (folder/'log').write_text('NTP service start\n')
    (folder/'bin/readlink').write_text('#!/bin/sh\nif [ "${TEST_OWNER:-1}" = 1 ]; then echo "$MU_OC_CORE_EXE"; else echo /wrong; fi\n',newline='\n')
    (folder/'bin/curl').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_ROOT/curl.args"\ncat > "$TEST_ROOT/curl.header"\n[ "${TEST_CURL_FAIL:-0}" = 0 ]\n',newline='\n')
    env=os.environ.copy();env.update({'TEST_ROOT':unix(folder),'TEST_BIN':unix(folder/'bin'),
        'TEST_SCRIPT':unix(WORK/'core-clock-guard.sh'),'MU_OC_BASE':unix(folder/'app'),'MU_OC_PROC_ROOT':unix(folder/'proc'),
        'MU_OC_LOG':unix(folder/'log'),'MU_OC_CORE_EXE':'/app/core','MU_OC_SECRET_FILE':unix(folder/'secret'),'MU_OC_CONTROLLER':'27971'})
    def run(*args,**extra):
        e=env.copy();e.update(extra)
        return subprocess.run([BASH,'--noprofile','--norc','-c','export PATH="$TEST_BIN:/usr/bin:/bin"; exec /bin/sh "$TEST_SCRIPT" "$@"','test',*args],env=e,capture_output=True,text=True,timeout=5)
    def check(name,condition):
        assert condition,name;passed.append(name);print('PASS',name)
    check('Prepare does not mark an unsynchronized core ready',run('prepare').returncode==0 and not (folder/'app/run/clock-ready.pid').exists())
    check('NTP start alone never flushes or declares readiness',run('step','123').returncode!=0 and not (folder/'curl.args').exists())
    (folder/'log').write_text('System clock is behind NTP time by 8h0m0s\n')
    check('A fresh NTP event triggers DNS flush and PID readiness',run('step','123').returncode==0 and (folder/'app/run/clock-ready.pid').read_text().strip()=='123')
    check('API key goes through stdin, not command arguments','dummy-test-secret' not in (folder/'curl.args').read_text() and 'dummy-test-secret' in (folder/'curl.header').read_text())
    count=(folder/'curl.args').read_text().count('\n');run('step','123')
    check('The same synchronization event is not handled twice',(folder/'curl.args').read_text().count('\n')==count)
    (folder/'log').write_text('System clock is behind NTP time by 7h59m59s\n')
    before=(folder/'app/run/clock-event').read_text()
    check('API failure preserves the event for retry',run('step','123',TEST_CURL_FAIL='1').returncode!=0 and (folder/'app/run/clock-event').read_text()==before)
    check('A failed flush recovers on the next attempt',run('step','123').returncode==0 and (folder/'app/run/clock-event').read_text()!=before)
    (folder/'log').write_text('System clock is behind NTP time by 7h59m58s\n')
    count=(folder/'curl.args').read_text().count('\n')
    check('A foreign process cannot cause a controller request',run('step','123',TEST_OWNER='0').returncode!=0 and (folder/'curl.args').read_text().count('\n')==count)
    run('prepare')
    check('Wait timeout uses monotonic uptime and remains bounded',run('wait','123','0').returncode!=0)
(ROOT/'results.json').write_text(json.dumps({'passed':len(passed),'tests':passed},indent=2),encoding='utf-8')
print(str(len(passed))+' passed')
