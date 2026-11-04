#!/usr/bin/env node
// Drives the patched proxy against the fake agent to check the resend path.
//
//   E  reject then accept : the agent rejects a few times, then accepts. The
//                            client must still get its answer.
//   F  reject forever     : the agent never accepts. The client must get an
//                            error instead of hanging, and the session must
//                            stay usable for the next prompt.
//
// Usage: node test_mcode_acp_proxy_resend.mjs [path-to-mcode-acp-proxy.js]
import { spawn } from 'node:child_process';
import readline from 'node:readline';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const PROXY = process.argv[2] || path.join(HERE, '../../../../.minimax-code/bin/mcode-acp-proxy.js');
const FAKE = path.join(HERE, 'fake-agent.js');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

function connect(mode, failCount) {
  const child = spawn(process.execPath, [PROXY, process.execPath, FAKE, mode, String(failCount)], {
    stdio: ['pipe', 'pipe', 'inherit'],
    // Keep the retry budget short so the "never accepts" scenario ends quickly.
    env: { ...process.env, MCODE_RESEND_MAX_WAIT_MS: '5000' },
  });
  let nextId = 1;
  const pending = new Map();
  const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on('line', (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.id !== undefined && msg.id !== null && msg.method === undefined) {
      const p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); p(msg); }
    }
  });
  const request = (method, params) => new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, resolve);
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
  return { child, request, stop: () => child.kill() };
}

// Wait for an answer, but give up after `limit` so a hang is observable.
function withTimeout(promise, limit) {
  return Promise.race([
    promise,
    sleep(limit).then(() => ({ timedOut: true })),
  ]);
}

async function newSession(c) {
  await c.request('initialize', { protocolVersion: 1, clientCapabilities: {} });
  const created = await c.request('session/new', { cwd: process.cwd(), mcpServers: [] });
  return created.result.sessionId;
}

// E: a rejection that clears after a few attempts must still produce an answer.
async function scenarioE() {
  console.log('--- scenario E: rejected twice, then accepted ---');
  const c = connect('reject-then-ok', 2);
  const sessionId = await newSession(c);
  const res = await withTimeout(
    c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'hi' }] }),
    10000,
  );
  check('E client got a response', !res.timedOut, res.timedOut ? 'no response within 10s' : JSON.stringify(res.result));
  check('E response is not an error', !res.error, res.error ? JSON.stringify(res.error) : '');
  c.stop();
}

// F: a rejection that never clears must not hang the client forever.
async function scenarioF() {
  console.log('--- scenario F: rejected forever ---');
  const c = connect('always-reject', 0);
  const sessionId = await newSession(c);
  const started = Date.now();
  const res = await withTimeout(
    c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'hi' }] }),
    30000,
  );
  const waited = Date.now() - started;
  check('F client got a response', !res.timedOut, res.timedOut ? `no response after ${waited}ms` : JSON.stringify(res.result ?? res.error));
  check('F the agent error reached the client', Boolean(res.error), res.error ? JSON.stringify(res.error) : 'no error payload');

  // The session must not be bricked: a later prompt has to be handled too.
  const follow = await withTimeout(
    c.request('session/prompt', { sessionId, prompt: [{ type: 'text', text: 'again' }] }),
    30000,
  );
  check('F session still answers the next prompt', !follow.timedOut, follow.timedOut ? 'no response within 30s' : JSON.stringify(follow.result ?? follow.error));
  c.stop();
}

await scenarioE();
await scenarioF();

console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
