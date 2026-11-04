#!/usr/bin/env python3
"""Apply the steering fix to the mcode ACP proxy.

T3 Code cannot steer an ACP session that is already running a turn: it sends
`session/cancel` and the new `session/prompt` back to back, and the agent
rejects the second prompt with

    -32602 Invalid params: A prompt is already active for this session.

because the cancelled turn has not finished yet. Once that turn has ended, the
runtime still owns it for a moment and answers with

    -32603 Session already has an active Turn.

Either error kills the turn, which leaves the thread unusable.

The proxy sits between T3 and the agent, so it can fix this without touching
the agent: it holds a prompt that arrives during a running turn, releases it
once that turn has finished, and resends it while the runtime still reports the
turn as busy.

The fix lives in the transport layer (the stdin/stdout plumbing), not in the
prompt and response handlers, so it applies to every revision of the proxy:
machines run older revisions whose recovery handling differs, and this only
needs the plumbing to look the same.

The patcher edits the proxy by shape rather than by byte offset, and refuses to
touch a file it does not recognise.

    python3 apply-mcode-acp-steer.py [path-to-mcode-acp-proxy.js]
"""
import os
import shutil
import subprocess
import sys
import time

DEFAULT = os.path.expanduser("~/.minimax-code/bin/mcode-acp-proxy.js")
TARGET = sys.argv[1] if len(sys.argv) > 1 else DEFAULT

MARKER = "RETRYABLE_REJECTION"

STATE_OLD = """const cancelledSessions = new Set();
"""

STATE_NEW = r"""const cancelledSessions = new Set();
// The agent runs one prompt turn per session at a time. A second session/prompt
// that arrives while a turn is still running is rejected with "A prompt is
// already active for this session.", and once the turn has ended the runtime
// still owns it for a moment and rejects the next prompt with "Session already
// has an active Turn.". T3 force sends a steering message by sending
// session/cancel and the new session/prompt back to back, so the proxy holds
// the prompt until the running turn has finished, and resends it while the
// runtime still reports the turn as busy.
const upstreamPrompts = new Map(); // sessionId -> request ids in flight upstream
const sentPrompts = new Map(); // request id key -> { sessionId, message }
const queuedPrompts = new Map(); // sessionId -> prompts waiting for the running turn to end
const cancelSent = new Set(); // sessions whose current turn was already cancelled upstream
const holdTimers = new Map(); // sessionId -> timer that force-sends a held prompt
const nativeMethods = new Set(); // extension methods the agent advertised
const proxyRequests = new Map(); // proxy request id -> { sessionId, pending }
const pendingSteers = new Map(); // sessionId -> steer attempt waiting for the agent's answer
const steeringSessions = new Map(); // sessionId -> client request id waiting for the steered turn
const goalStates = new Map(); // sessionId -> { goalId, status }
const newSessionRequests = new Set(); // request ids of session/new calls
const RETRYABLE_REJECTION = /already has an active turn|prompt is already active/i;
// A goal turn or a long tool call can hold the session for minutes, so a
// rejected prompt is retried until it is accepted or the budget runs out.
const RESEND_MAX_WAIT_MS = Number(process.env.MCODE_RESEND_MAX_WAIT_MS || 600000);
const RESEND_BASE_DELAY_MS = 150;
const RESEND_MAX_DELAY_MS = 2000;
const HOLD_TIMEOUT_MS = 30000;
const STEER_TIMEOUT_MS = 15000;
const STEER_RELAY_TIMEOUT_MS = 300000;
let proxySequence = 0;
"""

IDKEY_OLD = r"""function idKey(id) {
  return `${typeof id}:${String(id)}`;
}
"""

IDKEY_NEW = r"""function idKey(id) {
  return `${typeof id}:${String(id)}`;
}

function parseMessage(text) {
  try {
    const msg = JSON.parse(text);
    return msg && typeof msg === 'object' ? msg : null;
  } catch {
    return null;
  }
}

function sessionIdOf(msg) {
  const params = msg && msg.params;
  return params && typeof params.sessionId === 'string' ? params.sessionId : null;
}

function isBusySession(sessionId) {
  const set = upstreamPrompts.get(sessionId);
  return Boolean(set && set.size > 0);
}

// A prompt went upstream: remember it so its response can release the session.
function trackUpstreamPrompt(text) {
  const msg = parseMessage(text);
  if (!msg || msg.method !== 'session/prompt') return;
  const sessionId = sessionIdOf(msg);
  if (sessionId === null || msg.id === undefined || msg.id === null) return;
  const key = idKey(msg.id);
  let set = upstreamPrompts.get(sessionId);
  if (!set) {
    set = new Set();
    upstreamPrompts.set(sessionId, set);
  }
  set.add(key);
  // A resend reuses the request id, so keep the attempts already spent:
  // resetting it here would make the retry budget never run out.
  const previous = sentPrompts.get(key);
  sentPrompts.set(key, {
    sessionId,
    message: text,
    attempts: previous ? previous.attempts : 0,
    firstSentAt: previous ? previous.firstSentAt : Date.now(),
  });
  cancelSent.delete(sessionId);
}

// The turn ended: release the session and send a prompt that was held for it.
function releaseUpstreamPrompt(text) {
  const msg = parseMessage(text);
  if (!msg || msg.method || msg.id === undefined || msg.id === null) return;
  const key = idKey(msg.id);
  const sent = sentPrompts.get(key);
  if (!sent) return;
  sentPrompts.delete(key);
  const set = upstreamPrompts.get(sent.sessionId);
  if (!set) return;
  set.delete(key);
  if (set.size > 0) return;
  upstreamPrompts.delete(sent.sessionId);
  cancelSent.delete(sent.sessionId);
  const timer = holdTimers.get(sent.sessionId);
  if (timer) {
    clearTimeout(timer);
    holdTimers.delete(sent.sessionId);
  }
  const queue = queuedPrompts.get(sent.sessionId);
  if (!queue || queue.length === 0) return;
  const next = queue.shift();
  if (queue.length === 0) queuedPrompts.delete(sent.sessionId);
  writeChildMessage(next);
}

// The agent refused the prompt because the turn is still running. Resend it.
function retryableRejection(text) {
  const msg = parseMessage(text);
  if (!msg || msg.method || msg.id === undefined || msg.id === null) return null;
  if (!msg.error) return null;
  if (!RETRYABLE_REJECTION.test(String(msg.error.message || ''))) return null;
  return sentPrompts.get(idKey(msg.id)) ?? null;
}

function scheduleResend(sent) {
  const attempt = (sent.attempts || 0) + 1;
  if (Date.now() - (sent.firstSentAt || Date.now()) >= RESEND_MAX_WAIT_MS) return false;
  sent.attempts = attempt;
  const delay = Math.min(RESEND_BASE_DELAY_MS * 2 ** (attempt - 1), RESEND_MAX_DELAY_MS);
  const timer = setTimeout(() => {
    try {
      writeChildMessage(sent.message);
    } catch {}
  }, delay);
  if (typeof timer.unref === 'function') timer.unref();
  return true;
}

// The agent kept refusing the prompt: report the failure to the client instead
// of leaving the request unanswered, and release the session so the next
// prompt is not held forever.
function failRejectedPrompt(line) {
  const outgoing = interceptOutgoing(line);
  if (outgoing !== null) process.stdout.write(outgoing + '\n');
  releaseUpstreamPrompt(line);
}

// Hold a steering prompt until the running turn has ended.
function holdPrompt(sessionId, message, clientRequestId) {
  // The agent's native queue cannot own this: a cancelled session has its
  // queue paused, and a paused queue refuses every later turn ("The session
  // Queue is paused"). The local queue keeps the client's request id, so the
  // agent's real answer still reaches the client.
  queueLocally(sessionId, message);
}

// The local queue: the proxy keeps the message until the running turn ends.
function queueLocally(sessionId, message) {
  const queue = queuedPrompts.get(sessionId) ?? [];
  queue.push(message);
  queuedPrompts.set(sessionId, queue);
  // A steer is already steering this turn: cancelling it now would kill the
  // message that was just admitted.
  const steering = pendingSteers.has(sessionId) || steeringSessions.has(sessionId);
  if (!cancelSent.has(sessionId) && !steering) {
    cancelSent.add(sessionId);
    writeChildMessage(JSON.stringify({ jsonrpc: '2.0', method: 'session/cancel', params: { sessionId } }));
  }
  if (holdTimers.has(sessionId)) return;
  const timer = setTimeout(() => {
    holdTimers.delete(sessionId);
    const pending = queuedPrompts.get(sessionId);
    if (!pending || pending.length === 0) return;
    // The running turn never reported back. Send the prompt anyway so the
    // client gets an answer instead of waiting forever.
    upstreamPrompts.delete(sessionId);
    const next = pending.shift();
    if (pending.length === 0) queuedPrompts.delete(sessionId);
    writeChildMessage(next);
  }, HOLD_TIMEOUT_MS);
  if (typeof timer.unref === 'function') timer.unref();
  holdTimers.set(sessionId, timer);
}

// The agent has no goal slash command, so the proxy advertises one to the
// client: without it the goal control is invisible in the composer.
const GOAL_COMMAND = {
  name: 'goal',
  description: 'Set an autonomous goal loop (works systematically until the goal is achieved).',
};

function addGoalCommand(line) {
  const msg = parseMessage(line);
  if (!msg) return line;
  let changed = false;
  const add = (list) => {
    if (!Array.isArray(list)) return;
    if (list.some((command) => command && command.name === GOAL_COMMAND.name)) return;
    list.push({ ...GOAL_COMMAND });
    changed = true;
  };
  if (msg.result) {
    add(msg.result.slashCommands);
    // The agent sends no command list in the session result, so the client
    // would never see the goal command outside the notification.
    if (newSessionRequests.has(idKey(msg.id))) {
      if (!Array.isArray(msg.result.slashCommands)) {
        msg.result.slashCommands = [];
        changed = true;
      }
      add(msg.result.slashCommands);
      newSessionRequests.delete(idKey(msg.id));
    }
  }
  if (msg.method === 'session/update') {
    const update = msg.params?.update;
    if (update) {
      add(update.availableCommands);
      add(update.available_commands);
    }
  }
  return changed ? JSON.stringify(msg) : line;
}

// Decide what to do with a message the client sent: drop it, hold it, steer
// with it, or pass it on.
function steerIncoming(text) {
  const msg = parseMessage(text);
  if (!msg || !msg.method) return 'pass';
  const sessionId = sessionIdOf(msg);
  if (sessionId === null) return 'pass';
  if (msg.method === 'session/cancel') {
    // Nothing is running for this session: the agent would use the cancel to
    // swallow the next prompt of that session, so drop it.
    if (!isBusySession(sessionId)) return 'drop';
    // The turn was already cancelled. A second cancel has the same effect.
    if (cancelSent.has(sessionId)) return 'drop';
    // T3 asks for a force send with a cancel and the next prompt back to
    // back. The cancel goes straight through: it is what interrupts the
    // running turn, and the agent's native steer cannot do that.
    cancelSent.add(sessionId);
    return 'pass';
  }
  if (msg.method === 'session/prompt' && msg.id !== undefined && msg.id !== null) {
    if (isBusySession(sessionId)) {
      // A cancel is already on its way: the message belongs to the next turn.
      if (!cancelSent.has(sessionId) && tryNativeSteer(sessionId, msg.id, text)) return 'steer';
      holdPrompt(sessionId, text, msg.id);
      return 'hold';
    }
  }
  return 'pass';
}

// The text of a client prompt, used to steer the running turn.
function promptTextOf(message) {
  const msg = parseMessage(message);
  const prompt = msg?.params?.prompt;
  if (typeof prompt === 'string') return prompt;
  if (!Array.isArray(prompt)) return '';
  return prompt
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n');
}

// A "/goal ..." prompt from the client, mapped to the agent's native goal API.
//
// The literal command family, plus the natural language the goal-mode plugin
// used to detect ("set my goal to ...", "pause my goal", "what is my current
// goal"), so removing the plugin did not cost the user their phrasing.
function parseGoalCommand(message) {
  const msg = parseMessage(message);
  if (!msg || msg.method !== 'session/prompt') return null;
  if (msg.id === undefined || msg.id === null) return null;
  if (sessionIdOf(msg) === null) return null;
  const text = promptTextOf(message).trim();
  if (!text) return null;

  if (text.startsWith('/goal')) {
    const rest = text.slice(5).trim();
    if (!rest) return { kind: 'get' };
    const [head, ...tail] = rest.split(/\s+/);
    const word = head.toLowerCase();
    const argument = tail.join(' ').trim();
    if (word === 'status') return { kind: 'get' };
    if (word === 'pause') return { kind: 'pause' };
    if (word === 'resume') return { kind: 'resume' };
    if (word === 'stop' || word === 'cancel' || word === 'clear') return { kind: 'clear' };
    if (word === 'edit' || word === 'replace') return { kind: 'objective', argument };
    // "history" and "plan" have no native equivalent: they stay ordinary prompts.
    if (word === 'history' || word === 'plan' || word === 'edit-plan') return null;
    return { kind: 'create', argument: rest };
  }

  if (!/goal/i.test(text)) return null;

  const controls = [
    [/\/goal\s+(stop|cancel)\b|^(cancel|stop|abort)\s+(the\s+|my\s+|this\s+)?goal\b/i, 'clear'],
    [/\/goal\s+(clear|off|reset|none)\b|^(clear|remove|drop|forget)\s+(the\s+|my\s+|this\s+)?goal\b/i, 'clear'],
    [/\/goal\s+(pause|hold)\b|^(pause|hold|suspend|stop working on)\s+(the\s+|my\s+|this\s+)?goal\b|\/pause_goal\b/i, 'pause'],
    [/\/goal\s+(resume|start|continue|run)\b|^(resume|restart|continue)\s+(the\s+|my\s+|this\s+)?goal\b|\/resume_goal\b/i, 'resume'],
    [/\/goal\s+status\b|^goal\s+status\b|\bcurrent goal\b|\bshow (me )?(my )?goal\b|\bgoal state\b/i, 'get'],
  ];
  for (const [pattern, kind] of controls) {
    if (pattern.test(text)) return { kind };
  }

  // The plan family has no native equivalent, so those stay ordinary prompts.
  if (/\/goal\s+history\b|^goal\s+history\b|goal\s+lifecycle\b/i.test(text)) return null;
  if (/\/goal\s+(plan|edit-plan)\b|^(add|update|revise|set)\s+(the\s+)?(goal\s+)?plan\b/i.test(text)) return null;

  const objectiveEdit = text.match(/\/goal\s+edit\s+(.+)$/is)
    ?? text.match(/(?:change|update|revise|rewrite)\s+(?:the\s+|my\s+)?goal\s+(?:to|into)\s+(.+)$/is)
    ?? text.match(/\/goal\s+replace\s+(.+)$/is)
    ?? text.match(/(?:replace|swap)\s+(?:the\s+|my\s+)?goal\s+(?:with|for|to)\s+(.+)$/is);
  if (objectiveEdit) return { kind: 'objective', argument: objectiveEdit[1].trim() };

  const objective =
    text.match(/\/goal\s+(.+)$/s)?.[1] ??
    text.match(/^\s*goal\s*:\s*(.+)$/is)?.[1] ??
    text.match(/^(?:set|start|create|make)\s+(?:a\s+|an\s+|my\s+)?(?:new\s+)?goal\s+(?:to|for|that is|:)\s*(.+)$/is)?.[1] ??
    null;
  if (objective && objective.trim()) return { kind: 'create', argument: objective.trim() };
  // A bare "goal" is the status control. An incidental "/goal" in a path is
  // not: a hijacked prompt would be swallowed instead of answered.
  if (/^\s*goal\s*$/i.test(text)) return { kind: 'get' };
  return null;
}

// Run a goal command against the agent's native goal API and answer the client.
function handleGoalCommand(command, message) {
  const msg = parseMessage(message);
  const sessionId = sessionIdOf(msg);
  const clientRequestId = msg.id;
  const calls = {
    create: ['mcode/session/goal/create', { sessionId, objective: command.argument }],
    get: ['mcode/session/goal/get', { sessionId }],
    pause: ['mcode/session/goal/patch', { sessionId, status: 'paused' }],
    resume: ['mcode/session/goal/patch', { sessionId, status: 'active' }],
    clear: ['mcode/session/goal/clear', { sessionId }],
    objective: ['mcode/session/goal/patch', { sessionId, objective: command.argument }],
  };
  const [method, params] = calls[command.kind];
  // The prompt never reaches the agent, so it must not stay pending either.
  pendingPrompts.delete(idKey(clientRequestId));
  sendProxyRequest(method, params, { kind: 'goal', goalKind: command.kind, sessionId, clientRequestId });
}

// Tell the client what the goal command did.
function summarizeGoal(kind, result) {
  const goal = result?.goal;
  if (kind === 'clear') return result?.cleared ? 'Goal cleared.' : 'There is no goal to clear.';
  if (kind === 'objective') {
    if (!goal) return 'The goal objective could not be changed.';
    return `Goal ${goal.goalId} now reads:\n${goal.objective}`;
  }
  if (kind === 'get') {
    if (!goal) return 'No goal is set for this session.';
    const budget = goal.tokenBudget ? `, budget ${goal.tokensUsed}/${goal.tokenBudget} tokens` : '';
    return `Goal ${goal.goalId} is ${goal.status}${budget}. Turns ${goal.turnsUsed}.\n${goal.objective}`;
  }
  if (kind === 'create') {
    if (!goal) return 'The goal could not be created.';
    return `Goal ${goal.goalId} created (${goal.status}).\n${goal.objective}`;
  }
  if (!goal) return 'The goal could not be updated.';
  return `Goal ${goal.goalId} is now ${goal.status}.`;
}

function finishGoalCommand(proxyRequest, msg) {
  const { clientRequestId, sessionId, goalKind } = proxyRequest;
  const text = msg.error
    ? `The goal command failed: ${msg.error.message || msg.error.code}`
    : summarizeGoal(goalKind, msg.result);
  writeClientMessage({
    jsonrpc: '2.0',
    method: 'session/update',
    params: {
      sessionId,
      update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } },
    },
  });
  writeClientMessage({ jsonrpc: '2.0', id: clientRequestId, result: { stopReason: 'end_turn' } });
}

// Send a request to the agent on the proxy's own behalf. Its answer never
// reaches the client: the proxy correlates it by request id.
function sendProxyRequest(method, params, meta) {
  const id = `mcode-acp-${process.pid}-${++proxySequence}`;
  proxyRequests.set(idKey(id), meta);
  writeChildMessage({ jsonrpc: '2.0', id, method, params });
  return id;
}

// The agent advertised the mcode extension methods: remember which ones.
function noteNativeExtensions(line) {
  const ext = parseMessage(line)?.result?._meta?.['minimax-code/extensions'];
  if (!ext || !Array.isArray(ext.methods)) return;
  nativeMethods.clear();
  for (const method of ext.methods) nativeMethods.add(method);
}

// Steer the running turn with the client's message instead of holding it.
// Falls back to the old hold-and-resend path when the agent refuses.
function tryNativeSteer(sessionId, clientRequestId, message) {
  if (!nativeMethods.has('mcode/session/steer')) return false;
  if (pendingSteers.has(sessionId) || steeringSessions.has(sessionId)) return false;
  const text = promptTextOf(message);
  if (!text) return false;
  const pending = { clientRequestId, message, timer: null };
  pending.timer = setTimeout(() => {
    if (pendingSteers.get(sessionId) !== pending) return;
    pendingSteers.delete(sessionId);
    proxyRequests.delete(idKey(pending.id));
    fallbackToHold(sessionId, pending);
  }, STEER_TIMEOUT_MS);
  if (typeof pending.timer.unref === 'function') pending.timer.unref();
  pendingSteers.set(sessionId, pending);
  pending.id = sendProxyRequest('mcode/session/steer', { sessionId, text }, { sessionId, pending });
  return true;
}

// The agent refused the steer: hold the prompt and send it once the running
// turn has ended, the way the proxy did before the extension existed.
function fallbackToHold(sessionId, pending) {
  holdPrompt(sessionId, pending.message);
}

// The agent sends its own extension notifications, which are not part of ACP.
// The useful ones are translated for the client, the rest are dropped: T3
// does not know these methods.
function handleExtensionNotification(line) {
  const msg = parseMessage(line);
  if (!msg || typeof msg.method !== 'string') return false;
  if (!msg.method.startsWith('mcode/session/')) return false;
  const sessionId = msg.params?.sessionId;
  if (msg.method === 'mcode/session/goal_update') {
    handleGoalUpdate(sessionId, msg.params?.goal);
  }
  return true;
}

// Tell the client when the goal changes state.
function handleGoalUpdate(sessionId, goal) {
  if (!goal || !sessionId) return;
  const previous = goalStates.get(sessionId);
  goalStates.set(sessionId, { goalId: goal.goalId, status: goal.status });
  if (previous && previous.status === goal.status) return;
  writeClientMessage({
    jsonrpc: '2.0',
    method: 'session/update',
    params: {
      sessionId,
      update: {
        sessionUpdate: 'agent_message_chunk',
        content: { type: 'text', text: `Goal ${goal.goalId} is now ${goal.status}.` },
      },
    },
  });
}

// The agent answered a request the proxy sent on its own behalf.
function handleProxyResponse(proxyRequest, msg) {
  if (proxyRequest.kind === 'goal') {
    finishGoalCommand(proxyRequest, msg);
    return;
  }
  const sessionId = proxyRequest.sessionId;
  const pending = proxyRequest.pending;
  if (!pending || pendingSteers.get(sessionId) !== pending) return;
  pendingSteers.delete(sessionId);
  clearTimeout(pending.timer);
  if (msg.error) {
    fallbackToHold(sessionId, pending);
    return;
  }
  // The steer was admitted: the message is part of the running turn now.
  const steering = { clientRequestId: pending.clientRequestId, timer: null };
  steering.timer = setTimeout(() => {
    if (steeringSessions.get(sessionId) !== steering) return;
    steeringSessions.delete(sessionId);
    writeClientMessage({
      jsonrpc: '2.0',
      id: pending.clientRequestId,
      error: { code: -32603, message: 'The steered turn did not report back in time.' },
    });
  }, STEER_RELAY_TIMEOUT_MS);
  if (typeof steering.timer.unref === 'function') steering.timer.unref();
  steeringSessions.set(sessionId, steering);
}

// A response to a request the proxy sent on its own behalf.
function takeProxyResponse(line) {
  const msg = parseMessage(line);
  if (!msg || msg.method || msg.id === undefined || msg.id === null) return null;
  const key = idKey(msg.id);
  const proxyRequest = proxyRequests.get(key);
  if (!proxyRequest) return null;
  proxyRequests.delete(key);
  handleProxyResponse(proxyRequest, msg);
  return msg;
}

// The steered turn ended: give the client the same answer for the steering
// request that the turn reported for the prompt it replaced.
function relaySteeringResponse(line) {
  const msg = parseMessage(line);
  if (!msg || msg.method || msg.id === undefined || msg.id === null) return null;
  const sent = sentPrompts.get(idKey(msg.id));
  if (!sent) return null;
  const steering = steeringSessions.get(sent.sessionId);
  if (!steering) return null;
  steeringSessions.delete(sent.sessionId);
  clearTimeout(steering.timer);
  const payload = { jsonrpc: '2.0', id: steering.clientRequestId };
  if (msg.error) payload.error = msg.error;
  else payload.result = msg.result;
  return JSON.stringify(payload);
}

function writeClientMessage(message) {
  process.stdout.write((typeof message === 'string' ? message : JSON.stringify(message)) + '\n');
}
"""

WRITE_OLD = r"""  writeChildMessage = (message) => child.stdin.write(JSON.stringify(message) + '\n');
"""

WRITE_NEW = r"""  writeChildMessage = (message) => {
    const text = typeof message === 'string' ? message : JSON.stringify(message);
    trackUpstreamPrompt(text);
    child.stdin.write(text + '\n');
  };
"""

RLOUT_OLD = """  rlOut.on('line', (line) => {
    const outgoing = interceptOutgoing(line);
    if (outgoing !== null) process.stdout.write(outgoing + '\\n');
  });
"""

RLOUT_NEW = r"""  rlOut.on('line', (line) => {
    // The agent's extension notifications are translated or dropped, never
    // forwarded: T3 does not know these methods.
    if (handleExtensionNotification(line)) return;
    // A response to a request the proxy sent itself never reaches the client.
    if (takeProxyResponse(line)) return;
    const rejected = retryableRejection(line);
    if (rejected) {
      // The turn is still running upstream. Resend the prompt instead of
      // failing the turn.
      if (scheduleResend(rejected)) return;
      failRejectedPrompt(line);
      return;
    }
    noteNativeExtensions(line);
    const outgoing = interceptOutgoing(addGoalCommand(line));
    if (outgoing !== null) {
      process.stdout.write(outgoing + '\n');
      const relay = relaySteeringResponse(line);
      if (relay !== null) process.stdout.write(relay + '\n');
    }
    releaseUpstreamPrompt(line);
  });
"""

RLIN_OLD = r"""  rlIn.on('line', (line) => child.stdin.write(interceptIncoming(line) + '\n'));
"""

RLIN_NEW = r"""  rlIn.on('line', (line) => {
    const outgoing = interceptIncoming(line);
    if (outgoing === null) return;
    // A "/goal ..." prompt is a goal command, not something for the agent.
    const goal = parseGoalCommand(outgoing);
    if (goal && nativeMethods.has('mcode/session/goal/create')) {
      handleGoalCommand(goal, outgoing);
      return;
    }
    if (goal) {
      // The agent has no native goal API: the prompt goes through unchanged.
      writeChildMessage(outgoing);
      return;
    }
    // 'defer', 'hold' and 'steer' are handled inside steerIncoming.
    if (steerIncoming(outgoing) === 'pass') writeChildMessage(outgoing);
  });
"""

SPAWN_OLD = r"""function startProxy(targetLauncher, targetArgs) {
  patchReleaseChunks(targetLauncher);

  const child = spawn(targetLauncher, targetArgs, {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: process.env
  });
"""

SPAWN_NEW = r"""// T3 Code owns the orchestration: the agent must not spawn its own children.
const T3_ORCHESTRATION_DIRECTIVE = `
[MANDATORY T3 ORCHESTRATION DIRECTIVE]
This agent runs inside T3 Code. T3 Code owns the orchestration, so the child
thread and its task must appear in the T3 Code UI and sidebar.

When asked to spawn or run a subagent, delegate, or child agent, do NOT use the
internal subagent tools (the \`task\` tool) and do NOT start a background agent
of your own. ALWAYS use T3 Code's orchestrator tool \`delegate_task\` (call it
through mcp_invoke with tool_name "mcp__t3-code__delegate_task"), and use
\`mcp__t3-code__task_cancel\` to stop a delegated task.
`;

// Write the system prompt the agent starts with. It is appended to the agent's
// own identity prompt, so it lands in the real system prompt.
function writeSystemPromptFile() {
  const os = require('node:os');
  let text = T3_ORCHESTRATION_DIRECTIVE;
  const extra = process.env.MCODE_APPEND_SYSTEM_PROMPT_FILE;
  if (extra) {
    try {
      text += `\n${fs.readFileSync(extra, 'utf8')}\n`;
    } catch {
      // A missing extra prompt must not stop the agent from starting.
    }
  }
  const file = path.join(os.tmpdir(), `mcode-acp-system-prompt-${process.pid}.txt`);
  try {
    fs.writeFileSync(file, text, 'utf8');
    return file;
  } catch {
    return null;
  }
}

function startProxy(targetLauncher, targetArgs) {
  patchReleaseChunks(targetLauncher);

  const systemPromptFile = writeSystemPromptFile();
  const args = systemPromptFile
    ? [...targetArgs, '--append-system-prompt-file', systemPromptFile]
    : targetArgs;
  const child = spawn(targetLauncher, args, {
    stdio: ['pipe', 'pipe', 'inherit'],
    env: process.env
  });
  // The agent can exit while the client is still sending; that is not a crash.
  child.stdin.on('error', () => {});
  child.stdout.on('error', () => {});
"""

INTERCEPT_OLD = r"""function interceptIncoming(line) {
  if (!line || !line.trim()) return line;
  try {
    const msg = JSON.parse(line);
"""

INTERCEPT_NEW = r"""function interceptIncoming(line) {
  if (!line || !line.trim()) return line;
  try {
    const msg = JSON.parse(line);
    if (msg && msg.method === 'initialize') {
      // Opt in to the agent's extension notifications: they carry the queue
      // and goal state the proxy reports to the client.
      msg.params = msg.params ?? {};
      msg.params.clientCapabilities = msg.params.clientCapabilities ?? {};
      msg.params.clientCapabilities._meta = {
        ...(msg.params.clientCapabilities._meta ?? {}),
        'minimax-code/extensions': { version: 1, notifications: true },
      };
      return JSON.stringify(msg);
    }
    if (msg && msg.method === 'session/new' && msg.id !== undefined && msg.id !== null) {
      newSessionRequests.add(idKey(msg.id));
    }
"""

EDITS = [
    ("state", STATE_OLD, STATE_NEW),
    ("helpers", IDKEY_OLD, IDKEY_NEW),
    ("write", WRITE_OLD, WRITE_NEW),
    ("rlout", RLOUT_OLD, RLOUT_NEW),
    ("rlin", RLIN_OLD, RLIN_NEW),
    ("spawn", SPAWN_OLD, SPAWN_NEW),
    ("intercept", INTERCEPT_OLD, INTERCEPT_NEW),
]


def patch(src):
    """Return the patched source, or None when the layout is not recognised."""
    for name, old, new in EDITS:
        if src.count(old) != 1:
            return None, f"anchor {name!r} found {src.count(old)} times"
        src = src.replace(old, new, 1)
    return src, None


def revert(src):
    """Return the unpatched source, or None when the file is not patched."""
    for name, old, new in reversed(EDITS):
        if src.count(new) != 1:
            return None, f"edit {name!r} found {src.count(new)} times"
        src = src.replace(new, old, 1)
    return src, None


def main():
    if not os.path.exists(TARGET):
        sys.exit(f"not found: {TARGET}")

    src = open(TARGET).read()

    if "--revert" in sys.argv[2:]:
        reverted, error = revert(src)
        if reverted is None:
            print(f"not a patched proxy ({error}): {TARGET}")
            return 0
        backup = f"{TARGET}.bak-{time.strftime('%Y%m%dT%H%M%S')}"
        shutil.copy2(TARGET, backup)
        open(TARGET, "w").write(reverted)
        check = subprocess.run(["node", "--check", TARGET], capture_output=True, text=True)
        if check.returncode != 0:
            shutil.copy2(backup, TARGET)
            sys.exit(f"reverted file does not parse; restored backup: {check.stderr.strip()}")
        print(f"reverted: {TARGET} (backup: {backup})")
        print("new ACP processes pick this up; restart T3 Code or open a new thread.")
        return 0

    if MARKER in src:
        print(f"already patched: {TARGET}")
        return 0

    patched, error = patch(src)
    if patched is None:
        sys.exit(f"unrecognised proxy layout ({error}): {TARGET}")

    backup = f"{TARGET}.bak-{time.strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(TARGET, backup)
    open(TARGET, "w").write(patched)

    check = subprocess.run(["node", "--check", TARGET], capture_output=True, text=True)
    if check.returncode != 0:
        shutil.copy2(backup, TARGET)
        sys.exit(f"patched file does not parse; restored backup: {check.stderr.strip()}")

    print(f"patched: {TARGET} (backup: {backup})")
    print("new ACP processes pick this up; restart T3 Code or open a new thread.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


