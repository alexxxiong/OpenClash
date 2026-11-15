"""Destination-scoped BBR in local Traefik namespaces; no global TCP/routing changes."""
import argparse,contextlib,fcntl,ipaddress,json,os,re,signal,subprocess,threading
from pathlib import Path

PREFIX='192.168.3.0/24';PROTOCOL='242'
K3S='/usr/local/bin/k3s'

def command(args,**kwargs):
    return subprocess.check_output(args,timeout=12,stderr=subprocess.DEVNULL,**kwargs).decode().strip()

@contextlib.contextmanager
def namespace(pid,expected=None):
    fd=os.open('/proc/'+str(pid)+'/ns/net',os.O_RDONLY)
    try:
        inode=os.fstat(fd).st_ino
        if inode==os.stat('/proc/1/ns/net').st_ino:raise RuntimeError('Refusing host network namespace')
        if expected is not None and inode!=expected:raise RuntimeError('Namespace identity changed')
        def ip(*args):
            return command(['nsenter','--net=/proc/self/fd/'+str(fd),'ip','-4',*args],pass_fds=(fd,))
        yield inode,ip
    finally:os.close(fd)

def matches(route,record):
    return (route.get('dst')==PREFIX and route.get('gateway')==record['gateway']
            and route.get('dev')==record['dev'] and str(route.get('protocol'))==PROTOCOL
            and any(x.get('congestion')=='bbr' for x in route.get('metrics',[])))

class Optimizer:
    def __init__(self,base):
        self.base=Path(base);self.base.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.state=self.base/'routes.json'
    def read(self):return json.loads(self.state.read_text()) if self.state.exists() else []
    def save(self,records):
        staged=self.state.with_suffix('.new');staged.write_text(json.dumps(records,indent=2));staged.chmod(0o600);staged.replace(self.state)
    @contextlib.contextmanager
    def lock(self):
        with (self.base/'lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX);yield
    def apply(self,targets):
        with self.lock():
            records=self.read()
            records=[r for r in records if Path('/proc/'+str(r['pid'])+'/ns/net').exists() and os.stat('/proc/'+str(r['pid'])+'/ns/net').st_ino==r['inode']]
            for pid,cid in targets:
                with namespace(pid) as (inode,ip):
                    defaults=json.loads(ip('-j','route','show','default'))
                    if len(defaults)!=1:raise RuntimeError('Expected one existing default route')
                    gateway=defaults[0]['gateway'];dev=defaults[0]['dev']
                    if not ipaddress.ip_address(gateway).is_private or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}',dev):raise RuntimeError('Unexpected CNI next hop')
                    record={'pid':pid,'cid':cid,'inode':inode,'gateway':gateway,'dev':dev}
                    current=[r for r in json.loads(ip('-j','route','show','exact',PREFIX)) if r['dst']==PREFIX]
                    if current:
                        if record not in records or len(current)!=1 or not matches(current[0],record):raise RuntimeError('Existing route is not owned; preserving it')
                        continue
                    ip('route','add',PREFIX,'via',gateway,'dev',dev,'proto',PROTOCOL,'congctl','bbr')
                    if record not in records:records.append(record)
                    try:self.save(records)
                    except Exception:
                        ip('route','del',PREFIX,'via',gateway,'dev',dev,'proto',PROTOCOL);raise
                    print('Applied destination-scoped BBR in namespace '+str(inode),flush=True)
            self.save(records)
    def cleanup(self):
        with self.lock():
            for record in self.read():
                try:
                    with namespace(record['pid'],record['inode']) as (_,ip):
                        current=json.loads(ip('-j','route','show','exact',PREFIX))
                        if len(current)==1 and matches(current[0],record):
                            ip('route','del',PREFIX,'via',record['gateway'],'dev',record['dev'],'proto',PROTOCOL)
                except (FileNotFoundError,ProcessLookupError,RuntimeError):pass
            self.save([])

def local_traefik_targets():
    data=json.loads(command([K3S,'kubectl','--kubeconfig=/etc/rancher/k3s/k3s.yaml','--request-timeout=5s','-n','kube-system','get','pods','-l','app.kubernetes.io/name=traefik','-o','json']))
    rows=[]
    for pod in data['items']:
        if pod['status'].get('phase')!='Running':continue
        for c in pod['status'].get('containerStatuses',[]):
            if c.get('name')!='traefik' or not c.get('state',{}).get('running'):continue
            cid=c.get('containerID','').removeprefix('containerd://')
            if not re.fullmatch('[0-9a-f]{64}',cid):raise RuntimeError('Unexpected container identity')
            try:info=json.loads(command([K3S,'crictl','inspect',cid]))
            except subprocess.CalledProcessError:continue # another node's container
            rows.append((int(info['info']['pid']),cid))
    return rows

def main():
    p=argparse.ArgumentParser();p.add_argument('--state-dir',default='/var/lib/mu5250-home-tcp');p.add_argument('--once',action='store_true');p.add_argument('--cleanup',action='store_true');p.add_argument('--test-pid',type=int);a=p.parse_args()
    if a.test_pid:
        if not a.once or not str(Path(a.state_dir)).startswith('/tmp/mu-home-tcp-test-') or os.environ.get('MU_TCP_TEST_HOST_NS')!=os.readlink('/proc/1/ns/net'):raise RuntimeError('Isolated test context required')
    o=Optimizer(a.state_dir)
    if a.cleanup:o.cleanup();return
    if a.once:o.apply([(a.test_pid,'isolated-test')] if a.test_pid else local_traefik_targets());return
    stopped=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopped.set());signal.signal(signal.SIGINT,lambda *_:stopped.set())
    while not stopped.is_set():
        try:o.apply(local_traefik_targets())
        except Exception as e:print(type(e).__name__+': '+str(e),flush=True)
        stopped.wait(15)

if __name__=='__main__':main()
