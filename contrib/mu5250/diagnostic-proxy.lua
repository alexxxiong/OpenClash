-- Installed only inside the MU5250 private namespace. No service-wide environment changes.
local util = require "luci.util"
local config = require "luci.openclash"
local M = {}

function M.command(command)
	local port = tonumber(config.uci_get_config("config", "mixed_port")) or 7893
	assert(port == math.floor(port) and port > 0 and port < 65536, "Invalid mixed proxy port")
	local proxy = util.shellquote("http://127.0.0.1:" .. port)
	local bypass = util.shellquote("localhost,127.0.0.1,::1," .. (config.lanip(true) or "192.168.3.1"))
	return "env http_proxy=" .. proxy .. " https_proxy=" .. proxy .. " all_proxy=" .. proxy
		.. " HTTP_PROXY=" .. proxy .. " HTTPS_PROXY=" .. proxy .. " ALL_PROXY=" .. proxy
		.. " no_proxy=" .. bypass .. " NO_PROXY=" .. bypass .. " /bin/sh -c " .. util.shellquote(command)
end

function M.wrap_sys(sys)
	return setmetatable({
		exec = function(command) return sys.exec(M.command(command)) end,
		call = function(command) return sys.call(M.command(command)) end
	}, { __index = sys })
end

return M
