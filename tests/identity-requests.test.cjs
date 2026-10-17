const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const identityCode = html.slice(html.indexOf('const identityRequests='), html.indexOf('\nfunction hardwareDisplayName'));
const controlsCode = html.match(/^function updateReady\(\).*$/m)[0];

function page() {
  const requests = [], elements = new Map(), statuses = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: 'image.bin'});
    return elements.get(id);
  };
  const context = vm.createContext({
    Promise, Map, encodeURIComponent, language: 'en', agent: true,
    selectedDevice: null, selectedBoard: {displayName: 'Tracker'}, selectedVersion: {},
    $: element, tr: key => key,
    local: url => new Promise((resolve, reject) => requests.push({url, resolve, reject})),
    renderDevices() {}, hardwareDisplayName: () => 'Tracker', upsertDevice() {}, addEventLog() {},
    setStatus(id, message) { statuses.push({id, message}); },
  });
  context.setSelectedDevice = device => { context.selectedDevice = device; };
  vm.runInContext(identityCode + '\n' + controlsCode, context);
  return {context, requests, element, statuses};
}

const identity = name => ({name, hardware: 'HELTEC_WIRELESS_TRACKER', firmware: '2.7.26', verified: true});

test('discovery and repeated Identify clicks share one pending request and show progress', async () => {
  const {context, requests, element, statuses} = page();
  const device = {ip: '192.168.1.20'};
  const discovery = context.readDeviceIdentity(device.ip);
  const manual = context.identifyDevice(device);
  assert.equal(context.identifyDevice(device), manual);
  assert.equal(requests.length, 1);
  assert.equal(element('identify').disabled, true);
  assert.equal(element('identify').textContent, 'Reading identity…');
  assert.equal(element('flash').disabled, true);
  requests[0].resolve(identity('Node A'));
  await Promise.all([discovery, manual]);
  assert.equal(device.name, 'Node A');
  assert.equal(element('identify').disabled, false);
  assert.equal(element('flash').disabled, false);
  assert.match(statuses.at(-1).message, /Node A/);
});

test('late response from a previous selection does not replace the current firmware target', async () => {
  const {context, requests} = page();
  const a = context.identifyDevice({ip: '192.168.1.20'});
  const b = context.identifyDevice({ip: '192.168.1.21'});
  requests[1].resolve(identity('Node B'));
  await b;
  requests[0].resolve(identity('Node A'));
  await a;
  assert.equal(context.selectedDevice.ip, '192.168.1.21');
  assert.equal(context.selectedDevice.name, 'Node B');
});

test('failed identification unlocks controls and allows an explicit retry', async () => {
  const {context, requests, element} = page();
  const device = {ip: '192.168.1.20'};
  const failed = context.identifyDevice(device);
  requests[0].reject(new Error('Handshake timed out'));
  await failed;
  assert.equal(device.scanState, 'failed');
  assert.equal(element('identify').disabled, false);
  const retry = context.identifyDevice(device);
  assert.equal(requests.length, 2);
  requests[1].resolve(identity('Node A'));
  await retry;
  assert.equal(device.scanState, 'done');
});
