#!/usr/bin/env python3
"""Apply the steering fix to the mcode ACP proxy.

T3 Code cannot steer an ACP session that is already running a turn: it sends
`session/cancel` and the new `session/prompt` back to back, and the agent
rejects the second prompt with

    -32602 Invalid params: A prompt is already active for this session.

because the cancelled turn has not finished yet. Once that turn ends, the
runtime still owns it for a moment and answers with

    -32603 Session already has an active Turn.

Either error kills the turn, which leaves the thread unusable.

The proxy sits between T3 and the agent, so it can fix this without touching
the agent: it holds a prompt that arrives during a running turn, releases it
once that turn has finished, and resends it while the runtime still reports the
turn as busy.

The proxy is the same script on every machine, so this edits it by shape rather
than by byte offset, and refuses to touch a file it does not recognise. The
result is checked against the known-good patched file before it is written.

    python3 apply-mcode-acp-steer.py [path-to-mcode-acp-proxy.js]
"""
import os
import shutil
import subprocess
import sys
import time

DEFAULT = os.path.expanduser("~/.minimax-code/bin/mcode-acp-proxy.js")
TARGET = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
KNOWN_GOOD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcode-acp-proxy-steer.js")

MARKER = "TURN_BUSY_PATTERN"

STATE_OLD = """const cancelledSessions = new Set();
"""

STATE_NEW = """const cancelledSessions = new Set();
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
"""

IDKEY_OLD = r"""function idKey(id) {
  return `${typeof id}:${String(id)}`;
}
"""

IDKEY_NEW = r"""function idKey(id) {
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
"""

RECOVERY_CANCEL_OLD = """      recoveryPrompts.delete(recoveryKey);
      pendingPrompts.delete(idKey(request.id));
      cancelledSessions.delete(request.sessionId);
      process.stdout.write(JSON.stringify({
"""

RECOVERY_CANCEL_NEW = """      recoveryPrompts.delete(recoveryKey);
      pendingPrompts.delete(idKey(request.id));
      cancelledSessions.delete(request.sessionId);
      releaseSession(request.sessionId);
      process.stdout.write(JSON.stringify({
"""

RECOVERY_SEND_OLD = r"""      });
    } catch {
      recoveryPrompts.delete(recoveryKey);
      pendingPrompts.delete(idKey(request.id));
      process.stdout.write(JSON.stringify(recoveryError(request, attempt, kind)) + '\n');
    }
"""

RECOVERY_SEND_NEW = r"""      });
      occupySession(request.sessionId, recoveryId);
    } catch {
      recoveryPrompts.delete(recoveryKey);
      pendingPrompts.delete(idKey(request.id));
      releaseSession(request.sessionId);
      process.stdout.write(JSON.stringify(recoveryError(request, attempt, kind)) + '\n');
    }
"""

RECOVERY_RESPONSE_OLD = """        pendingPrompts.delete(idKey(recovery.request.id));
        cancelledSessions.delete(recovery.request.sessionId);
        if (failureKind) {
          return JSON.stringify(recoveryError(recovery.request, recovery.attempt, failureKind));
        }
        msg.id = recovery.request.id;
        return JSON.stringify(msg);
"""

RECOVERY_RESPONSE_NEW = """        pendingPrompts.delete(idKey(recovery.request.id));
        cancelledSessions.delete(recovery.request.sessionId);
        if (failureKind) {
          releaseSession(recovery.request.sessionId);
          return JSON.stringify(recoveryError(recovery.request, recovery.attempt, failureKind));
        }
        msg.id = recovery.request.id;
        releaseSession(recovery.request.sessionId);
        return JSON.stringify(msg);
"""

PROMPT_RESPONSE_OLD = """        if (failureKind && writeChildMessage) {
          beginRecovery(request, 1, failureKind);
          return null;
        }
        pendingPrompts.delete(responseKey);
        cancelledSessions.delete(request.sessionId);
      }
"""

PROMPT_RESPONSE_NEW = """        if (failureKind && writeChildMessage) {
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
"""

INCOMING_OLD = """    if (msg && msg.method === 'session/prompt' && msg.params && msg.id !== undefined && msg.id !== null && typeof msg.params.sessionId === 'string') {
      cancelledSessions.delete(msg.params.sessionId);
      pendingPrompts.set(idKey(msg.id), { id: msg.id, sessionId: msg.params.sessionId });
    }
    if (msg && msg.method === 'session/cancel' && msg.params && typeof msg.params.sessionId === 'string') {
      cancelledSessions.add(msg.params.sessionId);
    }
"""

INCOMING_NEW = """    if (msg && msg.method === 'session/prompt' && msg.params && msg.id !== undefined && msg.id !== null) {
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
"""

STDIN_OLD = r"""  rlIn.on('line', (line) => child.stdin.write(interceptIncoming(line) + '\n'));
"""

STDIN_NEW = r"""  rlIn.on('line', (line) => {
    const outgoing = interceptIncoming(line);
    if (outgoing !== null) child.stdin.write(outgoing + '\n');
  });
"""

EDITS = [
    ("state", STATE_OLD, STATE_NEW),
    ("helpers", IDKEY_OLD, IDKEY_NEW),
    ("recovery-cancel", RECOVERY_CANCEL_OLD, RECOVERY_CANCEL_NEW),
    ("recovery-send", RECOVERY_SEND_OLD, RECOVERY_SEND_NEW),
    ("recovery-response", RECOVERY_RESPONSE_OLD, RECOVERY_RESPONSE_NEW),
    ("prompt-response", PROMPT_RESPONSE_OLD, PROMPT_RESPONSE_NEW),
    ("incoming", INCOMING_OLD, INCOMING_NEW),
    ("stdin", STDIN_OLD, STDIN_NEW),
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

    known_good = None
    if os.path.exists(KNOWN_GOOD):
        known_good = open(KNOWN_GOOD).read()
    if known_good is not None and patched != known_good:
        sys.exit(f"patch result differs from the known-good file; refusing to write: {TARGET}")

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
