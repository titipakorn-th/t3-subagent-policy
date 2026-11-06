#!/usr/bin/env bash
# Install / check / remove the T3 subagent cost policy in the global agent
# instruction files read by codex and Claude-family agents, and the mcode ACP
# proxy patch that makes steering, goals and the system prompt work in T3.
#
#   ./install.sh            install (idempotent)
#   ./install.sh --check    report status, exit 1 if missing
#   ./install.sh --uninstall remove the policy block and the proxy patch
#   ./install.sh --print    print the policy block only
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="install"
for a in "$@"; do
  case "$a" in
    --check|--uninstall|--print|--help|-h) MODE="${a#--}" ;;
  esac
done

if [ "$MODE" = "help" ]; then
  echo "usage: install.sh [--check|--uninstall|--print]"
  exit 0
fi

python3 - "$DIR" "$@" <<'PY'
import os, re, shutil, sys, time

DIR = sys.argv[1]
ARGS = sys.argv[2:]
MODE = "install"
for a in ARGS:
    if a in ("--check", "--uninstall", "--print", "--help", "-h"):
        MODE = a.lstrip("-")
if MODE == "help":
    print("usage: install.sh [--check|--uninstall|--print]")
    sys.exit(0)

BEGIN = "<!-- t3-subagent-cost-policy:begin -->"
END = "<!-- t3-subagent-cost-policy:end -->"
LEGACY_HEADING = "## Subagent cost policy"


def gemini_targets():
    """Gemini reads GEMINI.md from GEMINI_HOME. T3's antigravity clip proxy sets
    GEMINI_HOME to its own directory, so ~/.gemini/GEMINI.md alone is not enough."""
    homes = []
    env_home = os.environ.get("GEMINI_HOME")
    if env_home:
        homes.append(env_home)
    homes.append(os.path.expanduser("~/.gemini"))
    homes.append(os.path.expanduser("~/.t3/tools/antigravity-cliproxy/gemini-home"))
    homes.append(os.path.expanduser("~/.t3/tools/antigravity-cliproxy/gemini-home/antigravity-acp"))
    out = []
    for home in homes:
        path = os.path.join(home, "GEMINI.md")
        if path not in out and (os.path.isdir(home) or os.path.exists(path)):
            out.append(path)
    return out


# One global instruction file per provider family T3 can drive.
TARGETS = [
    os.path.expanduser("~/.codex/AGENTS.md"),            # codex
    os.path.expanduser("~/.claude/CLAUDE.md"),           # claudeAgent / claude-sdk agents
    os.path.expanduser("~/.config/opencode/AGENTS.md"),  # opencode (global AGENTS.md)
] + gemini_targets()

policy_path = os.path.join(DIR, "policy.md")
if not os.path.exists(policy_path):
    sys.exit(f"missing {policy_path}")
policy = open(policy_path).read().strip("\n")

block = f"{BEGIN}\n{policy}\n{END}\n"


def strip_block(text: str) -> str:
    """Remove the managed block and any legacy '## Subagent cost policy' section."""
    if BEGIN in text and END in text:
        text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)
    lines = text.splitlines(keepends=True)
    out, skipping = [], False
    for line in lines:
        if line.startswith(LEGACY_HEADING):
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            out.append(line)
    text = "".join(out)
    return text.rstrip("\n") + "\n"


def install(path: str) -> str:
    text = open(path).read() if os.path.exists(path) else ""
    new = strip_block(text).rstrip("\n") + "\n\n" + block
    if new == text:
        return "unchanged"
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    open(path, "w").write(new)
    return "installed"


if MODE == "print":
    print(block)
    sys.exit(0)

if MODE == "check":
    bad = False
    for t in TARGETS:
        ok = os.path.exists(t) and BEGIN in open(t).read() and END in open(t).read()
        print(f"{'OK  ' if ok else 'MISS'} {t}")
        bad = bad or not ok
    sys.exit(1 if bad else 0)

if MODE == "uninstall":
    for t in TARGETS:
        if os.path.exists(t):
            text = open(t).read()
            if BEGIN in text or LEGACY_HEADING in text:
                shutil.copy2(t, f"{t}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                open(t, "w").write(strip_block(text))
                print(f"removed  {t}")
            else:
                print(f"skipped  {t} (no policy)")
        else:
            print(f"skipped  {t} (missing)")
    sys.exit(0)

for t in TARGETS:
    print(f"{install(t):9} {t}")
PY

# --- the mcode ACP proxy patch -------------------------------------------
# The policy text alone does not fix steering, goals or the system prompt:
# those live in the proxy that sits between T3 Code and the mcode agent.
PROXY="$HOME/.minimax-code/bin/mcode-acp-proxy.js"
PATCHER="$DIR/patches/apply-mcode-acp-steer.py"
PROXY_MARKER="RETRYABLE_REJECTION"

# --- the antigravity clip proxy shim --------------------------------------
# Every machine runs a different shim revision. An older one lists
# claude-*-high and friends as separate models instead of a thinking level
# selector, and only forwards an effort for the models in its PROXY_MODELS
# table, so "low" was cosmetic. The repo carries the known-good shim.
SHIM="$HOME/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py"
SHIM_PATCHER="$DIR/patches/apply-antigravity-effort.py"

case "$MODE" in
  print)
    # The policy block only.
    exit 0
    ;;
  check)
    if [ -f "$PROXY" ] && grep -q "$PROXY_MARKER" "$PROXY"; then
      echo "OK   $PROXY (patched)"
    else
      echo "MISS $PROXY (not patched)"
      exit 1
    fi
    if [ -f "$PROXY" ] && grep -q "MCODE_T3_OWN_DELEGATION" "$PROXY"; then
      echo "OK   $PROXY (internal delegation gated off)"
    else
      echo "MISS $PROXY (internal delegation not gated off)"
      exit 1
    fi
    # The shim only exists where T3 runs the antigravity clip proxy.
    if [ -f "$SHIM" ]; then
      python3 "$SHIM_PATCHER" "$SHIM" --check || exit 1
    fi
    exit 0
    ;;
  uninstall)
    if [ -f "$PROXY" ] && [ -f "$PATCHER" ]; then
      python3 "$PATCHER" "$PROXY" --revert || true
    else
      echo "skipped  $PROXY (missing)"
    fi
    exit 0
    ;;
esac

if [ -f "$PROXY" ] && [ -f "$PATCHER" ]; then
  python3 "$PATCHER" "$PROXY" || true
else
  echo "skipped  $PROXY (missing)"
fi

if [ -f "$SHIM" ] && [ -f "$SHIM_PATCHER" ]; then
  python3 "$SHIM_PATCHER" "$SHIM" || true
else
  echo "skipped  $SHIM (missing)"
fi
