#!/usr/bin/env node
// Scenario tests for the mcode ACP proxy steering fix.
//
//   A  force send   : session/cancel + session/prompt back to back (T3 steer)
//   B  prompt only  : session/prompt while a turn runs, no cancel from the client
//   C  idle cancel  : session/cancel while nothing runs, then a normal prompt
//   D  two prompts  : two session/prompt messages while a turn runs
//
// Usage: node patches/test_mcode_acp_steer.mjs [path-to-mcode-acp]
// Needs a logged-in MiniMax Code runtime; each scenario drives a real agent turn.
import { spawn } from 'node:child_process';
import readline from 'node:readline';

const target = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp';
const CWD = process.env.MCODE_STEER_CWD || process.cwd();

function connect() {
  const child = spawn(target, [], { stdio: ['pipe', 'pipe', 'inherit'] });
  let nextId = 1;
  const pending = new Map();
  const answers = [];
  const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.method === 'session/update') {
      const u = msg.params?.update;
      if (u?.sessionUpdate === 'agent_message_chunk') {
        const c = u.content;
        const txt = c?.text ?? (c?.content ?? []).map(x => x.text ?? '').join('');
        if (txt) answers.push(txt);
      }
      return;
    }
    if (msg.id !== undefined && msg.id !== null && msg.method === undefined) {
      const p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); p(msg); }
    }
  });
  const send = (m) => child.stdin.write(JSON.stringify(m) + '\n');
  const request = (method, params) => new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, resolve);
    send({ jsonrpc: '2.0', id, method, params });
  });
  const notify = (method, params) => send({ jsonrpc: '2.0', method, params });
  return { child, request, notify, answers, stop: () => child.kill() };
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const LONG = 'Use the bash tool to run exactly: sleep 20; echo LONG_DONE . Then reply with the word DONE.';
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

async function newSession(c) {
  await c.request('initialize', {
    protocolVersion: 1,
    clientCapabilities: { fs: { readTextFile: true, writeTextFile: true }, terminal: true },
  });
  const created = await c.request('session/new', { cwd: CWD, mcpServers: [] });
  return created.result.sessionId;
}

// A: the T3 force send - cancel and the steering prompt sent back to back.
async function scenarioA() {
  const c = connect();
  const sessionId = await newSession(c);
  const first = c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: LONG }] });
  await sleep(3000);
  c.notify('session/cancel', { sessionId });
  const steer = c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'Reply with exactly STEER_A_OK.' }] });
  const steerRes = await steer;
  const firstRes = await first;
  const said = c.answers.join('');
  check('A steer prompt answered', !steerRes.error, steerRes.error ? `${steerRes.error.code} ${steerRes.error.message}` : JSON.stringify(steerRes.result));
  check('A model followed the steering message', said.includes('STEER_A_OK'), said.slice(-80));
  check('A cancelled turn reported cancelled', firstRes.result?.stopReason === 'cancelled', JSON.stringify(firstRes.result));
  c.stop();
}

// B: a prompt arrives while a turn runs and the client sends no cancel.
async function scenarioB() {
  const c = connect();
  const sessionId = await newSession(c);
  const first = c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: LONG }] });
  await sleep(3000);
  const steer = c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'Reply with exactly STEER_B_OK.' }] });
  const steerRes = await steer;
  const said = c.answers.join('');
  check('B prompt answered', !steerRes.error, steerRes.error ? `${steerRes.error.code} ${steerRes.error.message}` : JSON.stringify(steerRes.result));
  check('B model followed the steering message', said.includes('STEER_B_OK'), said.slice(-80));
  await first;
  c.stop();
}

// C: a cancel arrives while nothing is running - it must not swallow the next prompt.
async function scenarioC() {
  const c = connect();
  const sessionId = await newSession(c);
  c.notify('session/cancel', { sessionId });
  await sleep(500);
  c.answers.length = 0;
  const res = await c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'Reply with exactly STEER_C_OK.' }] });
  const said = c.answers.join('');
  check('C prompt after idle cancel answered', !res.error, res.error ? `${res.error.code} ${res.error.message}` : JSON.stringify(res.result));
  check('C prompt after idle cancel not swallowed', said.includes('STEER_C_OK'), said.slice(-80));
  c.stop();
}

// D: two prompts while a turn runs - both must be answered.
async function scenarioD() {
  const c = connect();
  const sessionId = await newSession(c);
  const first = c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: LONG }] });
  await sleep(3000);
  const p1 = c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'Reply with exactly STEER_D1_OK.' }] });
  const p2 = c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'Reply with exactly STEER_D2_OK.' }] });
  const [r1, r2] = [await p1, await p2];
  const said = c.answers.join('');
  check('D first queued prompt answered', !r1.error, r1.error ? `${r1.error.code} ${r1.error.message}` : JSON.stringify(r1.result));
  check('D second queued prompt answered', !r2.error, r2.error ? `${r2.error.code} ${r2.error.message}` : JSON.stringify(r2.result));
  check('D both steering messages reached the model', said.includes('STEER_D1_OK') && said.includes('STEER_D2_OK'), said.slice(-120));
  await first;
  c.stop();
}

console.log('--- scenario A: force send (cancel + prompt back to back) ---');
await scenarioA();
console.log('--- scenario B: prompt only while a turn runs ---');
await scenarioB();
console.log('--- scenario C: cancel while idle ---');
await scenarioC();
console.log('--- scenario D: two prompts while a turn runs ---');
await scenarioD();

console.log(failures === 0 ? '\nall scenarios passed' : `\n${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
