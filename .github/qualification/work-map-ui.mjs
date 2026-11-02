import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile, appendFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, execFileSync } from 'node:child_process';

const origin = process.env.UI_ORIGIN || 'http://127.0.0.1:8771/';
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
      if (response.url.startsWith(origin) && ['app.js', 'registry.json'].includes(new URL(response.url).pathname.split('/').pop()))
        responses.set(new URL(response.url).pathname.split('/').pop(), requestId);
    } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text);
  });
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
  const url = origin + '?view=table#pr%3A16834';
  const navigation = await send('Page.navigate', { url });
  assert.equal(navigation.errorText, undefined);
  await until(() => evaluate("document.getElementById('details')?.hidden === false && document.querySelector('#details .contributor-decision h3') && document.querySelector('#work-table button[data-node]')"));
  const live = await evaluate(`(() => {
    const n = registry.nodes.find(n => n.id === 'pr:16834'), d=n.contributorReview;
    const card = document.querySelector('#details .contributor-decision');
    const link = [...card.querySelectorAll('a')].find(a => a.textContent === 'Open prefilled issue ↗');
    const href = link && new URL(link.href);
    return {url: location.href, selected, repo: n.repo, head: d.head, kind: d.kind,
      title: card.querySelector('h3').innerText, expectedTitle: d.title,
      bodyEqual: card.querySelector('.decision-body')?.textContent === d.body,
      titleParameterEqual: href?.searchParams.get('title') === d.title,
      bodyParameterEqual: href?.searchParams.get('body') === d.body,
      destination: href && href.origin + href.pathname, keys: href && [...href.searchParams.keys()],
      sourceURL: document.querySelector('#details .detail-top a').href, rows: document.querySelectorAll('#work-table button[data-node]').length,
      freshness:document.querySelector('#freshness-text').innerText, checkedAt:model.checkedAt,
      registryAt:registry.meta.updatedAt, visible: !document.getElementById('details').hidden};
  })()`);
  result.cases.push({id:'pr:16834', ...live});
  console.log('Observed primary card', JSON.stringify(live));
  assert.equal(live.url, url); assert.equal(live.selected,'pr:16834'); assert.equal(live.kind,'issue'); assert.ok(live.visible && live.rows > 0);
  assert.equal(live.title, live.expectedTitle); assert.ok(live.bodyEqual && live.titleParameterEqual && live.bodyParameterEqual);
  assert.equal(live.sourceURL,'https://github.com/google/gvisor/pull/16834');
  assert.equal(live.destination,'https://github.com/bazel-contrib/bazel-gazelle/issues/new');
  assert.deepEqual(live.keys.sort(), ['body','title']);
  const snapshot=JSON.parse(await readFile('github-status.json'));
  assert.equal(live.registryAt,snapshot.registryDate); assert.equal(live.checkedAt,snapshot.checkedAt);
  result.firstCardVerifiedAt = new Date().toISOString();
  await writeFile('work-map-ui-result.json', JSON.stringify(result));
  const pendingCards=JSON.parse(await readFile('registry.json')).nodes.filter(n=>n.contributorReview?.status==='pending' && n.id!=='pr:16834');
  for (const n of pendingCards) {
    await send('Page.navigate', {url:origin+'?view=table#'+encodeURIComponent(n.id)});
    await until(()=>evaluate("selected === "+JSON.stringify(n.id)+" && !document.getElementById('details').hidden"));
    const observed=await evaluate(`(() => {
      const n=nodeMap().get(selected), ds=visibleDecisions(n), secs=[...document.querySelectorAll('#details .contributor-decision')];
      const expected=ds.filter(d=>states(n)['contributor-review'] || contributorDecisionNeedsRefresh(n,d));
      const match=expected.length===secs.length && expected.every((d,i)=>{
        const changed=contributorDecisionNeedsRefresh(n,d);
        return secs[i].querySelector('h3').textContent===(changed?'Check the new PR head':d.title || 'Decision requested')
          && (changed || !d.body || secs[i].querySelector('.decision-body')?.textContent===d.body);
      });
      return {id:selected, kind:n.contributorReview?.kind, sections:secs.length, match,
        titleMatches:document.querySelector('#details .item-title').textContent===n.title};
    })()`);
    result.cases.push(observed); assert.equal(observed.id,n.id); assert.ok(observed.match && observed.titleMatches, n.id);
  }
  for (const k of ['proposal','amendment']) assert.ok(result.cases.some(c=>c.kind===k && c.sections>0));
  result.files = {};
  for (const [name, requestId] of responses) {
    const {body, base64Encoded} = await send('Network.getResponseBody', {requestId});
    const bytes = Buffer.from(body, base64Encoded ? 'base64' : 'utf8');
    result.files[name] = {sha256:createHash('sha256').update(bytes).digest('hex'), bytes:bytes.length,
      equalsCheckout:bytes.equals(await readFile(name))};
  }
  assert.ok(['app.js','registry.json'].every(name => result.files[name]?.equalsCheckout));
  result.diagnosis = 'VERIFIED_PAIRED_REGISTRY_AND_RENDERED_DECISIONS';
  assert.deepEqual(exceptions, []);
  result.status = 'PASS';
} catch (error) {
  result.status = 'FAIL'; result.error = { name: error.name, message: error.message }; process.exitCode = 1;
  if (socket?.readyState === 1) result.pageOnFailure = await evaluate(`({url:location.href,title:document.title,
    status:document.getElementById('freshness-text')?.innerText, details:document.querySelector('#details .item-title')?.textContent,
    visibleCount:document.getElementById('visible-count')?.innerText, rows:document.querySelectorAll('#work-table button[data-node]').length})`).catch(()=>null);

} finally {
  result.finishedAt = new Date().toISOString(); result.runtimeExceptions = exceptions;
  await writeFile('work-map-ui-result.json', JSON.stringify(result, null, 2) + '\n');
  const summary = `Deployed work-map browser smoke: ${result.status}\n\n` +
    result.routes.map(r => `${r.reason}: ${r.count} visible cards (${r.url})`).join('\n') + '\n' +
    `Observed ${result.cases.length} selected cards` + (result.error ? `\n${result.error.message}` : '') + '\n';
  console.log(summary);
  if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY, summary);
  socket?.close();
  if (chrome) chrome.kill('SIGTERM');
  await rm(profile, { recursive: true, force: true, maxRetries: 6, retryDelay: 150 }).catch(() => {});

}
