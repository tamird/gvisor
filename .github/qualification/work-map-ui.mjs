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
async function navigate(reason) {
  const url = origin + '?view=table&reason=' + reason;
  const response = await send('Page.navigate', { url });
  assert.equal(response.errorText, undefined);
  await until(() => evaluate(`document.querySelector('input[data-reason="${reason}"]')?.checked &&
    document.querySelector('#work-table button[data-node]') !== null &&
    document.querySelector('#freshness-text').textContent.includes('Work map')`));
  const row = await evaluate(`({url: location.href,
    chips: Array.from(document.querySelectorAll('input[data-reason]')).filter(e => ${JSON.stringify(reasons)}.includes(e.dataset.reason))
      .map(e => ({id: e.dataset.reason, text: e.nextElementSibling.textContent, checked: e.checked})),
    ids: Array.from(document.querySelectorAll('#work-table button[data-node]'), e => e.dataset.node),
    registryAt: registry.meta.updatedAt, snapshotAt: model.checkedAt,
    scripts: Array.from(document.scripts, e => e.src).filter(Boolean)})`);
  assert.deepEqual(row.chips.map(c => c.id), reasons);
  assert.deepEqual(row.chips.filter(c => c.checked).map(c => c.id), [reason]);
  assert.ok(row.scripts.includes(origin + 'app.js?v=20261102-decision-queues'));
  const count = Number(row.chips.find(c => c.id === reason).text.match(/ (\d+)$/)?.[1]);
  assert.ok(count > 0);
  assert.equal(row.ids.length, count);
  assert.equal(new Set(row.ids).size, count);
  result.routes.push({ ...row, reason, count });
  return row;
}
async function openCard(id, kind, draftButton) {
  const observed = await evaluate(`(() => {
    const row = document.querySelector('#work-table button[data-node="${id}"]').closest('tr');
    const button = ${draftButton ? "row.querySelector('button.decision-source')" : "row.querySelector('button[data-node]')"};
    const buttonText = button.textContent;
    button.click();
    const n = registry.nodes.find(n => n.id === ${JSON.stringify(id)});
    const expected = [n.contributorReview, ...(n.additionalReviews || [])].filter(d => d?.status === 'pending' && d.kind === ${JSON.stringify(kind)});
    return {url: location.href, buttonText, visible: !document.getElementById('details').hidden,
      sections: Array.from(document.querySelectorAll('#details .contributor-decision'), e => ({
        title: e.querySelector('h3').textContent, body: e.querySelector('.decision-body')?.textContent,
        links: Array.from(e.querySelectorAll('a'), a => a.href)})),
      expected: expected.map(d => ({title: d.title, body: d.body, url: d.url}))};
  })()`);
  assert.ok(observed.visible);
  assert.ok(observed.expected.length > 0);
  assert.equal(observed.sections.length, observed.expected.length);
  for (const [i, expected] of observed.expected.entries()) {
    assert.equal(observed.sections[i].title, expected.title);
    assert.equal(observed.sections[i].body, expected.body);
    assert.ok(observed.sections[i].links.includes(expected.url));
  }
  if (draftButton) assert.match(observed.buttonText, /^Read (?:\d+ )?draft reviews?$/);
  result.cases.push({ id, kind, url: observed.url, button: observed.buttonText,
    drafts: observed.sections.map(s => ({title: s.title, bodySHA256: createHash('sha256').update(s.body).digest('hex'), links: s.links})), status: 'PASS' });
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
    } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text);
  });
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  await send('Page.enable'); await send('Runtime.enable');
  await navigate('review-changes');
  await openCard('branch:plugin-toolchain-unwind', 'proposal', false);
  await navigate('post-review');
  await openCard('pr:16735', 'review', true);
  await navigate('post-reply');
  // Exercise the checkbox handler, including deselecting the previous category.
  await evaluate(`document.querySelector('input[data-reason="post-reply"]').click();
    document.querySelector('input[data-reason="review-changes"]').click()`);
  const clicked = await evaluate(`({url: location.href, ids: Array.from(document.querySelectorAll('#work-table button[data-node]'), e => e.dataset.node)})`);
  assert.deepEqual(new URL(clicked.url).searchParams.getAll('reason'), ['review-changes']);
  assert.deepEqual(clicked.ids.slice().sort(), result.routes[0].ids.slice().sort());
  result.cases.push({ id: 'filter-checkbox', status: 'PASS', url: clicked.url, count: clicked.ids.length });
  assert.deepEqual(exceptions, []);
  result.status = 'PASS';
} catch (error) {
  result.status = 'FAIL'; result.error = { name: error.name, message: error.message }; process.exitCode = 1;
} finally {
  socket?.close();
  if (chrome) { chrome.kill('SIGTERM'); await Promise.race([new Promise(resolve => chrome.once('exit', resolve)), delay(2000)]); if (chrome.exitCode === null) chrome.kill('SIGKILL'); }
  await rm(profile, { recursive: true, force: true });
  result.finishedAt = new Date().toISOString(); result.runtimeExceptions = exceptions;
  await writeFile('work-map-ui-result.json', JSON.stringify(result, null, 2) + '\n');
  const summary = `Deployed work-map browser smoke: ${result.status}\n\n` +
    result.routes.map(r => `${r.reason}: ${r.count} visible cards (${r.url})`).join('\n') + '\n' +
    result.cases.map(c => `${c.id}: ${c.status}`).join('\n') + (result.error ? `\n${result.error.message}` : '') + '\n';
  console.log(summary);
  if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY, summary);
}
