import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
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

test('every open snapshot PR shares its import decision with the periodic planner', () => {
  const context = client();
  const registry = JSON.parse(readFileSync(new URL('./registry.json', import.meta.url)));
  const snapshot = JSON.parse(readFileSync(new URL('./github-status.json', import.meta.url)));
  const script = `import json, sys
from work_states import public_import_disposition
from reapproval import reminder_plan
from datetime import datetime
registry, snapshot = json.load(sys.stdin)
nodes = {(n.get('repo'), n.get('number')): n for n in registry['nodes'] if n['type'] == 'pr'}
rows = []
for pr in snapshot['prs']:
    if pr['status'] != 'open': continue
    node = nodes.get((pr.get('repo'), pr['number']), {})
    disposition = public_import_disposition(pr, node.get('approvedPatch'), node.get('landedPatch'))
    plan = reminder_plan(pr, node, datetime.fromisoformat(snapshot['checkedAt'].replace('Z', '+00:00')), snapshot['owner'], frozenset())
    assert plan['publicImportDisposition'] == disposition
    rows.append([pr.get('repo'), pr['number'], disposition])
print(json.dumps(rows))`;
  const rows = JSON.parse(execFileSync('python3', ['-c', script], {
    cwd: new URL('.', import.meta.url), input: JSON.stringify([registry, snapshot]), encoding: 'utf8',
  }));
  assert.ok(rows.length > 0);
  context.registryInput = registry;
  vm.runInContext('registry = registryInput', context);
  const actions = [];
  for (const [repo, number, disposition] of rows) {
    const pr = snapshot.prs.find(item => item.repo === repo && item.number === number);
    const node = registry.nodes.find(item => item.type === 'pr' && item.repo === repo && item.number === number);
    pr.publicImportDisposition = disposition;
    context.nodeInput = { ...node, type: 'pr', github: pr };
    assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(publicImportDisposition(nodeInput))', context)), disposition, pr.url);
    const reasons = JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, registry.meta.owner))', context));
    const recoveryActions = ['maintainer-import-refresh', 'maintainer-reapproval', 'maintainer-reimport', 'maintainer-import-landed'];
    assert.deepEqual(reasons.filter(reason => recoveryActions.includes(reason.id)).map(reason => reason.id),
      recoveryActions.includes(disposition.action) ? [disposition.action] : [], pr.url);
    if (['maintainer-import-refresh', 'maintainer-reapproval', 'maintainer-reimport'].includes(disposition.action)) {
      assert.ok(reasons.some(reason => reason.id === disposition.action), pr.url);
      actions.push({ number: pr.number, action: disposition.action, exports: disposition.binding.imports.map(item => item.number) });
    }
    disposition.binding.sourceHead = '0'.repeat(40);
    assert.equal(vm.runInContext('publicImportDisposition(nodeInput)', context), null);
  }
  console.log(JSON.stringify({ publicImportAudit: { openPRs: rows.length, actions } }));
});

test('missing import labels remain actionable despite equivalent approval, then clear on relabel', () => {
  const context = client();
  const fixture = JSON.parse(readFileSync(new URL('./import-trigger-fixtures.json', import.meta.url)));
  for (const row of fixture.rows.filter(row => row.expectedAction === 'restore-import-label')) {
    context.nodeInput = { ...row.node, github: row.pr };
    // Obtain the same source-owned decision used by the alarm, not a test double.
    const script = `import json, sys
from work_states import public_import_disposition
row = json.load(sys.stdin)
print(json.dumps(public_import_disposition(row['pr'], row['node'].get('approvedPatch'))))`;
    row.pr.publicImportDisposition = JSON.parse(execFileSync('python3', ['-c', script], {
      cwd: new URL('.', import.meta.url), input: JSON.stringify(row), encoding: 'utf8',
    }));
    const reasons = JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
    assert.match(reasons.find(item => item.id === 'maintainer-reimport').detail, /restore that label/);
    row.pr.labels = ['ready to pull'];
    assert.equal(vm.runInContext('publicImportDisposition(nodeInput)', context), null);
    row.pr.publicImportDisposition = JSON.parse(execFileSync('python3', ['-c', script], {
      cwd: new URL('.', import.meta.url), input: JSON.stringify(row), encoding: 'utf8',
    }));
    const relabeled = JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
    assert.equal(relabeled.some(item => item.id === 'maintainer-reimport'), false);
    assert.match(relabeled.find(item => item.id === 'maintainer-unknown').detail, /Waiting for Copybara/);
  }
});

test('registry rejects workstreams that cannot be selected in the filter', () => {
  const context = client();
  const node = { id: 'branch:task', type: 'branch', group: 'runtime' };
  context.registryInput = { ...context.registryInput, nodes: [node], edges: [],
    groups: [{ id: 'runtime', label: 'Runtime & kernel' }] };
  vm.runInContext('validateRegistry(registryInput)', context);
  for (const group of ['runsc', 'state', 'kernel', 'netstack']) {
    node.group = group;
    assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid node group/);
  }
});

test('old open Copybara PRs call for reapproval of the current source', () => {
  const context = client();
  const fixture = JSON.parse(readFileSync(new URL('./import-trigger-fixtures.json', import.meta.url)));
  for (const row of fixture.openExportRows) {
    context.nodeInput = { ...row.node, github: row.pr };
    const script = `import json, sys
from work_states import public_import_disposition
row = json.load(sys.stdin)
print(json.dumps(public_import_disposition(row['pr'], row['node'].get('approvedPatch'))))`;
    row.pr.publicImportDisposition = JSON.parse(execFileSync('python3', ['-c', script], {
      cwd: new URL('.', import.meta.url), input: JSON.stringify(row), encoding: 'utf8',
    }));
    const reasons = JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
    const reapproval = reasons.find(item => item.id === 'maintainer-reapproval');
    assert.equal(!!reapproval, row.expectedAction === 'reapprove-amendment', String(row.pr.number));
    if (reapproval) {
      assert.equal(reapproval.title, 'Reapprove current source PR');
      assert.match(reapproval.detail, new RegExp(row.pr.head.slice(0, 12)));
      assert.match(reapproval.detail, new RegExp('#' + row.pr.imports[0].number));
      assert.equal(reasons.some(item => ['maintainer-import-refresh', 'maintainer-import-previous'].includes(item.id)), false);
      if (row.pr.number === 15940) assert.match(reapproval.detail, /success public checks \(15 reported\)/);
      for (const reviewer of ['EtiennePerot', 'lsfriend']) for (const labels of [[], ['ready to pull']]) {
        const current = structuredClone(row);
        current.pr.githubReviewDecision = 'REVIEW_REQUIRED';
        current.pr.labels = labels;
        const oldReview = current.pr.feedback.items.find(item => item.id === current.node.approvedPatch.reviewId);
        current.pr.feedback.items.push({ ...oldReview, id: 'fresh', author: { ...oldReview.author, login: reviewer },
          commit: { oid: current.pr.head }, submittedAt: current.observedAt, updatedAt: current.observedAt, body: '' });
        current.pr.publicImportDisposition = JSON.parse(execFileSync('python3', ['-c', script], {
          cwd: new URL('.', import.meta.url), input: JSON.stringify(current), encoding: 'utf8',
        }));
        context.nodeInput = { ...current.node, github: current.pr };
        const freshReasons = JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
        assert.equal(freshReasons.some(item => ['maintainer-reapproval', 'maintainer-import-refresh'].includes(item.id)), false);
        if (row.pr.number === 15940 && !labels.length) {
          assert.equal(freshReasons.find(item => item.id === 'maintainer-import-label').title, 'Restore ready to pull');
          assert.equal(freshReasons.some(item => item.title === 'Reapproved; waiting for Copybara'), false);
        } else {
          assert.ok(freshReasons.some(item => item.title === 'Reapproved; waiting for Copybara'));
        }
        const outsider = current.pr.feedback.items.at(-1);
        outsider.author = { login: 'outsider', __typename: 'User' };
        outsider.authorAssociation = 'NONE';
        current.pr.publicImportDisposition = JSON.parse(execFileSync('python3', ['-c', script], {
          cwd: new URL('.', import.meta.url), input: JSON.stringify(current), encoding: 'utf8',
        }));
        assert.equal(current.pr.publicImportDisposition.binding.currentApproval, false);
        assert.ok(JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context))
          .some(item => item.id === 'maintainer-reapproval'));
      }
    }
  }
});

test('cross-references remain visible evidence without creating graph relationships', () => {
  const context = client(), pr = attentionNode().github;
  context.snapshotInput.checkedAt = pr.checkedAt = '2026-10-21T02:54:53Z';
  const event = { id: 'reference-13826', createdAt: '2026-10-16T15:59:46Z', source: {
    __typename: 'PullRequest', number: 13826, repository: { nameWithOwner: 'buildbuddy-io/buildbuddy', isPrivate: false },
    url: 'https://github.com/buildbuddy-io/buildbuddy/pull/13826', title: 'Enable vhost-net',
    body: 'This supersedes #13803. <script>not markup</script>', author: { login: 'bduffany' },
    headRefOid: 'a'.repeat(40), updatedAt: '2026-10-16T16:00:00Z',
  } };
  pr.crossReferences = { complete: false, items: [event] };
  context.snapshotInput.prs = [pr];
  context.prInput = pr;
  const createElement = tag => ({ tag, children: [], append(...children) { this.children.push(...children); } });
  context.document = { createElement };
  context.parentInput = createElement('section');
  vm.runInContext('validateSnapshot(snapshotInput); appendCrossReferences(parentInput, prInput)', context);
  const [details, incomplete] = context.parentInput.children;
  assert.match(details.children[1].textContent, /do not establish a dependency, replacement or approval/);
  assert.equal(details.children[2].href, event.source.url);
  assert.equal(details.children[4].textContent, event.source.body);
  assert.match(incomplete.textContent, /inventory incomplete/);
  assert.equal(context.snapshotInput.prs.length, 1);
  for (const mutate of [
    reference => { reference.items.push(structuredClone(event)); },
    reference => { reference.items[0].source.url = 'https://github.com/other/repo/pull/13826'; },
    reference => { reference.items[0].source.headRefOid = 'unknown'; },
    reference => { reference.items[0].source.repository.isPrivate = true; },
    reference => { reference.complete = 'true'; },
  ]) {
    pr.crossReferences = structuredClone({ complete: true, items: [event] });
    mutate(pr.crossReferences);
    assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput)', context), /Invalid PR cross-references/);
  }
  delete pr.crossReferences;
  vm.runInContext('validateSnapshot(snapshotInput)', context);
});

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
  const expected = `git range-diff --creation-factor=999 ${before}^! ${after}^!`;
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
    assert.equal(command, expectedBefore ? `git range-diff --creation-factor=999 ${expectedBefore}^! ${after}^!` : null);
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

test('new registry decisions appear in table, DAG and counts before a provider refresh', () => {
  const nodes = ['nginx-checkpoint-http-readiness', 'benchmark-alpine'].map((name, index) => {
    const head = String(index + 1).repeat(40);
    return { id: `branch:${name}`, type: 'branch', title: name, repo: 'tamird/gvisor', head,
      contributorReview: { status: 'pending', head, url: `https://github.com/tamird/gvisor/commit/${head}` } };
  });
  nodes.push({ id: 'branch:approved', type: 'branch', title: 'Approved child', status: 'Awaiting prerequisites' });
  const { context, controls } = historyView(nodes);
  context.document.getElementById = id => {
    if (!controls.has(id)) controls.set(id, { value: '', checked: false, scrollTop: 0, scrollLeft: 0,
      querySelectorAll: () => [], scrollTo() {} });
    return controls.get(id);
  };
  vm.runInContext(`validateRegistry(registryInput); validateSnapshot(snapshotInput, registryInput);
    updateRegistryControls = () => {}; render = () => {}; setFreshness = () => {};
    applyLive(snapshotInput, registryInput); reasonFilters = new Set(['author-proposal']);`, context);
  const expected = nodes.slice(0, 2).map(node => node.id);
  for (const expression of ['tableNodes()', 'graphNodes()'])
    assert.deepEqual(JSON.parse(vm.runInContext(`JSON.stringify(${expression}.map(node => node.id))`, context)), expected);
  assert.equal(vm.runInContext('attentionGroups(tableNodes(), "tamird")[0].entries.length', context), 2);
  for (const node of vm.runInContext('tableNodes()', context)) {
    assert.equal(node.workStates['contributor-review'].scope, node.head);
    assert.equal(node.workStates['contributor-review'].since, context.registryInput.meta.updatedAt);
    assert.equal(node.workStates['contributor-review'].observedAt, context.registryInput.meta.updatedAt);
  }
  assert.deepEqual(context.snapshotInput.workStates, {});
});

test('startup keeps registry decisions visible when no GitHub snapshot is usable', async () => {
  const head = 'c'.repeat(40), branch = { id: 'branch:proposal', type: 'branch', title: 'Proposal', repo: 'tamird/gvisor', head,
    contributorReview: { status: 'pending', head, url: `https://github.com/tamird/gvisor/commit/${head}` } };
  const pr = { id: 'pr:1', type: 'pr', title: 'Published work', repo: 'google/gvisor', number: 1,
    url: 'https://github.com/google/gvisor/pull/1' };
  const { context } = historyView([branch, pr]);
  context.localStorage = { getItem: () => null };
  context.AbortSignal = { timeout: () => undefined };
  context.fetch = async url => url === 'registry.json'
    ? { ok: true, json: async () => context.registryInput } : { ok: false, status: 503 };
  vm.runInContext('updateRegistryControls = () => {}; initialize = () => {}; restoreView = () => false; render = () => {};', context);
  await vm.runInContext('start()', context);
  vm.runInContext('reasonFilters = new Set(["author-proposal"])', context);
  for (const expression of ['tableNodes()', 'graphNodes()'])
    assert.deepEqual(JSON.parse(vm.runInContext(`JSON.stringify(${expression}.map(node => node.id))`, context)), [branch.id]);
  assert.equal(vm.runInContext('model.nodes[0].workStates["contributor-review"].since', context), context.registryInput.meta.updatedAt);
  assert.equal(vm.runInContext('model.checkedAt', context), undefined);
  assert.equal(vm.runInContext('model.nodes[1].github', context), undefined);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(model.nodes[1].workStates)', context)), {});
});

test('registry decisions retain only same-candidate ages and never revive consumed decisions', () => {
  const context = client(), head = 'a'.repeat(40), next = 'b'.repeat(40);
  const node = { id: 'branch:proposal', type: 'branch', head,
    contributorReview: { status: 'pending', head, url: `https://github.com/tamird/gvisor/commit/${head}` } };
  const previous = { scope: head, since: '2026-10-12T10:00:00Z', observedAt: context.snapshotInput.checkedAt,
    basis: 'observed', qualifier: null };
  context.nodeInput = node;
  context.snapshotInput.workStates[node.id] = { 'contributor-review': previous };
  const record = () => vm.runInContext('stateRecords(snapshotInput, nodeInput)["contributor-review"]', context);
  assert.equal(record().since, previous.since);
  assert.equal(record().observedAt, context.registryInput.meta.updatedAt);
  // Publishing another candidate cannot reuse the earlier candidate's clock.
  node.head = node.contributorReview.head = next;
  node.contributorReview.url = `https://github.com/tamird/gvisor/commit/${next}`;
  assert.equal(record().scope, next);
  assert.equal(record().since, context.registryInput.meta.updatedAt);
  // Moving a branch without updating its decision does not authorize that head.
  node.head = head;
  assert.equal(record(), undefined);
  node.head = next;
  for (const status of ['approved', 'rejected']) {
    node.contributorReview.status = status;
    assert.equal(record(), undefined);
  }
  delete node.contributorReview;
  node.status = 'Withdrawn'; node.inactive = true;
  assert.equal(record(), undefined);
  assert.equal(vm.runInContext('historical(nodeInput)', context), true);
  node.contributorChangesRequested = { head: next };
  assert.equal(record(), undefined);
  assert.deepEqual(idsFor(node), ['author-feedback']);
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

test('outgoing review decisions remain visible on external PRs and across public head changes', () => {
  const context = client(), node = attentionNode();
  node.github.headOwner = 'EtiennePerot';
  node.github.checkedAt = context.snapshotInput.checkedAt;
  node.contributorReview = { status: 'pending', kind: 'review', head: node.github.head,
    title: 'Authorize the outgoing review', summary: 'Code findings addressed; measurements requested.',
    body: 'Code LGTM. Please include the measurement command.', url: node.url };
  const record = { scope: node.github.head, basis: 'observed', since: context.registryInput.meta.updatedAt,
    observedAt: context.registryInput.meta.updatedAt };
  context.registryInput = { ...context.registryInput, nodes: [{ ...node, repo: 'google/gvisor' }], edges: [], groups: [] };
  context.snapshotInput.prs = [node.github]; context.snapshotInput.workStates[node.id] = { 'contributor-review': record };
  context.nodeInput = node;
  vm.runInContext('validateRegistry(registryInput); validateSnapshot(snapshotInput, registryInput)', context);
  node.workStates = vm.runInContext('stateRecords(snapshotInput, nodeInput)', context);
  assert.equal(reasonsFor(node).find(reason => reason.id === 'author-proposal').owner, 'user');
  node.github.revisionChanged = true; node.github.head = 'd'.repeat(40);
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["contributor-review"].scope', context), record.scope);
  delete node.contributorReview;
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["contributor-review"]', context), undefined);
});

test('PR amendments keep the proposed commit and published base distinct', () => {
  const context = client(), node = attentionNode(), head = 'c'.repeat(40);
  node.repo = 'google/gvisor';
  node.github.checkedAt = context.snapshotInput.checkedAt;
  node.contributorReview = { status: 'pending', kind: 'amendment', head, baseHead: node.github.head,
    title: 'Review the amendment', summary: 'Forward source-only dependencies.',
    body: 'Review this exact proposed commit before updating the public PR.',
    url: `https://github.com/tamird/gvisor/commit/${head}` };
  const record = { scope: head, basis: 'observed', since: context.registryInput.meta.updatedAt,
    observedAt: context.registryInput.meta.updatedAt };
  context.registryInput = { ...context.registryInput, nodes: [node], edges: [], groups: [] };
  context.snapshotInput.prs = [node.github];
  context.snapshotInput.workStates[node.id] = { 'contributor-review': record };
  context.nodeInput = node;
  context.location = { href: 'https://tamird.github.io/gvisor/' };
  vm.runInContext('validateRegistry(registryInput); validateSnapshot(snapshotInput, registryInput)', context);
  node.workStates = vm.runInContext('stateRecords(snapshotInput, nodeInput)', context);
  assert.equal(reasonsFor(node).find(reason => reason.id === 'author-proposal').owner, 'user');
  assert.equal(vm.runInContext('contributorDecisionHeadChanged(nodeInput)', context), false);
  assert.equal(vm.runInContext('refinementComparison(nodeInput).command', context),
    `git range-diff --creation-factor=999 ${node.github.head}^! ${head}^!`);
  const groups = vm.runInContext('attentionGroups([nodeInput], "tamird", false, new Set(["author-proposal"]))', context);
  assert.equal(groups[0].entries[0].copyURL, `https://tamird.github.io/gvisor/?view=table&reason=author-proposal#${encodeURIComponent(node.id)}`);
  node.github.head = 'd'.repeat(40);
  assert.equal(vm.runInContext('contributorDecisionHeadChanged(nodeInput)', context), true);
  assert.equal(vm.runInContext('refinementComparison(nodeInput)', context), null);
  node.workStates = vm.runInContext('stateRecords(snapshotInput, nodeInput)', context);
  assert.equal(node.workStates['contributor-review'], undefined);
  assert.ok(!idsFor(node).includes('author-proposal'));
  const refresh = reasonsFor(node).find(reason => reason.id === 'agent-refresh');
  assert.match(refresh.detail, new RegExp(node.contributorReview.baseHead.slice(0, 9)));
  assert.match(refresh.detail, new RegExp(node.github.head.slice(0, 9)));
  for (const change of [{ baseHead: head }, { baseHead: 'unknown' }, { body: '' },
    { url: `https://github.com/tamird/other/commit/${head}` },
    { url: `https://github.com/other/gvisor/commit/${head}` }]) {
    const original = { ...node.contributorReview }; Object.assign(node.contributorReview, change);
    assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid contributor decision/);
    node.contributorReview = original;
  }
  for (const status of ['open', 'closed', 'merged']) {
    node.github.status = status;
    node.github.head = status === 'open' ? head : 'd'.repeat(40);
    node.workStates = vm.runInContext('stateRecords(snapshotInput, nodeInput)', context);
    assert.equal(node.workStates['contributor-review'], undefined);
    assert.equal(vm.runInContext('contributorDecision(nodeInput)', context), null);
    assert.equal(vm.runInContext('contributorAmendmentNeedsRefresh(nodeInput)', context), false);
    assert.ok(!idsFor(node).includes('author-proposal'));
    assert.ok(!idsFor(node).includes('agent-refresh'));
    assert.equal(vm.runInContext('historical(nodeInput)', context), status !== 'open');
  }
  delete node.contributorReview;
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["contributor-review"]', context), undefined);
});

test('outgoing decisions require reviewable text and the owning discussion', () => {
  const context = client();
  const node = { id: 'issue:2', type: 'issue', url: 'https://github.com/google/gvisor/issues/2',
    contributorReview: { status: 'pending', kind: 'reply', title: 'Authorize reply',
      summary: 'Reply about the experiment.', body: 'The measured result is attached.', url: 'https://github.com/google/gvisor/issues/2' } };
  context.registryInput = { ...context.registryInput, nodes: [node], edges: [], groups: [] };
  vm.runInContext('validateRegistry(registryInput)', context);
  for (const change of [{ body: '' }, { url: 'https://github.com/google/gvisor/issues/3' }, { kind: 'proposal' }]) {
    const original = { ...node.contributorReview }; Object.assign(node.contributorReview, change);
    assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid contributor decision/);
    node.contributorReview = original;
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

function sharedFixContext(importOnly = false) {
  const context = client(), node = attentionNode(16051), remedy = attentionNode(16048);
  node.repo = remedy.repo = node.github.repo = remedy.github.repo = 'google/gvisor';
  const checked = importOnly ? attentionImport(node, 16050) : node.github;
  if (importOnly) node.github.imports.push(checked);
  const signal = { number: checked.number, head: checked.head, id: 'gazelle-1', name: 'gazelle',
    workflowRun: 123, job: 456, jobURL: 'https://github.com/google/gvisor/actions/runs/123/job/456' };
  checked.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [
    { id: signal.id, name: signal.name, url: signal.jobURL, state: 'FAILURE', kind: 'check-run', appSlug: 'github-actions' }] };
  const fix = 'c'.repeat(40);
  node.ciRecovery = { kind: 'shared-source-fix', sourceHead: node.github.head, observedAt: '2026-10-12T12:00:00Z',
    remedy: remedy.id, fixCommit: fix, sourceContainsFix: false,
    ancestryURL: `https://github.com/google/gvisor/compare/${fix}...${node.github.head}`, checks: [signal] };
  node.followup = { owner: 'already-assigned', sourceHead: node.github.head };
  remedy.github.status = 'merged'; remedy.github.mergeCommit = fix; remedy.github.mergedAt = '2026-10-12T13:00:00Z';
  context.nodeInput = node; context.remedyInput = remedy; context.checkedInput = checked;
  context.registryInput = { ...context.registryInput, nodes: [node, remedy], groups: [],
    edges: [{ from: remedy.id, to: node.id, type: 'blocked_by' }] };
  vm.runInContext('registry = registryInput; model = { nodes: registryInput.nodes };', context);
  return context;
}

test('merged CI remedy releases an exact source or import failure despite an existing owner', () => {
  for (const importOnly of [false, true]) {
    const context = sharedFixContext(importOnly);
    vm.runInContext('validateRegistry(registryInput)', context);
    const recovery = () => vm.runInContext('sourceFixRecovery(nodeInput)?.state', context);
    const reasons = () => JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
    assert.equal(recovery(), 'ready');
    assert.ok(reasons().some(reason => reason.id === 'author-ci-remedy' && reason.owner === 'agent'));
    assert.ok(reasons().some(reason => reason.id === (importOnly ? 'maintainer-import-checks' : 'author-public-checks')));
    context.remedyInput.github.status = 'open';
    assert.equal(recovery(), 'waiting');
    assert.ok(!reasons().some(reason => reason.id === 'author-ci-remedy'));
  }
});

test('an open remedy releases inspection without predicting its future merge commit', () => {
  const context = sharedFixContext(), recovery = context.nodeInput.ciRecovery;
  context.remedyInput.github.status = 'open';
  delete recovery.fixCommit; delete recovery.sourceContainsFix; delete recovery.ancestryURL;
  vm.runInContext('validateRegistry(registryInput)', context);
  assert.equal(vm.runInContext('sourceFixRecovery(nodeInput).state', context), 'waiting');
  context.remedyInput.github.status = 'merged';
  assert.equal(vm.runInContext('sourceFixRecovery(nodeInput).state', context), 'inspect');
  assert.ok(vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "author-ci-remedy")', context));
  recovery.fixCommit = 'd'.repeat(40); recovery.sourceContainsFix = false;
  recovery.ancestryURL = `https://github.com/google/gvisor/compare/${recovery.fixCommit}...${recovery.sourceHead}`;
  assert.equal(vm.runInContext('sourceFixRecovery(nodeInput).state', context), 'inspect');
  recovery.fixCommit = context.remedyInput.github.mergeCommit;
  recovery.ancestryURL = `https://github.com/google/gvisor/compare/${recovery.fixCommit}...${recovery.sourceHead}`;
  assert.equal(vm.runInContext('sourceFixRecovery(nodeInput).state', context), 'ready');
  recovery.sourceContainsFix = true;
  assert.equal(vm.runInContext('sourceFixRecovery(nodeInput)', context), null);
});

test('stale or superseding evidence cannot release a rebase and never hides failures', () => {
  for (const mutate of [
    c => { c.nodeInput.github.head = 'd'.repeat(40); },
    c => { c.nodeInput.head = 'd'.repeat(40); },
    c => { c.nodeInput.github.revisionChanged = true; },
    c => { c.nodeInput.ciRecovery.sourceContainsFix = true; },
    c => { c.remedyInput.github.status = 'closed'; },
    c => { c.checkedInput.checks.complete = false; },
    c => { c.checkedInput.checks.contexts[0].id = 'new-failure'; },
    c => { c.checkedInput.checks.contexts[0].state = 'SUCCESS'; },
    c => { c.checkedInput.checks.contexts[0].state = 'PENDING'; },
    c => { c.checkedInput.checks.contexts[0].url += '0'; },
    c => { c.checkedInput.checks.contexts.push({ ...c.checkedInput.checks.contexts[0], id: 'another' }); c.checkedInput.checks.total++; },
    c => { c.checkedInput.matchesSourceHead = false; },
  ]) {
    const context = sharedFixContext(true); mutate(context);
    assert.equal(vm.runInContext('sourceFixRecovery(nodeInput)', context), null, mutate.toString());
    assert.ok(!vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "author-ci-remedy")', context));
  }
  const context = sharedFixContext();
  context.checkedInput.checks.contexts[0].id = 'new-failure';
  assert.ok(vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "author-public-checks")', context));
  for (const mutate of [
    c => { c.registryInput.edges = []; },
    c => { c.nodeInput.ciRecovery.sourceContainsFix = 'unknown'; },
    c => { c.nodeInput.ciRecovery.ancestryURL = 'https://github.com/google/gvisor'; },
    c => { c.nodeInput.ciRecovery.checks[0].head = 'unknown'; },
  ]) {
    const changed = sharedFixContext(); mutate(changed);
    assert.throws(() => vm.runInContext('validateRegistry(registryInput)', changed), /Invalid shared source fix/, mutate.toString());
  }
});

function triagedContext() {
  const context = client();
  const fixture = JSON.parse(readFileSync(new URL('./attention_dispositions_fixture.json', import.meta.url)));
  context.nodesInput = fixture.nodes;
  context.registryInput = { meta: { repo: 'google/gvisor', owner: 'tamird', updatedAt: fixture.observedAt,
    repositories: { 'google/gvisor': 'copybara', 'buildbuddy-io/buildbuddy': 'none' } },
    nodes: context.nodesInput, groups: [], edges: [] };
  vm.runInContext('registry = registryInput; model = { nodes: nodesInput }; validateRegistry(registryInput)', context);
  return context;
}
test('retained triaged reasons consistently move to external follow-up without rewriting raw signals', () => {
  const context = triagedContext(), original = JSON.stringify(context.nodesInput.map(node => node.github));
  const sourceNodes = context.nodesInput.filter(node => node.followup?.attentionDispositions);
  context.sourcesInput = sourceNodes;
  for (const node of sourceNodes) {
    context.nodeInput = node;
    const record = node.followup.attentionDispositions[0];
    assert.equal(vm.runInContext('attentionDisposition(nodeInput, nodeInput.followup.attentionDispositions[0].reason)?.title', context), record.title);
    let reasons = JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
    assert.ok(!reasons.some(reason => reason.owner === 'agent'), node.id);
    assert.ok(reasons.some(reason => reason.id === 'maintainer-triaged' && reason.detail.includes(record.nextAction)), node.id);
    delete node.followup.attentionDispositions;
    reasons = JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird"))', context));
    assert.ok(reasons.some(reason => reason.id === record.reason && reason.owner === 'agent'), node.id);
    node.followup.attentionDispositions = [record];
  }
  context.nodeInput = sourceNodes.find(node => node.number === 13803);
  assert.ok(!vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "maintainer-review")', context));
  const replacement = context.nodesInput.find(node => node.number === 13826);
  replacement.github.status = 'merged';
  assert.ok(vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "maintainer-review")', context));
  assert.ok(vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "author-conflicts")', context));
  replacement.github.status = 'open';
  assert.equal(JSON.stringify(context.nodesInput.map(node => node.github)), original);
  const groups = JSON.parse(vm.runInContext('JSON.stringify(attentionGroups(sourcesInput, "tamird"))', context));
  assert.ok(groups.every(group => group.owner === 'maintainer'));
  assert.equal(groups.reduce((count, group) => count + group.entries.length, 0), 3);
  const triaged = JSON.parse(vm.runInContext('JSON.stringify(attentionGroups(sourcesInput, "tamird", true, new Set(["maintainer-triaged"])))', context));
  assert.equal(triaged.length, 1); assert.equal(triaged[0].id, 'maintainer-triaged');
  assert.equal(triaged[0].entries.length, 3);
  const text = vm.runInContext('attentionText(attentionGroups(sourcesInput, "tamird"), registryInput.meta.updatedAt)', context);
  assert.match(text, /Needs maintainer attention: Awaiting maintainer follow-up/);
  assert.ok(!text.includes('Agent work'));
  assert.equal(text.split('https://').length - 1, 3);
  const elements = Object.fromEntries(['show-resolved', 'state-filters', 'all-reasons', 'legacy-state-filter',
    'attention-agent-summary', 'attention-user-summary', 'attention-maintainer-summary'].map(id => [id, {
      checked: false, querySelectorAll: () => [], setAttribute() {},
    }]));
  context.document = { getElementById: id => elements[id] };
  vm.runInContext('initializing = false; matchesBase = node => sourcesInput.includes(node); drawStateFilters()', context);
  assert.equal(elements['attention-agent-summary'].textContent, 'Agent work · 0');
  assert.equal(elements['attention-maintainer-summary'].textContent, 'Needs maintainer attention · 3');
  context.nodeInput = sourceNodes[0];
  assert.equal(vm.runInContext('reasonFilters = new Set(["maintainer-triaged"]); matches(nodeInput)', context), true);
  assert.equal(vm.runInContext('reasonFilters = new Set(["maintainer-import-checks"]); matches(nodeInput)', context), false);
});

test('new feedback, changed approval and explicit assignments invalidate the reviewed observation', () => {
  for (const mutate of [
    node => { node.github.head = 'f'.repeat(40); },
    node => { node.github.revisionChanged = true; },
    node => { node.github.feedback.complete = false; },
    node => { node.github.threads.complete = false; },
    node => { node.github.reviewsComplete = false; },
    node => { node.github.approvedAt = '2026-10-21T08:00:00Z'; },
    node => { node.github.feedback.items.find(item => item.kind === 'inline').body = 'Please change this implementation.'; },
    node => { node.github.feedback.items.push({ kind: 'inline', id: 'new-reply', threadId: 'new-thread',
      author: { login: 'reviewer' }, body: 'Another issue', createdAt: '2026-10-21T08:00:00Z', isResolved: false }); },
    node => { const comment = node.github.feedback.items.find(item => item.kind === 'comment');
      node.followup.owner = 'assigned-owner'; node.followup.actionableComment = { head: node.github.head, commentId: comment.id, url: comment.url }; },
    node => { delete node.followup.attentionDispositions[0].binding; },
  ]) {
    const context = triagedContext(), node = context.nodesInput.find(node => node.number === 15670);
    context.nodeInput = node; mutate(node);
    assert.equal(vm.runInContext('attentionDisposition(nodeInput, "author-feedback")', context), null, mutate.toString());
    assert.ok(vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.owner === "agent")', context), mutate.toString());
  }
});

test('rerun and import identities or replacement changes reopen triaged work', () => {
  for (const [number, reason, mutations] of [
    [15499, 'maintainer-import-checks', [
      node => { node.github.imports[0].head = 'f'.repeat(40); },
      node => { node.github.imports[0].checks.contexts.find(check => check.state === 'FAILURE').id = 'new-same-status-run'; },
      node => { node.github.imports[0].checks.contexts.find(check => check.state === 'FAILURE').url += '?attempt=2'; },
      node => { node.github.imports[0].checks.complete = false; },
      node => { node.github.importsComplete = false; },
      node => { node.github.imports[0].status = 'closed'; },
    ]],
    [13803, 'author-conflicts', [
      (node, context) => { context.nodesInput.find(item => item.number === 13826).github.status = 'merged'; },
      (node, context) => { context.nodesInput.find(item => item.number === 13826).github.status = 'closed'; },
      (node, context) => { context.nodesInput.find(item => item.number === 13826).github.head = 'f'.repeat(40); },
      (node, context) => { context.nodesInput.find(item => item.number === 13826).github.repo = 'other/repo'; },
      (node, context) => { context.nodesInput.splice(context.nodesInput.findIndex(item => item.number === 13826), 1); },
    ]],
  ]) for (const mutate of mutations) {
    const context = triagedContext(), node = context.nodesInput.find(node => node.number === number);
    context.nodeInput = node; mutate(node, context);
    assert.equal(vm.runInContext(`attentionDisposition(nodeInput, "${reason}")`, context), null, mutate.toString());
  }
  const context = triagedContext(), node = context.nodesInput.find(node => node.number === 15670);
  context.nodeInput = node;
  node.github.checkedAt = '2026-10-22T08:00:00Z'; node.github.mergeabilityCheckedAt = node.github.checkedAt;
  assert.ok(vm.runInContext('attentionDisposition(nodeInput, "author-feedback")', context));
  node.github.checks.contexts.push({ id: 'new-source-failure', name: 'new test', state: 'FAILURE', purpose: 'validation', kind: 'check-run' });
  node.github.checks.total++; node.github.checks.state = 'FAILURE';
  assert.ok(vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "author-public-checks")', context));
  assert.ok(vm.runInContext('attentionReasons(nodeInput, "tamird").some(reason => reason.id === "maintainer-triaged")', context));
});

function assignedCommentNode() {
  const node = attentionNode(15926); node.repo = 'google/gvisor';
  const comment = { kind: 'comment', id: 'comment-1', url: `${node.url}#issuecomment-6295602307`,
    author: { login: 'reviewer' }, body: 'Please check the partial-segment behavior.' };
  node.github.feedback.items.push(comment); node.github.imports = [attentionImport(node)];
  node.followup = { owner: 'hostinet_delivery', sourceHead: node.github.head,
    actionableComment: { head: node.github.head, commentId: comment.id, url: comment.url } };
  return node;
}

test('a queued nit leaves the approved import on its normal path and reopens on new feedback', () => {
  const node = attentionNode(16496), pr = node.github, context = client();
  pr.imports = [attentionImport(node, 16542)];
  pr.threads = { complete: true, unresolved: 1, total: 1 };
  const comment = { kind: 'inline', id: 'nit', threadId: 'thread', author: { login: 'reviewer' },
    url: `${node.url}#discussion_r1`, body: 'Mention the reserved thread.', isResolved: false };
  pr.feedback.items = [comment]; context.nodeInput = node;
  assert.ok(idsFor(node).includes('author-feedback'));
  const binding = JSON.parse(vm.runInContext('JSON.stringify(attentionDispositionEvidence(nodeInput, "author-feedback"))', context));
  node.followup = { attentionDispositions: [{ reason: 'author-feedback', action: 'batch-cleanup',
    title: 'Queued for cleanup', nextAction: 'Reply on the original thread after the cleanup.', evidence: [comment.url], binding }] };
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  comment.body = 'Please fix this before import.';
  assert.ok(idsFor(node).includes('author-feedback'));
  comment.body = 'Mention the reserved thread.';
  const reply = { ...comment, id: 'reply', author: { login: 'tamird' }, body: 'Fixed in the cleanup commit.', createdAt: '2026-10-28T13:00:00Z' };
  comment.createdAt = '2026-10-28T00:00:00Z';
  pr.feedback.items.push(reply);
  assert.ok(idsFor(node).includes('maintainer-response')); // The reply is not bound yet.
  node.followup.attentionDispositions[0].binding = JSON.parse(vm.runInContext('JSON.stringify(attentionDispositionEvidence(nodeInput, "author-feedback"))', context));
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  pr.feedback.items.push({ ...comment, id: 'followup', body: 'Found another issue.', createdAt: '2026-10-28T14:00:00Z' });
  assert.ok(idsFor(node).includes('author-feedback'));
});

test('assigned top-level feedback blocks merge attention without rewriting approval', () => {
  const node = assignedCommentNode(), assignment = node.followup.actionableComment;
  delete node.followup.actionableComment;
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  node.followup.actionableComment = assignment;
  const original = JSON.stringify(node.github);
  assert.deepEqual(idsFor(node), ['author-feedback']);
  const reason = reasonsFor(node)[0];
  assert.equal(reason.owner, 'agent'); assert.match(reason.detail, /hostinet_delivery/);
  assert.ok(reason.detail.includes(assignment.url));
  const context = client(); context.nodeInput = node;
  context.snapshotInput.workStates[node.id] = { 'waiting-merge': { scope: node.github.head, since: null, basis: 'unknown' } };
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["waiting-merge"]', context), undefined);
  assert.ok(context.snapshotInput.workStates[node.id]['waiting-merge']);
  assert.equal(JSON.stringify(node.github), original);
  node.github.head = 'b'.repeat(40); node.followup.sourceHead = node.github.head;
  assert.ok(!idsFor(node).includes('author-feedback'));
  assignment.head = node.github.head;
  assert.ok(idsFor(node).includes('author-feedback'));
  delete node.followup.actionableComment;
  assert.ok(!idsFor(node).includes('author-feedback'));
  for (const mutate of [
    node => { node.github.feedback.items[0].id = 'another-comment'; },
    node => { node.github.feedback.items[0].url += '0'; },
    node => { node.github.feedback.items[0].kind = 'review'; },
    node => { node.github.feedback.items = []; },
    node => { node.github.feedback.complete = false; },
    node => { node.github.revisionChanged = true; },
    node => { node.github.status = 'closed'; },
  ]) {
    const changed = assignedCommentNode(); mutate(changed);
    assert.ok(!idsFor(changed).includes('author-feedback'), mutate.toString());
  }
});

test('actionable comment assignments require an owner and exact PR evidence', () => {
  for (const mutate of [null,
    node => { node.followup.actionableComment = null; },
    node => { node.followup.owner = ''; },
    node => { node.followup.actionableComment.head = 'unknown'; },
    node => { node.followup.actionableComment.commentId = ''; },
    node => { node.followup.actionableComment.url = 'https://github.com/google/gvisor/pull/1#issuecomment-1'; },
    node => { node.type = 'branch'; },
  ]) {
    const context = client(); context.nodeInput = assignedCommentNode();
    context.registryInput = { meta: { repo: 'google/gvisor', owner: 'tamird', updatedAt: '2026-10-13T18:00:00Z' },
      nodes: [context.nodeInput], groups: [], edges: [] };
    if (mutate) {
      mutate(context.nodeInput);
      assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid actionable comment/, mutate.toString());
    } else vm.runInContext('validateRegistry(registryInput)', context);
  }
});

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

test('internal import success and opaque feedback failure remain distinct from public checks', () => {
  const node = attentionNode(16097), context = client();
  node.github.checks.contexts.push(serviceStatus('import/copybara', 'SUCCESS'), serviceStatus('feedback/copybara', 'SUCCESS'));
  node.github.checks.total = 3;
  context.nodeInput = node;
  assert.equal(vm.runInContext('STATE_LABELS["awaiting-import"]', context), 'Awaiting public export');
  assert.equal(reasonsFor(node)[0].title, 'Waiting for public export');
  assert.match(reasonsFor(node)[0].detail, /successful internal import/);
  node.github.checks.contexts[2].state = 'FAILURE';
  node.github.checks.state = 'FAILURE';
  assert.equal(reasonsFor(node)[0].id, 'maintainer-internal-feedback');
  assert.match(reasonsFor(node)[0].detail, /No public log URL.*internal diagnostics/);
  assert.ok(!idsFor(node).includes('author-public-checks'));
  node.github.checks.contexts[0].state = 'FAILURE';
  assert.equal(reasonsFor(node)[0].id, 'author-public-checks');
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

test('canceled Buildkite runs remain distinct from CI configuration and failed tests; other failures still need diagnosis', () => {
  const node = attentionNode(16374), imported = attentionImport(node, 16470);
  node.github.imports = [imported];
  const canceled = { ...serviceStatus('buildkite/first-pass', 'FAILURE'),
    description: 'Build #50 canceled by Ryan El Kochta', url: 'https://buildkite.com/gvisor/first-pass/builds/50' };
  imported.checks = { complete: true, state: 'FAILURE', total: 1, contexts: [canceled] };
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-ci-canceled').detail, /Import #16470: Build #50 canceled/);
  assert.ok(!idsFor(node).includes('maintainer-import-checks'));
  assert.ok(!idsFor(node).includes('maintainer-merge'));
  node.github.checks.contexts.push(serviceStatus('feedback/copybara', 'FAILURE'));
  node.github.checks.total++;
  node.github.checks.state = 'FAILURE';
  assert.ok(idsFor(node).includes('maintainer-internal-feedback'));
  assert.ok(idsFor(node).includes('maintainer-ci-canceled'));
  imported.checks.contexts.push(serviceStatus('generate', 'FAILURE'));
  imported.checks.total++;
  const failed = reasonsFor(node).find(reason => reason.id === 'maintainer-import-checks').detail;
  assert.match(failed, /generate: FAILURE/);
  assert.doesNotMatch(failed, /buildkite\/first-pass/);

  const context = client(); context.nodeInput = node;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github.imports[0]).text', context), 'Failure');
  imported.checks.contexts.pop(); imported.checks.total--;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github.imports[0]).text', context), 'Canceled');
  assert.equal(vm.runInContext('checkInfo(nodeInput.github.imports[0]).passed', context), false);
  assert.equal(vm.runInContext('failureSources(nodeInput).length', context), 0);
  node.github.mergeable = 'CONFLICTING';
  assert.equal(reasonsFor(node)[0].id, 'author-conflicts');
  assert.equal(imported.mergeable, 'MERGEABLE');
  node.github.mergeable = 'MERGEABLE';

  node.github.imports = [];
  node.github.checks = { complete: true, state: 'FAILURE', total: 1, contexts: [canceled] };
  assert.match(reasonsFor(node).find(reason => reason.id === 'maintainer-ci-canceled').detail, /Source #16374/);
  assert.ok(!idsFor(node).includes('author-public-checks'));
  canceled.url = 'https://buildkite.com/gvisor/first-pass/builds/51';
  assert.ok(idsFor(node).includes('author-public-checks'));
  canceled.url = 'https://buildkite.com/gvisor/first-pass/builds/50';
  canceled.description = 'Build #50 failed (33 minutes, 41 seconds)';
  assert.ok(idsFor(node).includes('author-public-checks'));
  assert.ok(!idsFor(node).includes('maintainer-ci-canceled'));
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

test('closed exports retain unresolved public failures without reviving canceled or superseded runs', () => {
  const node = attentionNode(16382), imported = attentionImport(node, 16385);
  imported.status = 'closed'; node.github.imports = [imported];
  node.github.checks.contexts.push(serviceStatus('feedback/copybara', 'FAILURE'));
  node.github.checks.total++; node.github.checks.state = 'FAILURE';
  imported.checks = { complete: true, state: 'FAILURE', total: 1, contexts: [{
    ...serviceStatus('buildkite/first-pass', 'FAILURE'), description: 'Build #56 canceled by Ryan El Kochta',
    url: 'https://buildkite.com/gvisor/first-pass/builds/56',
  }] };
  assert.deepEqual(idsFor(node), ['maintainer-reimport', 'maintainer-internal-feedback']);
  assert.match(reasonsFor(node)[0].detail, /#16385 closed without merging/);
  imported.checks.contexts[0].description = 'Build #56 failed';
  assert.equal(reasonsFor(node)[0].id, 'maintainer-import-checks');
  const context = client(); context.nodeInput = node;
  assert.equal(vm.runInContext('failureSources(nodeInput)[0].number', context), 16385);
  const replacement = attentionImport(node, 16520); node.github.imports.push(replacement);
  assert.ok(!idsFor(node).includes('maintainer-import-checks'));
  assert.equal(vm.runInContext('failureSources(nodeInput).length', context), 0);
  replacement.status = 'merged';
  assert.ok(!idsFor(node).includes('maintainer-import-checks'));
  assert.equal(reasonsFor(node)[0].id, 'maintainer-import-landed');
});

test('a bound infrastructure diagnosis names the retry and expires on new failure evidence', () => {
  const context = triagedContext(), node = context.nodesInput.find(node => node.number === 15499);
  const disposition = node.followup.attentionDispositions[0];
  disposition.action = 'ci-infrastructure-retry'; context.nodeInput = node;
  vm.runInContext('validateRegistry(registryInput)', context);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(attentionReasons(nodeInput, "tamird").map(reason => reason.id))', context)), ['maintainer-ci-infrastructure']);
  const text = vm.runInContext('attentionText(attentionGroups([nodeInput], "tamird"), registryInput.meta.updatedAt)', context);
  assert.match(text, /CI infrastructure retry/); assert.doesNotMatch(text, /Awaiting maintainer follow-up/);
  disposition.action = 'guess-from-description';
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid attention disposition/);
  disposition.action = 'ci-infrastructure-retry';
  node.github.imports[0].checks.contexts.find(check => check.state === 'FAILURE').id = 'new-failure';
  assert.equal(vm.runInContext('attentionReasons(nodeInput, "tamird")[0].id', context), 'maintainer-import-checks');
});

function sourceAuthenticationFailure() {
  const node = attentionNode(13849);
  node.repo = 'buildbuddy-io/buildbuddy'; node.id = 'pr:buildbuddy-io/buildbuddy:13849';
  node.url = 'https://github.com/buildbuddy-io/buildbuddy/pull/13849';
  node.ciRecovery = { kind: 'source-check-authentication', reason: 'missing-api-key', sourceHead: node.github.head,
    observedAt: '2026-10-13T17:35:00Z', checks: [{ id: 'source-check', name: 'build', workflowRun: 39303727616,
      job: 118523915013, jobURL: 'https://github.com/buildbuddy-io/buildbuddy/actions/runs/39303727616/job/118523915013' },
      { id: 'push-source-check', name: 'build', workflowRun: 39303727617,
        job: 118523915014, jobURL: 'https://github.com/buildbuddy-io/buildbuddy/actions/runs/39303727617/job/118523915014' }] };
  node.github.checks = { complete: true, state: 'FAILURE', total: 2, contexts: node.ciRecovery.checks.map(signal => (
    { id: signal.id, name: signal.name, kind: 'check-run', appSlug: 'github-actions', state: 'FAILURE', purpose: 'validation', url: signal.jobURL }
  )) };
  return node;
}

test('proven same-name source authentication failures are maintainer work without hiding unrelated source failures', () => {
  const node = sourceAuthenticationFailure();
  const reason = reasonsFor(node).find(reason => reason.id === 'maintainer-ci-retry');
  assert.equal(reason.owner, 'maintainer'); assert.equal(reason.title, 'Maintainer CI configuration');
  assert.match(reason.detail, /missing API key/);
  assert.ok(!idsFor(node).includes('author-public-checks'));
  assert.ok(reasonsFor(node).every(reason => reason.owner !== 'agent'));
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
    node => { node.github.checks.contexts[0].name = 'renamed'; },
    node => { node.github.checks.contexts.pop(); node.github.checks.total--; },
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
  assert.equal(vm.runInContext('ATTENTION_OWNERS.user', context), 'Needs your attention');
});

test('owned branch work routes to the agent without inventing a contributor decision', () => {
  const head = 'c'.repeat(40), node = { id: 'branch:cookie', type: 'branch', repo: 'tamird/gvisor',
    status: 'Agent work', head, followup: { owner: 'root', sourceHead: head,
      nextAction: 'Correct timestamp parity and execute the omitted e2e owner.' } };
  const context = client(); context.nodeInput = node;
  context.snapshotInput.workStates[node.id] = { 'contributor-review': { scope: head, basis: 'unknown', since: null } };
  node.workStates = vm.runInContext('stateRecords(snapshotInput, nodeInput)', context);
  assert.equal(node.workStates['contributor-review'], undefined);
  const reasons = reasonsFor(node);
  assert.deepEqual(reasons.map(reason => [reason.id, reason.owner]), [['agent-branch-work', 'agent']]);
  assert.match(reasons[0].detail, /root: Correct timestamp parity/);
  assert.equal(vm.runInContext('attentionGroups([nodeInput], "tamird")[0].id', context), 'agent-branch-work');
  for (const mutate of [
    item => { item.followup.sourceHead = 'd'.repeat(40); },
    item => { item.head = item.followup.sourceHead = 'unknown'; },
    item => { item.followup.owner = ''; },
    item => { item.followup.nextAction = ''; },
    item => { item.repo = 'someone/gvisor'; },
    item => { item.status = 'Prepared proposal'; },
  ]) {
    const stale = structuredClone(node); mutate(stale);
    assert.ok(!reasonsFor(stale).some(reason => reason.id === 'agent-branch-work'));
  }
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

test('agreed release wait leaves the maintainer queue; new feedback and upstream versions surface', () => {
  const node = attentionNode(), pr = node.github;
  node.releaseWait = { head: pr.head, checkedVersion: 'v5.6.1', repository: 'docker/compose',
    confirmedAt: '2026-10-14T00:00:00Z', nextAction: 'Verify the official dind image.' };
  pr.releaseVersion = 'v5.6.1'; pr.reviewDecision = 'CHANGES_REQUESTED';
  pr.feedback.items = [{ kind: 'review', state: 'CHANGES_REQUESTED', commit: { oid: 'b'.repeat(40) },
    id: 'request', author: { login: 'reviewer' }, submittedAt: '2026-10-12T10:00:00Z' }];
  assert.deepEqual(idsFor(node), ['upstream-release']);
  const c = client(); c.nodeInput = node;
  c.snapshotInput.workStates[node.id] = { 'changes-requested': { scope: pr.head } };
  assert.deepEqual(Object.keys(vm.runInContext('stateRecords(snapshotInput, nodeInput)', c)), ['waiting-release']);
  for (const version of ['v5.6.2', null]) {
    pr.releaseVersion = version;
    assert.deepEqual(idsFor(node), ['agent-release-check']);
  }
  pr.releaseVersion = 'v5.6.1';
  const url = pr.url + '#issuecomment-123';
  pr.feedback.items.push({ kind: 'comment', author: { login: 'reviewer' }, url, createdAt: '2026-10-15T09:00:00Z' });
  assert.deepEqual(idsFor(node), ['author-feedback']);
  assert.ok(reasonsFor(node)[0].detail.includes(url));
  pr.feedback.items.pop();
  pr.feedback.items.push({ kind: 'inline', threadId: 'later', author: { login: 'reviewer' },
    createdAt: '2026-10-15T10:00:00Z', isResolved: false });
  pr.threads = { complete: true, total: 1, unresolved: 1 };
  assert.deepEqual(idsFor(node), ['author-feedback']);
});

test('a closed export of a verified unchanged patch calls for re-import, not review', () => {
  const node = attentionNode(), pr = node.github;
  node.repo = pr.repo = 'google/gvisor';
  const old = 'b'.repeat(40);
  pr.githubReviewDecision = 'APPROVED'; pr.reviewDecision = 'REVIEW_REQUIRED';
  pr.headIntroducedAt = '2026-10-10T11:00:00Z'; pr.reviewRequestsComplete = true; pr.reviewRequests = [];
  pr.feedback.items = [{ kind: 'review', id: 'review-1', state: 'APPROVED', author: { login: 'reviewer' },
    submittedAt: '2026-10-09T12:00:00Z', commit: { oid: pr.head } }];
  const attempt = attentionImport(node);
  Object.assign(attempt, { sourceHead: old, matchesSourceHead: false, status: 'closed' });
  pr.imports = [attempt];
  assert.deepEqual(idsFor(node), ['agent-rebase-check']);
  node.approvedPatch = { head: pr.head, approvedHead: old, reviewId: 'review-1', reviewer: 'reviewer',
    submittedAt: '2026-10-09T12:00:00Z', verifiedAt: '2026-10-13T10:00:00Z',
    method: 'stable-patch-id', oldPatchId: 'c'.repeat(40), newPatchId: 'c'.repeat(40) };
  assert.deepEqual(idsFor(node), ['maintainer-reimport']);
  assert.match(reasonsFor(node)[0].detail, new RegExp('#' + attempt.number));
  pr.feedback.items.push({ kind: 'review', id: 'new-approval', state: 'APPROVED', body: '',
    submittedAt: '2026-10-14T10:00:00Z', author: { login: 'reviewer' }, commit: { oid: pr.head } });
  assert.deepEqual(idsFor(node), ['maintainer-reimport']);
  pr.feedback.items.at(-1).body = 'Approved, but wait for the dependency.';
  assert.ok(!idsFor(node).includes('maintainer-reimport'));
  pr.feedback.items.pop();
  attempt.status = 'open';
  assert.deepEqual(idsFor(node), ['maintainer-import-previous']);
  const context = client(); context.nodeInput = node;
  assert.match(vm.runInContext('attentionText(attentionGroups([nodeInput], "tamird"), "2026-10-13T10:00:00Z")', context), /Waiting for earlier export to merge/);
  attempt.checks = { complete: true, total: 1, state: 'FAILURE', contexts: [serviceStatus('buildkite/pipeline', 'FAILURE')] };
  assert.equal(reasonsFor(node)[0].id, 'maintainer-import-checks');
  attempt.status = 'closed';
  assert.deepEqual(idsFor(node), ['maintainer-import-checks', 'maintainer-reimport']);
  const closedContext = client(); closedContext.nodeInput = node;
  assert.equal(vm.runInContext('attentionGroups([nodeInput], "tamird", true, new Set(["maintainer-reimport"]))[0].id', closedContext), 'maintainer-reimport');
  attempt.status = 'open';
  node.followup = { attentionDispositions: [{ reason: 'maintainer-import-checks', action: 'ci-infrastructure-retry',
    title: 'Dependency download failed', nextAction: 'Retry this job', evidence: [node.url] }] };
  const disposition = node.followup.attentionDispositions[0];
  disposition.binding = JSON.parse(vm.runInContext('JSON.stringify(attentionDispositionEvidence(nodeInput, "maintainer-import-checks"))', context));
  assert.equal(disposition.binding.imports[0].sourceHead, old);
  assert.equal(reasonsFor(node)[0].id, 'maintainer-ci-infrastructure');
  attempt.checks.contexts[0].id = 'new-failed-attempt';
  assert.equal(reasonsFor(node)[0].id, 'maintainer-import-checks');
  delete node.followup;
  attempt.checks = structuredClone(attentionImport(node).checks);
  attempt.mergeable = 'CONFLICTING';
  assert.deepEqual(idsFor(node), ['maintainer-import-refresh']);
  pr.mergeable = 'CONFLICTING';
  assert.equal(reasonsFor(node)[0].id, 'author-conflicts');
  pr.mergeable = attempt.mergeable = 'MERGEABLE';
  attempt.status = 'merged';
  assert.deepEqual(idsFor(node), ['maintainer-import-landed']);
  assert.match(vm.runInContext('attentionText(attentionGroups([nodeInput], "tamird"), "2026-10-13T10:00:00Z")', context), /Reconcile merged export/);
  pr.imports.push(attentionImport(node));
  assert.deepEqual(idsFor(node), ['maintainer-merge']);
  delete node.approvedPatch;
  node.reapproval = { head: pr.head, kind: 'content-amendment' };
  assert.deepEqual(idsFor(node), ['maintainer-review']);
});

test('exact landed-patch proof removes import action without closing GitHub or hiding feedback', () => {
  const node = attentionNode(16053), pr = node.github;
  const imported = attentionImport(node, 16326);
  imported.status = 'merged'; imported.sourceHead = 'c'.repeat(40); imported.matchesSourceHead = false;
  pr.imports = [imported];
  assert.ok(!idsFor(node).includes('landed-source-open')); // An unrelated merged export is insufficient.
  node.landedPatch = { head: pr.head, method: 'merge-tree-identical', upstreamHead: 'd'.repeat(40),
    upstreamTree: 'e'.repeat(40), replayedTree: 'e'.repeat(40), verifiedAt: '2026-10-27T18:00:00Z',
    evidence: ['https://github.com/google/gvisor/commit/' + 'd'.repeat(40)] };
  assert.deepEqual(idsFor(node), ['landed-source-open']);
  const context = client(); context.nodeInput = node;
  const groups = JSON.parse(vm.runInContext('JSON.stringify(attentionGroups([nodeInput], "tamird"))', context));
  assert.equal(groups[0].owner, 'other'); assert.equal(groups[0].title, 'Landed; source PR still open');
  context.snapshotInput.workStates[node.id] = { 'awaiting-import': { scope: pr.head }, 'maintainer-review': { scope: pr.head } };
  assert.deepEqual(Object.keys(vm.runInContext('stateRecords(snapshotInput, nodeInput)', context)), ['imported']);
  assert.equal(pr.status, 'open');
  pr.feedback.items.push({ kind: 'inline', threadId: 'new', isResolved: false, author: { login: 'reviewer' },
    createdAt: '2026-10-27T18:10:00Z' });
  pr.threads.unresolved = pr.threads.total = 1;
  assert.deepEqual(idsFor(node), ['author-feedback', 'landed-source-open']);
  pr.feedback.items = []; pr.threads.unresolved = pr.threads.total = 0;
  for (const key of ['head', 'replayedTree']) {
    const old = node.landedPatch[key]; node.landedPatch[key] = 'f'.repeat(40);
    assert.ok(!idsFor(node).includes('landed-source-open'), key);
    node.landedPatch[key] = old;
  }
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

test('superseded attempts stay visible without creating a current source failure', () => {
  const context = client(), node = attentionNode();
  const old = { ...node.github.checks.contexts[0], id: 'old', state: 'CANCELLED' };
  node.github.checks.state = 'FAILURE';
  node.github.checks.supersededContexts = [old];
  node.github.checks.reportedTotal = node.github.checks.total + 1;
  context.nodeInput = node;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Success');
  assert.match(vm.runInContext('checkInfo(nodeInput.github).detail', context), /1 superseded.*aggregate is FAILURE/);
  assert.ok(!idsFor(node).includes('author-public-checks'));
  node.github.checks.complete = false;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Failure');
  node.github.checks.complete = true;
  node.github.checks.contexts[0].state = 'IN_PROGRESS';
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Pending');
  node.github.checks.contexts[0].state = 'FAILURE';
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Failure');
});

test('a confirmed conflict takes priority over opaque import progress', () => {
  const node = attentionNode(15337);
  node.github.checks.contexts = [serviceStatus('import/copybara', 'SUCCESS')];
  const context = client(); context.nodeInput = node;
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).text', context), 'Unknown');
  assert.equal(vm.runInContext('checkInfo(nodeInput.github).passed', context), false);
  assert.deepEqual(idsFor(node), ['maintainer-unknown']);
  assert.equal(reasonsFor(node)[0].title, 'Waiting for public export');
  assert.equal(vm.runInContext('attentionGroups([nodeInput], "tamird")[0].title', context), 'Public export pending or draft');
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

test('provider branch mismatch routes refresh without rewriting PR evidence', () => {
  const node = attentionNode(), context = client();
  node.github.mergeable = 'CONFLICTING'; node.github.branchHead = 'd'.repeat(40);
  const raw = structuredClone(node.github);
  context.nodeInput = node;
  assert.deepEqual(idsFor(node), ['agent-refresh']);
  assert.deepEqual(node.github, raw);
  assert.match(vm.runInContext('mergeDescription(nodeInput.github)', context), /its mergeability is unknown/);
  assert.ok(vm.runInContext('conflictRecord(nodeInput.github)', context));
  const snapshot = { ...context.snapshotInput, workStates: { [node.id]: { 'waiting-merge': { scope: node.github.head } } } };
  context.snapshotInput = snapshot;
  assert.equal(vm.runInContext('stateRecords(snapshotInput, nodeInput)["waiting-merge"]', context), undefined);
  for (const branchHead of [undefined, null, node.github.head]) {
    node.github.branchHead = branchHead;
    assert.ok(idsFor(node).includes('author-conflicts'));
  }
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
  original.branchHead = 'd'.repeat(40);
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
    assert.equal(current.branchHead, null);
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
  assert.ok(idsFor(node).includes('maintainer-reimport'));
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

function historyView(nodes, investigations = []) {
  const context = client();
  const controls = new Map();
  context.location = new URL('https://example.test/?view=table');
  context.document = { getElementById: id => {
    if (!controls.has(id)) controls.set(id, { value: '', checked: false, options: [{ value: '' }],
      querySelectorAll: () => [], setAttribute() {}, getBoundingClientRect: () => ({ width: 1000, height: 800 }) });
    return controls.get(id);
  } };
  context.registryInput = { ...context.registryInput, nodes, investigations, edges: [], groups: [] };
  context.nodesInput = nodes;
  vm.runInContext('registry = registryInput; model = { nodes: nodesInput, edges: [], statesAvailable: true }; view = "table";', context);
  return { context, controls };
}

test('graph selection outlines only direct prerequisites and dependents without changing arrows', () => {
  const nodes = ['a', 'b', 'c', 'd', 'e'].map(id => ({ id, type: 'branch', title: id, status: 'Agent work', url: 'https://example.test/' + id }));
  const { context, controls } = historyView(nodes);
  context.edgesInput = [['a', 'b', 'depends_on'], ['b', 'c', 'blocked_by'], ['c', 'd', 'depends_on'], ['b', 'e', 'includes']]
    .map(([from, to, type]) => ({ from, to, type }));
  function svgNode(tag) {
    return { tag, attrs: {}, children: [], setAttribute(k, v) { this.attrs[k] = String(v); },
      append(...children) { this.children.push(...children); }, replaceChildren() { this.children = []; }, addEventListener() {} };
  }
  const content = svgNode('g'); controls.set('graph-content', content);
  context.document.createElementNS = (_, tag) => svgNode(tag);
  vm.runInContext('model.edges = edgesInput; view = "dag"', context);
  function render(id) {
    context.selectedInput = id;
    vm.runInContext('selected = selectedInput; drawGraph(nodesInput)', context);
    const groups = content.children.filter(el => el.attrs.class?.startsWith('node '));
    const related = groups.filter(el => / (?:prerequisite|dependent)$/.test(el.attrs.class));
    const arrows = content.children.find(el => el.attrs['aria-hidden'] === 'true');
    return { related: related.map(el => [el.children[0].attrs['data-node'], el.attrs.class.split(' ').at(-1)]),
      selected: groups.filter(el => el.children[0].attrs['aria-pressed'] === 'true').map(el => el.children[0].attrs['data-node']),
      labels: Object.fromEntries(groups.map(el => [el.children[0].attrs['data-node'], el.children[0].attrs['aria-label']])),
      arrows: arrows.children.map(el => el.attrs) };
  }
  const baseline = render(null), b = render('b'), c = render('c');
  assert.deepEqual(b.selected, ['b']);
  assert.deepEqual(b.related, [['a', 'prerequisite'], ['c', 'dependent']]);
  assert.match(b.labels.a, /Prerequisite of the selected task/); assert.match(b.labels.c, /Depends on the selected task/);
  assert.deepEqual(c.related, [['b', 'prerequisite'], ['d', 'dependent']]);
  assert.deepEqual(b.arrows, baseline.arrows); assert.deepEqual(c.arrows, baseline.arrows);
  assert.deepEqual(baseline.arrows.map(el => el.class), ['edge depends_on', 'edge blocked_by', 'edge depends_on']);
  assert.deepEqual(render(null), baseline);
});

test('default visibility hides inactive history but retains live, blocked and newly actionable work', () => {
  const old = { id: 'branch:delivered', type: 'branch', title: 'Delivered proposal', status: 'Published as #1', inactive: true };
  const open = { ...attentionNode(1), inactive: true };
  const decision = { ...old, id: 'branch:new-decision', head: 'a'.repeat(40), contributorReview: { status: 'pending', head: 'a'.repeat(40) } };
  const changes = { ...old, id: 'branch:changes', head: 'b'.repeat(40), contributorChangesRequested: { head: 'b'.repeat(40) } };
  const work = { ...old, id: 'branch:ongoing', status: 'Agent work' };
  const blocked = { id: 'branch:blocked', type: 'branch', title: 'Approved child', status: 'Awaiting prerequisites' };
  const external = { id: 'external:release', type: 'issue', title: 'Release pending', githubState: 'closed', status: 'Source workaround delivered; release pending' };
  const terminal = ['resolved', 'withdrawn', 'retired', 'superseded', 'closed', 'merged'].map(status =>
    ({ id: `branch:${status}`, type: 'branch', title: status, status }));
  const merged = { ...attentionNode(2), status: 'merged', github: { ...attentionNode(2).github, status: 'merged' } };
  const nodes = [old, ...terminal, merged, open, decision, changes, work, blocked, external];
  const { context, controls } = historyView(nodes);
  const ids = expression => JSON.parse(vm.runInContext(`JSON.stringify(${expression}.map(n => n.id))`, context));
  const active = [open, decision, changes, work, blocked].map(n => n.id);
  assert.deepEqual(ids('tableNodes()'), active);
  assert.deepEqual(ids('graphNodes()'), active);
  vm.runInContext('initializing = false; drawStateFilters()', context);
  assert.equal(controls.get('all-reasons').textContent, `All ${active.length}`);
  controls.get('show-resolved').checked = true;
  assert.deepEqual(ids('tableNodes()'), nodes.map(n => n.id));
  const groups = JSON.parse(vm.runInContext('JSON.stringify(attentionGroups(tableNodes(), "tamird"))', context));
  assert.equal(groups.find(group => group.id === 'resolved').entries.length, terminal.length + 3);
});

test('provider issue closure removes active blockers without claiming deployment, and reopening restores them', () => {
  const closed = { id: 'external:capacity', type: 'issue', title: 'Capacity', status: 'deployment unverified',
    githubState: 'closed', githubStateReason: 'NOT_PLANNED' };
  const dependent = { id: 'branch:consumer', type: 'branch', title: 'Consumer', status: 'Agent work' };
  const unrelated = { id: 'branch:other', type: 'branch', title: 'Other work', status: 'Awaiting prerequisites' };
  const { context, controls } = historyView([closed, dependent, unrelated]);
  context.edgesInput = [{ from: closed.id, to: dependent.id, type: 'blocked_by' }];
  vm.runInContext('model.edges = edgesInput', context);
  const ids = expression => JSON.parse(vm.runInContext(`JSON.stringify(${expression}.map(n => n.id))`, context));
  for (const expression of ['tableNodes()', 'graphNodes()']) assert.deepEqual(ids(expression), [dependent.id, unrelated.id]);
  assert.equal(vm.runInContext('activeDependencyEdges().length', context), 0);
  assert.equal(vm.runInContext('status(model.nodes[0])', context), 'Issue closed · not planned');
  vm.runInContext('initializing = false; drawStateFilters()', context);
  assert.equal(controls.get('all-reasons').textContent, 'All 2');
  closed.contributorReview = { kind: 'reply', status: 'pending', url: 'https://github.com/buildbuddy-io/buildbuddy/issues/1' };
  context.nodeInput = closed;
  closed.workStates = vm.runInContext('stateRecords(snapshotInput, nodeInput)', context);
  assert.equal(closed.workStates['contributor-review'].scope, closed.contributorReview.url);
  assert.deepEqual(idsFor(closed), ['author-proposal']);
  assert.deepEqual(ids('tableNodes()'), [closed.id, dependent.id, unrelated.id]);
  assert.equal(vm.runInContext('activeDependencyEdges().length', context), 0);
  delete closed.contributorReview;
  closed.workStates = vm.runInContext('stateRecords(snapshotInput, nodeInput)', context);
  assert.equal(closed.workStates['contributor-review'], undefined);
  context.location = new URL('https://example.test/?view=dag#external%3Acapacity');
  vm.runInContext('applyView(readViewURL())', context);
  assert.deepEqual(ids('graphNodes()'), [closed.id, dependent.id]);
  assert.equal(vm.runInContext('activeDependencyEdges().length', context), 0);
  controls.get('show-resolved').checked = true;
  assert.deepEqual(ids('graphNodes()'), [closed.id, dependent.id, unrelated.id]);
  vm.runInContext('resetFilters(false); model.nodes[0].githubState = "open"; model.nodes[0].githubStateReason = "REOPENED"; model.nodes[0].status = "closed"', context);
  assert.deepEqual(ids('tableNodes()'), [closed.id, dependent.id, unrelated.id]);
  assert.equal(vm.runInContext('activeDependencyEdges().length', context), 1);
  assert.equal(vm.runInContext('status(model.nodes[0])', context), 'Issue open · reopened');
});

test('issue snapshot validates the provider identity and keeps a new explicit reply decision visible after closure', () => {
  const context = client();
  const node = { id: 'external:logs', type: 'issue', repo: 'buildbuddy-io/buildbuddy', number: 13542,
    title: 'Worker output', status: 'deployment unverified', githubState: 'closed' };
  context.registryInput.nodes = [node];
  context.snapshotInput.issues[node.id] = { githubState: 'closed', githubStateReason: 'COMPLETED',
    githubURL: 'https://github.com/buildbuddy-io/buildbuddy/issues/13542', closedAt: '2026-09-29T15:20:00Z',
    updatedAt: '2026-09-29T15:50:00Z' };
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  context.nodeInput = node;
  assert.equal(vm.runInContext('historical(nodeInput)', context), true);
  node.contributorReview = { kind: 'reply', status: 'pending', url: context.snapshotInput.issues[node.id].githubURL };
  assert.equal(vm.runInContext('historical(nodeInput)', context), false);
  const issue = context.snapshotInput.issues[node.id];
  delete context.snapshotInput.issues[node.id];
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Incomplete issue inventory/);
  context.snapshotInput.issues[node.id] = issue;
  issue.unavailable = 'Issue relocated; last observation retained.';
  issue.unavailableAt = '2026-10-12T11:48:00Z';
  vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context);
  assert.equal(vm.runInContext('historical({...nodeInput, contributorReview: undefined, ...snapshotInput.issues[nodeInput.id]})', context), true);
  context.snapshotInput.issues[node.id].githubURL = 'https://github.com/other/repo/issues/13542';
  assert.throws(() => vm.runInContext('validateSnapshot(snapshotInput, registryInput)', context), /Invalid issue provider/);
});

test('history deep links expose the selected record and its live dependency without adding a blocker', () => {
  const old = { id: 'branch:withdrawn', type: 'branch', title: 'Old proposal', status: 'Withdrawn', inactive: true };
  const live = { id: 'branch:replacement', type: 'branch', title: 'Replacement', status: 'Agent work' };
  const { context, controls } = historyView([old, live]);
  context.edgesInput = [{ from: old.id, to: live.id, type: 'depends_on' }];
  vm.runInContext('model.edges = edgesInput', context);
  assert.equal(vm.runInContext('activeDependencyEdges().length', context), 0);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(tableNodes().map(n => n.id))', context)), [live.id]);
  context.location = new URL(`https://example.test/?view=dag&focus=${encodeURIComponent(old.id)}#${encodeURIComponent(old.id)}`);
  vm.runInContext('applyView(readViewURL())', context);
  assert.equal(controls.get('show-resolved').checked, false);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(graphNodes().map(n => n.id))', context)), [old.id, live.id]);
  assert.equal(vm.runInContext('selected', context), old.id);
  assert.equal(vm.runInContext('focus', context), old.id);
  context.location = new URL(`https://example.test/?view=dag#${encodeURIComponent(old.id)}`);
  vm.runInContext('applyView(readViewURL())', context);
  assert.equal(vm.runInContext('focus', context), null);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(graphNodes().map(n => n.id))', context)), [old.id, live.id]);
  assert.equal(vm.runInContext('displayDependencyEdges().length', context), 1);
  assert.equal(vm.runInContext('components()[0].ids.size', context), 2);
  assert.equal(vm.runInContext('dagLayout(graphNodes(), {}).positions.get("branch:replacement").x > dagLayout(graphNodes(), {}).positions.get("branch:withdrawn").x', context), true);
  vm.runInContext('resetFilters(false)', context);
  assert.equal(vm.runInContext('selected', context), null);
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(graphNodes().map(n => n.id))', context)), [live.id]);
  assert.equal(vm.runInContext('displayDependencyEdges().length', context), 0);
  context.unrelatedInput = { id: 'branch:unrelated', type: 'branch', title: 'Unrelated', status: 'Agent work' };
  vm.runInContext('model.nodes.push(unrelatedInput); selected = "branch:withdrawn"', context);
  controls.get('show-resolved').checked = true;
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(graphNodes().map(n => n.id))', context)), [old.id, live.id, 'branch:unrelated']);
});

test('investigations share history visibility and keep concluded direct links readable', () => {
  const items = ['active', 'concluded'].map(status => ({ id: `investigation:${status}`, title: status,
    status, summary: status, owner: 'agent', findings: [], evidence: [] }));
  const { context, controls } = historyView([], items);
  context.itemsInput = items;
  const visible = () => JSON.parse(vm.runInContext('JSON.stringify(itemsInput.filter(investigationMatches).map(n => n.id))', context));
  assert.deepEqual(visible(), ['investigation:active']);
  controls.get('show-resolved').checked = true;
  assert.deepEqual(visible(), items.map(n => n.id));
  context.location = new URL('https://example.test/?view=investigations#investigation%3Aconcluded');
  vm.runInContext('applyView(readViewURL())', context);
  assert.equal(controls.get('show-resolved').checked, false);
  assert.equal(vm.runInContext('selected', context), 'investigation:concluded');
  assert.deepEqual(visible(), items.map(n => n.id));
  context.location = new URL('https://example.test/?view=investigations&investigation-status=concluded');
  vm.runInContext('applyView(readViewURL())', context);
  assert.equal(controls.get('show-resolved').checked, true);
  assert.deepEqual(visible(), ['investigation:concluded']);
});

test('history URLs round-trip in every view and old resolved links retain their intent', () => {
  const { context, controls } = historyView([]);
  for (const view of ['dag', 'table', 'investigations']) {
    context.location = new URL(`https://example.test/?view=${view}&history=1`);
    vm.runInContext('applyView(readViewURL())', context);
    assert.equal(controls.get('show-resolved').checked, true);
    assert.equal(vm.runInContext('currentViewURL()', context), `/?view=${view}&history=1`);
    context.location = new URL(`https://example.test/?view=${view}`);
    vm.runInContext('applyView(readViewURL())', context);
    assert.equal(controls.get('show-resolved').checked, false);
  }
  context.location = new URL('https://example.test/?view=table&resolved=1');
  vm.runInContext('applyView(readViewURL())', context);
  assert.equal(vm.runInContext('currentViewURL()', context), '/?view=table&history=1');
  vm.runInContext('resetFilters(false)', context);
  assert.equal(controls.get('show-resolved').checked, false);
  for (const view of ['dag', 'table']) {
    context.savedInput = { view, history: false, investigationStatus: 'concluded' };
    vm.runInContext('applyView(savedInput)', context);
    assert.equal(controls.get('show-resolved').checked, false);
  }
  context.savedInput = { view: 'investigations', history: false, investigationStatus: 'concluded' };
  vm.runInContext('applyView(savedInput)', context);
  assert.equal(controls.get('show-resolved').checked, false);
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

// Existing repo-qualified IDs join registered-only observations without creating duplicates.
test('registered-only repositories require direct review and retain curated node identity', () => {
  const context = client(), repo = 'nodejs/node';
  const external = { ...attentionNode(66874).github, repo,
    url: `https://github.com/${repo}/pull/66874`, importPolicy: 'none', checkedAt: context.snapshotInput.checkedAt };
  context.registryInput.meta.repositories = { 'google/gvisor': 'copybara', [repo]: 'none' };
  context.registryInput.meta.registeredOnlyRepositories = [repo];
  context.registryInput.nodes = [{ id: `pr:${repo}:66874`, type: 'pr', repo, number: 66874,
    url: external.url, title: 'Curated Node change', summary: 'Preserved delivery evidence' }];
  context.registryInput.edges = []; context.registryInput.groups = [];
  context.snapshotInput.repositories = context.registryInput.meta.repositories;
  context.snapshotInput.prs = [external]; context.snapshotInput.workStates = {};
  context.document = { getElementById: () => ({ scrollTop: 0, scrollLeft: 0, querySelectorAll: () => [], scrollTo() {} }) };
  vm.runInContext('validateRegistry(registryInput); validateSnapshot(snapshotInput, registryInput); updateRegistryControls = () => {}; render = () => {}; setFreshness = () => {}; applyLive(snapshotInput, registryInput)', context);
  assert.equal(vm.runInContext('model.nodes.length', context), 1);
  assert.equal(vm.runInContext('model.nodes[0].summary', context), 'Preserved delivery evidence');
  context.registryInput.meta.registeredOnlyRepositories = ['google/gvisor'];
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /registered-only/);
  context.registryInput.meta.registeredOnlyRepositories = null;
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /registered-only/);
});

test('copying contributor decisions includes proposal and outgoing-review tracker links', () => {
  const context = client(), pr = attentionNode(15814);
  pr.github.headOwner = 'EtiennePerot';
  pr.contributorReview = { status: 'pending', kind: 'review', head: pr.github.head, url: pr.url };
  pr.workStates['contributor-review'] = { scope: pr.github.head };
  const branch = { id: 'branch:review/fix', type: 'branch', head: 'b'.repeat(40), contributorReview: { status: 'pending', head: 'b'.repeat(40) },
    workStates: { 'contributor-review': { scope: 'b'.repeat(40) } } };
  context.location = { href: 'https://tamird.github.io/gvisor/?q=old#stale' };
  context.nodesInput = [pr, branch];
  for (const grouped of [true, false]) {
    context.groupedInput = grouped;
    const text = vm.runInContext('attentionText(attentionGroups(nodesInput, "tamird", groupedInput, new Set(["author-proposal"])), "2026-10-24T11:02:00Z")', context);
    assert.deepEqual(text.split('\n').filter(line => line.startsWith('https://')), [
      'https://tamird.github.io/gvisor/?view=table&reason=author-proposal#pr%3A15814',
      'https://tamird.github.io/gvisor/?view=table&reason=author-proposal#branch%3Areview%2Ffix',
    ]);
  }
});


test('human-only submissions stay separate from review decisions and clear when settled', () => {
  const context = client();
  const node = { id: 'issue:google/benchmark:1634', type: 'issue', head: 'a'.repeat(40),
    humanAction: { status: 'pending', kind: 'submission', head: 'a'.repeat(40), title: 'Submit manually',
      summary: 'Human contribution required', body: 'Review the local commit and submit it yourself.',
      url: 'https://github.com/google/benchmark/issues/1634', policyURL: 'https://github.com/google/benchmark/blob/main/AGENTS.md' } };
  context.nodeInput = node;
  context.location = { href: 'https://tamird.github.io/gvisor/' };
  context.registryInput = { ...context.registryInput, nodes: [node], edges: [], groups: [] };
  vm.runInContext('validateRegistry(registryInput)', context);
  assert.deepEqual(idsFor(node), ['human-submission']);
  assert.equal(vm.runInContext('contributorDecision(nodeInput)', context), null);
  const text = vm.runInContext('attentionText(attentionGroups([nodeInput], "tamird", false, new Set(["human-submission"])), "2026-10-24T11:02:00Z")', context);
  assert.ok(text.includes('https://tamird.github.io/gvisor/?view=table&reason=human-submission#issue%3Agoogle%2Fbenchmark%3A1634'));
  node.head = 'b'.repeat(40);
  assert.deepEqual(idsFor(node), []);
  assert.throws(() => vm.runInContext('validateRegistry(registryInput)', context), /Invalid human-only action/);
  delete node.humanAction;
  vm.runInContext('validateRegistry(registryInput)', context);
  assert.deepEqual(idsFor(node), []);
});


test('ungrouped combined human attention copies both reviews and manual actions', () => {
  const context = client(), pr = attentionNode(15814);
  pr.github.headOwner = 'EtiennePerot';
  pr.contributorReview = { status: 'pending', kind: 'review', head: pr.github.head, url: pr.url };
  pr.workStates['contributor-review'] = { scope: pr.github.head };
  const branch = { id: 'branch:fix', type: 'branch', head: 'b'.repeat(40), contributorReview: { status: 'pending', head: 'b'.repeat(40) },
    workStates: { 'contributor-review': { scope: 'b'.repeat(40) } } };
  const manual = { id: 'issue:google/benchmark:1634', type: 'issue', head: 'c'.repeat(40),
    humanAction: { status: 'pending', kind: 'submission', head: 'c'.repeat(40), summary: 'Submit manually' } };
  context.nodesInput = [pr, branch, manual];
  context.location = { href: 'https://tamird.github.io/gvisor/' };
  const text = vm.runInContext('attentionText(attentionGroups(nodesInput, "tamird", false, new Set(["author-proposal", "human-submission"])), "2026-10-24T11:02:00Z")', context);
  assert.deepEqual(text.split('\n').filter(line => line.startsWith('https://')), [
    'https://tamird.github.io/gvisor/?view=table&reason=author-proposal#pr%3A15814',
    'https://tamird.github.io/gvisor/?view=table&reason=author-proposal#branch%3Afix',
    'https://tamird.github.io/gvisor/?view=table&reason=human-submission#issue%3Agoogle%2Fbenchmark%3A1634',
  ]);
});
