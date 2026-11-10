import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import vm from 'node:vm';

const root = process.argv[2];
if (!root) throw new Error('Provide the directory containing app.js and the paired metadata.');
const bytes = Object.fromEntries(['app.js', 'registry.json', 'github-status.json'].map(name => [name, readFileSync(name === 'github-status.json' && process.argv[3] ? resolve(process.argv[3]) : resolve(root, name))]));
const source = bytes['app.js'].toString();
if (!source.endsWith('start();\n')) throw new Error('Unexpected application entry point; review the validator binding.');
const context = vm.createContext({ URL });
vm.runInContext(source.slice(0, -'start();\n'.length), context, { filename: 'app.js' });
context.candidateRegistry = JSON.parse(bytes['registry.json']);
context.candidateSnapshot = JSON.parse(bytes['github-status.json']);
vm.runInContext('validateRegistry(candidateRegistry); validateSnapshot(candidateSnapshot, candidateRegistry);', context);
for (const node of context.candidateRegistry.nodes) {
  context.node = node;
  if (vm.runInContext("trackerMaintenance(node)", context) &&
      [node.contributorReview, ...(node.additionalReviews || [])].some(item => item?.status === "pending")) {
    throw new Error(`Record tracker maintenance as agent work: ${node.id}`);
  }
  const decision = node.contributorReview;
  if (decision?.status !== 'pending') continue;
  if (node.type === 'branch' &&
      (node.head !== decision.head || node.url !== decision.url || !decision.url.endsWith('/commit/' + decision.head.slice(0, 9)))) {
    throw new Error(`Link the proposal and its card to the same single commit: ${node.id}`);
  }
  context.node = node;
  if (vm.runInContext('alreadyReviewed(node, node.contributorReview)', context)) {
    throw new Error(`Already reviewed: ${node.id} at ${decision.head || decision.url}`);
  }
  if (node.type === 'branch' && decision.kind === 'proposal' &&
      (node.refinement || node.settledReviews?.some(review => review.kind === 'proposal' && review.head !== decision.head)) &&
      !vm.runInContext('refinementComparison(node, node.contributorReview)', context)) {
    throw new Error(`Missing range-diff for revised proposal: ${node.id} at ${decision.head}`);
  }
}
console.log(JSON.stringify({
  status: 'PASS', validator: 'Unmodified app.js validateRegistry and validateSnapshot',
  registryDate: context.candidateRegistry.meta.updatedAt, snapshotAt: context.candidateSnapshot.checkedAt,
  inputs: Object.fromEntries(Object.entries(bytes).map(([name, body]) => [name, { bytes: body.length, sha256: createHash('sha256').update(body).digest('hex') }])),
}));
