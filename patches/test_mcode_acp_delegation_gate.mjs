#!/usr/bin/env node
// T3 Code owns the orchestration, so the agent must not spawn children of its
// own: the internal task tool would start children that never appear as T3
// threads. The proxy therefore rewrites the capability gate that registers
// that tool, and gives the marker only to the ACP child it starts.
//
//   node test_mcode_acp_delegation_gate.mjs [proxy]
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const PROXY = process.argv[2] || '/Users/prince/.minimax-code/bin/mcode-acp-proxy.js';
const FAKE = path.join(HERE, 'fake-agent.js');
const MARKER = 'MCODE_T3_OWN_DELEGATION';
let failures = 0;

function check(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ' :: ' + detail : ''}`);
  if (!ok) failures++;
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 't3-delegation-gate-'));
const release = path.join(tmp, 'release');
const chunks = path.join(release, 'lib/node_modules/@minimax-ai/code/chunks');
fs.mkdirSync(chunks, { recursive: true });

// A chunk shaped like the real one: the gate decides whether the internal
// task tool is registered at all.
const gate = 'function Ovs(e,t){let n=e.enableTaskTool!==!1&&t.features.delegation;return n}';
const chunk = path.join(chunks, 'chunk-fake.js');
fs.writeFileSync(chunk, gate);
// A chunk without the gate must be left alone.
const other = path.join(chunks, 'chunk-other.js');
fs.writeFileSync(other, 'const unrelated = 1;\n');

// The launcher stands in for the release launcher, so the release directory
// the proxy patches is the one above.
const launcher = path.join(release, 'agent.sh');
fs.writeFileSync(launcher, [
  '#!/bin/sh',
  `printf '%s' "\${${MARKER}}" > "$(dirname "$0")/marker.txt"`,
  `exec "${process.execPath}" "${FAKE}" "$@"`,
  '',
].join('\n'));
fs.chmodSync(launcher, 0o755);

function runProxy() {
  const child = spawn(process.execPath, [PROXY, launcher, 'goal', '1'], {
    stdio: ['pipe', 'pipe', 'ignore'],
  });
  child.stdin.write(JSON.stringify({
    jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: 1 },
  }) + '\n');
  return child;
}

async function waitFor(checkFn, ms = 5000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (checkFn()) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return checkFn();
}

let child = runProxy();
const patched = await waitFor(() => fs.readFileSync(chunk, 'utf8') !== gate);
check('the gate is rewritten', patched, fs.readFileSync(chunk, 'utf8').slice(0, 120));
check('the gate asks for the marker',
  fs.readFileSync(chunk, 'utf8').includes(`process.env.${MARKER}!=="1"`));
check('a chunk without the gate is untouched',
  fs.readFileSync(other, 'utf8') === 'const unrelated = 1;\n');
await waitFor(() => fs.existsSync(path.join(release, 'marker.txt')));
check('the child gets the marker',
  fs.existsSync(path.join(release, 'marker.txt'))
  && fs.readFileSync(path.join(release, 'marker.txt'), 'utf8') === '1',
  fs.existsSync(path.join(release, 'marker.txt'))
    ? fs.readFileSync(path.join(release, 'marker.txt'), 'utf8')
    : 'missing');

child.kill();

// The gate decides the tool: with the marker the ACP child has no internal
// task tool, without it a standalone TUI of the same release keeps its own.
const expression = fs.readFileSync(chunk, 'utf8').match(/let n=(.*);return/)?.[1];
const gateValue = (env) => new Function('process', 'e', 't',
  `return ${expression}`)({ env }, { enableTaskTool: true }, { features: { delegation: true } });
check('the ACP child has no internal task tool', expression && gateValue({ [MARKER]: '1' }) === false);
check('a standalone TUI keeps its own subagents', expression && gateValue({}) === true);

// Running it again changes nothing.
const before = fs.readFileSync(chunk, 'utf8');
const markerFile = path.join(release, 'marker.txt');
fs.rmSync(markerFile, { force: true });
child = runProxy();
await waitFor(() => fs.existsSync(markerFile));
child.kill();
check('the rewrite is idempotent', fs.readFileSync(chunk, 'utf8') === before);

fs.rmSync(tmp, { recursive: true, force: true });
console.log(failures === 0 ? '\nall checks passed' : `\n${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
