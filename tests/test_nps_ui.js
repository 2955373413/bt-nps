// 无浏览器依赖的接口、保存流程与防火墙参数测试。
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'nps', 'index.html'), 'utf8');
const script = source.match(/<script type="text\/javascript">([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
let plugin;
let requests = [];
let messages = [];
let confirms = [];
let loads = [];
let response = {status: true, msg: '成功'};
const chain = {find() { return this; }, prop() { return this; }};
function jquery() {
  let value = '';
  return {
    text(input) { value = String(input); return this; },
    html() { return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  };
}
vm.runInNewContext(script.replace('  nps.init();', '  __capture(nps);'), {
  $: jquery,
  __capture(value) { plugin = value; },
  bt_tools: {
    send(payload, callback, options) { requests.push(payload); loads.push(options); callback(response); },
    msg(message) { messages.push(message); }
  },
  bt: {simple_confirm(options, callback) { confirms.push(options); callback(); }}
});

assert(plugin);
assert.strictEqual(plugin.escape('<img onerror=x>'), '&lt;img onerror=x&gt;');
plugin.message({status: false, msg: '<script>alert(1)</script>'});
assert(messages.pop().msg.startsWith('&lt;script&gt;'));
plugin.request('get_info', {role: 'npc'}, result => assert.strictEqual(result.status, true));
assert.strictEqual(requests.pop().url, '/plugin?action=a&name=nps&s=get_info');
response = 'bad';
plugin.request('get_info', {}, result => assert.strictEqual(result.status, false));
response = {status: true};

requests = [];
const makeForm = (type = 'tcp', web = '0.0.0.0') => ({find(selector) { return {val() { return selector.includes('bridge_type') ? type : web; }}; }});
const form = makeForm();
plugin.firewall('bridge_port', '8024', makeForm('kcp'));
let rule = JSON.parse(requests.pop().data.data);
assert.strictEqual(rule.protocol, 'udp');
assert.strictEqual(rule.ports, '8024');
plugin.firewall('bridge_port', '8024', form);
assert.strictEqual(JSON.parse(requests.pop().data.data).protocol, 'tcp');
plugin.firewall('http_proxy_port', '18080', form);
assert.strictEqual(JSON.parse(requests.pop().data.data).protocol, 'tcp');
plugin.firewall('p2p_port', '6000', form);
rule = JSON.parse(requests.pop().data.data);
assert.strictEqual(rule.protocol, 'udp');
assert.strictEqual(rule.ports, '6000-6002');
plugin.firewall('p2p_port', '65534', form);
plugin.firewall('bridge_port', '0', form);
plugin.firewall('bridge_port', '80;touch', form);
plugin.firewall('bridge_port', '8024', makeForm('invalid'));
plugin.firewall('unknown_port', '8024', form);
plugin.firewall('p2p_port', '', form);
plugin.firewall('web_port', '18081', makeForm('tcp', '127.0.0.1'));
assert.strictEqual(requests.length, 0);

plugin.body = chain;
plugin.view = 7;
let shown = [];
plugin.show = page => shown.push(page);
const payload = {role: 'npc', revision: 'revision-7', config: '{}'};
plugin.save('set_config', payload, false, 7, 'config');
assert.strictEqual(requests.length, 1);
assert.strictEqual(requests[0].data.revision, 'revision-7');
assert.deepStrictEqual(shown, ['config']);
requests = [];
shown = [];
plugin.save('set_config', payload, true, 7, 'config');
assert.strictEqual(requests.length, 2);
assert.strictEqual(requests[1].url, '/plugin?action=a&name=nps&s=service_admin');
assert.strictEqual(requests[1].data.role, 'npc');
assert.strictEqual(requests[1].data.status, 'restart');
requests = [];
plugin.save('set_config', payload, true, 6, 'config');
assert.strictEqual(requests.length, 0);
response = {status: false, msg: '模拟保存失败'};
plugin.save('set_config', payload, true, 7, 'config');
assert.strictEqual(requests.length, 1);
assert(requests[0].url.endsWith('s=set_config'));
assert(confirms.length > 0);

requests = [];
loads = [];
response = {status: true};
plugin.role = 'npc';
plugin.install('npc', false, 'local');
assert.strictEqual(requests.length, 1);
assert(requests[0].url.endsWith('s=install'));
assert.strictEqual(requests[0].data.role, 'npc');
assert.strictEqual(requests[0].data.source, 'local');
assert(loads[0].load.includes('校验本地包'));
assert(confirms[confirms.length - 1].msg.includes('不联网'));
assert(!source.includes('安装需访问 GitHub'));
assert(source.includes('package_available'));
assert(source.includes('online_available'));
assert(source.includes('ehang-io/nps v0.26.10'));
assert(!source.includes('compatibility_error'));

requests = [];
loads = [];
plugin.install('nps', true, 'online');
assert.strictEqual(requests.length, 1);
assert(requests[0].url.endsWith('s=reinstall'));
assert.strictEqual(requests[0].data.source, 'online');
assert(loads[0].load.includes('在线包'));
assert(confirms[confirms.length - 1].msg.includes('GitHub'));
assert(source.includes('备份并重装（在线）'));

assert.deepStrictEqual(Array.from(plugin.clientFields.find(field => field[0] === 'conn_type')[4]), ['tcp', 'kcp']);
assert.deepStrictEqual(Array.from(plugin.serverFields.find(field => field[0] === 'bridge_type')[4]), ['tcp', 'kcp']);
assert.deepStrictEqual(Array.from(plugin.serverFields.find(field => field[0] === 'log_level')[4]), ['0', '1', '2', '3', '4', '5', '6', '7']);
assert(plugin.serverFields.find(field => field[0] === 'p2p_port')[3].includes('18000'));
assert(plugin.serverFields.find(field => field[0] === 'http_proxy_port')[3].includes('18080'));
assert(plugin.serverFields.find(field => field[0] === 'https_proxy_port')[3].includes('18443'));

console.log('前端脚本语法、HTML 转义、本地/在线安装、保存重启和 TCP/UDP 防火墙参数测试通过');
