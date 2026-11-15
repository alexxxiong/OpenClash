import subprocess,sys,tempfile,os,json,shutil
from pathlib import Path
script=sys.argv[1]
lab=Path(tempfile.mkdtemp(prefix='mu-home-tcp-test-',dir='/tmp'))
env={**os.environ,'MU_TCP_TEST_HOST_NS':os.readlink('/proc/1/ns/net')}
child=subprocess.Popen(['unshare','--net','--fork','/bin/sh','-c','set -e; ip link add dummy0 type dummy; ip addr add 10.200.0.2/24 dev dummy0; ip link set dummy0 up; ip route add default via 10.200.0.1 dev dummy0; echo $$; exec sleep 120'],stdout=subprocess.PIPE,text=True)
pid=int(child.stdout.readline());prefix='192.168.3.0/24'
def ns(*args):return subprocess.check_output(['nsenter','-t',str(pid),'-n',*args],text=True).strip()
def invoke(*args,success=True):
    p=subprocess.run(['python3',script,'--state-dir',str(lab),*args],env=env,capture_output=True,text=True)
    assert (p.returncode==0)==success,(p.stdout,p.stderr)
def once(success=True):invoke('--once','--test-pid',str(pid),success=success)
def routes():return json.loads(ns('ip','-4','-j','route','show'))
original=routes();host_default=Path('/proc/sys/net/ipv4/tcp_congestion_control').read_text()
try:
    invoke('--once','--test-pid','1',success=False)
    assert routes()==original
    print('PASS host namespace refuses without touching routes')
    ns('ip','route','add',prefix,'via','10.200.0.1','dev','dummy0','proto','static')
    foreign=routes();once(success=False);assert routes()==foreign
    ns('ip','route','del',prefix)
    print('PASS existing foreign route is preserved')
    once();current=routes();owned=[r for r in current if r['dst']==prefix]
    assert len(owned)==1 and str(owned[0]['protocol'])=='242' and owned[0]['metrics']==[{'congestion':'bbr'}]
    assert ns('sysctl','-n','net.ipv4.tcp_congestion_control')=='cubic'
    assert Path('/proc/sys/net/ipv4/tcp_congestion_control').read_text()==host_default
    print('PASS scoped BBR uses existing next hop; global defaults unchanged')
    once();assert routes()==current
    assert len(json.loads((lab/'routes.json').read_text()))==1
    print('PASS repeated apply is idempotent')
    invoke('--cleanup');invoke('--cleanup');assert routes()==original
    print('PASS owned cleanup restores original routes')
    once();ns('ip','route','change',prefix,'via','10.200.0.1','dev','dummy0','proto','242','congctl','reno')
    changed=routes();invoke('--cleanup');assert routes()==changed
    ns('ip','route','del',prefix)
    print('PASS cleanup preserves a route changed by someone else')
    (lab/'routes.new').mkdir();once(success=False);assert routes()==original
    (lab/'routes.new').rmdir()
    print('PASS state write failure rolls back its new route')
finally:
    subprocess.run(['kill','-TERM',str(pid)],check=False)
    child.wait(timeout=5)
    assert lab.resolve().parent==Path('/tmp') and lab.name.startswith('mu-home-tcp-test-')
    shutil.rmtree(lab)
