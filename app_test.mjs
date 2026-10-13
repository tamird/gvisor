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

function attentionNode(number = 1) {
  const head = 'a'.repeat(40), url = `https://github.com/google/gvisor/pull/${number}`;
  return { id: `pr:${number}`, type: 'pr', number, title: `Change ${number}`, url, status: 'open', workStates: {}, github: {
    number, url, head, headOwner: 'tamird', status: 'open', reviewsComplete: true, reviewDecision: 'APPROVED',
    approvedHead: head, approvedAt: '2026-10-13T09:00:00Z', headCommittedAt: '2026-10-13T08:00:00Z',
    headIntroducedAt: '2026-10-13T08:01:00Z', headHistoryComplete: true, mergeable: 'MERGEABLE',
    importsComplete: true, imports: [], threads: { complete: true, unresolved: 0, total: 0 }, feedback: { complete: true, items: [] },
    checks: { complete: true, total: 1, state: 'SUCCESS', contexts: [{ name: 'test', state: 'SUCCESS', purpose: 'validation', kind: 'check-run' }] },
  } };
}
function attentionImport(node, number = 2) {
  return { ...attentionNode(number).github, head: 'b'.repeat(40), headOwner: 'google', sourceHead: node.github.head, matchesSourceHead: true };
}
function reasonsFor(node) {
  const context = client(); context.nodeInput = node;
  return JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
}
function idsFor(node) { return reasonsFor(node).map(reason => reason.id); }
function serviceStatus(name, state) { return { name, state, kind: 'status', purpose: 'validation', url: null }; }

test('attention assigns simultaneous public and internal failures to their actual owners', () => {
  const node = attentionNode();
  node.github.checks = { complete: true, total: 2, state: 'FAILURE', contexts: [serviceStatus('import/copybara', 'ERROR'), serviceStatus('gazelle', 'FAILURE')] };
  assert.ok(idsFor(node).includes('author-public-checks'));
  assert.ok(idsFor(node).includes('maintainer-import-failed'));
  node.github.checks.contexts = [serviceStatus('import/copybara', 'SUCCESS'), serviceStatus('feedback/copybara', 'FAILURE')];
  assert.ok(!idsFor(node).includes('author-public-checks'));
  assert.ok(idsFor(node).includes('maintainer-internal-feedback'));
  const imported = attentionImport(node); imported.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [serviceStatus('buildkite/pipeline', 'FAILURE')] }; node.github.imports = [imported];
  assert.ok(idsFor(node).includes('maintainer-internal-feedback'));
  assert.ok(idsFor(node).includes('maintainer-import-checks'));
  assert.ok(idsFor(node).includes('maintainer-merge'));
});

test('agent work, contributor decisions and maintainer blockers have distinct owners', () => {
  const node = attentionNode(); node.github.mergeable = 'CONFLICTING';
  node.github.checks = { complete: true, total: 2, state: 'FAILURE', contexts: [serviceStatus('gazelle', 'FAILURE'), serviceStatus('import/copybara', 'ERROR')] };
  node.github.threads = { complete: true, total: 1, unresolved: 1 };
  node.github.feedback.items = [{ kind: 'inline', threadId: 'thread', isResolved: false, author: { login: 'reviewer' }, createdAt: '2026-10-13T09:10:00Z' }];
  node.workStates['contributor-review'] = { scope: 'c'.repeat(40) };
  const owners = Object.fromEntries(reasonsFor(node).map(reason => [reason.id, reason.owner]));
  assert.equal(owners['author-conflicts'], 'agent'); assert.equal(owners['author-public-checks'], 'agent'); assert.equal(owners['author-feedback'], 'agent');
  assert.equal(owners['author-proposal'], 'user'); assert.equal(owners['maintainer-import-failed'], 'maintainer');
  assert.deepEqual(reasonsFor(node).filter(reason => reason.owner === 'user').map(reason => reason.id), ['author-proposal']);
  const branch = { type: 'branch', head: 'c'.repeat(40), contributorChangesRequested: { head: 'c'.repeat(40) }, workStates: {} };
  assert.deepEqual(reasonsFor(branch).map(reason => reason.owner), ['agent']);
  const context = client();
  assert.equal(vm.runInContext('ATTENTION_OWNERS.agent', context), 'Agent work');
  assert.equal(vm.runInContext('ATTENTION_OWNERS.user', context), 'Needs your decision');
});

test('attention uses current approvals and import associations without inventing import failures', () => {
  const node = attentionNode(), imported = attentionImport(node);
  imported.sourceHead = 'c'.repeat(40); imported.matchesSourceHead = false; imported.status = 'closed'; node.github.imports = [imported];
  node.github.feedback.items = [{ kind: 'comment', body: 'The import failed last week.' }];
  assert.deepEqual(idsFor(node), ['maintainer-import']);
  node.github.approvedHead = 'c'.repeat(40);
  assert.deepEqual(idsFor(node), ['maintainer-review']);
  node.github.revisionChanged = true;
  assert.deepEqual(idsFor(node), ['agent-refresh']);
  assert.equal(reasonsFor(node)[0].owner, 'agent');
});

test('unanswered feedback stays author work after a mechanical new head or outdated location', () => {
  const node = attentionNode(); node.github.reviewDecision = 'CHANGES_REQUESTED'; node.github.threads = { complete: true, total: 1, unresolved: 1 };
  const comment = { kind: 'inline', threadId: 'thread', isResolved: false, isOutdated: false, createdAt: '2026-10-13T09:10:00Z', author: { login: 'reviewer', __typename: 'User' } };
  node.github.feedback.items = [comment];
  assert.ok(idsFor(node).includes('author-feedback')); assert.ok(!idsFor(node).includes('maintainer-response'));
  node.github.feedback.items.push({ ...comment, createdAt: '2026-10-13T09:20:00Z', author: { login: 'tamird', __typename: 'User' } });
  assert.ok(!idsFor(node).includes('author-feedback')); assert.ok(idsFor(node).includes('maintainer-response'));
  node.github.feedback.items.push({ ...comment, createdAt: '2026-10-13T09:21:00Z' });
  assert.ok(idsFor(node).includes('author-feedback'));
  node.github.headIntroducedAt = '2026-10-13T09:30:00Z'; node.github.head = 'd'.repeat(40);
  assert.ok(idsFor(node).includes('author-feedback')); assert.ok(!idsFor(node).includes('maintainer-response'));
  for (const item of node.github.feedback.items) item.isOutdated = true;
  assert.ok(idsFor(node).includes('author-feedback')); assert.ok(!idsFor(node).includes('maintainer-response'));
  node.github.feedback.items.push({ ...comment, createdAt: '2026-10-13T09:40:00Z', author: { login: 'tamird' } });
  assert.ok(!idsFor(node).includes('author-feedback')); assert.ok(idsFor(node).includes('maintainer-response'));
  assert.equal(node.github.reviewDecision, 'CHANGES_REQUESTED'); // Preserve GitHub's fact, not its old action assignment.
});

test('known bot feedback needs author triage even with incomplete review inventory', () => {
  const node = attentionNode();
  const comment = { kind: 'inline', threadId: 'bot-thread', isResolved: false, isOutdated: false, createdAt: '2026-10-13T09:10:00Z', author: { login: 'review-bot', __typename: 'Bot' } };
  node.github.feedback.items = [comment]; node.github.threads = { complete: true, total: 1, unresolved: 1 };
  assert.ok(idsFor(node).includes('author-feedback')); assert.ok(!idsFor(node).includes('maintainer-unknown'));
  node.github.reviewsComplete = false; node.github.feedback.complete = false; node.github.threads.complete = false;
  assert.ok(idsFor(node).includes('author-feedback')); assert.ok(idsFor(node).includes('maintainer-unknown'));
  node.github.feedback.items.push({ ...comment, createdAt: '2026-10-13T09:20:00Z', author: { login: 'tamird' } });
  assert.ok(!idsFor(node).includes('author-feedback')); assert.ok(idsFor(node).includes('maintainer-response')); assert.ok(idsFor(node).includes('maintainer-unknown'));
});

test('accepted older snapshots without feedback render an explicit unknown reason', () => {
  const context = client(), node = attentionNode(); node.github.labels = []; node.github.checkedAt = context.snapshotInput.checkedAt; delete node.github.feedback;
  context.snapshotInput.prs = [node.github]; context.nodeInput = node;
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  assert.ok(JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context)).some(reason => reason.id === 'maintainer-unknown'));
  node.github.feedback = { complete: true, items: null };
  assert.ok(idsFor(node).includes('maintainer-unknown')); assert.ok(!idsFor(node).includes('author-feedback'));
});

test('review-level holds and incomplete discussion remain unknown instead of author code requests', () => {
  const node = attentionNode(); node.github.reviewDecision = 'CHANGES_REQUESTED';
  node.github.feedback.items = [{ kind: 'review', state: 'CHANGES_REQUESTED', commit: { oid: node.github.head }, author: { login: 'reviewer' }, body: 'Hold for internal exceptions.' }];
  assert.deepEqual(idsFor(node), ['maintainer-unknown']);
  node.github.feedback.complete = false;
  assert.deepEqual(idsFor(node), ['maintainer-unknown']);
});

test('a fresh mergeable head overrides old conflict metadata', () => {
  const node = attentionNode(); node.workStates.conflicts = { scope: 'c'.repeat(40) };
  assert.ok(!idsFor(node).includes('author-conflicts'));
  node.github.mergeable = 'UNKNOWN'; node.github.mergeabilityObservation = { state: 'CONFLICTING', head: node.github.head, checkedAt: '2026-10-13T09:00:00Z' };
  assert.ok(idsFor(node).includes('author-conflicts'));
  node.github.mergeabilityObservation.head = 'c'.repeat(40);
  assert.ok(!idsFor(node).includes('author-conflicts'));
});

test('a directly observed new-head conflict remains actionable while old checks are invalidated', () => {
  const context = client(), node = attentionNode(); node.github.checkedAt = '2026-10-13T09:00:00Z'; context.nodeInput = node;
  context.observationInput = { head: 'd'.repeat(40), base: 'e'.repeat(40), status: 'open', state: 'CONFLICTING', checkedAt: '2026-10-13T10:00:00Z' };
  const result = JSON.parse(vm.runInContext('conflictResults.set(nodeInput.number, observationInput); nodeInput.github = withConflictResult(nodeInput.github); JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
  assert.deepEqual(result.map(reason => reason.id), ['author-conflicts', 'agent-refresh']);
  assert.equal(node.github.revisionChanged, true); assert.equal(node.github.checks, null);
});

test('unpublished proposal decisions do not replace the published head review', () => {
  const node = attentionNode(); node.github.reviewDecision = 'REVIEW_REQUIRED'; node.workStates['contributor-review'] = { scope: 'c'.repeat(40) };
  assert.ok(idsFor(node).includes('author-proposal')); assert.ok(idsFor(node).includes('maintainer-review'));
  const context = client(); context.nodeInput = node;
  assert.equal(vm.runInContext('attentionGroups([nodeInput], "tamird")[0].id', context), 'maintainer-review');
  assert.equal(vm.runInContext('attentionGroups([nodeInput], "tamird", true, new Set(["author-proposal"]))[0].id', context), 'author-proposal');
});

test('imported-unmerged does not assert readiness and administrative failures are maintainer work', () => {
  const node = attentionNode(), imported = attentionImport(node); node.github.imports = [imported]; imported.mergeable = 'UNKNOWN'; imported.checks.complete = false;
  assert.ok(idsFor(node).includes('maintainer-merge')); assert.ok(idsFor(node).includes('maintainer-unknown'));
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-merge').detail, /mergeability unknown/);
  imported.status = 'closed'; assert.ok(!idsFor(node).includes('maintainer-import-failed'));
  assert.ok(idsFor(node).includes('maintainer-unknown'));
  node.github.imports = []; node.github.checks.contexts.push({ name: 'assign', state: 'FAILURE', purpose: 'reviewer-assignment' }); node.github.checks.total = 2; node.github.checks.state = 'FAILURE';
  assert.ok(!idsFor(node).includes('author-public-checks')); assert.ok(idsFor(node).includes('maintainer-assignment'));
});

test('table copying is plaintext, one full source URL per line, with disjoint displayed groups', () => {
  const node = attentionNode(10); node.title = 'Title ] with\nnew line'; node.github.mergeable = 'CONFLICTING';
  node.github.checks.contexts.push(serviceStatus('import/copybara', 'ERROR')); node.github.checks.total = 2;
  node.github.imports = [attentionImport(node, 20)];
  const draft = attentionNode(30); draft.status = draft.github.status = 'draft';
  const branch = { id: 'branch:x', type: 'branch', title: 'Branch', url: 'https://github.com/tamird/gvisor/tree/x', workStates: {} };
  const context = client(); context.nodesInput = [node, draft, branch];
  const text = vm.runInContext('attentionText(attentionGroups(nodesInput, "tamird"), "2026-10-13T09:05:00Z")', context);
  assert.match(text, /Public GitHub snapshot: 2026-10-13 09:05 UTC/);
  assert.ok(!/^#{1,6} |^- |\[[^\n]*\]\(|`/m.test(text));
  assert.deepEqual(text.split('\n').filter(line => line.startsWith('https://')), ['https://github.com/google/gvisor/pull/10', 'https://github.com/google/gvisor/pull/30']);
  assert.ok(!text.includes('/pull/20')); assert.ok(!text.includes('Title')); assert.ok(!text.includes('/tree/x'));
  assert.equal(text.split('/pull/10').length - 1, 1);
  const selected = vm.runInContext('attentionText(attentionGroups([nodesInput[0]], "tamird", true, new Set(["maintainer-import-failed"])), "2026-10-13T09:05:00Z")', context);
  assert.match(selected, /^Could you help/); assert.match(selected, /Needs maintainer attention: Import failed/);
  assert.ok(!selected.includes('Agent work'));
});

test('shared table URLs preserve multiple attention reasons and legacy handoff navigation', () => {
  const context = client(); context.location = new URL('https://example.test/?view=handoff&q=15518#handoff-import-failed');
  const alias = JSON.parse(vm.runInContext('JSON.stringify(readViewURL())', context)); assert.equal(alias.view, 'table'); assert.equal(alias.tableGrouping, true); assert.equal(alias.search, '15518');
  context.location = new URL('https://example.test/?view=table&group-by=attention&reason=author-conflicts&reason=maintainer-import-failed&sort=number&order=asc');
  const shared = JSON.parse(vm.runInContext('JSON.stringify(readViewURL())', context)); assert.deepEqual(shared.reasonFilters, ['author-conflicts', 'maintainer-import-failed']); assert.equal(shared.sortKey, 'number'); assert.equal(shared.sortDirection, 1);
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
