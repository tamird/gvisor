'use strict';
const http = require('node:http');
const { performance } = require('node:perf_hooks');
const begin = performance.now();
const mode = process.env.TIMING_CONTROL;
const mark = (label) => console.log('PHASE', Math.round(performance.now() - begin), label);

let lastTick = begin;
setInterval(() => {
  const now = performance.now();
  if (now - lastTick > 350) mark('timer gap ' + Math.round(now - lastTick));
  lastTick = now;
}, 50).unref();

// Observe body events without adding a data listener or making the stream flow.
const responses = new WeakMap();
const emit = http.IncomingMessage.prototype.emit;
http.IncomingMessage.prototype.emit = function(event, ...args) {
  const id = responses.get(this);
  if (id && (event === 'data' || event === 'end')) mark(event + ' ' + id);
  return emit.call(this, event, ...args);
};

const send = http.get;
let clientRequests = 0;
http.get = function(...args) {
  const id = ++clientRequests;
  mark('get ' + id);
  const req = send.apply(this, args);
  req.once('socket', (socket) => {
    mark('assigned ' + id);
    if (socket.connecting) {
      socket.once('lookup', () => mark('lookup ' + id));
      socket.once('connect', () => mark('connect ' + id));
    }
  });
  req.prependOnceListener('response', (res) => {
    responses.set(res, id);
    mark('headers ' + id);
  });
  if (mode === 'pause-first' && id === 1) {
    mark('begin event-loop pause');
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1250);
    mark('end event-loop pause');
  }
  return req;
};

const create = http.createServer;
let serverRequests = 0;
http.createServer = function(...args) {
  const server = create.apply(this, args);
  server.prependListener('request', (req, res) => {
    const id = ++serverRequests;
    mark('server request ' + id);
    res.once('drain', () => mark('drain ' + id));
    res.once('finish', () => mark('finish ' + id));
    if (mode === 'hold-second-end' && id === 2) {
      res.end = () => { mark('held second end'); return res; };
    }
  });
  return server;
};
