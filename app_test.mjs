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
  vm.runInContext("registry = registryInput", context);
  return context;
}

test('branch refinements render and copy the reviewed commit ranges without a PR', async () => {
  const context = client();
  const before = '7e371d8acc44a23b419a84618471efe9ffc51c42';
  const after = 'ca492fef95e67b769f0369c68e18a7edb28e1f2d';
  const node = { id: 'branch:compact', type: 'branch', repo: 'tamird/gvisor', head: after,
    refinement: { before: { head: before }, after: { head: after } } };
  context.registryInput = { ...context.registryInput, nodes: [node], edges: [], groups: [] };
  context.nodeInput = node;
  vm.runInContext('validateRegistry(registryInput)', context);
  const createElement = tag => ({ tag, children: [], attributes: {}, listeners: {},
    append(...children) { this.children.push(...children); },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() { this.focused = true; }, select() { this.selected = true; } });
  context.document = { createElement };
  context.parentInput = createElement('section');
  let copied;
  context.navigator = { clipboard: { writeText: async value => { copied = value; } } };
  vm.runInContext('appendRangeDiffCommand(parentInput, refinementComparison(nodeInput))', context);
  const [description, input, copy, message] = context.parentInput.children[0].children;
  const expected = `git range-diff ${before}^! ${after}^!`;
  assert.match(description.textContent, /Earlier revision 7e371d8ac → current revision ca492fef9/);
  assert.equal(input.value, expected);
  assert.equal(input.readOnly, true);
  await copy.listeners.click();
  assert.equal(copied, expected);
  assert.equal(message.textContent, 'Copied.');
  context.navigator.clipboard.writeText = async () => { throw Error('Denied'); };
  await copy.listeners.click();
  assert.equal(input.focused, true);
  assert.equal(input.selected, true);
  assert.match(message.textContent, /Select and copy/);
  node.refinement.before.base = 'a'.repeat(40);
  node.refinement.after.base = 'b'.repeat(40);
  assert.equal(vm.runInContext('refinementComparison(nodeInput).command', context),
    `git range-diff ${'a'.repeat(40)}..${before} ${'b'.repeat(40)}..${after}`);
  node.github = { head: 'c'.repeat(40) };
  assert.equal(vm.runInContext('refinementComparison(nodeInput)', context), null);
});

test('promotion uses an exact-head branch refinement when the PR comparison is stale', () => {
  const before = 'a'.repeat(40), after = 'b'.repeat(40), old = 'c'.repeat(40), earlier = 'd'.repeat(40);
  const branchRefinement = { before: { head: before }, after: { head: after } };
  for (const [prRefinement, branchHead, expectedBefore] of [
    [undefined, after, before],
    [{ before: { head: earlier }, after: { head: old } }, after, before],
    [{ before: { head: earlier }, after: { head: after } }, after, earlier],
    [{ before: { head: earlier }, after: { head: old } }, old, null],
  ]) {
    const context = client();
    const branch = { id: 'branch:proposal', type: 'branch', repo: 'tamird/gvisor', ref: 'proposal', head: branchHead,
      refinement: { ...branchRefinement, after: { head: branchHead } } };
    const pr = { id: 'pr:1', type: 'pr', number: 1, repo: 'google/gvisor', url: 'https://github.com/google/gvisor/pull/1', head: prRefinement?.after.head,
      ...(prRefinement ? { refinement: prRefinement } : {}) };
    context.registryInput = { ...context.registryInput, nodes: [branch, pr], edges: [], groups: [] };
    context.snapshotInput.prs = [{ ...attentionNode().github, head: after, ref: 'proposal', headRepo: 'tamird/gvisor' }];
    context.document = { getElementById: () => ({ scrollTop: 0, scrollLeft: 0, querySelectorAll: () => [], scrollTo() {} }) };
    vm.runInContext('updateRegistryControls = () => {}; render = () => {}; setFreshness = () => {}; applyLive(snapshotInput, registryInput)', context);
    assert.equal(vm.runInContext('model.nodes.length', context), 1);
    const command = vm.runInContext('refinementComparison(model.nodes[0])?.command ?? null', context);
    assert.equal(command, expectedBefore ? `git range-diff ${expectedBefore}^! ${after}^!` : null);
  }
});

test('refinement metadata rejects malformed ranges but tolerates a newer node head', () => {
  const context = client(), before = 'a'.repeat(40), after = 'b'.repeat(40);
  const node = { id: 'branch:proposal', type: 'branch', repo: 'tamird/gvisor', head: after };
  context.registryInput = { ...context.registryInput, nodes: [node], edges: [], groups: [] };
  for (const refinement of [null, {}, { before: { head: before }, after: { head: before } },
    { before: { head: before, base: before }, after: { head: after } },
    { before: { head: before, base: 'HEAD; echo unsafe' }, after: { head: after } }]) {
    node.refinement = refinement;
    assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid refinement comparison/);
  }
  node.refinement = { before: { head: before }, after: { head: after } };
  node.head = 'c'.repeat(40);
  context.nodeInput = node;
  vm.runInContext('validateRegistry(registryInput)', context);
  assert.equal(vm.runInContext('refinementComparison(nodeInput)', context), null);
});

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

test('contributor decisions bind the registered branch repository and exact proposal head', () => {
  const context = client(), head = 'c'.repeat(40);
  for (const repo of ['tamird/gvisor', 'tamird/go']) {
    const node = { id: 'branch:proposal', type: 'branch', repo, head,
      contributorReview: { status: 'pending', head, url: `https://github.com/${repo}/commit/${head}` },
      workStates: { 'contributor-review': { scope: head, basis: 'unknown', since: null } } };
    context.registryInput = { ...context.registryInput, nodes: [node], edges: [], groups: [] };
    context.nodeInput = node;
    vm.runInContext('validateRegistry(registryInput)', context);
    assert.deepEqual(idsFor(node), ['author-proposal']);
    assert.equal(reasonsFor(node)[0].owner, 'user');
    const correct = node.contributorReview.url;
    for (const url of [`https://github.com/tamird/unrelated/commit/${head}`,
      `https://github.com/${repo}/commit/${'d'.repeat(40)}`, `https://github.com/google/go/commit/${head}`]) {
      node.contributorReview.url = url;
      assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid contributor decision/);
    }
    node.contributorReview.url = correct;
    node.contributorChangesRequested = { head, url: correct };
    delete node.contributorReview;
    vm.runInContext('validateRegistry(registryInput)', context);
    node.contributorChangesRequested.url = `https://github.com/tamird/unrelated/commit/${head}`;
    assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid contributor changes request/);
    node.contributorChangesRequested.url = correct;
    node.repo = 'tamird/go/extra';
    assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid contributor changes request/);
  }
});

test('direct terminal status refresh does not refresh other PR states', () => {
  const context = client();
  context.snapshotInput.prs = [{ number: 1, url: 'https://github.com/google/gvisor/pull/1', imports: [], head: 'a'.repeat(40), labels: [], threads: {}, checkedAt: context.snapshotInput.checkedAt,
    statusObservation: { checkedAt: '2026-10-12T18:05:00Z' } }];
  const record = { scope: 'a'.repeat(40), basis: 'transition', since: '2026-10-12T17:54:16Z', observedAt: '2026-10-12T18:05:00Z' };
  context.snapshotInput.workStates['pr:1'] = { merged: record };
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  context.snapshotInput.workStates['pr:1'] = { 'maintainer-review': record };
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid state observation/);
});

test('scoped mergeability observations use each source or import PR clock', () => {
  const context = client();
  const source = { number: 1, url: 'https://github.com/google/gvisor/pull/1', head: 'a'.repeat(40), labels: [], threads: {}, checkedAt: '2026-10-12T18:05:00Z' };
  const imported = { number: 2, url: 'https://github.com/google/gvisor/pull/2', head: 'b'.repeat(40), labels: [], threads: {}, checkedAt: '2026-10-12T17:00:00Z' };
  source.imports = [imported];
  for (const item of [source, imported]) {
    item.mergeabilityCheckedAt = item.checkedAt;
    item.mergeabilityObservation = { state: 'MERGEABLE', head: item.head, checkedAt: item.checkedAt };
  }
  context.snapshotInput.prs = [source];
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  assert.equal(context.snapshotInput.checkedAt, '2026-10-12T11:48:00Z');
  for (const item of [source, imported]) {
    const future = new Date(Date.parse(item.checkedAt) + 1000).toISOString();
    item.mergeabilityCheckedAt = future;
    assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid mergeability observation time/);
    item.mergeabilityCheckedAt = item.checkedAt;
    item.mergeabilityObservation.checkedAt = future;
    assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid mergeability observation/);
    item.mergeabilityObservation.checkedAt = item.checkedAt;
  }
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
    importsComplete: true, imports: [], labelsComplete: true, labels: [], threads: { complete: true, unresolved: 0, total: 0 }, feedback: { complete: true, items: [] },
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

test('verified unchanged approval routes imports without rewriting fresh review provenance', () => {
  const node = attentionNode(); node.repo = 'google/gvisor';
  const pr = node.github;
  pr.githubReviewDecision = 'REVIEW_REQUIRED'; pr.reviewDecision = 'REVIEW_REQUIRED';
  pr.reviewRequestsComplete = true; pr.reviewRequests = [];
  pr.feedback.items = [{ kind: 'review', id: 'review-1', state: 'APPROVED', author: { login: 'reviewer' },
    submittedAt: '2026-10-09T12:00:00Z', commit: { oid: 'b'.repeat(40) } }];
  node.approvedPatch = { head: pr.head, approvedHead: 'b'.repeat(40), reviewId: 'review-1', reviewer: 'reviewer',
    submittedAt: '2026-10-09T12:00:00Z', verifiedAt: '2026-10-13T10:00:00Z',
    method: 'stable-patch-id', oldPatchId: 'c'.repeat(40), newPatchId: 'c'.repeat(40) };
  pr.labels = ['ready to pull'];
  assert.deepEqual(idsFor(node), ['maintainer-import']);
  pr.imports = [attentionImport(node)];
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  const context = client(); context.nodeInput = node;
  const original = JSON.stringify(node.github);
  context.snapshotInput.workStates[node.id] = { 'maintainer-review': { scope: pr.head, since: null, basis: 'unknown' } };
  assert.equal(vm.runInContext('effectiveReviewDecision(nodeInput.github)', context), 'REVIEW_REQUIRED');
  assert.equal(vm.runInContext('reviewInfo(nodeInput.github, nodeInput).text', context), 'Approved patch · rebased');
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["maintainer-review"]', context), undefined);
  assert.equal(JSON.stringify(node.github), original);
  context.snapshotInput.workStates[node.id] = { review: { scope: pr.head, since: null, basis: 'unknown' } };
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["maintainer-review"]', context), undefined);
  pr.statusChanged = true;
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["maintainer-review"]', context), undefined);
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["waiting-merge"]', context), undefined);
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["awaiting-import"]', context), undefined);
  delete pr.statusChanged;
  pr.feedback.items.push({ kind: 'comment', author: { login: 'tamird' }, createdAt: '2026-10-13T11:00:00Z', body: 'Please review the rebase.' });
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  const checkRejected = changed => {
    const c = client(); c.nodeInput = changed;
    assert.equal(vm.runInContext('approvedPatch(nodeInput)', c), null);
  };
  const gated = structuredClone(node);
  gated.repo = 'buildbuddy-io/buildbuddy'; gated.github.repo = gated.repo;
  gated.github.checks = { complete: true, total: 1, state: 'ERROR', contexts: [
    { kind: 'status', name: 'linux', state: 'ERROR', description: 'Check requires approving review', url: 'https://app.buildbuddy.io' }] };
  const gateContext = client(); gateContext.nodeInput = gated;
  gateContext.snapshotInput.workStates[node.id] = { 'maintainer-review': { scope: pr.head, since: null, basis: 'unknown', qualifier: 'BuildBuddy requires approving review before these checks run' } };
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["maintainer-review"].qualifier', gateContext), 'BuildBuddy requires approving review before these checks run');
  for (const change of [{ head: 'd'.repeat(40) }, { githubReviewDecision: 'CHANGES_REQUESTED' },
    { reviewsComplete: false }, { threads: { complete: true, unresolved: 1 } }, { reviewRequests: [{ login: 'reviewer' }] }])
    checkRejected({ ...node, github: { ...pr, ...change } });
  for (const change of [{ head: 'd'.repeat(40) }, { reviewId: 'different' }, { reviewer: 'other' },
    { submittedAt: '2026-10-10T12:00:00Z' }, { newPatchId: 'd'.repeat(40) }, { verifiedAt: 'not-a-time' }])
    checkRejected({ ...node, approvedPatch: { ...node.approvedPatch, ...change } });
  for (const association of [null, 'd'.repeat(40)]) {
    const changed = structuredClone(node); changed.github.feedback.items[0].commit.oid = association; checkRejected(changed);
  }
  const edited = structuredClone(node); edited.github.feedback.items[0].updatedAt = '2026-10-13T11:00:00Z'; checkRejected(edited);
  for (const state of ['DISMISSED', 'CHANGES_REQUESTED']) {
    const changed = structuredClone(node); changed.github.feedback.items[0].state = state; checkRejected(changed);
  }
  const changed = structuredClone(node);
  changed.github.feedback.items.push({ kind: 'comment', author: { login: 'reviewer' }, createdAt: '2026-10-13T11:00:00Z', body: 'Please hold this.' });
  checkRejected(changed);
  context.registryInput = { meta: { repo: 'google/gvisor', owner: 'tamird', updatedAt: '2026-10-13T12:00:00Z' }, nodes: [node], edges: [], groups: [] };
  vm.runInContext('validateRegistry(registryInput)', context);
  for (const item of [pr, ...pr.imports]) item.checkedAt = '2026-10-13T09:00:00Z';
  context.snapshotInput.registryDate = context.registryInput.meta.updatedAt;
  context.snapshotInput.prs = [pr];
  for (const state of ['awaiting-import', 'waiting-merge']) {
    const record = { scope: pr.head, basis: 'observed', since: node.approvedPatch.verifiedAt, observedAt: node.approvedPatch.verifiedAt };
    context.snapshotInput.workStates[node.id] = { [state]: record };
    vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
    record.observedAt = '2026-10-13T10:01:00Z';
    assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid state observation/);
    record.observedAt = node.approvedPatch.verifiedAt;
    context.snapshotInput.workStates[node.id] = { 'checks-pending': record };
    assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid state observation/);
    context.snapshotInput.workStates[node.id] = { [state]: record };
    pr.reviewsComplete = false;
    assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid state observation/);
    pr.reviewsComplete = true;
  }
  node.approvedPatch.newPatchId = 'd'.repeat(40);
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid approved patch equivalence/);
});

test('Copybara failures remain import attention without a public validation failure', () => {
  const node = attentionNode();
  node.github.checks.contexts.push(serviceStatus('import/copybara', 'ERROR'));
  node.github.checks.total = 2; node.github.checks.state = 'FAILURE';
  assert.deepEqual(idsFor(node), ['maintainer-import-failed']);
  const context = client(); context.nodeInput = node;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Passed · import issue');
  assert.equal(vm.runInContext('failureSources(nodeInput).length', context), 0);
  assert.equal(node.github.checks.contexts[1].state, 'ERROR');
  context.snapshotInput.workStates[node.id] = { failing: { scope: `1:${node.github.head}`, basis: 'unknown', since: null } };
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput).failing', context), undefined);
  node.github.checks.complete = false;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).tone', context), 'bad');
  assert.ok(vm.runInContext('stateRecords(snapshotInput, nodeInput).failing', context));
  assert.deepEqual(idsFor(node), ['agent-refresh', 'maintainer-import-failed']);
  node.github.checks.complete = true;
  node.github.checks.contexts[0].state = 'FAILURE';
  assert.ok(idsFor(node).includes('author-public-checks'));
  assert.ok(idsFor(node).includes('maintainer-import-failed'));
  node.github.checks.contexts[0].state = 'SUCCESS';
  const imported = attentionImport(node);
  imported.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [serviceStatus('generate', 'FAILURE')] };
  node.github.imports = [imported];
  assert.ok(idsFor(node).includes('maintainer-import-checks'));
  assert.equal(reasonsFor(node).find(reason => reason.id === 'maintainer-import-checks').owner, 'agent');
  imported.checks.contexts = [serviceStatus('feedback/copybara', 'ERROR'), serviceStatus('generate', 'SUCCESS')];
  imported.checks.total = 2;
  assert.ok(!idsFor(node).includes('maintainer-import-checks'));
  assert.ok(idsFor(node).includes('maintainer-internal-feedback'));
});

test('attention assigns simultaneous public and internal failures to their actual owners', () => {
  const node = attentionNode();
  node.github.checks = { complete: true, total: 2, state: 'FAILURE', contexts: [serviceStatus('import/copybara', 'ERROR'), serviceStatus('gazelle', 'FAILURE')] };
  assert.ok(idsFor(node).includes('author-public-checks'));
  assert.ok(idsFor(node).includes('maintainer-import-failed'));
  node.github.checks.contexts = [serviceStatus('import/copybara', 'SUCCESS'), serviceStatus('feedback/copybara', 'FAILURE')];
  assert.ok(!idsFor(node).includes('author-public-checks'));
  assert.ok(idsFor(node).includes('maintainer-internal-feedback'));
  const imported = attentionImport(node); imported.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [{ ...serviceStatus('buildkite/pipeline', 'FAILURE'), url: 'https://buildkite.com/gvisor/pipeline/builds/50515' }] }; node.github.imports = [imported];
  assert.ok(!idsFor(node).includes('maintainer-internal-feedback'));
  assert.ok(idsFor(node).includes('maintainer-import-checks'));
  assert.equal(reasonsFor(node).find(reason => reason.id === 'maintainer-import-checks').owner, 'agent');
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-import-checks').detail, /Source feedback\/copybara: FAILURE/);
  assert.ok(!idsFor(node).some(id => id === 'maintainer-merge'));
  imported.checks.contexts[0].url = null;
  assert.ok(!idsFor(node).includes('maintainer-internal-feedback'));
  assert.ok(idsFor(node).includes('maintainer-import-checks'));
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-import-checks').detail, /direct link not recorded/);
  imported.checks.contexts = []; imported.checks.complete = false;
  assert.ok(!idsFor(node).includes('maintainer-internal-feedback'));
  assert.ok(idsFor(node).includes('maintainer-import-checks'));
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-import-checks').detail, /refresh and investigate/);
});

function deniedImportRetry() {
  const node = attentionNode(15333), imported = attentionImport(node, 15339);
  node.repo = 'google/gvisor';
  node.ciRecovery = { kind: 'dependency-download-retry-denied', sourceHead: node.github.head,
    importNumber: imported.number, importHead: imported.head, workflowRun: 38434851950, job: 115426066833,
    jobURL: 'https://github.com/google/gvisor/actions/runs/38434851950/job/115426066833',
    checkId: 'check-1', checkName: 'generate', statuses: [{ id: 'status-1', name: 'go tests' }],
    httpStatus: 403, observedAt: '2026-10-13T17:35:00Z' };
  imported.checks = { complete: true, state: 'FAILURE', total: 2, contexts: [
    { id: 'check-1', name: 'generate', kind: 'check-run', appSlug: 'github-actions', state: 'FAILURE', purpose: 'validation', url: node.ciRecovery.jobURL },
    { ...serviceStatus('go tests', 'FAILURE'), id: 'status-1' },
  ] };
  node.github.imports = [imported];
  return node;
}

function sourceAuthenticationFailure() {
  const node = attentionNode(13849);
  node.repo = 'buildbuddy-io/buildbuddy'; node.id = 'pr:buildbuddy-io/buildbuddy:13849';
  node.url = 'https://github.com/buildbuddy-io/buildbuddy/pull/13849';
  node.ciRecovery = { kind: 'source-check-authentication', reason: 'missing-api-key', sourceHead: node.github.head,
    observedAt: '2026-10-13T17:35:00Z', checks: [{ id: 'source-check', name: 'build', workflowRun: 39303727616,
      job: 118523915013, jobURL: 'https://github.com/buildbuddy-io/buildbuddy/actions/runs/39303727616/job/118523915013' }] };
  node.github.checks = { complete: true, state: 'FAILURE', total: 1, contexts: [
    { id: 'source-check', name: 'build', kind: 'check-run', appSlug: 'github-actions', state: 'FAILURE', purpose: 'validation', url: node.ciRecovery.checks[0].jobURL },
  ] };
  return node;
}

test('proven source authentication failures are maintainer work without hiding unrelated source failures', () => {
  const node = sourceAuthenticationFailure();
  const reason = reasonsFor(node).find(reason => reason.id === 'maintainer-ci-retry');
  assert.equal(reason.owner, 'maintainer'); assert.equal(reason.title, 'Maintainer CI configuration');
  assert.match(reason.detail, /missing API key/);
  assert.ok(!idsFor(node).includes('author-public-checks'));
  assert.equal(node.github.checks.state, 'FAILURE');
  node.github.checks.contexts.push({ ...serviceStatus('lint', 'FAILURE'), id: 'uninspected' }); node.github.checks.total++;
  assert.ok(idsFor(node).includes('maintainer-ci-retry'));
  assert.match(reasonsFor(node).find(reason => reason.id === 'author-public-checks').detail, /lint/);
});

test('source authentication recovery expires when its head or check evidence changes', () => {
  const mutations = [
    node => { node.github.head = 'c'.repeat(40); },
    node => { node.github.checks.contexts[0].id = 'new-job'; },
    node => { delete node.github.checks.contexts[0].id; },
    node => { node.github.checks.contexts[0].url += '?different'; },
    node => { node.github.checks.contexts[0].appSlug = 'another-app'; },
    node => { node.github.checks.complete = false; },
    node => { node.github.checks.contexts.push({ ...node.github.checks.contexts[0], id: 'new-job', state: 'IN_PROGRESS' }); node.github.checks.total++; },
  ];
  for (const mutate of mutations) {
    const node = sourceAuthenticationFailure(); mutate(node);
    assert.ok(!idsFor(node).includes('maintainer-ci-retry'), mutate.toString());
    assert.ok(idsFor(node).includes('author-public-checks'), mutate.toString());
  }
  const node = sourceAuthenticationFailure(); node.github.checks.contexts[0].state = 'SUCCESS';
  assert.ok(!idsFor(node).includes('maintainer-ci-retry'));
  delete node.ciRecovery; node.github.checks.contexts[0].state = 'FAILURE'; node.summary = 'Missing API key';
  assert.ok(idsFor(node).includes('author-public-checks'));
});

test('source authentication schema requires known repository and unique exact job identities', () => {
  const mutations = [
    node => { node.ciRecovery.kind = 'unknown'; },
    node => { node.ciRecovery.reason = 'source defect'; },
    node => { node.ciRecovery.sourceHead = 'unknown'; },
    node => { node.ciRecovery.observedAt = '2027-01-01T00:00:00Z'; },
    node => { node.ciRecovery.checks = []; },
    node => { node.ciRecovery.checks[0].jobURL = 'https://example.com/log'; },
    node => { node.ciRecovery.checks.push({ ...node.ciRecovery.checks[0] }); },
  ];
  for (const mutate of [null, ...mutations]) {
    const context = client(); context.nodeInput = sourceAuthenticationFailure();
    context.registryInput = { meta: { repo: 'google/gvisor', owner: 'tamird', updatedAt: '2026-10-13T18:00:00Z',
      repositories: { 'google/gvisor': 'copybara', 'buildbuddy-io/buildbuddy': 'none' } }, nodes: [context.nodeInput], groups: [], edges: [] };
    if (mutate) {
      mutate(context.nodeInput);
      assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid CI recovery/, mutate.toString());
    } else vm.runInContext('validateRegistry(registryInput)', context);
  }
});

test('denied exact import retry is maintainer work while unrelated failures stay agent work', () => {
  const node = deniedImportRetry();
  assert.ok(idsFor(node).includes('maintainer-ci-retry'));
  assert.ok(!idsFor(node).includes('maintainer-import-checks'));
  assert.ok(!idsFor(node).includes('maintainer-internal-feedback'));
  assert.equal(reasonsFor(node).find(reason => reason.id === 'maintainer-ci-retry').owner, 'maintainer');
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-ci-retry').detail, /HTTP 403/);
  assert.equal(node.github.imports[0].checks.state, 'FAILURE');
  node.github.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [serviceStatus('feedback/copybara', 'FAILURE')] };
  assert.ok(idsFor(node).includes('maintainer-internal-feedback'));
  assert.ok(!idsFor(node).includes('maintainer-import-checks'));
  node.github.imports[0].checks.contexts.push({ ...serviceStatus('buildkite/pipeline', 'FAILURE'), id: 'another-status' });
  node.github.imports[0].checks.total++;
  assert.ok(idsFor(node).includes('maintainer-import-checks'));
  assert.ok(idsFor(node).includes('maintainer-ci-retry'));
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-import-checks').detail, /buildkite\/pipeline/);
});

test('retry recovery expires on changed heads, missing identities, or superseding checks', () => {
  const mutations = [
    node => { node.github.head = 'c'.repeat(40); node.github.imports[0].sourceHead = node.github.head; },
    node => { node.github.imports[0].head = 'c'.repeat(40); },
    node => { node.github.imports[0].checks.contexts[0].id = 'new-job'; },
    node => { node.github.imports[0].checks.contexts[0].state = 'SUCCESS'; },
    node => { node.github.imports[0].checks.contexts[1].id = 'new-status'; },
    node => { delete node.github.imports[0].checks.contexts[0].id; },
    node => { node.github.imports[0].checks.complete = false; },
    node => { node.github.imports[0].checks.contexts.push({ ...node.github.imports[0].checks.contexts[0], id: 'new-job', state: 'IN_PROGRESS' }); node.github.imports[0].checks.total++; },
  ];
  for (const mutate of mutations) {
    const node = deniedImportRetry(); mutate(node);
    assert.ok(!idsFor(node).includes('maintainer-ci-retry'), mutate.toString());
    assert.ok(idsFor(node).includes('maintainer-import-checks'), mutate.toString());
  }
  const node = deniedImportRetry(); delete node.ciRecovery;
  node.summary = 'Retry failed with HTTP 403';
  assert.ok(idsFor(node).includes('maintainer-import-checks'));
  assert.ok(!idsFor(node).includes('maintainer-ci-retry'));
});

test('CI recovery schema binds public job and exact identities without rejecting older snapshots', () => {
  const context = client(); context.nodeInput = deniedImportRetry();
  context.registryInput = { meta: { repo: 'google/gvisor', owner: 'tamird', updatedAt: '2026-10-13T18:00:00Z' }, nodes: [context.nodeInput], groups: [], edges: [] };
  vm.runInContext('validateRegistry(registryInput)', context);
  context.nodeInput.ciRecovery.jobURL = 'https://example.com/job/115426066833';
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid CI recovery/);
  context.nodeInput.ciRecovery.jobURL = 'https://github.com/google/gvisor/actions/runs/38434851950/job/115426066833';
  context.nodeInput.ciRecovery.statuses.push({ id: 'status-1', name: 'another status' });
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid CI recovery/);
  const legacy = client(); legacy.nodeInput = attentionNode(); legacy.nodeInput.github.checkedAt = legacy.snapshotInput.checkedAt;
  legacy.snapshotInput.prs = [legacy.nodeInput.github];
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', legacy);
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
  assert.deepEqual(idsFor(node), ['maintainer-import-label']);
  node.github.labels = ['ready to pull'];
  assert.deepEqual(idsFor(node), ['maintainer-import']);
  node.github.labelsComplete = false;
  assert.deepEqual(idsFor(node), ['agent-refresh']);
  node.github.labelsComplete = true;
  imported.sourceHead = node.github.head; imported.matchesSourceHead = true; imported.status = 'open'; node.github.labels = [];
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  node.github.imports = [];
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
  assert.ok(idsFor(node).includes('author-feedback')); assert.ok(idsFor(node).includes('agent-refresh'));
  node.github.feedback.items.push({ ...comment, createdAt: '2026-10-13T09:20:00Z', author: { login: 'tamird' } });
  assert.ok(!idsFor(node).includes('author-feedback')); assert.ok(idsFor(node).includes('maintainer-response')); assert.ok(idsFor(node).includes('agent-refresh'));
});

test('accepted older snapshots without feedback render an explicit unknown reason', () => {
  const context = client(), node = attentionNode(); node.github.labels = []; node.github.checkedAt = context.snapshotInput.checkedAt; delete node.github.feedback;
  context.snapshotInput.prs = [node.github]; context.nodeInput = node;
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  assert.ok(JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context)).some(reason => reason.id === 'agent-refresh'));
  node.github.feedback = { complete: true, items: null };
  assert.ok(idsFor(node).includes('agent-refresh')); assert.ok(!idsFor(node).includes('author-feedback'));
});

test('review-level holds and incomplete discussion remain unknown instead of author code requests', () => {
  const node = attentionNode(); node.github.reviewDecision = 'CHANGES_REQUESTED';
  node.github.feedback.items = [{ kind: 'review', state: 'CHANGES_REQUESTED', commit: { oid: node.github.head }, author: { login: 'reviewer' }, body: 'Hold for internal exceptions.' }];
  assert.deepEqual(idsFor(node), ['maintainer-response']);
  node.github.feedback.complete = false;
  assert.deepEqual(idsFor(node), ['agent-refresh']);
});

test('a confirmed conflict takes priority over opaque import progress', () => {
  const node = attentionNode(15337);
  node.github.checks.contexts = [serviceStatus('import/copybara', 'SUCCESS')];
  const context = client(); context.nodeInput = node;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Unknown');
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).passed', context), false);
  assert.deepEqual(idsFor(node), ['maintainer-unknown']);
  assert.equal(reasonsFor(node)[0].title, 'Import succeeded; no public PR');
  node.github.checks.complete = false;
  assert.deepEqual(idsFor(node), ['agent-refresh']);
  node.github.checks.complete = true;
  node.github.checks.contexts.push(serviceStatus('public-test', 'UNKNOWN'));
  node.github.checks.total++;
  assert.deepEqual(idsFor(node), ['agent-refresh']);
  node.github.checks.contexts.pop(); node.github.checks.total--;
  node.github.mergeable = 'CONFLICTING';
  assert.deepEqual(idsFor(node), ['author-conflicts']);
  node.github.mergeable = 'UNKNOWN';
  assert.deepEqual(idsFor(node), ['agent-refresh']);
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
  const result = JSON.parse(vm.runInContext('conflictResults.set(prIdentity(nodeInput.github), observationInput); nodeInput.github = withConflictResult(nodeInput.github); JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
  assert.deepEqual(result.map(reason => reason.id), ['author-conflicts', 'agent-refresh']);
  assert.equal(node.github.revisionChanged, true); assert.equal(node.github.checks, null);
});

test('direct REST refresh replaces the repository and branch identity even on the same head', async () => {
  const context = client(), original = attentionNode().github;
  original.headRepo = 'tamird/gvisor'; original.ref = 'old-name'; original.checkedAt = '2026-10-13T09:00:00Z';
  context.originalInput = original;
  context.document = { querySelector: () => null };
  context.AbortSignal = { timeout: () => undefined };
  vm.runInContext('registry = registryInput; drawDetails = () => {}; applyLive = () => {};', context);
  for (const repo of [{ full_name: 'tamird/renamed-fork' }, null, undefined]) {
    context.fetch = async () => ({ ok: true, json: async () => ({ number: 1, html_url: 'https://github.com/google/gvisor/pull/1',
      head: { sha: original.head, ref: 'new-name', ...(repo === undefined ? {} : { repo }) },
      base: { sha: 'b'.repeat(40) }, mergeable: true, state: 'open', merged: false, draft: false }) });
    await vm.runInContext('lastConflictAttempt = 0; refreshConflict(1)', context);
    const current = JSON.parse(vm.runInContext('JSON.stringify(withConflictResult(originalInput))', context));
    assert.equal(current.headRepo, repo?.full_name ?? null);
    assert.equal(current.ref, 'new-name');
    assert.equal(current.revisionChanged, false);
    assert.equal(current.head, original.head);
  }
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
  assert.deepEqual(idsFor(node), ['agent-refresh']);
  imported.mergeable = 'MERGEABLE'; imported.checks.complete = true;
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  imported.mergeable = 'CONFLICTING';
  assert.deepEqual(idsFor(node), ['author-conflicts']);
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

test('repository identity separates same-number PRs, promotion and conflict observations', async () => {
  const context = client(), repo = 'buildbuddy-io/buildbuddy';
  const primary = attentionNode(7).github, external = { ...attentionNode(7).github,
    repo, url: `https://github.com/${repo}/pull/7`, importPolicy: 'none', ref: 'topic', headRepo: 'tamird/buildbuddy' };
  for (const pr of [primary, external]) pr.checkedAt = context.snapshotInput.checkedAt;
  context.registryInput = { ...context.registryInput, nodes: [
    { id: 'branch:bb', type: 'branch', repo: 'tamird/buildbuddy', ref: 'topic', title: 'BB work' },
    { id: 'branch:gv', type: 'branch', repo: 'tamird/gvisor', ref: 'topic', title: 'Different work' },
  ], edges: [], groups: [] };
  context.registryInput.meta.repositories = { 'google/gvisor': 'copybara', [repo]: 'none' };
  context.snapshotInput.prs = [primary, external];
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /lacks repository inventory/);
  context.snapshotInput.repositories = context.registryInput.meta.repositories;
  context.document = { getElementById: () => ({ scrollTop: 0, scrollLeft: 0, querySelectorAll: () => [], scrollTo() {} }) };
  vm.runInContext('validateSnapshot(snapshotInput, registryInput); updateRegistryControls = () => {}; render = () => {}; setFreshness = () => {}; applyLive(snapshotInput, registryInput)', context);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(model.nodes.map(n => n.id).sort())', context)),
    ['branch:gv', 'pr:7', `pr:${repo}:7`]);
  context.externalInput = external; context.primaryInput = primary;
  vm.runInContext('conflictResults.set(prIdentity(externalInput), {head: "d".repeat(40), status: "open", state: "CONFLICTING", checkedAt: "2026-10-14T00:00:00Z"})', context);
  assert.equal(vm.runInContext('withConflictResult(primaryInput).head', context), primary.head);
  assert.equal(vm.runInContext('withConflictResult(externalInput).head', context), 'd'.repeat(40));
  context.document = { querySelector: () => null };
  context.AbortSignal = { timeout: () => undefined };
  let requestedURL;
  context.fetch = async url => { requestedURL = url; return { ok: true, json: async () => ({
    number: 7, html_url: external.url, head: { sha: external.head, ref: 'topic', repo: { full_name: 'tamird/buildbuddy' } },
    base: { sha: 'b'.repeat(40) }, mergeable: true, state: 'open', merged: false, draft: false,
  }) }; };
  vm.runInContext('drawDetails = () => {}; applyLive = () => {};', context);
  await vm.runInContext(`refreshConflict(7, 'buildbuddy-io/buildbuddy')`, context);
  assert.equal(requestedURL, `https://api.github.com/repos/${repo}/pulls/7`);
  assert.equal(vm.runInContext('withConflictResult(primaryInput).head', context), primary.head);
  external.url = primary.url;
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid PR inventory/);
});

test('direct-review approvals do not acquire Copybara actions', () => {
  const node = attentionNode(); node.repo = 'buildbuddy-io/buildbuddy'; node.github.repo = node.repo;
  node.github.importPolicy = 'none';
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  node.github.checks.state = 'PENDING'; node.github.checks.contexts[0].state = 'IN_PROGRESS';
  assert.deepEqual(idsFor(node), []);
  node.github.checks.state = 'FAILURE'; node.github.checks.contexts[0].state = 'FAILURE';
  assert.deepEqual(idsFor(node), ['author-public-checks']);
});

test('branchless graph endpoints reuse investigation records and reject missing groups', () => {
  const context = client();
  const investigation = { id: 'investigation:kernel', title: 'Select guest kernel', status: 'active',
    owner: 'agent', group: 'bb', summary: 'Unpublished work', updatedAt: context.registryInput.meta.updatedAt,
    evidence: [], findings: [] };
  context.registryInput = { ...context.registryInput, nodes: [{ id: 'external:kernel', type: 'issue' }],
    groups: [{ id: 'bb', label: 'BuildBuddy' }], investigations: [investigation],
    edges: [{ from: investigation.id, to: 'external:kernel', type: 'depends_on' }] };
  vm.runInContext('validateRegistry(registryInput)', context);
  const projection = JSON.parse(vm.runInContext('JSON.stringify(registryNodes(registryInput)[1])', context));
  assert.equal(projection.id, investigation.id);
  assert.match(projection.url, /view=investigations#investigation%3Akernel$/);
  assert.equal(projection.summary, investigation.summary);
  context.location = new URL(projection.url);
  const route = JSON.parse(vm.runInContext('JSON.stringify(readViewURL())', context));
  assert.equal(route.view, 'investigations'); assert.equal(route.selected, investigation.id);
  delete investigation.group;
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /invalid relationships/);
});


test('BuildBuddy approval gates retain ERROR without claiming executed failure or green checks', () => {
  const node = attentionNode(); node.github.repo = 'buildbuddy-io/buildbuddy'; node.github.importPolicy = 'none';
  const gate = { ...serviceStatus('Test (darwin_arm64)', 'ERROR'),
    description: 'Check requires approving review', url: 'https://app.buildbuddy.io' };
  node.github.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [gate] };
  const context = client(); context.nodeInput = node;
  assert.deepEqual(idsFor(node), ['maintainer-review']);
  assert.match(reasonsFor(node)[0].detail, /have not run/);
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Approval required');
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).passed', context), false);
  assert.equal(vm.runInContext('failureSources(nodeInput).length', context), 0);
  assert.equal(gate.state, 'ERROR');
  const mixed = structuredClone(node); mixed.github.checks.total = 2;
  mixed.github.checks.contexts.push(serviceStatus('linux', 'SUCCESS')); context.mixedInput = mixed;
  assert.equal(vm.runInContext('checkInfo(mixedInput.github).passed', context), false);
  assert.equal(vm.runInContext('checkInfo(mixedInput.github).text', context), 'Approval required');
  assert.deepEqual(idsFor(mixed), ['maintainer-review']);
  for (const change of [{ description: undefined }, { description: 'Check requires approving review later' },
    { url: 'https://example.com' }, { kind: 'check-run' }, { state: 'FAILURE' }]) {
    const other = structuredClone(node); Object.assign(other.github.checks.contexts[0], change);
    assert.ok(idsFor(other).includes('author-public-checks'), JSON.stringify(change));
  }
  const otherRepo = structuredClone(node); otherRepo.github.repo = 'google/gvisor';
  assert.ok(idsFor(otherRepo).includes('author-public-checks'));
  node.github.checks.complete = false;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).tone', context), 'bad');
  node.github.checks.complete = true;
  node.github.checks.contexts.push(serviceStatus('linux', 'FAILURE')); node.github.checks.total = 2;
  assert.ok(idsFor(node).includes('author-public-checks'));
  assert.ok(idsFor(node).includes('maintainer-review'));
});
