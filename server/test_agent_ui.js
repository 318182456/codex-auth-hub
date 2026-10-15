const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    innerHTML: '', textContent: '', value: '', disabled: false, handlers: {},
    classList: {add() {}, remove() {}},
    addEventListener(event, handler) { this.handlers[event] = handler; },
    appendChild() {}, remove() {}
  });
  return elements.get(id);
}
let agents = [{agent_id: 'node', hostname: '<host>', client_ip: '192.0.2.4', is_online: true,
  current_account: {email: 'user@example.com', account_id: 'acct', has_auth: true},
  configured_watches: [{profile_id: 7}], binding_mode: 'auto',
  watches: [{profile_id: 7, status: 'synced'}], last_heartbeat: new Date().toISOString(), version: '1.2.0'}];
let saved;
let fail = false;
const context = vm.createContext({
  document: {getElementById: element, createElement: () => element('toast')},
  console: {error() {}}, setTimeout() {},
  fetch: async (url, options) => {
    if (fail) throw new Error('offline');
    if (options?.method === 'PUT') saved = [url, JSON.parse(options.body)];
    return {ok: true, json: async () => agents};
  }
});
const source = fs.readFileSync(__dirname + '/static/app.js', 'utf8');
const escapeFunction = source.slice(source.indexOf('function escapeHtml('), source.indexOf('// 标签页切换'));
vm.runInContext(source.slice(0, source.indexOf('function updateStats()')) + escapeFunction, context);

(async () => {
  vm.runInContext('currentProfiles = [{id:7,name:"Test Account",account_email:"user@example.com"}]', context);
  await context.fetchAgents();
  assert.match(element('agents-tbody').innerHTML, /已连接/);
  assert.match(element('agents-tbody').innerHTML, /&lt;host&gt;/);
  assert.match(element('agents-tbody').innerHTML, /user@example.com/);
  context.openAgentBinding(0);
  assert.match(element('agent-binding-profile').innerHTML, /Test Account/);
  element('agent-binding-profile').value = '7';
  await element('form-agent-binding').handlers.submit({preventDefault() {}});
  assert.deepEqual(saved, ['/api/agents/node/binding', {profile_id: 7}]);
  agents[0].is_online = false;
  await context.fetchAgents();
  assert.match(element('agents-tbody').innerHTML, /离线/);
  assert.equal(element('agents-summary').textContent, '（在线 0 / 共 1）');
  fail = true;
  await context.fetchAgents();
  assert.equal(element('agents-summary').textContent, '（状态未知）');
  console.log('Agent UI: connection, account binding, escaping and error state OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
