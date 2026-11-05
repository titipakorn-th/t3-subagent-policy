#!/usr/bin/env python3
"""Regenerate the patcher's code blocks from the known-good proxy.

The patcher edits the deployed proxy by shape, so its "new" strings have to
match the known-good file byte for byte. Hand-editing both sides drifts, so
this script copies the blocks out of the known-good file instead.

It also writes the table of older patch revisions, taken from this repo's own
history, so the patcher can migrate a proxy that carries an older revision
instead of leaving it behind.

    python3 sync-patcher-blocks.py

Run it after editing mcode-acp-proxy-steer.js, then run test_mcode_acp_steer.py
to confirm the patcher and the known-good file still agree.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "mcode-acp-proxy-steer.js")
PATCHER = os.path.join(HERE, "apply-mcode-acp-steer.py")

fixture = open(FIXTURE).read()
patcher = open(PATCHER).read()

# The blocks the patcher replaces, and where they live in the known-good file.
BLOCKS = {
    "STATE_NEW": (
        fixture.index("const cancelledSessions = new Set();"),
        fixture.index("const maxRecoveryAttempts"),
    ),
    "IDKEY_NEW": (
        fixture.index("function idKey(id) {"),
        fixture.index("function runtimeFailureKind(error) {"),
    ),
    "RLOUT_NEW": (
        fixture.index("  rlOut.on('line', (line) => {"),
        fixture.index("  const rlIn = readline.createInterface"),
    ),
    "RLIN_NEW": (
        fixture.index("  rlIn.on('line', (line) => {"),
        fixture.index("  rlIn.on('close'"),
    ),
    "SPAWN_NEW": (
        fixture.index("// T3 Code owns the orchestration"),
        fixture.index("  writeChildMessage = (message) => {"),
    ),
    "INTERCEPT_NEW": (
        fixture.index("function interceptIncoming(line) {"),
        fixture.index(
            "    if (msg && msg.method === 'session/prompt' && msg.params && msg.id !== undefined"
        ),
    ),
}

# The baseline the patcher starts from: the original proxy's interceptIncoming.
INTERCEPT_OLD = """function interceptIncoming(line) {
  if (!line || !line.trim()) return line;
  try {
    const msg = JSON.parse(line);
"""


def replace_block(text, name, body):
    pattern = re.compile(r"^" + name + r" = (?:r?)\"\"\".*?\"\"\"$", re.S | re.M)
    if not pattern.search(text):
        return text + f'\n{name} = r"""{body}"""\n'
    return pattern.sub(lambda m: f'{name} = r"""{body}"""', text, count=1)


def older_revisions():
    """The sha256 of every earlier revision of the known-good file.

    A proxy carrying one of these was patched from the same baseline, so the
    current known-good file replaces it exactly.
    """
    try:
        revisions = subprocess.run(
            ["git", "log", "--format=%H", "--", "patches/mcode-acp-proxy-steer.js"],
            cwd=os.path.dirname(HERE), capture_output=True, text=True, check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    table = {}
    for sha in revisions:
        blob = subprocess.run(
            ["git", "show", f"{sha}:patches/mcode-acp-proxy-steer.js"],
            cwd=os.path.dirname(HERE), capture_output=True, text=True,
        )
        if blob.returncode != 0:
            continue
        digest = hashlib.sha256(blob.stdout.encode()).hexdigest()
        table[digest] = sha[:12]
    return table


def older_patchers():
    """The anchors of every earlier revision of the patcher itself.

    A machine patched by an earlier revision carries that revision's text.
    Replacing those texts with the current ones migrates the file, whatever
    baseline it was patched from.
    """
    try:
        revisions = subprocess.run(
            ["git", "log", "--format=%H", "--", "patches/apply-mcode-acp-steer.py"],
            cwd=os.path.dirname(HERE), capture_output=True, text=True, check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    table = {}
    for sha in revisions:
        blob = subprocess.run(
            ["git", "show", f"{sha}:patches/apply-mcode-acp-steer.py"],
            cwd=os.path.dirname(HERE), capture_output=True, text=True,
        )
        if blob.returncode != 0:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write(blob.stdout)
            old_patcher = handle.name
        edits = subprocess.run(
            [sys.executable, "-c",
             "import importlib.util,json,sys\n"
             "spec=importlib.util.spec_from_file_location('old',sys.argv[1])\n"
             "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
             "print(json.dumps([[n,new] for n,o,new in m.EDITS]))\n",
             old_patcher],
            capture_output=True, text=True,
        )
        os.unlink(old_patcher)
        if edits.returncode != 0:
            continue
        try:
            anchors = json.loads(edits.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            continue
        if anchors:
            table[sha[:12]] = anchors
    return table


for name, (start, end) in BLOCKS.items():
    # One trailing newline: the baseline keeps its own blank lines after the
    # replaced region, so the block must not add one of its own.
    body = fixture[start:end].rstrip("\n") + "\n"
    patcher = replace_block(patcher, name, body)

# The interceptIncoming anchor: the patcher needs the baseline text and the
# new one, and the new one comes from the known-good file.
if "INTERCEPT_OLD = r" not in patcher:
    patcher = patcher.replace(
        "EDITS = [",
        'INTERCEPT_OLD = r"""' + INTERCEPT_OLD + '"""\n\nEDITS = [',
        1,
    )
patcher = replace_block(patcher, "INTERCEPT_NEW", fixture[
    BLOCKS["INTERCEPT_NEW"][0]:BLOCKS["INTERCEPT_NEW"][1]
])

# Every definition has to come before EDITS uses it.
edits_at = patcher.index("EDITS = [")
moved = []
for match in re.finditer(r'^[A-Z_]+ = r?"""(?:.|\n)*?"""\n', patcher[edits_at:], re.M):
    moved.append(match.group(0))
if moved:
    for block in moved:
        patcher = patcher.replace(block, "", 1)
    patcher = patcher.replace("EDITS = [", "\n".join(moved) + "\nEDITS = [", 1)
if '("intercept", INTERCEPT_OLD, INTERCEPT_NEW)' not in patcher:
    patcher = patcher.replace(
        '    ("spawn", SPAWN_OLD, SPAWN_NEW),',
        '    ("spawn", SPAWN_OLD, SPAWN_NEW),\n    ("intercept", INTERCEPT_OLD, INTERCEPT_NEW),',
    )

# The table of older patch revisions, so the patcher can migrate them.
revisions = older_revisions()
table = "KNOWN_OLD_REVISIONS = {\n" + "".join(
    f'    "{digest}": "{sha}",\n' for digest, sha in sorted(revisions.items())
) + "}\n"
patcher = re.sub(r"^KNOWN_OLD_REVISIONS = \{.*?^\}\n", table, patcher, flags=re.S | re.M)
if "KNOWN_OLD_REVISIONS = {" not in patcher:
    patcher = patcher.replace("EDITS = [", table + "\nEDITS = [", 1)

# The anchors of the older patchers, so the patcher can migrate them.
patchers = older_patchers()
anchors = "MIGRATION_ANCHORS = {\n" + "".join(
    f'    "{sha}": {items!r},\n' for sha, items in sorted(patchers.items())
) + "}\n"
# A lambda replacement: re.sub would turn the escaped newlines in the text
# into real ones and break the file.
patcher = re.sub(r"^MIGRATION_ANCHORS = \{.*?^\}\n", lambda m: anchors, patcher, flags=re.S | re.M)
if "MIGRATION_ANCHORS = {" not in patcher:
    patcher = patcher.replace("EDITS = [", anchors + "\nEDITS = [", 1)

open(PATCHER, "w").write(patcher)
print("patcher blocks regenerated from", os.path.basename(FIXTURE))
print(f"known older patch revisions: {len(revisions)}")
print(f"known older patchers: {len(patchers)}")
