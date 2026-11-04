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

STATE_NEW = """const cancelledSessions = new Set();
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
const RETRYABLE_REJECTION = /already has an active turn|prompt is already active/i;
const RESEND_MAX_ATTEMPTS = 8;
const RESEND_BASE_DELAY_MS = 150;
const RESEND_MAX_DELAY_MS = 2000;
const HOLD_TIMEOUT_MS = 30000;
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
  sentPrompts.set(key, { sessionId, message: text, attempts: 0 });
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
  if (attempt > RESEND_MAX_ATTEMPTS) return;
  sent.attempts = attempt;
  const delay = Math.min(RESEND_BASE_DELAY_MS * 2 ** (attempt - 1), RESEND_MAX_DELAY_MS);
  const timer = setTimeout(() => {
    try {
      writeChildMessage(sent.message);
    } catch {}
  }, delay);
  if (typeof timer.unref === 'function') timer.unref();
}

// Hold a steering prompt until the running turn has ended.
function holdPrompt(sessionId, message) {
  const queue = queuedPrompts.get(sessionId) ?? [];
  queue.push(message);
  queuedPrompts.set(sessionId, queue);
  if (!cancelSent.has(sessionId)) {
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

// Decide what to do with a message the client sent: drop it, hold it, or pass it on.
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
    cancelSent.add(sessionId);
    return 'pass';
  }
  if (msg.method === 'session/prompt' && msg.id !== undefined && msg.id !== null) {
    if (isBusySession(sessionId)) {
      holdPrompt(sessionId, text);
      return 'hold';
    }
  }
  return 'pass';
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

RLOUT_NEW = """  rlOut.on('line', (line) => {
    const rejected = retryableRejection(line);
    if (rejected) {
      // The turn is still running upstream. Resend the prompt instead of
      // failing the turn.
      scheduleResend(rejected);
      return;
    }
    const outgoing = interceptOutgoing(line);
    if (outgoing !== null) process.stdout.write(outgoing + '\\n');
    releaseUpstreamPrompt(line);
  });
"""

RLIN_OLD = r"""  rlIn.on('line', (line) => child.stdin.write(interceptIncoming(line) + '\n'));
"""

RLIN_NEW = r"""  rlIn.on('line', (line) => {
    const outgoing = interceptIncoming(line);
    if (outgoing === null) return;
    if (steerIncoming(outgoing) !== 'pass') return;
    writeChildMessage(outgoing);
  });
"""

EDITS = [
    ("state", STATE_OLD, STATE_NEW),
    ("helpers", IDKEY_OLD, IDKEY_NEW),
    ("write", WRITE_OLD, WRITE_NEW),
    ("rlout", RLOUT_OLD, RLOUT_NEW),
    ("rlin", RLIN_OLD, RLIN_NEW),
]


def patch(src):
    """Return the patched source, or None when the layout is not recognised."""
    for name, old, new in EDITS:
        if src.count(old) != 1:
            return None, f"anchor {name!r} found {src.count(old)} times"
        src = src.replace(old, new, 1)
    return src, None


def main():
    if not os.path.exists(TARGET):
        sys.exit(f"not found: {TARGET}")

    src = open(TARGET).read()

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
