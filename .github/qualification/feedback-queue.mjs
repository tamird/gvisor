import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import vm from 'node:vm';

const read = (rev, file) => execFileSync('git', ['show', rev + ':' + file], {encoding:'utf8', maxBuffer:32<<20});
const snapshot = JSON.parse(read('HEAD^^', 'github-status.json'));
const sweep = snapshot.projectFeedbackReview;
const id = 'investigation:project-feedback';
assert.equal(sweep.lastReviewed, '2026-11-02T08:31:43+00:00');
assert.equal(sweep.dailyReviewDue, true);
assert.equal(Object.keys(sweep.changedWork).length, 590);

function results(rev) {
  const registry = JSON.parse(read(rev, 'registry.json'));
  const item = registry.investigations.find(row => row.id === id);
  assert.equal(item.lastReviewedAt, sweep.lastReviewed);
  const source = read(rev, 'app.js');
  assert.ok(source.endsWith('start();\n'));
  const ctx = vm.createContext({URL, data:registry, snapshot, id,
    document:{getElementById:()=>({value:'',checked:false})}});
  vm.runInContext(source.slice(0, -'start();\n'.length), ctx);
  return JSON.parse(vm.runInContext(`
    registry = data; liveSnapshot = snapshot;
    model = {nodes:registryNodes(registry).filter(node => node.id === id)};
    function selectedQueue(queue) {
      reasonFilters = new Set([queue]);
      return attentionGroups(tableNodes(), data.meta.owner).flatMap(group =>
        group.entries.map(entry => [group.id, entry.node.id, entry.node.url, entry.copyURL]));
    }
    JSON.stringify({agent:selectedQueue('queue-agent'), user:selectedQueue('queue-user')})
  `, ctx));
}

const before = results('HEAD^^');
const after = results('HEAD^');
assert.deepEqual(before, {agent:[], user:[]});
assert.deepEqual(after, {agent:[['agent-feedback-sweep', id,
  'https://tamird.github.io/gvisor/?view=investigations#investigation%3Aproject-feedback', null]], user:[]});
console.log(JSON.stringify({source:execFileSync('git',['rev-parse','HEAD^'],{encoding:'utf8'}).trim(),
  lastReviewed:sweep.lastReviewed, leads:Object.keys(sweep.changedWork).length, before, after}, null, 2));
