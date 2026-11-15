require 'json'

module Mu5250HomeDNS
  SETTINGS = '/data/tailscale-home/home.json'.freeze
  HOST = 'learn.dev.inspirai.store'.freeze
  ADDRESS = '192.168.50.20'.freeze

  def self.apply(config, path = SETTINGS)
    return config unless File.file?(path)
    settings = JSON.parse(File.read(path))
    return config unless settings['enabled'] == true
    raise 'Unexpected home service mapping' unless settings['hostname'] == HOST && settings['address'] == ADDRESS
    dns = config.fetch('dns')
    raise 'Home access requires a DNS mapping' unless dns.is_a?(Hash)
    raise 'Home access requires blacklist Fake-IP mode' unless [nil, 'blacklist'].include?(dns['fake-ip-filter-mode'])
    hosts = config['hosts'] ||= {}
    raise 'Existing Hosts is not a mapping' unless hosts.is_a?(Hash)
    if hosts.key?(HOST) && ![ADDRESS, [ADDRESS]].include?(hosts[HOST])
      raise 'Existing home hostname override differs'
    end
    filter = dns['fake-ip-filter'] ||= []
    raise 'Existing Fake-IP filter is not a list' unless filter.is_a?(Array)
    hosts[HOST] = ADDRESS
    dns['use-hosts'] = true
    filter << HOST unless filter.include?(HOST)
    config
  end
end
