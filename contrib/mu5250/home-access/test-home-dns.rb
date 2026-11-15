require 'json'
require_relative 'home-dns'

def assert(value, message)
  raise message unless value
end

seed = {'rules'=>['MATCH,Proxy'], 'proxy-groups'=>[{'name'=>'Proxy','type'=>'select','proxies'=>['manual']}],
        'dns'=>{'enable'=>true,'fake-ip-filter'=>['rule-set:oc-cn-domain'],'nameserver'=>['https://1.1.1.1/dns-query#Proxy']},
        'hosts'=>{'existing.example'=>'192.168.3.2'}}
directory=ARGV.fetch(0)
path=directory+'/fixture-settings.json'
raise 'Fixture path already exists' if File.exist?(path)
begin
  copy=Marshal.load(Marshal.dump(seed))
  assert(Mu5250HomeDNS.apply(copy,path)==seed,'Absent settings changed existing configuration')
  File.write(path,JSON.generate({'enabled'=>false}))
  assert(Mu5250HomeDNS.apply(copy,path)==seed,'Disabled home access changed existing configuration')
  File.write(path,JSON.generate({'enabled'=>true,'hostname'=>Mu5250HomeDNS::HOST,'address'=>Mu5250HomeDNS::ADDRESS}))
  Mu5250HomeDNS.apply(copy,path)
  assert(copy['hosts'][Mu5250HomeDNS::HOST]=='192.168.50.20','Home domain did not resolve to the private target')
  assert(copy['dns']['fake-ip-filter'].include?(Mu5250HomeDNS::HOST),'Home domain could still receive Fake-IP')
  assert(copy['rules']==seed['rules'] && copy['proxy-groups']==seed['proxy-groups'],'Routing policy changed')
  assert(copy['hosts']['existing.example']=='192.168.3.2' && copy['dns']['nameserver']==seed['dns']['nameserver'],'Unrelated Hosts or DNS changed')
  first=Marshal.load(Marshal.dump(copy));Mu5250HomeDNS.apply(copy,path)
  assert(copy==first,'Repeated application changed configuration')
  %w[whitelist conflict].each do |kind|
    bad=Marshal.load(Marshal.dump(seed))
    if kind=='whitelist'
      bad['dns']['fake-ip-filter-mode']='whitelist'
    else
      bad['hosts'][Mu5250HomeDNS::HOST]='192.168.50.99'
    end
    rejected=false
    begin;Mu5250HomeDNS.apply(bad,path);rescue RuntimeError;rejected=true;end
    assert(rejected,'Conflicting settings were silently overwritten')
  end
ensure
  File.unlink(path) if File.file?(path)
end
puts '7 home DNS and policy preservation checks passed'
