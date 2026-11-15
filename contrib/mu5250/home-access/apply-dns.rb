require 'yaml'
require 'json'
require 'date'
require '/data/tailscale-home/home-dns'

base='/data/tailscale-home'
path='/etc/openclash/oixCloud.yaml'
mode=ARGV.fetch(0)
raise 'Unexpected operation' unless %w[enable disable].include?(mode)
config=YAML.load_file(path,aliases:true,permitted_classes:[Date,Time])
original=File.binread(path)
preimage=base+'/dns-preimage.json'
host=Mu5250HomeDNS::HOST
if mode=='enable'
  unless File.exist?(preimage)
    dns=config.fetch('dns')
    before={'host_present'=>config.fetch('hosts',{}).key?(host),'host_value'=>config.fetch('hosts',{})[host],
            'filter_present'=>dns.fetch('fake-ip-filter',[]).include?(host),
            'use_hosts_present'=>dns.key?('use-hosts'),'use_hosts_value'=>dns['use-hosts']}
    File.write(preimage,JSON.generate(before));File.chmod(0600,preimage)
  end
  settings={'enabled'=>true,'hostname'=>host,'address'=>Mu5250HomeDNS::ADDRESS}
else
  settings={'enabled'=>false,'hostname'=>host,'address'=>Mu5250HomeDNS::ADDRESS}
end
settings_path=base+'/home.json'
settings_before=File.exist?(settings_path) ? File.binread(settings_path) : nil
File.write(settings_path+'.new',JSON.generate(settings));File.chmod(0600,settings_path+'.new');File.rename(settings_path+'.new',settings_path)
if mode=='enable'
  Mu5250HomeDNS.apply(config)
else
  before=JSON.parse(File.read(preimage))
  if before['host_present'];config['hosts'][host]=before['host_value']
  else;config.fetch('hosts',{}).delete(host);end
  config.fetch('dns').fetch('fake-ip-filter',[]).delete(host) unless before['filter_present']
  if before['use_hosts_present'];config['dns']['use-hosts']=before['use_hosts_value']
  else;config['dns'].delete('use-hosts');end
end

def reload_runtime(path)
  secret=IO.popen(['uci','-q','get','openclash.config.dashboard_password'],&:read).strip
  args=['curl','--noproxy','*','-sS','--max-time','30','-o','/dev/null','-w','%{http_code}',
        '-X','PUT','-H','@-','-H','Content-Type: application/json','--data-binary',JSON.generate({'path'=>path}),
        'http://127.0.0.1:9091/configs']
  response=IO.popen(args,'r+',err:File::NULL){|io|io.write('Authorization: Bearer '+secret+"\n");io.close_write;io.read}
  raise 'Runtime hot reload failed' unless $?.success? && %w[200 204].include?(response.strip)
end

begin
  File.write(path+'.home-new',YAML.dump(config));File.chmod(0600,path+'.home-new')
  File.rename(path+'.home-new',path)
  reload_runtime(path)
rescue
  File.binwrite(path+'.home-new',original);File.chmod(0600,path+'.home-new');File.rename(path+'.home-new',path)
  if settings_before;File.binwrite(settings_path,settings_before);else;File.unlink(settings_path);end
  reload_runtime(path) rescue nil
  raise
end
puts JSON.generate({'home_dns_enabled'=>settings['enabled'],'hostname'=>host,'address'=>settings['enabled'] ? Mu5250HomeDNS::ADDRESS : nil})
