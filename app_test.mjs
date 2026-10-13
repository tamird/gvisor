import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function client() {
  const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
  assert.ok(source.endsWith('start();\n'));
  const context = vm.createContext({ URL });
  vm.runInContext(source.slice(0, -'start();\n'.length), context);
  context.registryInput = { meta: { repo: 'google/gvisor', owner: 'tamird', updatedAt: '2026-10-12T18:05:00Z' } };
  context.snapshotInput = { schema: 1, repo: 'google/gvisor', owner: 'tamird', checkedAt: '2026-10-12T11:48:00Z',
    registryDate: context.registryInput.meta.updatedAt, issues: {}, prs: [], workStates: {} };
  return context;
}

test('new contributor observation survives an older full snapshot and controls age', () => {
  const context = client();
  const record = { scope: 'a'.repeat(40), basis: 'observed', since: '2026-10-12T14:41:00Z', observedAt: '2026-10-12T18:05:00Z' };
  context.snapshotInput.workStates['branch:proposal'] = { 'contributor-review': record };
  context.record = record;
  vm.runInContext('validateSnapshot(snapshotInput, registryInput); model = { checkedAt: snapshotInput.checkedAt };', context);
  assert.equal(vm.runInContext('age(record)', context), 'obs 3h');
  assert.equal(vm.runInContext('stateAge({workStates: {"contributor-review": record}})', context), 204 * 60000);
  record.observedAt = '2026-10-12T19:00:00Z';
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid state observation/);
});

test('direct terminal status refresh does not refresh other PR states', () => {
  const context = client();
  context.snapshotInput.prs = [{ number: 1, imports: [], head: 'a'.repeat(40), labels: [], threads: {}, checkedAt: context.snapshotInput.checkedAt,
    statusObservation: { checkedAt: '2026-10-12T18:05:00Z' } }];
  const record = { scope: 'a'.repeat(40), basis: 'transition', since: '2026-10-12T17:54:16Z', observedAt: '2026-10-12T18:05:00Z' };
  context.snapshotInput.workStates['pr:1'] = { merged: record };
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  context.snapshotInput.workStates['pr:1'] = { 'maintainer-review': record };
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid state observation/);
});

test('legacy observations retain the full-snapshot bound', () => {
  const context = client();
  const record = { scope: 'a'.repeat(40), basis: 'observed', since: '2026-10-12T10:00:00Z' };
  context.snapshotInput.workStates['branch:proposal'] = { 'contributor-review': record };
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  record.since = '2026-10-12T14:41:00Z';
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid state observation/);
});


test('browser conflict refresh retains first observation and uses the service clock', () => {
  const context = client();
  const head = 'a'.repeat(40);
  context.snapshotInput.workStates['pr:1'] = { conflicts: { scope: head, since: '2026-10-12T10:00:00Z', observedAt: '2026-10-12T11:00:00Z', basis: 'observed' } };
  context.node = { id: 'pr:1', type: 'pr', github: { head, status: 'open', imports: [], mergeable: 'CONFLICTING', conflictCheckedAt: '2026-10-12T15:00:00Z' } };
  vm.runInContext('model = { checkedAt: snapshotInput.checkedAt }; result = stateRecords(snapshotInput, node);', context);
  assert.equal(context.result.conflicts.since, '2026-10-12T10:00:00Z');
  assert.equal(context.result.conflicts.observedAt, '2026-10-12T15:00:00Z');
  assert.equal(vm.runInContext('age(result.conflicts)', context), 'obs 5h');
  context.node.github.status = 'merged';
  vm.runInContext('result = stateRecords(snapshotInput, node)', context);
  assert.equal(context.result.merged.observedAt, '2026-10-12T15:00:00Z');
});

function handoffNode(number = 1) {
  const head = 'a'.repeat(40), url = `https://github.com/google/gvisor/pull/${number}`;
  return { id: `pr:${number}`, type: 'pr', number, title: `Change ${number}`, url, workStates: {}, github: {
    number, url, head, headOwner: 'tamird', status: 'open', reviewsComplete: true, reviewDecision: 'APPROVED',
    approvedHead: head, approvedAt: '2026-10-13T09:00:00Z', headCommittedAt: '2026-10-13T08:00:00Z',
    headIntroducedAt: '2026-10-13T08:01:00Z', headHistoryComplete: true, mergeable: 'MERGEABLE',
    importsComplete: true, imports: [], checks: { complete: true, total: 1, state: 'SUCCESS', contexts: [{ name: 'test', state: 'SUCCESS', purpose: 'validation', kind: 'check-run' }] },
  } };
}
function handoffImport(node, number = 2) {
  return { ...handoffNode(number).github, head: 'b'.repeat(40), headOwner: 'google',
    sourceHead: node.github.head, matchesSourceHead: true };
}
function classifyHandoff(nodes) {
  const context = client(); context.nodesInput = nodes;
  return JSON.parse(vm.runInContext('JSON.stringify(handoffGroups(nodesInput, "tamird"))', context));
}
function handoffGroup(node) {
  return classifyHandoff([node]).find(group => group.entries.length)?.id;
}

test('handoff distinguishes explicit import errors, feedback failure, and import check failures', () => {
  const node = handoffNode();
  const status = (name, state) => ({ name, state, kind: 'status', purpose: 'validation', url: null });
  node.github.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [status('import/copybara', 'ERROR')] };
  node.workStates.failing = { scope: node.github.head, basis: 'observed', since: '2026-10-01T00:00:00Z' };
  assert.equal(handoffGroup(node), 'import-failed');
  const entry = classifyHandoff([node])[0].entries[0];
  assert.equal(entry.observation, ''); // A generic failure clock cannot date this context.
  assert.equal(entry.reason, 'import/copybara: ERROR.');
  assert.match(classifyHandoff([node])[0].ask, /internal import errors/);
  node.github.checks.contexts.push(status('gazelle', 'FAILURE')); node.github.checks.total = 2;
  assert.equal(handoffGroup(node), 'import-failed'); // Do not hide a concrete internal error.
  node.github.checks.contexts = [status('import/copybara', 'SUCCESS'), status('feedback/copybara', 'FAILURE')];
  assert.equal(handoffGroup(node), 'internal-status');
  assert.match(classifyHandoff([node]).find(group => group.entries.length).entries[0].reason, /import\/copybara: SUCCESS/);
  const imported = handoffImport(node); imported.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [status('Buildkite', 'FAILURE')] };
  node.github.imports = [imported];
  assert.equal(handoffGroup(node), 'import-checks');
});

test('handoff uses current-head approval and import association, not historical links or prose', () => {
  const node = handoffNode();
  node.github.feedback = { items: [{ body: 'The import failed last week.' }] };
  const imported = handoffImport(node); imported.sourceHead = 'c'.repeat(40); imported.matchesSourceHead = false; imported.status = 'closed';
  node.github.imports = [imported];
  assert.equal(handoffGroup(node), 'import');
  node.github.approvedHead = 'c'.repeat(40);
  node.workStates['maintainer-review'] = { scope: 'c'.repeat(40), basis: 'observed', since: '2026-10-01T00:00:00Z' };
  assert.equal(handoffGroup(node), 'review');
  assert.equal(classifyHandoff([node]).find(group => group.entries.length).entries[0].observation, '');
  node.github.revisionChanged = true;
  assert.equal(handoffGroup(node), 'internal-status');
});

test('handoff identifies imported-but-unmerged without declaring readiness', () => {
  const node = handoffNode(); const imported = handoffImport(node); node.github.imports = [imported];
  const scope = `${node.github.head},${imported.number}:${imported.head}`;
  node.workStates['waiting-merge'] = { scope, basis: 'observed', since: '2026-10-13T09:00:00Z' };
  assert.equal(handoffGroup(node), 'merge');
  imported.checks.complete = false;
  assert.equal(handoffGroup(node), 'merge');
  assert.match(classifyHandoff([node]).find(group => group.entries.length).entries[0].reason, /incomplete or unknown/);
  imported.checks.complete = true; imported.mergeable = 'UNKNOWN';
  assert.equal(handoffGroup(node), 'merge');
  assert.match(classifyHandoff([node]).find(group => group.entries.length).entries[0].reason, /mergeability unknown/);
  imported.status = 'draft';
  assert.equal(handoffGroup(node), 'internal-status');
  imported.status = 'closed';
  assert.equal(handoffGroup(node), 'internal-status');
  imported.status = 'merged';
  assert.equal(handoffGroup(node), 'internal-status');
  node.github.imports = []; node.github.importsComplete = false;
  assert.equal(handoffGroup(node), 'internal-status');
  node.github.importsComplete = true; node.github.checks.contexts = [{ name: 'import/copybara', kind: 'status', state: 'SUCCESS', purpose: 'validation' }];
  assert.equal(handoffGroup(node), 'internal-status');
});

test('handoff omits drafts, resolved sources and contributor-only work but not assignment errors', () => {
  const nodes = Array.from({ length: 6 }, (_, index) => handoffNode(index + 1));
  nodes[0].github.status = 'draft'; nodes[1].github.status = 'merged'; nodes[2].github.status = 'closed';
  nodes[3].github.reviewDecision = 'CHANGES_REQUESTED'; nodes[4].github.mergeable = 'CONFLICTING';
  nodes[5].github.checks.contexts[0].state = 'FAILURE'; nodes[5].github.checks.state = 'FAILURE';
  assert.equal(classifyHandoff(nodes).flatMap(group => group.entries).length, 0);
  const node = handoffNode(10);
  node.github.checks.contexts.push({ name: 'assign', state: 'FAILURE', purpose: 'reviewer-assignment' });
  node.github.checks.total = 2; node.github.checks.state = 'FAILURE';
  assert.equal(handoffGroup(node), 'import');
});

test('copied handoff has one linked source per group, revision-qualified imports and scoped observations', () => {
  const node = handoffNode(10); node.title = 'Title ] with\nnew line';
  const imported = handoffImport(node, 20); imported.matchesSourceHead = false; imported.sourceHead = 'c'.repeat(40); node.github.imports = [imported];
  node.workStates['awaiting-import'] = { scope: node.github.head, basis: 'observed', since: '2026-10-13T08:30:00Z' };
  const excluded = handoffNode(30); excluded.github.status = 'draft';
  const context = client(); context.nodesInput = [node, excluded];
  const text = vm.runInContext('handoffMarkdown(handoffGroups(nodesInput, "tamird"), "2026-10-13T09:05:00Z")', context);
  assert.match(text, /Public GitHub snapshot: 2026-10-13 09:05 UTC/);
  assert.match(text, /Title \\\] with new line/);
  assert.equal(text.split('https://github.com/google/gvisor/pull/10').length - 1, 1);
  assert.match(text, /Earlier-revision import #20\]\(https:\/\/github.com\/google\/gvisor\/pull\/20\)/);
  assert.match(text, /Awaiting-import state first observed 2026-10-13 08:30 UTC/);
  assert.ok(!text.includes('/pull/30'));
  assert.equal(text.split('## ').length - 1, 1);
  assert.equal(text.split('Could you confirm or start imports').length - 1, 1);
});

test('clipboard fallback and pending writes cannot restore a stale handoff after invalidation', async () => {
  const context = client();
  const elements = Object.fromEntries(['handoff-manual', 'handoff-copy-text', 'handoff-copy-status', 'handoff-copy-all']
    .map(id => [id, { hidden: true, value: '', textContent: '', focus() {}, select() {} }]));
  context.document = { getElementById: id => elements[id] };
  context.navigator = { clipboard: { writeText: async () => { throw Error('Denied'); } } };
  await vm.runInContext('copyHandoff("old scope", "Group")', context);
  assert.equal(elements['handoff-copy-text'].value, 'old scope');
  assert.equal(elements['handoff-manual'].hidden, false);
  vm.runInContext('invalidateHandoffCopy()', context);
  assert.equal(elements['handoff-copy-text'].value, '');
  assert.equal(elements['handoff-manual'].hidden, true);
  assert.equal(elements['handoff-copy-status'].textContent, '');
  let reject;
  context.navigator.clipboard.writeText = () => new Promise((_, rejectWrite) => { reject = rejectWrite; });
  const pending = vm.runInContext('copyHandoff("pending old scope", "Group")', context);
  vm.runInContext('invalidateHandoffCopy()', context);
  reject(Error('Denied later')); await pending;
  assert.equal(elements['handoff-copy-text'].value, '');
  assert.equal(elements['handoff-manual'].hidden, true);
  context.navigator.clipboard.writeText = async () => {};
  await vm.runInContext('copyHandoff("current scope", "Current group")', context);
  assert.equal(elements['handoff-copy-status'].textContent, 'Current group copied.');
  vm.runInContext('invalidateHandoffCopy()', context);
  assert.equal(elements['handoff-copy-status'].textContent, '');
});
