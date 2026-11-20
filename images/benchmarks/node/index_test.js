const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync('/usr/src/app/index.js', 'utf8');
function request() {
  let handler;
  const callbacks = [];
  const rendered = [];
  const errors = [];
  const app = {
    set() {},
    get(route, callback) { handler = callback; },
    listen() {},
  };
  const client = {
    get(key, callback) { callbacks.push(callback); },
    set() {},
  };
  vm.runInNewContext(source, {
    require(name) {
      if (name === 'express') return () => app;
      if (name === 'redis') return {createClient: () => client};
      if (name === 'secure-random-string') return opts => 'x'.repeat(opts.length);
      return require(name);
    },
    __dirname: '/usr/src/app',
    process: {argv: ['node', 'index.js', 'redis']},
  }, {filename: 'index.js'});
  handler({}, {render(view, locals) { rendered.push(Array.from(locals.text)); }},
      error => errors.push(error));
  assert.strictEqual(callbacks.length, 4);
  assert.strictEqual(rendered.length, 0, 'render must wait for Redis callbacks');
  return {callbacks, rendered, errors};
}

const success = request();
for (const index of [2, 0, 3]) {
  success.callbacks[index](null, Buffer.from(String(index).repeat(1024)));
  assert.strictEqual(success.rendered.length, 0);
}
success.callbacks[1](null, Buffer.from('1'.repeat(1024)));
assert.deepStrictEqual(success.rendered[0].map(s => s.length), [1024, 1024, 1024, 1024]);
assert.strictEqual(success.rendered.length, 1);
assert.strictEqual(success.errors.length, 0);

const failure = request();
const error = new Error('Redis failure');
failure.callbacks[2](error);
failure.callbacks[0](error);
failure.callbacks[3](null, Buffer.from('3'.repeat(1024)));
failure.callbacks[1](null, Buffer.from('1'.repeat(1024)));
assert.deepStrictEqual(failure.errors, [error]);
assert.strictEqual(failure.rendered.length, 0);

const missing = request();
missing.callbacks[0](null, null);
missing.callbacks[1](null, Buffer.from('1'.repeat(1024)));
assert.strictEqual(missing.errors.length, 1);
assert.strictEqual(missing.rendered.length, 0);
console.log(JSON.stringify({node_handler_witness: 'PASS', deferred: true,
  reordered: true, error_once: true, missing_value_error: true}));
