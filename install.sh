#!/usr/bin/env bash
# Install / check / remove the T3 subagent cost policy in the global agent
# instruction files read by codex and Claude-family agents.
#
#   ./install.sh            install (idempotent)
#   ./install.sh --check    report status, exit 1 if missing
#   ./install.sh --uninstall remove the policy block
#   ./install.sh --print    print the policy block only
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 - "$DIR" "$@" <<'PY'
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
# One global instruction file per provider family T3 can drive.
TARGETS = [
    os.path.expanduser("~/.codex/AGENTS.md"),            # codex
    os.path.expanduser("~/.claude/CLAUDE.md"),           # claudeAgent / claude-sdk agents
    os.path.expanduser("~/.config/opencode/AGENTS.md"),  # opencode (global AGENTS.md)
    os.path.expanduser("~/.gemini/GEMINI.md"),           # gemini / antigravity
]

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
