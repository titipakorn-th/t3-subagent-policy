#!/usr/bin/env python3
"""Self-check for apply-mcode-acp-steer.py.

Run: python3 test_mcode_acp_steer.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PATCHER = os.path.join(HERE, "apply-mcode-acp-steer.py")
KNOWN_GOOD = os.path.join(HERE, "mcode-acp-proxy-steer.js")

spec = importlib.util.spec_from_file_location("apply_mcode_acp_steer", PATCHER)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


def check(label, got, want):
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    print(f"ok  {label}: {got!r}")


def run(target):
    return subprocess.run(
        [sys.executable, PATCHER, target], capture_output=True, text=True
    )


# The patched file is the only fixture needed: reversing the edits rebuilds the
# unpatched baseline the patcher is supposed to recognise.
good = open(KNOWN_GOOD).read()
baseline = good
for _name, old, new in patcher.EDITS:
    assert baseline.count(new) == 1, "known-good file does not contain one edit"
    baseline = baseline.replace(new, old, 1)

with tempfile.TemporaryDirectory() as tmp:
    target = os.path.join(tmp, "mcode-acp-proxy.js")

    # 1. An unpatched proxy is patched, and the result is the known-good file.
    open(target, "w").write(baseline)
    result = run(target)
    check("patch exits 0", result.returncode, 0)
    check("patch reports success", "patched:" in result.stdout, True)
    check("patched file equals known-good", open(target).read(), good)
    check("a backup was written", any(
        name.startswith("mcode-acp-proxy.js.bak-") for name in os.listdir(tmp)
    ), True)

    # 2. Running it again changes nothing.
    before = open(target).read()
    result = run(target)
    check("second run exits 0", result.returncode, 0)
    check("second run is a no-op", "already patched" in result.stdout, True)
    check("second run leaves the file alone", open(target).read() == before, True)

    # 3. A file it does not recognise is refused, not mangled.
    unknown = os.path.join(tmp, "unknown.js")
    open(unknown, "w").write("console.log('not a proxy');\n")
    result = run(unknown)
    check("unknown file is refused", result.returncode != 0, True)
    check("unknown file is unchanged", open(unknown).read(), "console.log('not a proxy');\n")

    # 4. A missing file is reported, not created.
    missing = os.path.join(tmp, "nope.js")
    result = run(missing)
    check("missing file is reported", result.returncode != 0, True)
    check("missing file was not created", os.path.exists(missing), False)

    # 5. The older proxy revision (502-only recovery) is patched too: the fix
    #    only needs the transport layer, which both revisions share.
    older = baseline.replace(
        "const cancelledSessions = new Set();",
        "const cancelledSessions = new Set();\nconst legacyMarker = true;",
        1,
    )
    legacy = os.path.join(tmp, "legacy.js")
    open(legacy, "w").write(older)
    result = run(legacy)
    check("legacy revision is patched", result.returncode == 0, True)
    check("legacy revision keeps its own code", "legacyMarker" in open(legacy).read(), True)
    check("legacy revision got the fix", "RETRYABLE_REJECTION" in open(legacy).read(), True)

    # 6. The patch can be undone, so a machine can go back to the stock proxy.
    open(target, "w").write(good)
    result = subprocess.run(
        [sys.executable, PATCHER, target, "--revert"], capture_output=True, text=True
    )
    check("revert exits 0", result.returncode, 0)
    check("revert reports it", "reverted:" in result.stdout, True)
    check("revert restores the baseline", open(target).read() == baseline, True)
    result = subprocess.run(
        [sys.executable, PATCHER, target, "--revert"], capture_output=True, text=True
    )
    check("reverting twice is a no-op", result.returncode == 0 and "not a patched proxy" in result.stdout, True)

print("\nall checks passed")
