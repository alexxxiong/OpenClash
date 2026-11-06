// Regression checks for real IP query rendering with bounded request failures.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../../luci-app-openclash/root/www/luci-static/resources/openclash/js/myip.js',import.meta.url),'utf8');
const ipCode=source.slice(source.indexOf('    let IP = {'),source.indexOf('    let HTTP = {'));
const renderCode=source.slice(source.indexOf('    function show_ip_error('),source.indexOf('    function has_ip_cache('));
const routerCode=source.slice(source.indexOf('    function get_router_ip_info('),source.indexOf('    function show_ip_error('));
let count=0;
function fixture(fetchImpl,privateMode=false){
 const nodes={};for(const k of ['ipip','ipsb','pcol','ipify'])for(const suffix of ['','-geo'])nodes['ip-'+k+suffix]={textContent:'Querying...',innerHTML:'spinner'};
 let abortTimer;let xhr;
 const context=vm.createContext({
  $$:{getElementById:id=>nodes[id]},random:1,localStorage:{getItem:()=>privateMode?'true':null},
  AbortController,Uint8Array,TextDecoder,fetch:fetchImpl,
  setTimeout:fn=>{abortTimer=fn;return 1;},clearTimeout:()=>{},addTitleOnOverflow:()=>{},
  ipCheckXHR:null,XMLHttpRequest:class{constructor(){xhr=this;this.status=200;this.responseText='';}open(){}send(){}abort(){}}
 });
 vm.runInContext(ipCode+renderCode+routerCode+';globalThis.IP=IP;',context);
 return {context,nodes,abort:()=>abortTimer(),xhr:()=>xhr};
}
const response=(text,status=200)=>({ok:status===200,status,text:async()=>text});
async function test(name,run){await run();count++;console.log('PASS',name);}
await test('IPIP uses same-origin proxy query and parses padded stream',async()=>{
 let url;const f=fixture(async u=>{url=u;return response('  {"service":"ipip","ip":"198.51.100.8","geo":"US test"}\n {"complete":true} \n');});
 await f.context.IP.getIpipIP();assert.equal(url,'/cgi-bin/luci/admin/services/openclash/myip_check?service=ipip&proxy=1');
 assert.equal(f.nodes['ip-ipip'].textContent,'198.51.100.8');assert.equal(f.nodes['ip-ipip-geo'].textContent,'US test');
});
for(const [name,reply] of [['HTTP 503',response('',503)],['invalid response',response('upstream failure')],['backend error',response('{"service":"ipip","error":"query_failed"}\n')]]){
 await test(name+' ends spinner and clears stale address',async()=>{
  const f=fixture(async()=>reply);f.nodes['ip-ipip'].textContent='198.51.100.7';f.nodes['ip-ipip-geo'].textContent='old location';
  await f.context.IP.getIpipIP();assert.equal(f.nodes['ip-ipip'].textContent,'<%:Test failed%>');assert.equal(f.nodes['ip-ipip-geo'].textContent,'');
 });
}
await test('request timeout displays timeout instead of querying',async()=>{
 const f=fixture((u,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const e=new Error('aborted');e.name='AbortError';reject(e);})));const pending=f.context.IP.getIpipIP();f.abort();await pending;
 assert.equal(f.nodes['ip-ipip'].textContent,'<%:Timeout%>');
});
await test('privacy masks IP on failure',async()=>{
 const f=fixture(async()=>response('',503),true);await f.context.IP.getIpipIP();assert.equal(f.nodes['ip-ipip'].textContent,'***.***.***.***');
});
await test('router stream preserves successes and reports missing services',async()=>{
 const f=fixture();f.context.get_router_ip_info();const x=f.xhr();x.responseText='{"service":"ipify","ip":"198.51.100.9"}\n';x.onprogress();x.onload();
 assert.equal(f.nodes['ip-ipify'].textContent,'198.51.100.9');assert.equal(f.nodes['ip-ipip'].textContent,'<%:Test failed%>');
});
await test('router stream handles final record without newline',async()=>{
 const f=fixture();f.context.get_router_ip_info();const x=f.xhr();x.responseText='{"service":"ipip","ip":"198.51.100.9"}';x.onprogress();x.onload();assert.equal(f.nodes['ip-ipip'].textContent,'198.51.100.9');
});
await test('router timeout ends all unfinished spinners',async()=>{
 const f=fixture();f.context.get_router_ip_info();f.xhr().ontimeout();assert.equal(f.nodes['ip-ipip'].textContent,'<%:Timeout%>');
});
console.log(`${count} diagnostic UI checks passed`);
