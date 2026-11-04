#!/usr/bin/env bash
# The roll-out lifecycle, on a machine that has nothing yet.
#
# The repo exists to hand the policy and the proxy patch to other machines, so
# that path is worth testing: install, install again, uninstall, check.
#
#   ./patches/test_install_lifecycle.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAKE="$(mktemp -d)/home"
trap 'rm -rf "$(dirname "$FAKE")"' EXIT

mkdir -p "$FAKE/.minimax-code/bin"
# A stock, unpatched proxy to install onto: the known-good file with the
# patcher's own edits reversed. The patcher refuses a layout it does not
# recognise, so the fixture has to be a real proxy.
STOCK="$FAKE/.minimax-code/bin/mcode-acp-proxy.js"
python3 - "$DIR" "$STOCK" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location(
    "patcher", os.path.join(sys.argv[1], "patches", "apply-mcode-acp-steer.py"))
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
src = open(os.path.join(sys.argv[1], "patches", "mcode-acp-proxy-steer.js")).read()
for name, old, new in reversed(patcher.EDITS):
    assert src.count(new) == 1, name
    src = src.replace(new, old, 1)
open(sys.argv[2], "w").write(src)
PY

failures=0
check() {
  if [ "$2" = "0" ]; then echo "ok    $1"; else echo "FAIL  $1 :: $3"; failures=$((failures + 1)); fi
}

run() { HOME="$FAKE" "$DIR/install.sh" "$@" >/tmp/install-lifecycle.log 2>&1; }

# 1. Before: nothing is installed.
run --check
status=$?
[ "$status" = "1" ]
check "check reports the missing policy and proxy" "$?" "$(tail -2 /tmp/install-lifecycle.log | tr '\n' ' ')"
grep -q "not patched" /tmp/install-lifecycle.log
check "the proxy is reported as unpatched" "$?" ""

# 2. Install.
run
status=$?
[ "$status" = "0" ]
check "install succeeds" "$?" "$(tail -3 /tmp/install-lifecycle.log | tr '\n' ' ')"
grep -q "patched:" /tmp/install-lifecycle.log
check "the proxy is patched" "$?" ""
[ -f "$FAKE/.codex/AGENTS.md" ]
check "the policy block is installed" "$?" ""

# 3. Install again: nothing changes.
run
grep -q "already patched" /tmp/install-lifecycle.log
check "installing again is a no-op for the proxy" "$?" ""
grep -q "unchanged" /tmp/install-lifecycle.log
check "installing again is a no-op for the policy" "$?" ""

# 4. Check: everything is in place.
run --check
status=$?
[ "$status" = "0" ]
check "check passes after the install" "$?" "$(tail -2 /tmp/install-lifecycle.log | tr '\n' ' ')"

# 5. Uninstall: the policy goes, the proxy comes back.
run --uninstall
grep -q "reverted:" /tmp/install-lifecycle.log
check "the proxy is reverted" "$?" ""
grep -q "removed" /tmp/install-lifecycle.log
check "the policy block is removed" "$?" ""

# 6. Check again: the machine is back where it started.
run --check
status=$?
[ "$status" = "1" ]
check "check fails again after the uninstall" "$?" ""

# The reverted proxy must be the stock file, byte for byte.
cp "$STOCK" /tmp/stock-proxy.js
diff -q "$FAKE/.minimax-code/bin/mcode-acp-proxy.js" /tmp/stock-proxy.js >/dev/null
check "the reverted proxy is the stock file" "$?" ""

if [ "$failures" = "0" ]; then
  echo ""
  echo "all checks passed"
  exit 0
fi
echo ""
echo "$failures check(s) failed"
exit 1
