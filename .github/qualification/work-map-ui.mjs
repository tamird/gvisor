import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile, appendFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, execFileSync } from 'node:child_process';

const origin = 'https://tamird.github.io/gvisor/';
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
      if (response.url.startsWith(origin) && ['/gvisor/app.js', '/gvisor/registry.json', '/gvisor/github-status.json'].includes(new URL(response.url).pathname))
        responses.set(new URL(response.url).pathname.split('/').pop(), requestId);
    } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text);
  });
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable', {maxResourceBufferSize: 32 * 1024 * 1024, maxTotalBufferSize: 64 * 1024 * 1024});
  await send('Network.setCacheDisabled', {cacheDisabled: true});
  const url = origin + '?view=dag&reason=post-reply#pr%3A16834';
  const navigation = await send('Page.navigate', { url });
  assert.equal(navigation.errorText, undefined);
  await until(() => evaluate("document.getElementById('visible-count')?.innerText === 'Registry unavailable' || !document.getElementById('details')?.hidden"));
  const visible = await evaluate(`({url: location.href, registryStatus: document.getElementById('visible-count').innerText,
    freshness: document.getElementById('freshness-text').innerText,
    detailsHidden: document.getElementById('details').hidden,
    title: document.querySelector('#details .item-title')?.innerText,
    decisions: document.querySelectorAll('#details .contributor-decision').length,
    draftBodies: document.querySelectorAll('#details .decision-body').length,
    controls: Array.from(document.querySelectorAll('#details a, #details button'), e => ({text:e.innerText, href:e.href || null})),
    reasons: Array.from(document.querySelectorAll('input[data-reason]:checked'), e => e.dataset.reason),
    ids: Array.from(document.querySelectorAll('#graph [data-node]'), e => e.dataset.node)})`);
  result.cases.push({id: 'exact-user-route', ...visible});
  assert.equal(visible.url, url);
  assert.notEqual(visible.registryStatus, 'Registry unavailable');
  assert.equal(visible.detailsHidden, false);
  assert.deepEqual(visible.reasons, ['post-reply']);
  assert.equal(visible.decisions, 0);
  assert.equal(visible.draftBodies, 0);
  assert.ok(visible.controls.some(c => c.href === 'https://github.com/google/gvisor/pull/16834'));
  assert.ok(visible.controls.some(c => c.href === 'https://github.com/bazel-contrib/bazel-gazelle/issues/2499'));
  assert.ok(!visible.controls.some(c => /issues\/new/.test(c.href || '') || /Read .*draft|Open prefilled issue|Review (?:changes|proposal)/i.test(c.text)));
  result.files = {};
  const expected = {
    'app.js': 'f427253a5d5e60061d6fbc8af1753019346ea3e71ab041c8019be2b9b370b384',
    'registry.json': '19f0ac1c257be4e42a671796aeaae72646d6a0a1a0a6a240ad03efd68625d096',
    'github-status.json': 'a5b6c4c9f4f17b3eb381490919f765fe07684b501be5da67bae77fcb04f85244',
  };
  for (const [name, requestId] of responses) {
    const {body, base64Encoded} = await send('Network.getResponseBody', {requestId});
    const bytes = Buffer.from(body, base64Encoded ? 'base64' : 'utf8');
    result.files[name] = {sha256:createHash('sha256').update(bytes).digest('hex'), bytes:bytes.length};
  }
  for (const [name, digest] of Object.entries(expected)) assert.equal(result.files[name]?.sha256, digest, name);
  await evaluate("document.querySelector('#details button[aria-label=\"Close details\"]').click()");
  await until(() => evaluate("!location.hash && document.getElementById('details').hidden"));
  const filtered = await evaluate(`({url: location.href,
    count: document.getElementById('visible-count').innerText,
    reasons: Array.from(document.querySelectorAll('input[data-reason]:checked'), e => e.dataset.reason),
    ids: Array.from(document.querySelectorAll('#graph [data-node]'), e => e.dataset.node),
    detailsHidden: document.getElementById('details').hidden})`);
  result.routes.push({reason:'post-reply', ...filtered});
  assert.equal(filtered.url, origin + '?view=dag&reason=post-reply');
  assert.deepEqual(filtered.reasons, ['post-reply']);
  assert.equal(filtered.detailsHidden, true);
  assert.ok(!filtered.ids.includes('pr:16834'));
  assert.equal(new Set(filtered.ids).size, filtered.ids.length);
  result.cases[0].status = 'PASS';
  result.cases.push({id:'settled-item-absent-after-closing-details', status:'PASS'});
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
