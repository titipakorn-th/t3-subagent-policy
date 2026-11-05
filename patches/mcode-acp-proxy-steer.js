#!/usr/bin/env node
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');

const activeElicitations = new Map();
const pendingPrompts = new Map();
const recoveryPrompts = new Map();
const cancelledSessions = new Set();
// The patch revision this file carries. The patcher uses it to tell revisions
// apart, and it is what makes an older patch migratable instead of skipped.
const MCODE_ACP_PATCH = 2;
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
const goalRuns = new Map(); // sessionId -> the goal the user set in this session
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
// A goal runs as ordinary prompt turns, so it needs a cap of its own: the
// turn count stops it, the time cap stops a single long turn from running
// for hours.
const GOAL_MAX_TURNS = Number(process.env.MCODE_GOAL_MAX_TURNS || 20);
const GOAL_MAX_MS = Number(process.env.MCODE_GOAL_MAX_MS || 7200000);
// The agent ends a finished goal with this line.
const GOAL_DONE_MARKER = 'GOAL_COMPLETE';
let proxySequence = 0;
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
// T3 Code wraps the user's message in instruction blocks, so a goal command
// is inside <user_request>, not at the start of the prompt.
function userTextOf(message) {
  const msg = parseMessage(message);
  const blocks = Array.isArray(msg?.params?.prompt) ? msg.params.prompt : [];
  const text = blocks
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n');
  const inner = /<user_request>([\s\S]*?)<\/user_request>/.exec(text);
  return (inner ? inner[1] : text).trim();
}

// A goal command: the literal command family, or the natural language the
// goal-mode plugin used to detect. Every pattern is anchored, because T3's
// instruction blocks talk about goals themselves.
const GOAL_COMMANDS = [
  [/^\/goal\s+(stop|cancel)\b/i, 'clear'],
  [/^\/goal\s+(clear|off|reset|none)\b/i, 'clear'],
  [/^\/goal\s+(pause|hold)\b/i, 'pause'],
  [/^\/goal\s+(resume|start|continue|run)\b/i, 'resume'],
  [/^\/goal\s+status\b/i, 'get'],
  [/^goal\s+status\b/i, 'get'],
  [/^(what is|what's|whats|show me|show|tell me|describe|report)\s+(the\s+|my\s+|this\s+)?(current\s+)?goal\b/i, 'get'],
  [/^(cancel|stop|abort)\s+(the\s+|my\s+|this\s+)?goal\b/i, 'clear'],
  [/^(clear|remove|drop|forget)\s+(the\s+|my\s+|this\s+)?goal\b/i, 'clear'],
  [/^(pause|hold|suspend|stop working on)\s+(the\s+|my\s+|this\s+)?goal\b/i, 'pause'],
  [/^(resume|restart|continue)\s+(the\s+|my\s+|this\s+)?goal\b/i, 'resume'],
  [/^\/goal\s+edit\s+(.+)$/is, 'objective'],
  [/^(change|update|revise|rewrite)\s+(the\s+|my\s+)?goal\s+(?:to|into)\s+(.+)$/is, 'objective'],
  [/^\/goal\s+replace\s+(.+)$/is, 'objective'],
  [/^(replace|swap)\s+(the\s+|my\s+)?goal\s+(?:with|for|to)\s+(.+)$/is, 'objective'],
  [/^\/goal\s+(plan|edit-plan)\b/i, null],
  [/^(add|update|revise|set)\s+(the\s+)?(goal\s+)?plan\b/i, null],
];

// A "/goal ..." prompt from the client, mapped to the agent's native goal API.
function parseGoalCommand(message) {
  const msg = parseMessage(message);
  if (!msg || msg.method !== 'session/prompt') return null;
  if (msg.id === undefined || msg.id === null) return null;
  if (sessionIdOf(msg) === null) return null;
  const text = userTextOf(message);
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

  for (const [pattern, kind] of GOAL_COMMANDS) {
    const match = pattern.exec(text);
    if (!match) continue;
    if (!kind) return null;
    if (kind === 'objective') return { kind: 'objective', argument: match[1].trim() };
    return { kind };
  }

  // A goal the user asked for.
  const objective = /^(?:set|start|create|make)\s+(?:a\s+|an\s+|my\s+)?(?:new\s+)?goal\s+(?:to|for|that is|:)\s*(.+)$/is.exec(text);
  if (objective) return { kind: 'create', argument: objective[1].trim() };
  if (/^\/goal\b/.test(text)) return { kind: 'get' };
  return null;
}

// A goal the user set in this session.
//
// The agent's native goal API cannot be used for this: mcode runs a goal's
// turns in a child session and never projects them to the client, so the work
// would be invisible in T3 and could burn tokens for hours. The proxy runs the
// goal as ordinary prompt turns instead and keeps the client's request open,
// so every turn the agent makes reaches the thread while it happens.

// The prompt that starts or continues a goal.
function goalRunPrompt(run, first) {
  if (first) {
    return `${run.objective}\n\nThis is an autonomous goal: work on it systematically until it is met. When the objective is fully met, end your reply with the line ${GOAL_DONE_MARKER}.`;
  }
  return `Continue working on this goal:\n${run.objective}\n\nReport briefly what is done and what is left. If the objective is already met, reply with the line ${GOAL_DONE_MARKER} and nothing else.`;
}

// Send the next turn of a running goal.
function sendGoalTurn(run, first) {
  run.turns += 1;
  run.text = '';
  const id = `mcode-acp-goal-${process.pid}-${++proxySequence}`;
  proxyRequests.set(idKey(id), { kind: 'goal-run', run });
  writeChildMessage({
    jsonrpc: '2.0',
    id,
    method: 'session/prompt',
    params: { sessionId: run.sessionId, prompt: [{ type: 'text', text: goalRunPrompt(run, first) }] },
  });
}

// A native goal for this session would do the same work, invisibly.
function clearNativeGoal(sessionId) {
  if (!nativeMethods.has('mcode/session/goal/clear')) return;
  sendProxyRequest('mcode/session/goal/clear', { sessionId }, { kind: 'native' });
}

// Answer the request the goal is holding, and say what happened.
function answerGoalRun(run, summary) {
  const clientRequestId = run.clientRequestId;
  run.clientRequestId = null;
  if (clientRequestId === null || clientRequestId === undefined) return;
  pendingPrompts.delete(idKey(clientRequestId));
  writeClientMessage({
    jsonrpc: '2.0',
    method: 'session/update',
    params: {
      sessionId: run.sessionId,
      update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: summary } },
    },
  });
  writeClientMessage({ jsonrpc: '2.0', id: clientRequestId, result: { stopReason: 'end_turn' } });
}

// Answer a goal command that is not part of a running goal.
function answerGoalCommand(sessionId, clientRequestId, text) {
  pendingPrompts.delete(idKey(clientRequestId));
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

// The client asked for a goal: start the run and keep its request open.
function startGoalRun(sessionId, clientRequestId, objective) {
  const previous = goalRuns.get(sessionId);
  if (previous) {
    previous.status = 'stopped';
    answerGoalRun(previous, 'The goal was replaced by a new one.');
  }
  const run = {
    sessionId,
    clientRequestId,
    objective: String(objective || '').trim(),
    status: 'running',
    turns: 0,
    text: '',
    startedAt: Date.now(),
  };
  goalRuns.set(sessionId, run);
  clearNativeGoal(sessionId);
  sendGoalTurn(run, true);
}

// The turn of a running goal ended: continue the goal, or finish it.
function handleGoalTurnEnd(run, msg, line) {
  // A steered client request is answered by the turn that carried it.
  const relay = relaySteeringResponse(line);
  if (relay !== null) process.stdout.write(relay + '\n');
  releaseUpstreamPrompt(line);
  if (msg.error) {
    run.status = 'failed';
    answerGoalRun(run, `The goal turn failed: ${msg.error?.message || msg.error?.code}`);
    return;
  }
  if (run.status !== 'running') {
    answerGoalRun(run, `Goal ${run.status}.`);
    return;
  }
  if (/goal[_ ]complete/i.test(run.text)) {
    run.status = 'met';
    answerGoalRun(run, `Goal met after ${run.turns} turn(s).`);
    return;
  }
  if (run.turns >= GOAL_MAX_TURNS || Date.now() - run.startedAt >= GOAL_MAX_MS) {
    run.status = 'stopped';
    answerGoalRun(run, `Goal stopped after ${run.turns} turn(s) without being met.`);
    return;
  }
  sendGoalTurn(run, false);
}

// The text the agent produced during the running goal turn.
function accumulateGoalText(line) {
  const msg = parseMessage(line);
  if (!msg || msg.method !== 'session/update') return;
  const update = msg.params?.update;
  if (update?.sessionUpdate !== 'agent_message_chunk') return;
  const run = goalRuns.get(msg.params?.sessionId);
  if (!run || run.status !== 'running') return;
  if (typeof update.content?.text === 'string') run.text += update.content.text;
}

// Run a goal command: the running goal first, the native goal API only for
// goals the agent created on its own.
function handleGoalCommand(command, message) {
  const msg = parseMessage(message);
  const sessionId = sessionIdOf(msg);
  const clientRequestId = msg.id;
  const run = goalRuns.get(sessionId);
  if (command.kind === 'create') {
    startGoalRun(sessionId, clientRequestId, command.argument);
    return;
  }
  if (run) {
    if (command.kind === 'pause') {
      if (run.status !== 'running') {
        answerGoalCommand(sessionId, clientRequestId, `The goal is ${run.status}.`);
        return;
      }
      run.status = 'paused';
      answerGoalRun(run, 'Goal paused.');
      answerGoalCommand(sessionId, clientRequestId, 'Goal paused.');
      return;
    }
    if (command.kind === 'resume') {
      if (run.status === 'running') {
        answerGoalCommand(sessionId, clientRequestId, 'The goal is already running.');
        return;
      }
      run.status = 'running';
      run.clientRequestId = clientRequestId;
      sendGoalTurn(run, false);
      return;
    }
    if (command.kind === 'clear') {
      goalRuns.delete(sessionId);
      answerGoalRun(run, 'Goal cleared.');
      answerGoalCommand(sessionId, clientRequestId, 'Goal cleared.');
      return;
    }
    if (command.kind === 'objective') {
      run.objective = String(command.argument || '').trim();
      answerGoalCommand(sessionId, clientRequestId, `The goal now reads:\n${run.objective}`);
      return;
    }
    if (command.kind === 'get') {
      const minutes = Math.round((Date.now() - run.startedAt) / 60000);
      answerGoalCommand(sessionId, clientRequestId, `The goal is ${run.status}. Turns ${run.turns}, ${minutes} min.\n${run.objective}`);
      return;
    }
  }
  const calls = {
    get: ['mcode/session/goal/get', { sessionId }],
    pause: ['mcode/session/goal/patch', { sessionId, status: 'paused' }],
    resume: ['mcode/session/goal/patch', { sessionId, status: 'active' }],
    clear: ['mcode/session/goal/clear', { sessionId }],
    objective: ['mcode/session/goal/patch', { sessionId, objective: command.argument }],
  };
  const call = calls[command.kind];
  if (!call) {
    // A goal command the agent has no API for: the prompt goes through.
    writeChildMessage(message);
    return;
  }
  const [method, params] = call;
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
  } else if (msg.method === 'mcode/session/delegation_update') {
    handleDelegationUpdate(sessionId, msg.params?.snapshot);
  }
  return true;
}

// mcode spawned children of its own. T3 Code owns the orchestration, so the
// client is told: a silent second orchestrator would be invisible otherwise.
function delegationSummary(snapshot) {
  const members = snapshot?.members ?? [];
  if (!Array.isArray(members) || members.length === 0) return null;
  const names = members.map((member) => member?.title || member?.role || member?.sessionId || 'child');
  return `mcode started ${members.length} internal ${members.length === 1 ? 'delegate' : 'delegates'}: ${names.join(', ')}. T3 Code owns the orchestration - these do not appear as T3 threads.`;
}

function handleDelegationUpdate(sessionId, snapshot) {
  const text = delegationSummary(snapshot);
  if (!text) return;
  writeClientMessage({
    jsonrpc: '2.0',
    method: 'session/update',
    params: {
      sessionId,
      update: {
        sessionUpdate: 'agent_message_chunk',
        content: { type: 'text', text },
      },
    },
  });
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
function handleProxyResponse(proxyRequest, msg, line) {
  if (proxyRequest.kind === 'goal') {
    finishGoalCommand(proxyRequest, msg);
    return;
  }
  if (proxyRequest.kind === 'goal-run') {
    handleGoalTurnEnd(proxyRequest.run, msg, line);
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
  // The agent refused the prompt because a turn is still running: that is not
  // the end of the goal turn, so the prompt is sent again instead.
  if (proxyRequest.kind === 'goal-run' && msg.error && RETRYABLE_REJECTION.test(String(msg.error.message || ''))) {
    const sent = sentPrompts.get(key);
    if (sent && scheduleResend(sent)) return msg;
  }
  proxyRequests.delete(key);
  handleProxyResponse(proxyRequest, msg, line);
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
    } catch {
      recoveryPrompts.delete(recoveryKey);
      pendingPrompts.delete(idKey(request.id));
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
          return JSON.stringify(recoveryError(recovery.request, recovery.attempt, failureKind));
        }
        msg.id = recovery.request.id;
        return JSON.stringify(msg);
      }

      const request = pendingPrompts.get(responseKey);
      if (request) {
        const failureKind = msg.error && runtimeFailureKind(msg.error);
        if (failureKind && writeChildMessage) {
          beginRecovery(request, 1, failureKind);
          return null;
        }
        pendingPrompts.delete(responseKey);
        cancelledSessions.delete(request.sessionId);
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
    if (msg && msg.method === 'session/prompt' && msg.params && msg.id !== undefined && msg.id !== null && typeof msg.params.sessionId === 'string') {
      cancelledSessions.delete(msg.params.sessionId);
      pendingPrompts.set(idKey(msg.id), { id: msg.id, sessionId: msg.params.sessionId });
    }
    if (msg && msg.method === 'session/cancel' && msg.params && typeof msg.params.sessionId === 'string') {
      cancelledSessions.add(msg.params.sessionId);
      // A cancelled turn ends the goal: T3 sends a cancel with every message
      // it sends while a turn is running, and the goal must not carry on
      // behind the user's back.
      const run = goalRuns.get(msg.params.sessionId);
      if (run) run.status = 'stopped';
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

// T3 Code owns the orchestration: the agent must not spawn its own children.
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
  writeChildMessage = (message) => {
    const text = typeof message === 'string' ? message : JSON.stringify(message);
    trackUpstreamPrompt(text);
    child.stdin.write(text + '\n');
  };

  const rlOut = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  rlOut.on('line', (line) => {
    // The agent's answer to a running goal is the goal's output.
    accumulateGoalText(line);
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

  const rlIn = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rlIn.on('line', (line) => {
    const outgoing = interceptIncoming(line);
    if (outgoing === null) return;
    // A "/goal ..." prompt is a goal command, not something for the agent.
    const goal = parseGoalCommand(outgoing);
    if (goal) {
      handleGoalCommand(goal, outgoing);
      return;
    }
    // 'defer', 'hold' and 'steer' are handled inside steerIncoming.
    if (steerIncoming(outgoing) === 'pass') writeChildMessage(outgoing);
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
