#!/usr/bin/env node
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');

const activeElicitations = new Map();
const pendingPrompts = new Map();
const recoveryPrompts = new Map();
const cancelledSessions = new Set();
// The agent runs one prompt turn per session at a time. A second session/prompt
// that arrives while a turn is still running is rejected with
// "A prompt is already active for this session.", so a steering message sent
// during a turn has to wait until the running turn has been cancelled and has
// finished. These maps track that per session.
const runningPrompts = new Map(); // sessionId -> id of the prompt turn running upstream
const queuedPrompts = new Map(); // sessionId -> prompts waiting for the running turn to end
const cancelSent = new Set(); // sessions whose current turn was already cancelled upstream
const holdTimers = new Map(); // sessionId -> timer that force-sends a held prompt
const HOLD_TIMEOUT_MS = 30000;
// The runtime keeps ownership of a turn for a moment after the ACP prompt was
// cancelled, and refuses a new prompt with "Session already has an active
// Turn." until it lets go.
const TURN_BUSY_PATTERN = /already has an active turn/i;
const TURN_BUSY_MAX_ATTEMPTS = 8;
const TURN_BUSY_BASE_DELAY_MS = 150;
const TURN_BUSY_MAX_DELAY_MS = 2000;
const maxRecoveryAttempts = { gateway: 2, tpm: 3 };
const recoveryPolicies = {
  gateway: {
    delaysMs: [1000, 3000],
    label: '502 gateway error',
    prompt: 'The previous turn in this session was interrupted by a MiniMax Code Runtime HTTP 502. Continue the original task from the current conversation and workspace. First inspect the latest conversation, tool results, and workspace state. Do not repeat completed side effects. Finish only the remaining work. If the service fails again, report the current checkpoint clearly.',
  },
  tpm: {
    delaysMs: [60000],
    jitterMs: 5000,
    label: 'TPM rate limit',
    prompt: 'The previous turn in this session was interrupted by MiniMax Code Runtime error 1039, a TPM token limit. Continue the original task from the current conversation and workspace. First inspect the latest conversation, tool results, and workspace state. Do not repeat completed side effects. Finish only the remaining work. If the token limit still blocks the request, report the current checkpoint clearly.',
  },
};
let recoverySequence = 0;
let writeChildMessage;

function idKey(id) {
  return `${typeof id}:${String(id)}`;
}

function sessionIdOf(msg) {
  const params = msg && msg.params;
  return params && typeof params.sessionId === 'string' ? params.sessionId : null;
}

// A turn is running upstream for this session.
function occupySession(sessionId, id) {
  runningPrompts.set(sessionId, id);
  cancelSent.delete(sessionId);
}

// The turn finished upstream. Send the next queued prompt, if any.
function releaseSession(sessionId) {
  runningPrompts.delete(sessionId);
  cancelSent.delete(sessionId);
  const timer = holdTimers.get(sessionId);
  if (timer) {
    clearTimeout(timer);
    holdTimers.delete(sessionId);
  }
  const queue = queuedPrompts.get(sessionId);
  if (!queue || queue.length === 0) return;
  const next = queue.shift();
  if (queue.length === 0) queuedPrompts.delete(sessionId);
  pendingPrompts.set(idKey(next.id), { id: next.id, sessionId, message: next });
  occupySession(sessionId, next.id);
  writeChildMessage(next);
}

function isTurnBusyError(error) {
  return Boolean(error && typeof error.message === 'string' && TURN_BUSY_PATTERN.test(error.message));
}

// The runtime still owns the cancelled turn. Resend the prompt until it lets go.
function retryTurnBusyPrompt(request) {
  if (!request.message || !writeChildMessage) return false;
  const attempt = (request.turnBusyAttempts ?? 0) + 1;
  if (attempt > TURN_BUSY_MAX_ATTEMPTS) return false;
  request.turnBusyAttempts = attempt;
  const delay = Math.min(TURN_BUSY_BASE_DELAY_MS * 2 ** (attempt - 1), TURN_BUSY_MAX_DELAY_MS);
  const timer = setTimeout(() => {
    try {
      writeChildMessage(request.message);
    } catch {}
  }, delay);
  if (typeof timer.unref === 'function') timer.unref();
  return true;
}

// Hold a steering prompt until the running turn has ended.
function queuePrompt(sessionId, msg) {
  if (!writeChildMessage) return false;
  const queue = queuedPrompts.get(sessionId) ?? [];
  queue.push(msg);
  queuedPrompts.set(sessionId, queue);
  if (!cancelSent.has(sessionId)) {
    cancelSent.add(sessionId);
    writeChildMessage({ jsonrpc: '2.0', method: 'session/cancel', params: { sessionId } });
  }
  if (holdTimers.has(sessionId)) return true;
  const timer = setTimeout(() => {
    holdTimers.delete(sessionId);
    const pending = queuedPrompts.get(sessionId);
    if (!pending || pending.length === 0) return;
    // The running turn never reported back. Send the prompt anyway so the
    // client gets an answer instead of waiting forever.
    releaseSession(sessionId);
  }, HOLD_TIMEOUT_MS);
  if (typeof timer.unref === 'function') timer.unref();
  holdTimers.set(sessionId, timer);
  return true;
}

function runtimeFailureKind(error) {
  const message = error && typeof error === 'object' ? error.message : error;
  const text = String(message || '');
  if (/MiniMax Code Runtime failed[\s\S]*(?:rate limit exceeded\s*\(\s*TPM\s*\)|\b1039\b)/i.test(text)) return 'tpm';
  if (/Runtime continuation failed[\s\S]*(?:\b502\b|Bad Gateway)/i.test(text)) return 'gateway';
  return null;
}

function recoveryError(request, attempts, kind) {
  return {
    jsonrpc: '2.0',
    id: request.id,
    error: {
      code: -32603,
      message: `MiniMax Code Runtime ${recoveryPolicies[kind].label} persisted after ${attempts} recovery attempt${attempts === 1 ? '' : 's'}. Session ${request.sessionId} is preserved; continue in this same T3 thread.`,
    },
  };
}

function beginRecovery(request, attempt, kind) {
  // shortcut: TPM errors carry no reset hint; use Retry-After if the Runtime exposes it.
  const policy = recoveryPolicies[kind];
  const delayMs = policy.delaysMs[Math.min(attempt - 1, policy.delaysMs.length - 1)] + Math.floor(Math.random() * (policy.jitterMs || 0));
  const recoveryId = `mcode-acp-recovery-${process.pid}-${++recoverySequence}`;
  const recoveryKey = idKey(recoveryId);
  recoveryPrompts.set(recoveryKey, { request, attempt, kind });

  setTimeout(() => {
    if (cancelledSessions.has(request.sessionId)) {
      recoveryPrompts.delete(recoveryKey);
      pendingPrompts.delete(idKey(request.id));
      cancelledSessions.delete(request.sessionId);
      releaseSession(request.sessionId);
      process.stdout.write(JSON.stringify({
        jsonrpc: '2.0',
        id: request.id,
        error: { code: -32800, message: 'ACP recovery was cancelled.' },
      }) + '\n');
      return;
    }

    try {
      // Resume from persisted session state; replaying the original prompt could repeat completed tool calls.
      writeChildMessage({
        jsonrpc: '2.0',
        id: recoveryId,
        method: 'session/prompt',
        params: {
          sessionId: request.sessionId,
          prompt: [{
            type: 'text',
            text: policy.prompt,
          }],
        },
      });
      occupySession(request.sessionId, recoveryId);
    } catch {
      recoveryPrompts.delete(recoveryKey);
      pendingPrompts.delete(idKey(request.id));
      releaseSession(request.sessionId);
      process.stdout.write(JSON.stringify(recoveryError(request, attempt, kind)) + '\n');
    }
  }, delayMs);
}

function patchReleaseChunks(targetLauncher) {
  try {
    const releaseDir = path.dirname(targetLauncher);
    const chunksDir = path.join(releaseDir, 'lib/node_modules/@minimax-ai/code/chunks');
    if (!fs.existsSync(chunksDir)) return;
    const files = fs.readdirSync(chunksDir);
    for (const f of files) {
      if (!f.startsWith('run-acp-command-') || !f.endsWith('.js') || f.endsWith('.bak')) continue;
      const fullPath = path.join(chunksDir, f);
      let code = fs.readFileSync(fullPath, 'utf8');
      let changed = false;

      const p1 = 'if(e.type==="questionnaire.dismiss"||e.type==="questionnaire.superseded")return ho("questionnaire",e.requestId,e.sessionId);';
      const r1 = 'if(e.type==="questionnaire.superseded")return ho("questionnaire",e.requestId,e.sessionId);';
      if (code.includes(p1)) {
        code = code.replace(p1, r1);
        changed = true;
      }

      const p2 = 'if(t.type==="questionnaire.dismiss"||t.type==="questionnaire.superseded"){this.settleQuestionnaire(t.requestId,!1);return}';
      const r2 = 'if(t.type==="questionnaire.superseded"){this.settleQuestionnaire(t.requestId,!1);return}';
      if (code.includes(p2)) {
        code = code.replace(p2, r2);
        changed = true;
      }

      const p3 = 'settleQuestionnaire(t,r){let o=this.questionTurnByRequestId.get(t);o&&(this.questionnaireOutcomes.set(o,r),this.wake())}';
      const r3 = 'settleQuestionnaire(t,r){let o=this.questionTurnByRequestId.get(t);if(o){if(this.questionnaireOutcomes.get(o)===!0&&r===!1)return;this.questionnaireOutcomes.set(o,r),this.wake()}}';
      if (code.includes(p3)) {
        code = code.replace(p3, r3);
        changed = true;
      }

      if (changed) {
        fs.writeFileSync(fullPath, code, 'utf8');
      }
    }
  } catch {}
}

function interceptOutgoing(line) {
  if (!line || !line.trim()) return line;
  try {
    const msg = JSON.parse(line);
    if (msg && !msg.method && msg.id !== undefined && msg.id !== null) {
      const responseKey = idKey(msg.id);
      const recovery = recoveryPrompts.get(responseKey);
      if (recovery) {
        recoveryPrompts.delete(responseKey);
        const failureKind = msg.error && runtimeFailureKind(msg.error);
        if (failureKind && recovery.attempt < maxRecoveryAttempts[failureKind]) {
          beginRecovery(recovery.request, recovery.attempt + 1, failureKind);
          return null;
        }

        pendingPrompts.delete(idKey(recovery.request.id));
        cancelledSessions.delete(recovery.request.sessionId);
        if (failureKind) {
          releaseSession(recovery.request.sessionId);
          return JSON.stringify(recoveryError(recovery.request, recovery.attempt, failureKind));
        }
        msg.id = recovery.request.id;
        releaseSession(recovery.request.sessionId);
        return JSON.stringify(msg);
      }

      const request = pendingPrompts.get(responseKey);
      if (request) {
        const failureKind = msg.error && runtimeFailureKind(msg.error);
        if (failureKind && writeChildMessage) {
          beginRecovery(request, 1, failureKind);
          return null;
        }
        if (msg.error && isTurnBusyError(msg.error) && retryTurnBusyPrompt(request)) {
          // Resent upstream; the client keeps waiting for the answer.
          return null;
        }
        pendingPrompts.delete(responseKey);
        cancelledSessions.delete(request.sessionId);
        releaseSession(request.sessionId);
      }
    }
    if (msg && msg.method === 'elicitation/create' && msg.params) {
      const params = msg.params;
      const schema = params.requestedSchema;
      if (schema && schema.type === 'object' && schema.properties) {
        const props = schema.properties;
        const required = Array.isArray(schema.required) ? schema.required : [];
        const stepMap = {};
        const keysToDelete = [];

        for (const key of Object.keys(props)) {
          if (key.includes('__other')) {
            const baseKey = key.split('__other')[0];
            keysToDelete.push(key);
            if (!stepMap[baseKey]) {
              stepMap[baseKey] = { otherKey: key, options: [] };
            } else {
              stepMap[baseKey].otherKey = key;
            }
          }
        }

        for (const k of keysToDelete) {
          delete props[k];
          const reqIdx = required.indexOf(k);
          if (reqIdx !== -1) required.splice(reqIdx, 1);
        }

        for (const baseKey of Object.keys(props)) {
          const prop = props[baseKey];
          if (prop && Array.isArray(prop.oneOf) && prop.oneOf.length > 0) {
            const options = prop.oneOf.map(o => ({
              id: o.const !== undefined ? String(o.const) : '',
              label: o.title || (o.const !== undefined ? String(o.const) : '')
            }));

            if (!stepMap[baseKey]) {
              stepMap[baseKey] = { options };
            } else {
              stepMap[baseKey].options = options;
            }

            if (!Array.isArray(prop.enum) || prop.enum.length === 0) {
              prop.enum = options.map(o => o.label || o.id);
            }
          }
        }

        if (msg.id !== undefined && msg.id !== null) {
          activeElicitations.set(String(msg.id), { stepMap });
        }

        return JSON.stringify(msg);
      }
    }
    return line;
  } catch {
    return line;
  }
}

function interceptIncoming(line) {
  if (!line || !line.trim()) return line;
  try {
    const msg = JSON.parse(line);
    if (msg && msg.method === 'session/prompt' && msg.params && msg.id !== undefined && msg.id !== null) {
      const sessionId = sessionIdOf(msg);
      if (sessionId === null) return line;
      cancelledSessions.delete(sessionId);
      if (runningPrompts.has(sessionId)) {
        // A turn is still running for this session. The agent rejects a second
        // prompt, so hold this one until the running turn has been cancelled
        // and has finished.
        if (queuePrompt(sessionId, msg)) return null;
      }
      pendingPrompts.set(idKey(msg.id), { id: msg.id, sessionId, message: msg });
      occupySession(sessionId, msg.id);
    }
    if (msg && msg.method === 'session/cancel') {
      const sessionId = sessionIdOf(msg);
      if (sessionId === null) return line;
      cancelledSessions.add(sessionId);
      if (!runningPrompts.has(sessionId)) {
        // Nothing is running for this session. Forwarding the cancel would make
        // the agent swallow the next prompt of that session, so drop it.
        return null;
      }
      if (cancelSent.has(sessionId)) {
        // The turn was already cancelled. A second cancel would poison the next
        // prompt of that session, so drop it.
        return null;
      }
      cancelSent.add(sessionId);
    }
    if (msg && msg.id !== undefined && msg.id !== null) {
      const idStr = String(msg.id);
      if (activeElicitations.has(idStr)) {
        const { stepMap } = activeElicitations.get(idStr);
        activeElicitations.delete(idStr);

        if (msg.result && msg.result.action === 'accept' && msg.result.content && typeof msg.result.content === 'object') {
          const content = msg.result.content;
          for (const baseKey of Object.keys(stepMap)) {
            const info = stepMap[baseKey];
            const userVal = content[baseKey];
            if (userVal !== undefined && info && info.otherKey) {
              const opts = info.options || [];
              const isOption = opts.some(o => o.id === String(userVal) || o.label === String(userVal));
              if (!isOption && typeof userVal === 'string' && userVal.trim().length > 0) {
                content[info.otherKey] = userVal;
              }
            }
          }
        }
        return JSON.stringify(msg);
      }
    }
    return line;
  } catch {
    return line;
  }
}

function startProxy(targetLauncher, targetArgs) {
  patchReleaseChunks(targetLauncher);

  const child = spawn(targetLauncher, targetArgs, {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: process.env
  });
  writeChildMessage = (message) => child.stdin.write(JSON.stringify(message) + '\n');

  const rlOut = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  rlOut.on('line', (line) => {
    const outgoing = interceptOutgoing(line);
    if (outgoing !== null) process.stdout.write(outgoing + '\n');
  });

  const rlIn = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rlIn.on('line', (line) => {
    const outgoing = interceptIncoming(line);
    if (outgoing !== null) child.stdin.write(outgoing + '\n');
  });
  rlIn.on('close', () => child.stdin.end());

  const forwardSignal = (signal) => { try { child.kill(signal); } catch {} };
  child.on('close', (code, signal) => {
    if (signal) {
      process.removeListener(signal, forwardSignal);
      process.kill(process.pid, signal);
    }
    else process.exit(code ?? 0);
  });

  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(sig, forwardSignal);
  }
}

if (require.main === module) {
  const targetLauncher = process.argv[2];
  const targetArgs = process.argv.slice(3);
  if (!targetLauncher) {
    console.error("Usage: mcode-acp-proxy.js <path-to-launcher> [args...]");
    process.exit(1);
  }
  startProxy(targetLauncher, targetArgs);
} else {
  module.exports = { activeElicitations, interceptOutgoing, interceptIncoming, patchReleaseChunks };
}
