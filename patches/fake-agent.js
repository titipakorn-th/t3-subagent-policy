#!/usr/bin/env node
// Fake ACP agent used to drive the proxy's steering paths deterministically.
//
//   node fake-agent.js always-reject   -> every session/prompt is rejected with
//                                          "Session already has an active Turn."
//   node fake-agent.js reject-then-ok <n> -> the first n prompts are rejected,
//                                          the next one succeeds.
//   node fake-agent.js advertise-refuse-steer -> the agent advertises the mcode
//                                          extension methods but refuses a
//                                          steer, so the proxy must fall back
//                                          to holding the prompt.
//
// It never actually runs a turn, so the tests are fast and do not need a login.
import readline from 'node:readline';

const mode = process.argv[2] || 'always-reject';
const failCount = Number(process.argv[3] ?? 3);
let prompts = 0;
const seen = [];

const EXTENSIONS = {
  version: 1,
  methods: ['mcode/session/steer', 'mcode/session/queue/enqueue'],
  notifications: ['mcode/session/queue_update'],
};

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method === undefined) return; // a response, ignore

  const reply = (result) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result }) + '\n');

  if (msg.method === 'initialize') {
    const result = { protocolVersion: 1, agentCapabilities: {} };
    if (mode === 'advertise-refuse-steer') {
      result._meta = { 'minimax-code/extensions': EXTENSIONS };
    }
    reply(result);
    return;
  }
  if (msg.method === 'session/new') {
    reply({ sessionId: 'sess-1' });
    if (mode === 'delegation') {
      // The agent started children of its own.
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0',
        method: 'mcode/session/delegation_update',
        params: {
          sessionId: 'sess-1',
          snapshot: { schemaVersion: 1, rootSessionId: 'sess-1', members: [{ title: 'worker-one' }, { title: 'worker-two' }] },
        },
      }) + '\n');
    }
    return;
  }
  if (msg.method === 'mcode/session/steer') {
    // Advertised, but refused: the proxy has to fall back.
    process.stdout.write(JSON.stringify({
      jsonrpc: '2.0', id: msg.id,
      error: { code: -32601, message: 'Method not found' },
    }) + '\n');
    return;
  }
  if (msg.method === 'session/prompt') {
    prompts += 1;
    seen.push(prompts);
    if (mode === 'always-reject') {
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0', id: msg.id,
        error: { code: -32603, message: 'Session already has an active Turn.' },
      }) + '\n');
      return;
    }
    if (mode === 'reject-then-ok' && prompts <= failCount) {
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0', id: msg.id,
        error: { code: -32603, message: 'Session already has an active Turn.' },
      }) + '\n');
      return;
    }
    if (mode === 'advertise-refuse-steer' && prompts === 1) {
      // The first turn runs for a moment and is then cancelled, so the client
      // can send a second prompt while the session is busy.
      const id = msg.id;
      setTimeout(() => {
        process.stdout.write(JSON.stringify({
          jsonrpc: '2.0', id, result: { stopReason: 'cancelled' },
        }) + '\n');
      }, 2000);
      return;
    }
    reply({ stopReason: 'end_turn' });
    return;
  }
});
