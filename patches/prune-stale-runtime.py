#!/usr/bin/env python3
"""Prune the leftovers T3 Code and MiniMax Code leave behind after an update.

Dry run by default. `--apply` moves the victims to the macOS trash, so a
mistake is recoverable. `--permanent` deletes them outright.

The pinned-runtime rules mirror what T3 Code does internally (packages/cli/src/cli/update.ts):
a runtime under ~/.t3/runtime/versions is only removed when it is settled, is not the
active version of a background service, is not referenced by a running process or by a
launcher, and is not the version a `t3 update` just installed. T3 Code's own prune
returns early when there is no service-state.json, which is why old runtimes survive
forever on a desktop-only install.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
T3_HOME = Path(os.environ.get("T3CODE_HOME") or (HOME / ".t3"))
MINIMAX_HOME = Path(os.environ.get("MINIMAX_HOME") or (HOME / ".minimax"))
MINIMAX_CODE_HOME = HOME / ".minimax-code"

VERSIONS_DIR = T3_HOME / "runtime" / "versions"
SERVICE_STATE = T3_HOME / "runtime" / "service-state.json"
UPDATER_CACHE = HOME / "Library" / "Caches" / "t3code-updater"
OLD_APP_PROFILE = HOME / "Library" / "Application Support" / "t3code"

# A settled runtime: 1.2.3, 1.2.3-nightly.20261106.4262, 1.2.3-preview.4 ...
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.-][A-Za-z0-9.]+)?$")
# An update that is still installing is not settled yet.
INSTALL_GRACE_SECONDS = 3600

TRASH = MINIMAX_HOME / "bin" / "mavis-trash"

# Directories the local patcher leaves a .bak copy in on every run.
BACKUP_GLOBS = [
    MINIMAX_CODE_HOME / "bin" / "mcode-acp-proxy.js.bak-*",
    HOME / ".codex" / "AGENTS.md.bak-*",
    HOME / ".claude" / "CLAUDE.md.bak-*",
    HOME / ".gemini" / "GEMINI.md.bak-*",
    HOME / ".config" / "opencode" / "AGENTS.md.bak-*",
    T3_HOME / "tools" / "antigravity-cliproxy" / "gemini-home" / "GEMINI.md.bak-*",
]
# One-shot originals the patcher drops next to its target.
ORIGINAL_FILES = [MINIMAX_CODE_HOME / "bin" / "mcode.orig"]


def size_of(path: Path) -> int:
    """Bytes on disk, counting whatever is left after the file itself."""
    try:
        if path.is_symlink():
            return path.lstat().st_size
        if path.is_file():
            return path.stat().st_size
        total = 0
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def version_of_dir(path: Path) -> str | None:
    """The version a runtime directory claims to be, if it is a settled install."""
    if path.is_symlink() or not path.is_dir():
        return None
    if not VERSION_RE.match(path.name):
        return None
    sentinel = path / ".install-complete"
    try:
        if sentinel.read_text().strip() != path.name:
            return None
    except OSError:
        return None
    return path.name


def running_versions() -> set[str]:
    """Versions whose binary is currently executing."""
    found: set[str] = set()
    try:
        output = subprocess.run(
            ["ps", "-axo", "command="], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    prefix = str(VERSIONS_DIR) + "/"
    for line in output.splitlines():
        index = line.find(prefix)
        if index == -1:
            continue
        rest = line[index + len(prefix) :]
        name = rest.split("/")[0].split(" ")[0]
        if VERSION_RE.match(name):
            found.add(name)
    return found


def launcher_versions() -> set[str]:
    """Versions a launcher on this machine still points at."""
    found: set[str] = set()
    candidates: list[Path] = []
    bin_dir = T3_HOME / "bin"
    if bin_dir.is_dir():
        candidates.extend(sorted(bin_dir.iterdir()))
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "t3"
        if candidate.exists() or candidate.is_symlink():
            candidates.append(candidate)
    prefix = str(VERSIONS_DIR) + "/"
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                candidate = candidate.resolve()
            if not candidate.is_file() or candidate.stat().st_size > 1_000_000:
                continue
            text = candidate.read_text(errors="replace")
        except OSError:
            continue
        for match in re.finditer(re.escape(prefix) + r"([0-9][0-9A-Za-z.\-]*)", text):
            found.add(match.group(1))
    return found


def service_versions() -> set[str]:
    """Versions a background service still needs."""
    found: set[str] = set()
    try:
        state = json.loads(SERVICE_STATE.read_text())
    except (OSError, ValueError):
        return found
    for key in ("activeVersion",):
        value = state.get(key)
        if isinstance(value, str):
            found.add(value)
    update = state.get("update")
    if isinstance(update, dict):
        for key in ("fromVersion", "targetVersion"):
            value = update.get(key)
            if isinstance(value, str):
                found.add(value)
    return found


def prune_pinned_runtimes(keep_latest: int) -> list[Path]:
    """Old pinned runtimes nothing points at any more."""
    if not VERSIONS_DIR.is_dir():
        return []
    settled: list[tuple[float, str, Path]] = []
    for entry in sorted(VERSIONS_DIR.iterdir()):
        if entry.name.startswith("."):
            continue  # staging dirs, install locks, already pruned copies
        version = version_of_dir(entry)
        if version is None:
            continue
        try:
            age = time.time() - (entry / ".install-complete").stat().st_mtime
        except OSError:
            continue
        if age < INSTALL_GRACE_SECONDS:
            continue  # an update is still running
        settled.append((age, version, entry))
    # Newest install first.
    settled.sort(key=lambda item: item[0])

    protected = running_versions() | launcher_versions() | service_versions()
    victims: list[Path] = []
    for index, (_age, version, entry) in enumerate(settled):
        if index < keep_latest:
            continue
        if version in protected:
            continue
        victims.append(entry)
    return victims


def installed_app_versions() -> set[str]:
    """Versions of the T3 Code app bundles on this machine."""
    versions: set[str] = set()
    for root in (Path("/Applications"), HOME / "Applications"):
        if not root.is_dir():
            continue
        for bundle in root.glob("T3 Code*.app"):
            plist = bundle / "Contents" / "Info.plist"
            try:
                with plist.open("rb") as handle:
                    info = plistlib.load(handle)
            except (OSError, ValueError):
                continue
            version = info.get("CFBundleShortVersionString")
            if isinstance(version, str):
                versions.add(version)
    return versions


def prune_updater_cache() -> list[Path]:
    """Installer archives for a version that is already installed."""
    if not UPDATER_CACHE.is_dir():
        return []
    victims: list[Path] = []
    for archive in sorted(UPDATER_CACHE.glob("*.zip")):
        victims.append(archive)
    installed = installed_app_versions()
    pending = UPDATER_CACHE / "pending"
    if pending.is_dir():
        for archive in sorted(pending.glob("*.zip")):
            # T3-Code-<version>-arm64.zip
            parts = archive.stem.split("-")
            version = "-".join(parts[2:-1]) if len(parts) > 3 else ""
            if version and version in installed:
                victims.append(archive)  # this version is already running
                continue
            try:
                age = time.time() - archive.stat().st_mtime
            except OSError:
                continue
            if age < INSTALL_GRACE_SECONDS:
                continue  # an update may be installing it right now
            victims.append(archive)
        if not list(pending.glob("*.zip")):
            victims.append(pending / "update-info.json")
    return victims


def prune_backups(keep: int) -> list[Path]:
    """Old .bak copies of the files the local patcher rewrites."""
    victims: list[Path] = []
    for pattern in BACKUP_GLOBS:
        copies = sorted(pattern.parent.glob(pattern.name), key=lambda p: p.stat().st_mtime)
        for stale in copies[: max(0, len(copies) - keep)]:
            victims.append(stale)
    for original in ORIGINAL_FILES:
        if not original.exists():
            continue
        newer = [
            copy
            for copy in original.parent.glob(original.name + "*")
            if copy.name != original.name and copy.stat().st_mtime > original.stat().st_mtime
        ]
        if newer:
            victims.append(original)
    return victims


def prune_agent_dirs(max_age_days: int) -> list[Path]:
    """Runtime scratch from agent runs that finished long ago."""
    cutoff = time.time() - max_age_days * 86400
    victims: list[Path] = []
    run_dir = MINIMAX_HOME / "run"
    if run_dir.is_dir():
        for entry in sorted(run_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    victims.append(entry)
            except OSError:
                continue
    tasks_dir = MINIMAX_HOME / "background-tasks"
    if tasks_dir.is_dir():
        for entry in sorted(tasks_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    victims.append(entry)
            except OSError:
                continue
    leases = MINIMAX_HOME / "v2" / "runtime-owner-leases"
    if leases.is_dir():
        for entry in sorted(leases.iterdir()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    victims.append(entry)
            except OSError:
                continue
    return victims


def prune_releases(keep: int) -> list[Path]:
    """MiniMax Code releases older than the newest ones (rollback copies)."""
    releases = MINIMAX_CODE_HOME / "releases"
    if not releases.is_dir():
        return []
    entries = sorted(
        (entry for entry in releases.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return entries[keep:]


def remove(paths: list[Path], apply: bool, permanent: bool) -> int:
    """Move paths to the trash (or delete them) and return the bytes freed."""
    freed = 0
    for path in paths:
        freed += size_of(path)
    if not apply:
        return freed
    if not permanent and not TRASH.is_file():
        print(f"  ! trash helper not found; refusing to delete {len(paths)} paths", file=sys.stderr)
        return -1
    if permanent:
        for path in paths:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except OSError as error:
                print(f"  ! could not remove {path}: {error}", file=sys.stderr)
        return freed
    # One call per batch: the trash helper takes many paths at once.
    for start in range(0, len(paths), 200):
        batch = [str(path) for path in paths[start : start + 200]]
        try:
            subprocess.run([str(TRASH), "--", *batch], capture_output=True, timeout=600)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"  ! could not trash a batch: {error}", file=sys.stderr)
    return freed


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually remove the leftovers")
    parser.add_argument(
        "--permanent", action="store_true", help="delete instead of moving to the trash"
    )
    parser.add_argument(
        "--keep-latest-runtime",
        type=int,
        default=0,
        help="keep this many newest pinned runtimes (default 0: nothing points at them)",
    )
    parser.add_argument(
        "--keep-releases", type=int, default=2, help="keep this many MiniMax Code releases"
    )
    parser.add_argument(
        "--keep-backups", type=int, default=2, help="keep this many .bak copies per target"
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=14,
        help="minimum age of agent scratch to remove (default 14)",
    )
    parser.add_argument(
        "--agent-scratch",
        action="store_true",
        help="also remove finished agent runs (slow: thousands of directories)",
    )
    parser.add_argument(
        "--old-app-profile",
        action="store_true",
        help="also remove the retired v1 profile in ~/Library/Application Support/t3code",
    )
    args = parser.parse_args(argv)

    groups = [
        ("pinned runtimes", prune_pinned_runtimes(args.keep_latest_runtime)),
        ("updater cache", prune_updater_cache()),
        ("patch backups", prune_backups(args.keep_backups)),
        ("minimax code releases", prune_releases(args.keep_releases)),
    ]
    if args.agent_scratch:
        # Thousands of directories, and the trash moves them one by one: slow.
        groups.append(("agent scratch", prune_agent_dirs(args.max_age_days)))
    if args.old_app_profile:
        groups.append(("retired v1 app profile", [OLD_APP_PROFILE]))

    mode = "removing" if args.apply else "would remove"
    total = 0
    count = 0
    for label, victims in groups:
        if not victims:
            continue
        count += len(victims)
        freed = remove(victims, args.apply, args.permanent)
        if freed < 0:
            return 1
        total += freed
        print(f"{mode} {len(victims):>6} {label:<24} {freed / 1e6:9.1f} MB")
    if count == 0:
        print("nothing to prune")
    else:
        print(f"{'freed' if args.apply else 'reclaimable'}: {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
