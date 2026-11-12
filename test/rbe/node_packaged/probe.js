'use strict';
const http = require('node:http');
const common = require(process.cwd() + '/test/common');
const delay = Number(process.env.HTTP10_DELAY_MS);
console.log('image deadline', common.platformTimeout(50));

const createServer = http.createServer;
http.createServer = function(listener) {
  return createServer.call(this, function(req, res) {
    console.log('request', req.httpVersion);
    res.once('finish', () => console.log('finish', req.httpVersion));
    if (req.httpVersion === '1.0' && delay > 0) {
      console.log('delay HTTP/1.0 response', delay);
      setTimeout(() => listener.call(this, req, res), delay);
      return;
    }
    listener.call(this, req, res);
  });
};
