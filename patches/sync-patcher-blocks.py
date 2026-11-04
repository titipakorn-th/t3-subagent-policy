#!/usr/bin/env python3
"""Regenerate the patcher's code blocks from the known-good proxy.

The patcher edits the deployed proxy by shape, so its "new" strings have to
match the known-good file byte for byte. Hand-editing both sides drifts, so
this script copies the blocks out of the known-good file instead.

    python3 sync-patcher-blocks.py

Run it after editing mcode-acp-proxy-steer.js, then run test_mcode_acp_steer.py
to confirm the patcher and the known-good file still agree.
"""
import os
import re

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
open(PATCHER, "w").write(patcher)
print("patcher blocks regenerated from", os.path.basename(FIXTURE))
