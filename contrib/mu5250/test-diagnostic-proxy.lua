-- Run using the device's existing Lua interpreter; all controller I/O is mocked.
local module_path, controller_path = arg[1], arg[2]
local count = 0
local function test(name, run)
	run(); count = count + 1; print("PASS " .. name)
end
local port = "7893"
package.loaded["luci.openclash"] = {
	uci_get_config = function() return port end,
	lanip = function() return "192.168.3.1" end
}
local proxy = dofile(module_path)
local original = require "luci.sys"
test("proxy subprocess uses current mixed port and local bypass", function()
	local out = original.exec(proxy.command([[printf '%s|%s' "$https_proxy" "$no_proxy"]]))
	assert(out == "http://127.0.0.1:7893|localhost,127.0.0.1,::1,192.168.3.1", out)
end)
test("proxy wrapper leaves original sys table unchanged", function()
	local exec = original.exec
	local wrapped = proxy.wrap_sys(original)
	assert(wrapped ~= original and original.exec == exec)
	assert(wrapped.exec([[printf '%s' "$http_proxy"]]) == "http://127.0.0.1:7893")
end)
test("configured port is respected", function()
	port = "17893"; assert(proxy.command("true"):find("127.0.0.1:17893", 1, true)); port = "7893"
end)
test("invalid proxy port fails without starting a request", function()
	port = "0"; assert(not pcall(proxy.command, "true")); port = "7893"
end)

local file = assert(io.open(controller_path)); local source = file:read("*a"); file:close()
local begin = assert(source:find("function action_myip_check()", 1, true))
local finish = assert(source:find("\nfunction latency_test(", begin, true))
source = source:sub(begin, finish - 1)
local json = require "luci.jsonc"
local util = require "luci.util"
local function fixture(options)
	local output, status, command = {}, nil, nil
	local chunks = options.chunks or {"当前 IP：198.51.100.8  ", "来自于：美国 测试\n"}
	local cursor, terminated = 1, false
	local input = { close = function() end }
	function input:read() local v = chunks[cursor]; cursor = cursor + 1; return v end
	local env = setmetatable({
		HTTP = {
			formvalue = function(k) return (options.params or {service = "ipip", proxy = "1"})[k] end,
			prepare_content = function() end, header = function() end,
			status = function(v) status = v end,
			write_json = function(v) output[#output + 1] = v end,
			urlencode = function(v) return v end
		},
		uci = { get = function() return "7893" end },
		fs = { user_agent = "Diagnostic fixture" }, UTIL = util, json = json,
		SYS = { exec = function() error("Unexpected external command") end },
		nixio = {
			pipe = function() return input, {close = function() end} end,
			fork = function() return options.capture and 0 or 123 end,
			dup = function() end,
			exec = function(_, _, cmd) command = cmd; error("captured") end,
			waitpid = function() if not options.timeout or terminated then return 123 end end,
			kill = function(_, signal) if signal ~= 0 then terminated = true end; return not terminated end,
			nanosleep = function() end, const = {SIGTERM = 15, SIGKILL = 9}
		},
		try_read = function(fd) return fd:read() end,
		write_padded = function(v) output[#output + 1] = assert(json.parse(v)) end
	}, {__index = _G})
	local loaded = assert(loadstring(source)); setfenv(loaded, env); loaded()
	local ok, err = pcall(env.action_myip_check)
	if not options.capture then assert(ok, err) end
	return output, status, command
end
test("IPIP endpoint only queries a fixed HTTPS URL via mixed proxy", function()
	local _, _, cmd = fixture({capture = true, params = {service = "ipip", proxy = "1", url = "http://invalid.invalid/"}})
	assert(cmd:find("https://myip.ipip.net/", 1, true))
	assert(cmd:find("--proxy http://127.0.0.1:7893", 1, true))
	assert(not cmd:find("invalid.invalid", 1, true))
end)
test("router-mode IPIP keeps its original egress semantics", function()
	local _, _, cmd = fixture({capture = true, params = {service = "ipip"}})
	assert(not cmd:find("--proxy", 1, true))
end)
test("unknown services return 400 without creating a query", function()
	local _, status = fixture({params = {service = "invalid"}}); assert(status == 400)
end)
test("IPIP parser drains the final pipe fragment", function()
	local out = fixture({}); assert(out[1].ip == "198.51.100.8" and out[1].geo == "美国 测试")
end)
test("empty response emits a terminal failure", function()
	local out = fixture({chunks = {}}); assert(out[1].error == "query_failed" and out[2].complete)
end)
test("unparseable response emits a terminal failure", function()
	local out = fixture({chunks = {"upstream error"}}); assert(out[1].error == "query_failed")
end)
test("unfinished query times out and releases its mock process", function()
	local out = fixture({timeout = true, chunks = {}}); assert(out[1].error == "timeout")
end)
print(count .. " diagnostic proxy/controller checks passed")
