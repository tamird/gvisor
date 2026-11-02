import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile, appendFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, execFileSync } from 'node:child_process';

const origin = 'https://tamird.github.io/gvisor/';
const reasons = ['review-changes', 'post-review', 'post-reply'];
const result = { status: 'INCOMPLETE', startedAt: new Date().toISOString(), node: process.version, routes: [], cases: [] };
const profile = await mkdtemp(join(tmpdir(), 'work-map-ui-'));
const pending = new Map();
let chrome, socket, nextID = 0;
const exceptions = [];
const responses = new Map();
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(operation, timeout = 60000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const value = await operation();
    if (value) return value;
    await delay(200);
  }
  throw new Error('Timed out waiting for the deployed UI');
}
async function send(method, params = {}) {
  const id = ++nextID;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 20000);
    pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value); }, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const value = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  assert.equal(value.exceptionDetails, undefined, 'Browser evaluation failed');
  return value.result.value;
}
try {
  assert.equal(typeof WebSocket, 'function', 'The hosted Node must provide WebSocket');
  const executable = ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'].find(path => {
    try { execFileSync(path, ['--version'], { stdio: 'ignore' }); return true; } catch { return false; }
  });
  assert.ok(executable, 'Hosted Chromium not found');
  result.browser = execFileSync(executable, ['--version'], { encoding: 'utf8' }).trim();
  chrome = spawn(executable, ['--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
    '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
  const port = await until(async () => {
    try { return (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; } catch { return null; }
  }, 20000);
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = targets.find(t => t.type === 'page');
  assert.ok(page);
  socket = new WebSocket(page.webSocketDebuggerUrl);
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const waiter = pending.get(message.id); pending.delete(message.id);
      if (message.error) waiter?.reject(new Error(message.error.message)); else waiter?.resolve(message.result);
    } else if (message.method === 'Network.responseReceived') {
      const { response, requestId } = message.params;
      if (response.url.startsWith(origin) && ['/gvisor/app.js', '/gvisor/registry.json'].includes(new URL(response.url).pathname))
        responses.set(new URL(response.url).pathname.split('/').pop(), requestId);
    } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text);
  });
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
  const url = origin + '?view=table#pr%3A16834';
  const navigation = await send('Page.navigate', { url });
  assert.equal(navigation.errorText, undefined);
  await until(() => evaluate("document.getElementById('visible-count')?.innerText === 'Registry unavailable' || !!document.querySelector('#work-table button[data-node]')"));
  const visible = await evaluate(`({url: location.href, registryStatus: document.getElementById('visible-count').innerText,
    error: document.getElementById('freshness-text').innerText, rows: document.querySelectorAll('#work-table button[data-node]').length,
    detailsHidden: document.getElementById('details').hidden, textVisible: document.body.innerText.includes('Invalid contributor decision')})`);
  result.cases.push({id: 'live-pr-16834', ...visible});
  result.files = {};
  for (const [name, requestId] of responses) {
    const {body, base64Encoded} = await send('Network.getResponseBody', {requestId});
    const bytes = Buffer.from(body, base64Encoded ? 'base64' : 'utf8');
    result.files[name] = {sha256:createHash('sha256').update(bytes).digest('hex'), bytes:bytes.length,
      equalsCheckout:bytes.equals(await readFile(name))};
  }
  assert.equal(visible.url, url);
  assert.equal(visible.registryStatus, 'Registry unavailable');
  assert.match(visible.error, /Invalid contributor decision/);
  assert.equal(visible.textVisible, true);
  assert.equal(visible.detailsHidden, true);
  assert.equal(visible.rows, 0);
  assert.ok(result.files['app.js'] && result.files['registry.json']);
  result.diagnosis = 'DEPLOYED_INVALID_CONTRIBUTOR_DECISION';
  assert.deepEqual(exceptions, []);
  result.status = 'PASS';
} catch (error) {
  result.status = 'FAIL'; result.error = { name: error.name, message: error.message }; process.exitCode = 1;
} finally {
  result.finishedAt = new Date().toISOString(); result.runtimeExceptions = exceptions;
  await writeFile('work-map-ui-result.json', JSON.stringify(result, null, 2) + '\n');
  const summary = `Deployed work-map browser smoke: ${result.status}\n\n` +
    result.routes.map(r => `${r.reason}: ${r.count} visible cards (${r.url})`).join('\n') + '\n' +
    result.cases.map(c => `${c.id}: ${c.status}`).join('\n') + (result.error ? `\n${result.error.message}` : '') + '\n';
  console.log(summary);
  if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY, summary);
  socket?.close();
  if (chrome) chrome.kill('SIGTERM');
  await rm(profile, { recursive: true, force: true, maxRetries: 6, retryDelay: 150 }).catch(() => {});

}
