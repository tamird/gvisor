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
