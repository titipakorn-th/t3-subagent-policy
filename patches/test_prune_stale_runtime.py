"""Tests for the stale-runtime pruner.

The pruner only removes a pinned runtime that nothing points at any more, so the
tests pin down the three ways a runtime must survive.
"""

import importlib.util
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prune_stale_runtime", Path(__file__).with_name("prune-stale-runtime.py")
)
pruner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pruner)


def make_runtime(versions_dir: Path, version: str, settled: bool = True) -> Path:
    entry = versions_dir / version
    entry.mkdir(parents=True)
    (entry / "t3").write_bytes(b"binary")
    if settled:
        sentinel = entry / ".install-complete"
        sentinel.write_text(version)
        old = time.time() - 7200
        import os

        os.utime(sentinel, (old, old))
    return entry


@pytest.fixture
def home(tmp_path, monkeypatch):
    t3_home = tmp_path / ".t3"
    versions = t3_home / "runtime" / "versions"
    versions.mkdir(parents=True)
    (t3_home / "bin").mkdir(parents=True)
    monkeypatch.setattr(pruner, "T3_HOME", t3_home)
    monkeypatch.setattr(pruner, "VERSIONS_DIR", versions)
    monkeypatch.setattr(pruner, "SERVICE_STATE", t3_home / "runtime" / "service-state.json")
    monkeypatch.setattr(pruner, "HOME", tmp_path)
    monkeypatch.setattr(pruner, "MINIMAX_HOME", tmp_path / ".minimax")
    monkeypatch.setattr(pruner, "MINIMAX_CODE_HOME", tmp_path / ".minimax-code")
    monkeypatch.setattr(pruner, "UPDATER_CACHE", tmp_path / "updater")
    monkeypatch.setattr(pruner, "OLD_APP_PROFILE", tmp_path / "t3code")
    monkeypatch.setattr(pruner, "BACKUP_GLOBS", [])
    monkeypatch.setattr(pruner, "ORIGINAL_FILES", [])
    return tmp_path


def test_unreferenced_runtime_is_pruned(home):
    entry = make_runtime(pruner.VERSIONS_DIR, "0.1.3")
    assert pruner.prune_pinned_runtimes(keep_latest=0) == [entry]


def test_launcher_referenced_runtime_survives(home):
    entry = make_runtime(pruner.VERSIONS_DIR, "0.1.3")
    launcher = pruner.T3_HOME / "bin" / "t3"
    launcher.write_text(f'exec "{pruner.VERSIONS_DIR}/0.1.3/t3" "$@"\n')
    assert pruner.prune_pinned_runtimes(keep_latest=0) == []
    assert entry.exists()


def test_service_state_referenced_runtime_survives(home):
    entry = make_runtime(pruner.VERSIONS_DIR, "0.1.3")
    pruner.SERVICE_STATE.parent.mkdir(parents=True, exist_ok=True)
    pruner.SERVICE_STATE.write_text('{"activeVersion": "0.1.3"}')
    assert pruner.prune_pinned_runtimes(keep_latest=0) == []
    assert entry.exists()


def test_unsettled_runtime_survives(home):
    entry = make_runtime(pruner.VERSIONS_DIR, "0.1.3", settled=False)
    assert pruner.prune_pinned_runtimes(keep_latest=0) == []
    assert entry.exists()


def test_keep_latest_retains_the_newest(home):
    oldest = make_runtime(pruner.VERSIONS_DIR, "0.1.3")
    newest = make_runtime(pruner.VERSIONS_DIR, "0.1.4")
    assert pruner.prune_pinned_runtimes(keep_latest=1) == [oldest]
    assert newest.exists()


def test_backup_pruning_keeps_the_newest_copies(home):
    target = home / "mcode-acp-proxy.js"
    copies = []
    for index in range(4):
        copy = home / f"mcode-acp-proxy.js.bak-{index}"
        copy.write_text("x")
        import os

        os.utime(copy, (1000 + index, 1000 + index))
        copies.append(copy)
    monkey = pruner.BACKUP_GLOBS
    pruner.BACKUP_GLOBS = [home / "mcode-acp-proxy.js.bak-*"]
    try:
        victims = pruner.prune_backups(keep=2)
    finally:
        pruner.BACKUP_GLOBS = monkey
    assert victims == copies[:2]


def test_apply_refuses_to_delete_when_trash_helper_is_missing(home, capsys, monkeypatch):
    victim = home / "runtime.zip"
    victim.write_text("keep me")
    monkeypatch.setattr(pruner, "TRASH", home / "missing-trash")
    assert pruner.remove([victim], apply=True, permanent=False) == -1
    assert victim.read_text() == "keep me"
    assert "refusing to delete" in capsys.readouterr().err
