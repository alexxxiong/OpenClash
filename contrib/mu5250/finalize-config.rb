require 'yaml'
require 'date'

path = ARGV.fetch(0)
config = YAML.load_file(path, aliases: true, permitted_classes: [Date, Time])
raise 'Configuration must be a mapping' unless config.is_a?(Hash)
def setting(key, fallback = nil)
  value = IO.popen(['uci', '-q', 'get', "openclash.config.#{key}"], &:read).strip
  value.empty? ? fallback : value
end
def port(key, fallback)
  value = Integer(setting(key, fallback.to_s), 10)
  raise 'Port outside valid range' unless (1..65_535).cover?(value)
  value
end
config['external-controller'] = "0.0.0.0:#{port('cn_port', 9091)}"
config['secret'] = setting('dashboard_password')
raise 'The controller key is missing' unless config['secret'] && !config['secret'].empty?
config['allow-lan'] = true
config['bind-address'] = '*'
config['mixed-port'] = port('mixed_port', 7893)
config['port'] = port('http_port', 7890)
config['socks-port'] = port('socks_port', 7891)
config['redir-port'] = port('proxy_port', 7892)
config['tproxy-port'] = port('tproxy_port', 7895)
config['ipv6'] = true
config['profile'] ||= {}
config['profile']['store-selected'] = true
# The stock firmware owns wall time. The clock-patched oix signer uses NTP.Now.
config['ntp'] = {} unless config['ntp'].is_a?(Hash)
config['ntp']['enable'] = true
config['ntp']['server'] ||= 'time.apple.com'
config['ntp']['port'] ||= 123
config['ntp']['interval'] = 1
config['ntp']['write-to-system'] = false
config['ntp']['sync-system-time'] = false if config['ntp'].key?('sync-system-time')
config['tun'] = { 'enable' => false }
if setting('mu5250_udp_engine', 'tproxy') == 'tun'
  config['tun'] = {'enable'=>true, 'device'=>'mu-oc-tun', 'stack'=>'gvisor',
                   'auto-route'=>false, 'auto-redirect'=>false, 'auto-detect-interface'=>false,
                   'dns-hijack'=>[], 'strict-route'=>false, 'mtu'=>1500,
                   'inet6-address'=>['fdfe:7250::1/126']}
end
config['listeners']&.reject! {|x| x['name'] == 'mu5250-udp4'}
dns = config['dns'] ||= {}
dns['enable'] = true
dns['ipv6'] = true
dns['listen'] = "[::]:#{port('dns_port', 7874)}"
dns['default-nameserver'] ||= ['223.5.5.5', '119.29.29.29']
dns['nameserver'] ||= ['223.5.5.5', '119.29.29.29']
dns['fake-ip-range'] = '198.19.0.1/16' if dns['enhanced-mode'] == 'fake-ip'
File.write(path + '.mu5250-new', YAML.dump(config))
File.chmod(0o600, path + '.mu5250-new')
File.rename(path + '.mu5250-new', path)
