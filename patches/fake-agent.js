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
//   node fake-agent.js goal [turns] -> the agent answers every prompt with a
//                                          chunk of text and ends the turn. The
//                                          first "turns" turns do not finish the
//                                          goal, the last one does. It records
//                                          the calls it got in calls.json.
//
// It never actually runs a turn, so the tests are fast and do not need a login.
import fs from 'node:fs';
import readline from 'node:readline';

const mode = process.argv[2] || 'always-reject';
const failCount = Number(process.argv[3] ?? 3);
const goalTurns = Number(process.argv[3] ?? 2);
let prompts = 0;
const seen = [];

const EXTENSIONS = {
  version: 1,
  methods: ['mcode/session/steer', 'mcode/session/queue/enqueue', 'mcode/session/goal/clear'],
  notifications: ['mcode/session/queue_update'],
};

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const calls = [];
// The test reads the calls the agent got, so it can tell what the proxy did.
const writeCalls = () => {
  try {
    fs.writeFileSync(process.env.MCODE_FAKE_AGENT_CALLS || 'fake-agent-calls.json', JSON.stringify(calls));
  } catch {}
};
rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.method === undefined) return; // a response, ignore
  calls.push(msg.method);
  writeCalls();

  const reply = (result) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: msg.id, result }) + '\n');

  if (msg.method === 'initialize') {
    const result = { protocolVersion: 1, agentCapabilities: {} };
    if (mode === 'advertise-refuse-steer' || mode === 'goal') {
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
  if (msg.method === 'mcode/session/goal/clear') {
    reply({ cleared: true });
    return;
  }
  if (msg.method === 'session/prompt') {
    prompts += 1;
    seen.push(prompts);
    if (mode === 'goal') {
      // Every turn produces text the client can see. The goal is only met on
      // the last turn.
      const sessionId = msg.params?.sessionId;
      const done = prompts > goalTurns;
      const text = `goal turn ${prompts}${done ? '\nGOAL_COMPLETE' : ''}`;
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0',
        method: 'session/update',
        params: { sessionId, update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } } },
      }) + '\n');
      reply({ stopReason: 'end_turn' });
      return;
    }
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
