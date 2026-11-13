#!/usr/bin/env python3
"""Self-check for the antigravity shim installer.

The installer must install the repo's known-good shim over an older revision,
leave a revision it does not carry alone, and refuse a file that is not a shim.

    python3 test_antigravity_shim_install.py
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(HERE, "apply-antigravity-effort.py")
KNOWN_GOOD = os.path.join(HERE, "acp_clip_proxy_shim.py")

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"{'ok  ' if ok else 'FAIL'}  {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures += 1


def run(target):
    return subprocess.run([sys.executable, INSTALLER, target],
                          capture_output=True, text=True)


def write(path, text):
    with open(path, "w") as handle:
        handle.write(text)


def main():
    good = open(KNOWN_GOOD).read()
    tmp = tempfile.mkdtemp(prefix="t3-shim-install-")

    # An older revision: T3's shim before the model list was collapsed.
    older = os.path.join(tmp, "older.py")
    write(older, "class State:\n    pass\n\nPROXY_MODELS = {}\n")
    result = run(older)
    check("older revision is replaced", open(older).read() == good, result.stdout.strip())
    check("older revision leaves a backup",
          any(name.startswith("older.py.bak-") for name in os.listdir(tmp)),
          str(os.listdir(tmp)))
    check("older revision reports the install",
          "installed the known-good shim" in result.stdout, result.stdout.strip())

    # The known-good file itself: nothing to do.
    same = os.path.join(tmp, "same.py")
    write(same, good)
    result = run(same)
    check("known-good is left alone", open(same).read() == good)
    check("known-good reports already patched",
          "already patched" in result.stdout, result.stdout.strip())

    # A revision this repo does not carry must not be downgraded.
    newer = os.path.join(tmp, "newer.py")
    write(newer, good + "\n# a newer revision\ndef unsuffixed_model(m):\n    return m\n")
    result = run(newer)
    check("unknown revision is not downgraded", open(newer).read() != good
          and "unsuffixed_model" in open(newer).read())

    # A revision with the new model list but without everything the known-good
    # has is still older, and must be replaced.
    older = os.path.join(tmp, "older-but-new-list.py")
    write(older, good.replace("\ndef configured_mcp_servers():", "\ndef _removed():"))
    result = run(older)
    check("revision missing a known-good feature is replaced",
          open(older).read() == good, result.stdout.strip())

    previous = os.path.join(tmp, "previous.py")
    write(previous, good.replace("def effort_levels_for(", "def old_effort_levels_for(", 1))
    result = run(previous)
    check("previous model-routing revision is upgraded",
          open(previous).read() == good, result.stdout.strip())

    # A file that is not the clip proxy shim at all.
    foreign = os.path.join(tmp, "foreign.py")
    write(foreign, "print('hello')\n")
    result = run(foreign)
    check("foreign file is refused", result.returncode != 0
          and "unrecognised" in result.stderr + result.stdout,
          (result.stderr + result.stdout).strip())
    check("foreign file is untouched", open(foreign).read() == "print('hello')\n")

    # The check mode must report an older revision as missing.
    stale = os.path.join(tmp, "stale.py")
    write(stale, good.replace("\ndef configured_mcp_servers():", "\ndef _removed():"))
    result = subprocess.run([sys.executable, INSTALLER, stale, "--check"],
                             capture_output=True, text=True)
    check("check reports an older revision as MISS",
          result.returncode != 0 and "MISS" in result.stdout, result.stdout.strip())
    result = subprocess.run([sys.executable, INSTALLER, same, "--check"],
                             capture_output=True, text=True)
    check("check reports the known-good as OK",
          result.returncode == 0 and "OK" in result.stdout, result.stdout.strip())

    print("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
