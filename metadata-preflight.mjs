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
console.log(JSON.stringify({
  status: 'PASS', validator: 'Unmodified app.js validateRegistry and validateSnapshot',
  registryDate: context.candidateRegistry.meta.updatedAt, snapshotAt: context.candidateSnapshot.checkedAt,
  inputs: Object.fromEntries(Object.entries(bytes).map(([name, body]) => [name, { bytes: body.length, sha256: createHash('sha256').update(body).digest('hex') }])),
}));
