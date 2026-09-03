#!/usr/bin/env python3
"""
MintCleaner – selective temp and cache cleanup for Linux Mint.

Start this file (run.py) to launch the application.

Features:
- Single privileged helper via pkexec for root tasks, one authentication at startup.
- GUI becomes visible only AFTER authentication succeeded.
- Startup size analysis runs in the background with a modal progress dialog that
  lists every step, so it is visible what is measured and what is still pending.
- Live size analysis per measurable category, labels show current MB.
- Auto select items above a configured threshold in MB.
- Auto deselect items that are 0 MB or unknown size.
- User deletion mode selectable: Move to Trash (default) or Delete immediately.
  Note: Mode applies only to user scoped paths.
  Trash contents (~/.local/share/Trash/*) are always deleted when selected.
- Cleanup runs in the background with the same progress dialog.
- Persistent disk space report: free space before, deleted amount, free space
  after, and the change on disk. It stays visible after the cleanup.
- Window and taskbar icon from a generated PNG (see ui/window_icon.py).

No popups before or after deletion, progress is logged in the UI.
"""

import os
import re
import sys
import shlex
import glob
import json
import queue
import shutil
import getpass
import threading
import subprocess
from typing import Tuple, List, Dict, Any, Optional, Callable, Sequence

# Session variables that must never be inherited by the root helper.
# If kept, importing tkinter/GLib as root writes into the user's dconf and
# leaves root-owned files that break Cinnamon/Nemo icons and themes.
HELPER_SESSION_ENV_VARS: tuple[str, ...] = (
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "AT_SPI_BUS_ADDRESS",
    "GNOME_KEYRING_CONTROL",
    "GNOME_KEYRING_PID",
    "GPG_AGENT_INFO",
    "SSH_AUTH_SOCK",
    "SESSION_MANAGER",
    "XAUTHORITY",
    "DISPLAY",
    "WAYLAND_DISPLAY",
)


def sanitize_helper_environment() -> None:
    """
    Detach the privileged helper from the calling user's desktop session.

    pkexec may still leave user session variables in the environment. Combined
    with importing tkinter as root this recreates /run/user/<uid>/dconf/user
    and ~/.cache/dconf as root-owned files and removes desktop icons.
    """
    for key in HELPER_SESSION_ENV_VARS:
        os.environ.pop(key, None)
    # Force root-local config/cache paths so nothing writes into the user home.
    os.environ["HOME"] = "/root"
    os.environ["XDG_CACHE_HOME"] = "/root/.cache"
    os.environ["XDG_CONFIG_HOME"] = "/root/.config"
    os.environ["XDG_DATA_HOME"] = "/root/.local/share"
    os.environ["XDG_STATE_HOME"] = "/root/.local/state"


# Must run before tkinter/GLib imports when started as the pkexec helper.
if "--helper" in sys.argv:
    sanitize_helper_environment()

if __name__ == "__main__" and "--helper" not in sys.argv:
    from services.dependencies import ensure_runtime_dependencies

    ensure_runtime_dependencies()

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from ui.desktop_setup import (
    desktop_shortcut_installed,
    install_desktop_shortcut,
    refresh_desktop_shortcut,
    remove_desktop_shortcut,
)
from ui.nemo_setup import (
    install_nemo_action,
    nemo_action_installed,
    refresh_nemo_action,
    remove_nemo_action,
)
from ui.progress_dialog import ProgressDialog
from ui.window_icon import WM_CLASS_NAME, apply_window_icon, glyph_photo_image
from datetime import datetime
from urllib.parse import quote

# ----------------------------- Config -----------------------------

AUTOCHECK_THRESHOLD_MB = 100  # Auto select items larger than this many MB; set 0 to disable

# Short labels for the progress dialog, in the order they are measured.
# The keys are also the single source of truth for "measurable" categories.
CATEGORY_LABELS: Dict[str, str] = {
    "tmp": "/tmp and /var/tmp",
    "apt": "APT cleanup targets",
    "apt_cache": "APT package cache",
    "system_misc_caches": "General system caches",
    "system_extra_caches": "Additional system caches",
    "flatpak_syscache": "System Flatpak cache",
    "journal": "Systemd journal",
    "user_cache": "User cache (~/.cache)",
    "thumbnails": "Thumbnail cache",
    "trash": "Trash",
    "flatpak_app_cache": "Flatpak application cache",
    "firefox": "Firefox cache",
    "chrome": "Chrome/Chromium cache",
    "config_app_caches": "App caches in ~/.config",
    "dev_tool_caches": "Developer tool caches",
    "user_lang_tool_caches": "Language and tool caches",
    "python_artifacts": "Python leftovers (__pycache__, .venv)",
    "local_history": "Editor Local History (.history)",
    "editor_caches": "Editor caches (VSIX, logs, snapshots)",
    "editor_state": "Editor workspace state and history",
    "ai_assistant_logs": "AI assistant transcripts and logs",
    "go_mod_cache": "Go module cache (~/go/pkg/mod)",
    "node_modules": "Node.js node_modules folders",
    "composer_vendor": "Composer vendor folders",
    "build_output": "Project build output (dist, build, target)",
    "old_kernels": "Old kernels (not running)",
}

# Categories whose size can be measured.
MEASURABLE_KEYS: Tuple[str, ...] = tuple(CATEGORY_LABELS)

# Categories that must be measured by the privileged helper.
ROOT_MEASURABLE_KEYS: frozenset = frozenset({
    "tmp", "flatpak_syscache", "apt", "journal",
    "system_misc_caches", "system_extra_caches", "apt_cache",
})

# Categories whose paths are discovered by walking the home directory.
DISCOVERED_KEYS: Tuple[str, ...] = (
    "python_artifacts", "local_history",
    "node_modules", "composer_vendor", "build_output",
)

# Columns of the disk space table, left to right: free space before the cleanup,
# the amount that was freed, and the free space available afterwards. These
# captions are used once a cleanup has run.
RESULT_TABLE_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    ("Free space before cleanup", "before", "#2c3e50"),
    ("Space freed", "freed", "#1e7f3b"),
    ("Free space available now", "after", "#1a5fb4"),
)

# Captions of the same table while it projects what the current selection would
# free. Same order and same columns as RESULT_TABLE_COLUMNS.
PREVIEW_TABLE_CAPTIONS: Tuple[str, ...] = (
    "Free space now",
    "Selected categories can free",
    "Free space after cleanup",
)

# Compact headers for the same table in the activity log, which is narrower than
# the window. Same order as RESULT_TABLE_COLUMNS.
RESULT_TABLE_LOG_HEADERS: Tuple[str, ...] = ("Before", "Freed", "Now free")

# Same, for the projection of the current selection.
PREVIEW_TABLE_LOG_HEADERS: Tuple[str, ...] = ("Now free", "Can free", "Afterwards")

# Width in characters the activity log can show without wrapping.
LOG_WIDTH_CHARS = 46

# Labels of the two user deletion modes, shown in the combobox and the menu.
DELETE_MODE_LABELS: Dict[str, str] = {
    "delete": "Delete immediately",
    "trash": "Move to Trash",
}

# Colors of the highlighted main action button ("Clean Selected").
PRIMARY_BUTTON_BG = "#1e7f3b"
PRIMARY_BUTTON_ACTIVE = "#25984a"
PRIMARY_BUTTON_PRESSED = "#17662f"
PRIMARY_BUTTON_DISABLED = "#9aa5ae"
PRIMARY_BUTTON_FG = "#ffffff"

# Edge length of the broom glyph shown inside the main action button.
PRIMARY_BUTTON_GLYPH_SIZE = 22

# Colors of the result table, the border color also draws the grid lines.
TABLE_BORDER_COLOR = "#c3c9d0"
TABLE_HEADER_BG = "#eceff2"
TABLE_HEADER_FG = "#414952"
TABLE_CELL_BG = "#ffffff"

# Selection variables, without the "var_" prefix, used to snapshot the GUI state.
SELECTION_NAMES: Tuple[str, ...] = (
    "tmp", "user_cache", "thumbnails", "trash", "firefox", "chrome",
    "flatpak_app_cache", "config_app_caches", "dev_tool_caches",
    "user_lang_tool_caches", "python_artifacts", "local_history",
    "flatpak_user", "flatpak_repair_user", "flatpak_syscache",
    "flatpak_repair_system", "apt", "apt_cache", "system_misc_caches",
    "system_extra_caches", "journal", "old_kernels",
    "editor_caches", "editor_state", "ai_assistant_logs", "go_mod_cache",
    "node_modules", "composer_vendor", "build_output",
)

# ~/.cache entries that must never be deleted (desktop session / icons / themes).
PROTECTED_USER_CACHE_NAMES: frozenset = frozenset({
    "dconf",
    "ibus",
    "ibus-table",
    "imsettings",
    "keyring",
    "cinnamon",
    "muffin",
    "sessions",
    "session",
    "at-spi",
    "at-spi2",
    "gnome-shell",
    "evolution",
})

# Absolute path prefixes under $HOME that must never be deleted or trashed.
PROTECTED_HOME_RELATIVE_PREFIXES: tuple[str, ...] = (
    ".cache/dconf",
    ".config/dconf",
    ".icons",
    ".themes",
    ".local/share/icons",
    ".local/share/themes",
)

# /tmp and /var/tmp names that keep the graphical session alive.
PROTECTED_TMP_NAMES: frozenset = frozenset({
    ".X11-unix",
    ".ICE-unix",
    ".font-unix",
    ".Test-unix",
})

# Conservative cache-only directories in ~/.config.
# These paths contain temporary browser/Electron caches and can be recreated.
CONFIG_CACHE_PATTERNS: List[str] = [
    "~/.config/Code/Cache/*",
    "~/.config/Code/CachedData/*",
    "~/.config/Code/Code Cache/*",
    "~/.config/Code/GPUCache/*",
    "~/.config/Code/Service Worker/CacheStorage/*",
    "~/.config/Cursor/Cache/*",
    "~/.config/Cursor/CachedData/*",
    "~/.config/Cursor/Code Cache/*",
    "~/.config/Cursor/GPUCache/*",
    "~/.config/Cursor/Service Worker/CacheStorage/*",
    "~/.config/google-chrome/Default/Code Cache/*",
    "~/.config/google-chrome/Default/GPUCache/*",
    "~/.config/google-chrome/Default/Service Worker/CacheStorage/*",
    "~/.config/google-chrome/ShaderCache/*",
    "~/.config/BraveSoftware/Brave-Browser/Default/Code Cache/*",
    "~/.config/BraveSoftware/Brave-Browser/Default/GPUCache/*",
    "~/.config/BraveSoftware/Brave-Browser/Default/Service Worker/CacheStorage/*",
    "~/.config/BraveSoftware/Brave-Browser/ShaderCache/*",
]

# Common Linux user-space package/build caches outside ~/.cache.
# All entries are regenerated on demand by the related tools.
DEV_TOOL_CACHE_PATTERNS: List[str] = [
    "~/.npm/_cacache/*",
    # npx keeps a full node_modules tree per invoked package and never prunes it.
    "~/.npm/_npx/*",
    "~/.yarn/cache/*",
    "~/.yarn/berry/cache/*",
    "~/.pnpm-store/*",
    "~/.bun/install/cache/*",
    "~/.cargo/registry/cache/*",
    "~/.cargo/registry/src/*",
    "~/.gradle/caches/*",
    # Downloaded Gradle distributions, re-fetched by the wrapper on demand.
    "~/.gradle/wrapper/dists/*",
    "~/.gradle/daemon/*",
    # Maven, Ivy, sbt and NuGet keep unbounded artifact repositories.
    "~/.m2/repository/*",
    "~/.ivy2/cache/*",
    "~/.sbt/boot/*",
    "~/.nuget/packages/*",
    "~/.composer/cache/*",
]

# Additional language and package manager caches in user space.
USER_LANG_TOOL_CACHE_PATTERNS: List[str] = [
    "~/.cache/pip/*",
    "~/.cache/pypoetry/*",
    "~/.cache/uv/*",
    "~/.cache/go-build/*",
    "~/.cache/node-gyp/*",
    "~/.cache/fontconfig/*",
    "~/.cache/mesa_shader_cache/*",
    "~/.cache/yarn/*",
    "~/.cache/composer/*",
    "~/.cache/typescript/*",
    "~/.cache/deno/*",
    # Browser and Electron binaries pulled by test/build tooling.
    "~/.cache/ms-playwright/*",
    "~/.cache/puppeteer/*",
    "~/.cache/electron/*",
    "~/.cache/electron-builder/*",
    "~/.cache/Cypress/*",
    "~/.cache/JetBrains/*",
]

# Editor caches that the editor rebuilds on demand. No user data.
# CachedExtensionVSIXs holds installer archives kept after installation.
EDITOR_CACHE_PATTERNS: List[str] = [
    "~/.config/Code/CachedExtensionVSIXs/*",
    "~/.config/Code/CachedProfilesData/*",
    "~/.config/Code/CachedData/*",
    "~/.config/Code/logs/*",
    "~/.config/Cursor/CachedExtensionVSIXs/*",
    "~/.config/Cursor/CachedProfilesData/*",
    "~/.config/Cursor/CachedData/*",
    "~/.config/Cursor/logs/*",
    "~/.config/Cursor/snapshots/*",
    "~/.config/VSCodium/CachedExtensionVSIXs/*",
    "~/.config/VSCodium/CachedData/*",
    "~/.config/VSCodium/logs/*",
]

# Editor state that is regenerated but carries per-workspace history.
# Selecting this loses undo history, timeline entries and view state.
EDITOR_STATE_PATTERNS: List[str] = [
    "~/.config/Code/User/workspaceStorage/*",
    "~/.config/Code/User/History/*",
    "~/.config/Code/WebStorage/*",
    "~/.config/Cursor/User/workspaceStorage/*",
    "~/.config/Cursor/User/History/*",
    "~/.config/Cursor/WebStorage/*",
    # Cursor stores its chat/session history in one SQLite file that grows
    # without bound; it is recreated empty on next start.
    "~/.config/Cursor/User/globalStorage/state.vscdb",
    "~/.config/VSCodium/User/workspaceStorage/*",
    "~/.config/VSCodium/User/History/*",
]

# AI coding assistant transcripts and logs.
# Only conversation transcripts are matched. Persistent "memory" directories
# hold user-authored notes and are never touched by these patterns.
AI_ASSISTANT_LOG_PATTERNS: List[str] = [
    "~/.claude/projects/*/*.jsonl",
    "~/.claude/file-history/*",
    "~/.claude/shell-snapshots/*",
    "~/.claude/telemetry/*",
    "~/.codeium/logs/*",
    "~/.config/github-copilot/logs/*",
]

# Common Ubuntu/Linux system cache and transient data locations.
# These paths are safe to recreate and do not include user configuration.
SYSTEM_MISC_CACHE_PATTERNS: List[str] = [
    "/var/cache/fontconfig/*",
    "/var/cache/man/*",
    "/var/lib/apt/lists/*",
    "/var/lib/snapd/cache/*",
    "/var/cache/snapd/*",
    "/var/crash/*",
]

# Additional system-wide caches commonly found on Ubuntu and Linux Mint.
SYSTEM_EXTRA_CACHE_PATTERNS: List[str] = [
    "/var/cache/PackageKit/*",
    "/var/cache/fwupd/*",
    "/var/cache/ldconfig/*",
    "/var/lib/systemd/coredump/*",
]

# Regeneratable Python project leftovers under the home directory.
PYTHON_ARTIFACT_DIR_NAMES: frozenset = frozenset({"__pycache__", ".venv"})

# VS Code / Cursor Local History extension snapshots (xyz.local-history).
LOCAL_HISTORY_DIR_NAMES: frozenset = frozenset({".history"})

# Dependency and build directories that a project tool can recreate.
# Each name maps to the sibling files that prove a real project owns it.
# Without a matching sibling the directory is left alone, so unrelated folders
# called "build" or "vendor" (for example shipped CMS assets) stay untouched.
NODE_MODULES_DIR_NAMES: frozenset = frozenset({"node_modules"})
NODE_MODULES_MARKERS: Tuple[str, ...] = ("package.json",)

COMPOSER_VENDOR_DIR_NAMES: frozenset = frozenset({"vendor"})
COMPOSER_VENDOR_MARKERS: Tuple[str, ...] = ("composer.json",)

BUILD_OUTPUT_DIR_NAMES: frozenset = frozenset({
    "dist", "build", "target", ".next", ".nuxt", ".svelte-kit", ".parcel-cache",
})
BUILD_OUTPUT_MARKERS: Tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "Cargo.toml",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "go.mod",
)

# Go marks every file in its module cache read-only, so shutil.rmtree fails.
# The cache is cleared with the toolchain's own command instead.
GO_MODULE_CACHE_PATH = "~/go/pkg/mod"

# Do not descend into these while scanning home for named leftover dirs.
HOME_SCAN_SKIP_DIR_NAMES: frozenset = frozenset({
    ".git",
    "node_modules",
    ".cache",
    ".npm",
    ".yarn",
    ".pnpm-store",
    ".cargo",
    ".rustup",
    ".local",
    ".config",
    ".mozilla",
    ".thunderbird",
    ".var",
    ".snap",
    "snap",
    ".thumbnails",
    ".steam",
    ".wine",
    "Trash",
    ".Trash",
    ".gvfs",
    ".dbus",
    ".pki",
    ".cursor",
    ".vscode",
    "__pycache__",
    ".venv",
    ".history",
    # Version and package managers ship test fixtures and their own caches.
    # They are covered by the dedicated cache categories, so scanning inside
    # them only produces false positives.
    ".nvm",
    ".m2",
    ".gradle",
    ".ivy2",
    ".sbt",
    ".nuget",
    ".composer",
    ".bun",
    ".deno",
    ".pyenv",
    ".rbenv",
    ".sdkman",
    "go",
    # Assistant state and large SDK trees.
    ".claude",
    ".codeium",
    ".vscode-server",
    ".android",
    "Android",
})

# Cap home walk depth (from $HOME) for responsive size analysis.
HOME_SCAN_MAX_DEPTH = 12
PYTHON_ARTIFACT_MAX_DEPTH = HOME_SCAN_MAX_DEPTH
# Backwards-compatible alias used by older call sites / tests.
PYTHON_ARTIFACT_SKIP_DIR_NAMES = HOME_SCAN_SKIP_DIR_NAMES

# ----------------------------- Utilities (unprivileged) -----------------------------

def is_protected_path(path: str) -> bool:
    """
    Return True when a path must never be deleted or moved to Trash.

    Protects desktop session state (dconf), icon/theme directories, and X11
    socket directories under /tmp so Cinnamon/Nemo icons cannot disappear.
    Checks are independent of $HOME so the root helper stays safe too.

    :param path: File or directory path (may contain ~).
    :return: True when the path is protected.
    """
    expanded = os.path.abspath(os.path.expanduser(path))

    for relative in PROTECTED_HOME_RELATIVE_PREFIXES:
        marker = "/" + relative.replace("\\", "/")
        idx = expanded.find(marker)
        if idx != -1:
            after = expanded[idx + len(marker):]
            if after == "" or after.startswith(os.sep):
                return True

    parts = expanded.split(os.sep)
    try:
        cache_idx = parts.index(".cache")
    except ValueError:
        cache_idx = -1
    if cache_idx >= 0 and cache_idx + 1 < len(parts):
        if parts[cache_idx + 1] in PROTECTED_USER_CACHE_NAMES:
            return True

    for tmp_root in ("/tmp", "/var/tmp"):
        if expanded.startswith(tmp_root + os.sep):
            name = expanded[len(tmp_root) + 1:].split(os.sep, 1)[0]
            if name in PROTECTED_TMP_NAMES or name.startswith("pulse-"):
                return True
        elif expanded == tmp_root:
            # Never remove the tmp roots themselves.
            return True

    return False


def user_hicolor_shadows_system_icons() -> bool:
    """
    Return True when ~/.local/share/icons/hicolor replaces system hicolor unsafely.

    An incomplete user hicolor index.theme (common for custom app launchers)
    shadows /usr/share/icons/hicolor and hides Nemo/Cinnamon xsi-*-symbolic
    toolbar and sidebar icons.

    :return: True when repair is needed.
    """
    index_path = os.path.expanduser("~/.local/share/icons/hicolor/index.theme")
    if not os.path.isfile(index_path):
        return False
    try:
        with open(index_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return False
    # Safe user overlays list scalable/actions (or inherit full hicolor coverage).
    if "scalable/actions" in text or "xsi-" in text:
        return False
    return "Directories=" in text


def repair_user_hicolor_shadow() -> Tuple[bool, str]:
    """
    Remove incomplete user hicolor theme metadata that hides system icons.

    Keeps custom app icons under ~/.local/share/icons/hicolor/*/apps/ intact.
    Only removes index.theme and icon-theme.cache when they shadow xsi icons.

    :return: (changed, log_message)
    """
    if not user_hicolor_shadows_system_icons():
        return False, "User hicolor theme does not shadow system icons."

    base = os.path.expanduser("~/.local/share/icons/hicolor")
    removed: List[str] = []
    for name in ("index.theme", "icon-theme.cache"):
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        backup = path + ".mint-cleaner-backup"
        try:
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(path, backup)
            removed.append(os.path.basename(path))
        except OSError as exc:
            return False, f"Could not repair user hicolor theme: {exc}"

    if not removed:
        return False, "User hicolor theme does not shadow system icons."
    return True, (
        "Repaired incomplete ~/.local/share/icons/hicolor theme "
        f"(moved {', '.join(removed)} aside). Nemo/Cinnamon system icons restored."
    )


def human_mb(n_bytes: int) -> str:
    """
    Convert bytes to a human friendly MB string with one decimal.
    """
    mb = n_bytes / (1024 * 1024)
    return f"{mb:.1f} MB"


def human_gb(n_bytes: int) -> str:
    """
    Convert bytes to a human friendly GB string with two decimals.
    """
    gb = n_bytes / (1024 * 1024 * 1024)
    return f"{gb:.2f} GB"


def human_size(n_bytes: int) -> str:
    """
    Format a byte count as MB below 1 GB and as GB above.

    :param n_bytes: Size in bytes.
    :return: Human readable size string.
    """
    if abs(n_bytes) >= 1024 * 1024 * 1024:
        return human_gb(n_bytes)
    return human_mb(n_bytes)


def human_delta(n_bytes: int) -> str:
    """
    Format a signed byte difference with an explicit sign.

    :param n_bytes: Difference in bytes.
    :return: Signed human readable size string.
    """
    if n_bytes == 0:
        return "±0 MB"
    sign = "+" if n_bytes > 0 else "-"
    return f"{sign}{human_size(abs(n_bytes))}"


def format_text_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> List[str]:
    """
    Render a plain text table for the activity log.

    Column widths follow the longest cell, so the table stays aligned in the
    monospaced log widget.

    :param headers: Column captions.
    :param rows: Table rows, each with one cell per column.
    :return: Rendered lines, including the separator lines.
    """
    cells = [list(headers)] + [list(row) for row in rows]
    widths = [
        max(len(str(row[index])) for row in cells)
        for index in range(len(headers))
    ]
    separator = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def render(row: Sequence[str]) -> str:
        """Render one row with padded cells."""
        return "| " + " | ".join(
            str(value).ljust(width) for value, width in zip(row, widths)
        ) + " |"

    lines = [separator, render(headers), separator]
    lines += [render(row) for row in rows]
    lines.append(separator)
    return lines


def cleanup_table_row(result: Dict[str, Any]) -> Dict[str, str]:
    """
    Build the cleanup result table row from a finished cleanup run.

    The middle column is the real change of the free disk space, so the row adds
    up: free space before plus freed equals free space now. The deleted data
    volume can differ from it, for example when files were moved to the Trash or
    when a process still holds deleted files open, and is reported separately.

    :param result: Result dict produced by the cleanup worker.
    :return: Mapping of column key to formatted value.
    """
    free_before = primary_free_bytes(result.get("disk_before", {}))
    free_after = primary_free_bytes(result.get("disk_after", {}))
    gained = free_after - free_before
    return {
        "before": human_gb(free_before),
        "freed": human_size(gained) if gained >= 0 else human_delta(gained),
        "after": human_gb(free_after),
    }


def selected_potential_bytes(sizes: Dict[str, int], selection: Dict[str, bool]) -> int:
    """
    Sum the measured sizes of all currently selected measurable categories.

    This is the amount a cleanup would free with the current selection.

    :param sizes: Measured size in bytes per category key.
    :param selection: Mapping of selection name to bool.
    :return: Total size in bytes.
    """
    return sum(
        sizes.get(key, 0) for key in MEASURABLE_KEYS if selection.get(key)
    )


def projection_table_row(free_bytes: int, potential_bytes: int) -> Dict[str, str]:
    """
    Build the table row that projects the effect of the current selection.

    Uses the same columns as cleanup_table_row(), so the table shows the
    projection and the later real result in the same place.

    :param free_bytes: Free bytes available now.
    :param potential_bytes: Bytes the current selection would free.
    :return: Mapping of column key to formatted value.
    """
    return {
        "before": human_gb(free_bytes),
        "freed": human_size(potential_bytes),
        "after": human_gb(free_bytes + potential_bytes),
    }


def trash_mode_delays_space(selection: Dict[str, bool], delete_mode: str) -> bool:
    """
    Return True when the selection only moves user data to the Trash.

    In that case the projected space is not released on disk before the Trash is
    emptied. Trash contents themselves are always deleted immediately.

    :param selection: Mapping of selection name to bool.
    :param delete_mode: "trash" or "delete".
    :return: True when a hint about the Trash is needed.
    """
    if delete_mode != "trash":
        return False
    return any(
        selection.get(key)
        for key in MEASURABLE_KEYS
        if key not in ROOT_MEASURABLE_KEYS and key != "trash"
    )


def disk_report_targets() -> List[Tuple[str, str]]:
    """
    Return the filesystems the disk report covers.

    Always reports the root filesystem, which holds /tmp and /var, and adds the
    home filesystem when it is a separate mount.

    :return: List of (label, path) pairs.
    """
    home = os.path.expanduser("~")
    targets: List[Tuple[str, str]] = [("System (/)", "/")]
    try:
        if os.stat(home).st_dev != os.stat("/").st_dev:
            targets.append((f"Home ({home})", home))
    except OSError:
        pass
    return targets


def disk_snapshot() -> Dict[str, Tuple[int, int]]:
    """
    Read free and total bytes for every reported filesystem.

    :return: Mapping of filesystem label to (free_bytes, total_bytes).
    """
    snapshot: Dict[str, Tuple[int, int]] = {}
    for label, path in disk_report_targets():
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        snapshot[label] = (usage.free, usage.total)
    return snapshot


def primary_free_bytes(snapshot: Dict[str, Tuple[int, int]]) -> int:
    """
    Return free bytes of the first filesystem in a snapshot.

    :param snapshot: Snapshot created by disk_snapshot().
    :return: Free bytes, or 0 for an empty snapshot.
    """
    for free, _total in snapshot.values():
        return free
    return 0


def format_disk_line(snapshot: Dict[str, Tuple[int, int]]) -> str:
    """
    Build a one line summary of all filesystems in a snapshot.

    :param snapshot: Snapshot created by disk_snapshot().
    :return: Human readable summary, empty when the snapshot is empty.
    """
    parts = [
        f"{label}: {human_gb(free)} free of {human_gb(total)}"
        for label, (free, total) in snapshot.items()
    ]
    return "  ·  ".join(parts)


def size_of_path(path: str) -> int:
    """
    Compute size in bytes of a file, directory or glob pattern.
    Ignores permission errors, broken symlinks and protected session paths.

    :param path: File, directory or glob pattern.
    :return: Total size in bytes.
    """
    path = os.path.expanduser(path)
    if os.path.exists(path) and not glob.has_magic(path):
        if is_protected_path(path):
            return 0
        if os.path.isdir(path) and not os.path.islink(path):
            total = 0
            for root, dirnames, files in os.walk(path, onerror=lambda e: None):
                # Skip protected children when measuring caches.
                dirnames[:] = [
                    d for d in dirnames
                    if not is_protected_path(os.path.join(root, d))
                ]
                for f in files:
                    fp = os.path.join(root, f)
                    if is_protected_path(fp):
                        continue
                    try:
                        if not os.path.islink(fp):
                            total += os.path.getsize(fp)
                    except Exception:
                        pass
            return total
        if os.path.isfile(path) and not os.path.islink(path):
            try:
                return os.path.getsize(path)
            except Exception:
                return 0
    total = 0
    for p in glob.glob(path, recursive=False):
        if is_protected_path(p):
            continue
        total += size_of_path(p)
    return total


def size_of_patterns(patterns: List[str]) -> int:
    """
    Compute the combined size in bytes for a list of patterns.

    :param patterns: List of file or directory patterns.
    :return: Total size in bytes.
    """
    total = 0
    for pat in patterns:
        try:
            total += size_of_path(pat)
        except Exception:
            pass
    return total


def find_named_dirs_under_home(
    target_names: frozenset,
    root: Optional[str] = None,
    max_depth: int = HOME_SCAN_MAX_DEPTH,
    skip_active_prefix: bool = False,
    require_siblings: Tuple[str, ...] = (),
) -> List[str]:
    """
    Find directories with exact target names under root.

    Walks with skip rules so caches, app data and VCS dirs are not scanned.
    Does not follow directory symlinks.

    :param target_names: Directory basenames to collect.
    :param root: Directory to scan (default: ~).
    :param max_depth: Maximum directory depth relative to root.
    :param skip_active_prefix: If True, skip sys.prefix when it matches a hit.
    :param require_siblings: When set, only collect a directory if at least one
        of these filenames exists next to it. This proves a project tool owns
        the directory and can rebuild it, so folders that merely share a common
        name (shipped CMS assets, a "build" documentation folder) are skipped.
    :return: Sorted list of absolute directory paths.
    """
    start = os.path.abspath(os.path.expanduser(root or "~"))
    if not os.path.isdir(start):
        return []

    active_prefix = ""
    if skip_active_prefix:
        try:
            active_prefix = os.path.abspath(sys.prefix)
        except Exception:
            active_prefix = ""

    found: List[str] = []
    start_depth = start.rstrip(os.sep).count(os.sep)

    for dirpath, dirnames, _ in os.walk(start, topdown=True, onerror=lambda e: None):
        depth = dirpath.rstrip(os.sep).count(os.sep) - start_depth
        if depth >= max_depth:
            dirnames[:] = []
            continue

        keep: List[str] = []
        for name in dirnames:
            if name in target_names:
                candidate = os.path.join(dirpath, name)
                if os.path.islink(candidate):
                    continue
                if not os.path.isdir(candidate):
                    continue
                if active_prefix and (
                    candidate == active_prefix
                    or active_prefix.startswith(candidate + os.sep)
                ):
                    continue
                if require_siblings and not any(
                    os.path.exists(os.path.join(dirpath, marker))
                    for marker in require_siblings
                ):
                    # No owning project next to it, so nothing could rebuild it.
                    # Treat it as an ordinary directory and keep descending, a
                    # real project may still live further down.
                    if name not in HOME_SCAN_SKIP_DIR_NAMES:
                        keep.append(name)
                    continue
                found.append(candidate)
                continue
            if name in HOME_SCAN_SKIP_DIR_NAMES:
                continue
            child = os.path.join(dirpath, name)
            if os.path.islink(child):
                continue
            keep.append(name)
        dirnames[:] = keep

    found.sort()
    return found


def find_python_artifact_dirs(
    root: Optional[str] = None,
    max_depth: int = PYTHON_ARTIFACT_MAX_DEPTH,
) -> List[str]:
    """
    Find regeneratable Python directories (__pycache__, .venv) under root.

    Skips the active interpreter prefix when it is a .venv path.
    """
    return find_named_dirs_under_home(
        PYTHON_ARTIFACT_DIR_NAMES,
        root=root,
        max_depth=max_depth,
        skip_active_prefix=True,
    )


def find_local_history_dirs(
    root: Optional[str] = None,
    max_depth: int = HOME_SCAN_MAX_DEPTH,
) -> List[str]:
    """
    Find editor Local History folders (.history) under root.

    These come from extensions such as xyz.local-history and store
    timeline/history snapshots. Deleting them removes that history data.
    """
    return find_named_dirs_under_home(
        LOCAL_HISTORY_DIR_NAMES,
        root=root,
        max_depth=max_depth,
        skip_active_prefix=False,
    )


def find_node_modules_dirs(
    root: Optional[str] = None,
    max_depth: int = HOME_SCAN_MAX_DEPTH,
) -> List[str]:
    """
    Find node_modules directories that belong to a project under root.

    Only directories with a package.json next to them are returned, so the
    dependency tree can be restored with a single "npm install".
    """
    return find_named_dirs_under_home(
        NODE_MODULES_DIR_NAMES,
        root=root,
        max_depth=max_depth,
        require_siblings=NODE_MODULES_MARKERS,
    )


def find_composer_vendor_dirs(
    root: Optional[str] = None,
    max_depth: int = HOME_SCAN_MAX_DEPTH,
) -> List[str]:
    """
    Find Composer vendor directories under root.

    Requires a composer.json next to the folder, which keeps vendor directories
    that ship as part of a CMS (for example Joomla's media/vendor) untouched.
    Restored with "composer install".
    """
    return find_named_dirs_under_home(
        COMPOSER_VENDOR_DIR_NAMES,
        root=root,
        max_depth=max_depth,
        require_siblings=COMPOSER_VENDOR_MARKERS,
    )


def find_build_output_dirs(
    root: Optional[str] = None,
    max_depth: int = HOME_SCAN_MAX_DEPTH,
) -> List[str]:
    """
    Find project build output directories (dist, build, target, .next, ...).

    Requires a build manifest next to the folder so only output of a real
    project is offered. Recreated by running the project's build again.
    """
    return find_named_dirs_under_home(
        BUILD_OUTPUT_DIR_NAMES,
        root=root,
        max_depth=max_depth,
        require_siblings=BUILD_OUTPUT_MARKERS,
    )


# Maps a discovered category to the scan that fills it. Single source of truth
# for both the size analysis and the cleanup plan.
DISCOVERY_FINDERS: Dict[str, Callable[[], List[str]]] = {
    "python_artifacts": find_python_artifact_dirs,
    "local_history": find_local_history_dirs,
    "node_modules": find_node_modules_dirs,
    "composer_vendor": find_composer_vendor_dirs,
    "build_output": find_build_output_dirs,
    # Module and header trees are world readable, so the size needs no helper.
    "old_kernels": lambda: kernel_package_paths(removable_kernel_packages()),
}


def installed_kernel_packages() -> List[Tuple[str, str]]:
    """
    List installed kernel packages as (package name, version tag).

    The version tag is the "6.17.0-40-generic" style suffix used to match a
    package against the running kernel. Packages without such a suffix, like
    the linux-image-generic metapackage, are not returned: removing those would
    stop future kernel updates.

    :return: List of (package, version tag), unsorted.
    """
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\t${Status}\n",
             "linux-image-*", "linux-headers-*", "linux-modules-*"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []

    pattern = re.compile(
        r"^linux-(?:image|headers|modules|modules-extra)-"
        r"(\d+\.\d+\.\d+-\d+-[a-z0-9-]+)$"
    )
    found: List[Tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or "installed" not in parts[1]:
            continue
        match = pattern.match(parts[0].strip())
        if match:
            found.append((parts[0].strip(), match.group(1)))
    return found


def _kernel_sort_key(tag: str) -> Tuple:
    """Build a comparable version key from a "6.17.0-40-generic" tag."""
    numbers = re.findall(r"\d+", tag)
    return tuple(int(n) for n in numbers)


def removable_kernel_packages(
    keep_fallback: bool = True,
    running: Optional[str] = None,
) -> List[str]:
    """
    Determine which kernel packages can be purged.

    "apt autoremove --purge" only removes kernels APT itself marked as
    automatically installed. Kernels that were pulled in manually stay behind
    forever, which is why old kernels can quietly occupy several GB. This
    resolves the actual package list instead.

    The running kernel is always kept. By default the newest remaining kernel
    is kept too, so a bootable fallback survives.

    :param keep_fallback: Keep the newest non-running kernel as a fallback.
    :param running: Running kernel tag, defaults to os.uname().release.
    :return: Sorted list of package names safe to purge.
    """
    packages = installed_kernel_packages()
    if not packages:
        return []

    current = running if running is not None else os.uname().release
    tags = {tag for _pkg, tag in packages}
    keep: set = {current}

    if keep_fallback:
        others = sorted(
            (t for t in tags if t != current),
            key=_kernel_sort_key,
        )
        if others:
            keep.add(others[-1])

    return sorted(pkg for pkg, tag in packages if tag not in keep)


def kernel_package_paths(packages: List[str]) -> List[str]:
    """
    Map kernel packages to the directories that hold their bulk on disk.

    Used to report a real size instead of "size unknown" before removal.

    :param packages: Kernel package names.
    :return: Existing module and header directories for those packages.
    """
    tags = sorted({
        match.group(1)
        for pkg in packages
        if (match := re.search(r"(\d+\.\d+\.\d+-\d+-[a-z0-9-]+)$", pkg))
    })
    paths: List[str] = []
    for tag in tags:
        if os.path.isdir(f"/lib/modules/{tag}"):
            paths.append(f"/lib/modules/{tag}")
        # Header trees appear both as linux-headers-<tag> and, on HWE kernels,
        # as the much larger shared linux-hwe-<series>-headers-<numeric tag>.
        numeric = tag.rsplit("-", 1)[0]
        for candidate in glob.glob(f"/usr/src/linux-headers-{tag}") + glob.glob(
            f"/usr/src/linux-*-headers-{numeric}"
        ):
            if os.path.isdir(candidate) and candidate not in paths:
                paths.append(candidate)
    return paths


def exists_in_path(binary: str) -> bool:
    """
    Return True if a binary exists in PATH.

    :param binary: Command name to search.
    :return: True if found, else False.
    """
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, binary)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return True
    return False


def log_append(widget: tk.Text, text: str) -> None:
    """
    Append text to the log Text widget.
    """
    widget.insert("end", text if text.endswith("\n") else text + "\n")
    widget.see("end")
    widget.update_idletasks()


def _unique_dest(base_dir: str, name: str) -> str:
    """
    Return a unique destination path inside base_dir for given name.
    Adds numeric suffix if file exists already.
    """
    dest = os.path.join(base_dir, name)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = os.path.join(base_dir, f"{stem}-{i}{ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def _write_trashinfo(info_dir: str, original_path: str, trashed_name: str) -> None:
    """
    Write a .trashinfo file according to the Freedesktop Trash spec.
    """
    encoded = quote(os.path.abspath(original_path), safe="/")
    deletion_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    info_path = os.path.join(info_dir, f"{trashed_name}.trashinfo")
    content = "[Trash Info]\n" \
              f"Path={encoded}\n" \
              f"DeletionDate={deletion_date}\n"
    with open(info_path, "w", encoding="utf-8") as fh:
        fh.write(content)


def trash_paths(patterns: List[str]) -> Tuple[int, str]:
    """
    Move user space files and directories to the user's Trash with correct metadata.
    Prefer gio if available, else compliant fallback that writes .trashinfo files.

    :param patterns: Patterns to move to trash.
    :return: (num_trashed, log_text)
    """
    logs: List[str] = []
    moved = 0
    use_gio = exists_in_path("gio")
    trash_dir = os.path.expanduser("~/.local/share/Trash/files")
    info_dir = os.path.expanduser("~/.local/share/Trash/info")

    os.makedirs(trash_dir, exist_ok=True)
    os.makedirs(info_dir, exist_ok=True)

    for pattern in patterns:
        for p in glob.glob(os.path.expanduser(pattern), recursive=False):
            if os.path.abspath(p).startswith(os.path.abspath(os.path.expanduser("~/.local/share/Trash/"))):
                continue
            if is_protected_path(p):
                logs.append(f"Skipped protected path: {p}")
                continue
            try:
                if use_gio:
                    proc = subprocess.run(["gio", "trash", p], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    if proc.returncode == 0:
                        logs.append(f"Trashed: {p}")
                        moved += 1
                    else:
                        logs.append(f"Failed to trash with gio: {p}, {proc.stdout.strip()}")
                else:
                    name = os.path.basename(p.rstrip(os.sep))
                    dest = _unique_dest(trash_dir, name)
                    shutil.move(p, dest)
                    _write_trashinfo(info_dir, p, os.path.basename(dest))
                    logs.append(f"Moved to Trash: {p} -> {dest}")
                    moved += 1
            except Exception as e:
                logs.append(f"Failed to move to Trash: {p}: {e}")
    return moved, "\n".join(logs)


def rm_paths(patterns: List[str]) -> Tuple[int, str]:
    """
    Remove user space files and directories using Python, skipping non existing paths.

    :param patterns: List of file or directory patterns.
    :return: (num_removed, log_text)
    """
    removed = 0
    logs = []
    for pattern in patterns:
        for p in glob.glob(os.path.expanduser(pattern), recursive=False):
            if is_protected_path(p):
                logs.append(f"Skipped protected path: {p}")
                continue
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                    logs.append(f"Removed directory: {p}")
                    removed += 1
                elif os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
                    logs.append(f"Removed file: {p}")
                    removed += 1
            except Exception as e:
                logs.append(f"Failed to remove {p}: {e}")
    return removed, "\n".join(logs)

# ----------------------------- Cleanup plan -----------------------------

def build_cleanup_plan(
    selection: Dict[str, bool],
    patterns: Dict[str, List[str]],
    journal_retention: str = "3d",
    rediscover: bool = True,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], List[str]]:
    """
    Build the cleanup plan from a plain selection mapping.

    Pure function without Tk access so it can run in a background thread.

    :param selection: Mapping of selection name (without "var_") to bool.
    :param patterns: Known patterns per category.
    :param journal_retention: Retention argument for journalctl --vacuum-time.
    :param rediscover: Re-scan home for regeneratable directories when True.
    :return: (plan, discovered_patterns, notes)
    """
    plan: Dict[str, List[str]] = {
        "user_py_delete": [],
        "user_cmds": [],
        "root_rm_patterns": [],
        "root_cmds": [],
    }
    discovered: Dict[str, List[str]] = {}
    notes: List[str] = []

    # User deletions
    if selection.get("user_cache"):
        plan["user_py_delete"].append("~/.cache/*")
    if selection.get("thumbnails"):
        plan["user_py_delete"].append("~/.thumbnails/*")
    if selection.get("trash"):
        plan["user_py_delete"].append("~/.local/share/Trash/*")
    if selection.get("firefox"):
        plan["user_py_delete"] += [
            "~/.mozilla/firefox/*.default*/cache2/*",
            "~/.cache/mozilla/firefox/*.default*/cache2/*",
        ]
    if selection.get("chrome"):
        plan["user_py_delete"] += [
            "~/.config/google-chrome/Default/Cache/*",
            "~/.cache/google-chrome/Default/Cache/*",
            "~/.config/chromium/Default/Cache/*",
            "~/.cache/chromium/Default/Cache/*",
        ]
    if selection.get("flatpak_app_cache"):
        plan["user_py_delete"].append("~/.var/app/*/cache/*")
    if selection.get("config_app_caches"):
        plan["user_py_delete"] += list(patterns.get("config_app_caches", []))
    if selection.get("dev_tool_caches"):
        plan["user_py_delete"] += list(patterns.get("dev_tool_caches", []))
    if selection.get("user_lang_tool_caches"):
        plan["user_py_delete"] += list(patterns.get("user_lang_tool_caches", []))
    if selection.get("editor_caches"):
        plan["user_py_delete"] += list(patterns.get("editor_caches", []))
    if selection.get("editor_state"):
        plan["user_py_delete"] += list(patterns.get("editor_state", []))
    if selection.get("ai_assistant_logs"):
        plan["user_py_delete"] += list(patterns.get("ai_assistant_logs", []))
    if selection.get("python_artifacts"):
        artifacts = (
            find_python_artifact_dirs()
            if rediscover
            else list(patterns.get("python_artifacts", []))
        )
        discovered["python_artifacts"] = artifacts
        plan["user_py_delete"] += artifacts
    if selection.get("local_history"):
        history_dirs = (
            find_local_history_dirs()
            if rediscover
            else list(patterns.get("local_history", []))
        )
        discovered["local_history"] = history_dirs
        plan["user_py_delete"] += history_dirs
    if selection.get("node_modules"):
        node_dirs = (
            find_node_modules_dirs()
            if rediscover
            else list(patterns.get("node_modules", []))
        )
        discovered["node_modules"] = node_dirs
        plan["user_py_delete"] += node_dirs
    if selection.get("composer_vendor"):
        vendor_dirs = (
            find_composer_vendor_dirs()
            if rediscover
            else list(patterns.get("composer_vendor", []))
        )
        discovered["composer_vendor"] = vendor_dirs
        plan["user_py_delete"] += vendor_dirs
    if selection.get("build_output"):
        output_dirs = (
            find_build_output_dirs()
            if rediscover
            else list(patterns.get("build_output", []))
        )
        discovered["build_output"] = output_dirs
        plan["user_py_delete"] += output_dirs

    # User commands
    if selection.get("go_mod_cache"):
        # Files in the module cache are read-only, so the toolchain has to
        # clear it. Without Go installed there is nothing to clear.
        if exists_in_path("go"):
            plan["user_cmds"].append("go clean -modcache")
        else:
            notes.append("go not found, skipping module cache cleanup.")
    if selection.get("flatpak_user"):
        if exists_in_path("flatpak"):
            plan["user_cmds"].append("flatpak uninstall --unused -y")
        else:
            notes.append("flatpak not found, skipping user flatpak uninstall.")
    if selection.get("flatpak_repair_user"):
        if exists_in_path("flatpak"):
            plan["user_cmds"].append("flatpak repair --user -y")
        else:
            notes.append("flatpak not found, skipping user flatpak repair.")

    # Root deletions as patterns handled by helper
    if selection.get("tmp"):
        plan["root_rm_patterns"] += ["/tmp/*", "/var/tmp/*"]
    if selection.get("flatpak_syscache"):
        plan["root_rm_patterns"] += ["/var/tmp/flatpak-cache/*"]
    if selection.get("apt_cache"):
        plan["root_rm_patterns"] += [
            "/var/cache/apt/archives/*",
            "/var/cache/apt/archives/partial/*",
        ]
    if selection.get("system_misc_caches"):
        plan["root_rm_patterns"] += list(patterns.get("system_misc_caches", []))
    if selection.get("system_extra_caches"):
        plan["root_rm_patterns"] += list(patterns.get("system_extra_caches", []))

    # Root commands
    if selection.get("flatpak_repair_system"):
        plan["root_cmds"].append("flatpak repair --system -y")
    if selection.get("apt"):
        plan["root_cmds"] += ["apt clean", "apt autoclean", "apt autoremove -y"]
    if selection.get("journal"):
        retention = (journal_retention or "").strip() or "3d"
        plan["root_cmds"].append(f"journalctl --vacuum-time={shlex.quote(retention)}")
    if selection.get("old_kernels"):
        # "apt autoremove --purge" only touches kernels APT marked as
        # automatically installed and silently leaves manually installed ones
        # behind, so the packages are resolved explicitly here.
        old_kernels = removable_kernel_packages()
        if old_kernels:
            plan["root_cmds"].append(
                "apt-get purge -y " + " ".join(shlex.quote(p) for p in old_kernels)
            )
            plan["root_cmds"].append("apt-get autoremove --purge -y")
            notes.append(
                f"{len(old_kernels)} old kernel packages will be purged, "
                "the running kernel and one fallback are kept."
            )
        else:
            notes.append("No removable old kernels found.")

    return plan, discovered, notes


def selection_from_vars(source: Any) -> Dict[str, bool]:
    """
    Read the selection state from an object that holds var_* Tk variables.

    :param source: Object with var_<name> attributes, usually the application.
    :return: Mapping of selection name to bool.
    """
    snapshot: Dict[str, bool] = {}
    for name in SELECTION_NAMES:
        var = getattr(source, f"var_{name}", None)
        snapshot[name] = bool(var.get()) if var is not None else False
    return snapshot


def plan_is_empty(plan: Dict[str, List[str]]) -> bool:
    """
    Return True when a plan contains no action at all.

    :param plan: Plan produced by build_cleanup_plan().
    :return: True when nothing would be executed.
    """
    return not any(plan.get(key) for key in
                   ("user_py_delete", "user_cmds", "root_rm_patterns", "root_cmds"))


def split_user_targets(
    user_patterns: List[str],
    delete_mode: str,
) -> Tuple[List[str], List[str]]:
    """
    Split user paths into Trash and immediate deletion lists.

    Trash contents are always deleted immediately, moving them inside the Trash
    would be pointless.

    :param user_patterns: Patterns selected for user space deletion.
    :param delete_mode: "trash" or "delete".
    :return: (to_trash, to_delete)
    """
    to_trash: List[str] = []
    to_delete: List[str] = []
    trash_root = os.path.abspath(os.path.expanduser("~/.local/share/Trash/"))
    for pattern in user_patterns:
        expanded = os.path.abspath(os.path.expanduser(pattern))
        if expanded.startswith(trash_root):
            to_delete.append(pattern)
        elif delete_mode == "trash":
            to_trash.append(pattern)
        else:
            to_delete.append(pattern)
    return to_trash, to_delete

# ----------------------------- Background job reporting -----------------------------

class JobReporter:
    """
    Thread safe progress reporter used by background workers.

    Workers never touch Tk. They push update messages into a queue that the
    main loop drains and forwards to the progress dialog and the activity log.
    """

    def __init__(self, updates: "queue.Queue") -> None:
        """
        Initialize the reporter with the queue to publish updates on.

        :param updates: Queue consumed by the Tk main loop.
        """
        self._updates = updates
        self._index = -1

    @property
    def index(self) -> int:
        """Return the index of the currently running step."""
        return self._index

    def add_steps(self, labels: Sequence[str]) -> None:
        """
        Announce additional steps that became known while running.

        :param labels: Step labels to append to the checklist.
        """
        self._updates.put(("add_steps", list(labels), None))

    def begin(self, label: str = "", note: str = "") -> int:
        """
        Start the next step.

        :param label: Step label, replaces the pre-announced one when set.
        :param note: Optional hint shown next to the current step name.
        :return: Index of the started step.
        """
        self._index += 1
        self._updates.put(("begin", self._index, (label, note)))
        return self._index

    def end(self, result: str = "", failed: bool = False) -> None:
        """
        Finish the current step.

        :param result: Short result text, for example a measured size.
        :param failed: True when the step failed.
        """
        self._updates.put(("end", self._index, (result, failed)))

    def subtitle(self, text: str) -> None:
        """
        Update the explanation line of the progress dialog.

        :param text: New subtitle.
        """
        self._updates.put(("subtitle", text, None))

    def live(self, payload: Dict[str, Any]) -> None:
        """
        Publish a partial result while the job is still running.

        Lets the window show numbers as soon as they exist instead of waiting
        for the whole job to finish.

        :param payload: Partial result, for example {"sizes": {key: bytes}}.
        """
        self._updates.put(("live", dict(payload), None))

    def log(self, text: str) -> None:
        """
        Append a line to the activity log of the main window.

        :param text: Log text.
        """
        self._updates.put(("log", str(text), None))

# ----------------------------- Single privileged helper via pkexec -----------------------------

class RootHelper:
    """
    Manage a single pkexec launched helper process that executes privileged actions.
    The helper implements a small RPC over JSON lines on stdin and stdout.
    """

    def __init__(self) -> None:
        """
        Initialize without starting the helper.
        """
        self.proc: Optional[subprocess.Popen[str]] = None
        # The RPC channel is a single pipe pair, so only one request at a time.
        self._lock = threading.Lock()

    def start(self, log: Optional[tk.Text] = None) -> bool:
        """
        Start the helper via pkexec once at app launch, return True if started.

        :param log: Optional Tk text widget to log status.
        :return: True if helper started and responded to ping, else False.
        """
        helper_cmd = [
            "pkexec",
            sys.executable,
            "-u",
            os.path.abspath(__file__),
            "--helper",
        ]
        try:
            self.proc = subprocess.Popen(
                helper_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            ok = self._rpc({"action": "ping"}) is True
            if ok and log:
                log_append(log, "[OK] Privileged helper ready, authentication done.")
            return bool(ok)
        except Exception as e:
            if log:
                log_append(log, f"[ERR] Failed to start helper: {e}")
            return False

    def _rpc(self, payload: Dict[str, Any]) -> Any:
        """
        Send a single JSON request and return the 'data' or True or False.

        Serialized with a lock, background workers and the GUI thread share one pipe.

        :param payload: Request dictionary with 'action' and optional 'args'.
        :return: Response data on success.
        :raises RuntimeError: On transport or helper error.
        """
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("helper not running")
        with self._lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("no response from helper")
        resp = json.loads(line)
        if resp.get("status") == "ok":
            return resp.get("data", True)
        raise RuntimeError(resp.get("error", "helper error"))

    def rm_rf_patterns(self, patterns: List[str]) -> Tuple[int, str]:
        """
        Recursively remove a list of root patterns, return rc and combined output.

        :param patterns: Patterns to remove as root.
        :return: (rc, combined_output)
        """
        try:
            return self._rpc({"action": "rm_rf_patterns", "args": {"patterns": patterns}})
        except Exception as e:
            return (1, str(e))

    def run_root_cmds(self, cmds: List[str]) -> List[Tuple[str, int, str]]:
        """
        Run a list of shell commands as root.

        :param cmds: List of shell commands to run.
        :return: List of tuples (cmd, rc, output).
        """
        try:
            return self._rpc({"action": "run_root_cmds", "args": {"cmds": cmds}})
        except Exception as e:
            return [(f"ERROR:{e}", 1, str(e))]

    def get_size_of_patterns(self, patterns: List[str]) -> int:
        """
        Get total size (bytes) of root‑owned patterns using the helper.

        :param patterns: List of glob patterns (root accessible).
        :return: Total size in bytes, or 0 on error.
        """
        try:
            return self._rpc({"action": "get_size", "args": {"patterns": patterns}})
        except Exception:
            return 0


HELPER = RootHelper()

# ----------------------------- GUI application -----------------------------

class MintCleanerApp(tk.Tk):
    """
    Tkinter GUI for selective cleanup with dynamic size analysis and a single pkexec helper.
    Modern UI with improved layout and styling.
    """

    def __init__(self, start_helper: bool = False):
        """
        Initialize the MintCleaner application, build UI.
        The helper is expected to be started BEFORE the window is created.
        """
        # className becomes WM_CLASS, panels use it to map the window to the
        # .desktop launcher (StartupWMClass) and show the correct taskbar icon.
        super().__init__(className=WM_CLASS_NAME)
        self.title("Mint Cleaner, Selective Temp and Cache Cleanup")
        self.geometry("1180x920")
        self.minsize(1000, 700)
        apply_window_icon(self)

        # Configure modern style
        self._setup_styles()

        self.username = getpass.getuser()

        # Deletion mode for user scoped actions. Deleting right away is the
        # default, moving to Trash only relocates the data and frees no space.
        self.delete_mode_var = tk.StringVar(master=self, value="delete")  # "trash" or "delete"

        # Checkboxes state
        self.var_tmp = tk.BooleanVar(master=self, value=False)                 # /tmp and /var/tmp
        self.var_user_cache = tk.BooleanVar(master=self, value=True)           # ~/.cache/*
        self.var_thumbnails = tk.BooleanVar(master=self, value=True)           # ~/.thumbnails/*
        self.var_trash = tk.BooleanVar(master=self, value=True)                # ~/.local/share/Trash/*
        self.var_firefox = tk.BooleanVar(master=self, value=False)             # Firefox caches
        self.var_chrome = tk.BooleanVar(master=self, value=False)              # Chrome or Chromium caches
        self.var_flatpak_user = tk.BooleanVar(master=self, value=False)        # flatpak uninstall --unused (user)
        self.var_flatpak_repair_user = tk.BooleanVar(master=self, value=False) # flatpak repair --user
        self.var_flatpak_syscache = tk.BooleanVar(master=self, value=False)    # /var/tmp/flatpak-cache/*
        self.var_flatpak_repair_system = tk.BooleanVar(master=self, value=False) # flatpak repair --system
        self.var_apt = tk.BooleanVar(master=self, value=False)                 # apt clean/autoclean/autoremove
        self.var_journal = tk.BooleanVar(master=self, value=False)             # journalctl vacuum
        # New options
        self.var_flatpak_app_cache = tk.BooleanVar(master=self, value=False)   # ~/.var/app/*/cache/*
        self.var_config_app_caches = tk.BooleanVar(master=self, value=False)   # Conservative ~/.config cache-only paths
        self.var_dev_tool_caches = tk.BooleanVar(master=self, value=False)     # npm/yarn/pnpm/cargo/gradle caches
        self.var_user_lang_tool_caches = tk.BooleanVar(master=self, value=False)  # pip/poetry/uv/go/fontconfig/mesa caches
        self.var_python_artifacts = tk.BooleanVar(master=self, value=False)    # __pycache__ and .venv under ~
        self.var_local_history = tk.BooleanVar(master=self, value=False)       # .history Local History snapshots
        self.var_apt_cache = tk.BooleanVar(master=self, value=False)           # /var/cache/apt/archives/*
        self.var_system_misc_caches = tk.BooleanVar(master=self, value=False)  # Common /var cache and crash directories
        self.var_system_extra_caches = tk.BooleanVar(master=self, value=False) # PackageKit/fwupd/ldconfig/coredump caches
        self.var_old_kernels = tk.BooleanVar(master=self, value=False)         # purge non-running kernels
        self.var_editor_caches = tk.BooleanVar(master=self, value=False)       # VSIX archives, editor logs, snapshots
        self.var_editor_state = tk.BooleanVar(master=self, value=False)        # workspaceStorage/History/state.vscdb
        self.var_ai_assistant_logs = tk.BooleanVar(master=self, value=False)   # AI assistant transcripts and logs
        self.var_go_mod_cache = tk.BooleanVar(master=self, value=False)        # go clean -modcache
        self.var_node_modules = tk.BooleanVar(master=self, value=False)        # node_modules next to a package.json
        self.var_composer_vendor = tk.BooleanVar(master=self, value=False)     # vendor next to a composer.json
        self.var_build_output = tk.BooleanVar(master=self, value=False)        # dist/build/target of real projects

        self.journal_retention = tk.StringVar(master=self, value="3d")

        # Patterns for size analysis (user measurable only)
        self.patterns: Dict[str, List[str]] = {
            "tmp": ["/tmp/*", "/var/tmp/*"],
            "user_cache": ["~/.cache/*"],
            "thumbnails": ["~/.thumbnails/*"],
            "trash": ["~/.local/share/Trash/*"],
            "firefox": [
                "~/.mozilla/firefox/*.default*/cache2/*",
                "~/.cache/mozilla/firefox/*.default*/cache2/*",
            ],
            "chrome": [
                "~/.config/google-chrome/Default/Cache/*",
                "~/.cache/google-chrome/Default/Cache/*",
                "~/.config/chromium/Default/Cache/*",
                "~/.cache/chromium/Default/Cache/*",
            ],
            "flatpak_syscache": ["/var/tmp/flatpak-cache/*"],
            "apt": ["/var/cache/apt/archives/*", "/var/cache/apt/archives/partial/*"],
            "journal": ["/var/log/journal/*", "/run/log/journal/*"],
            "flatpak_app_cache": ["~/.var/app/*/cache/*"],
            "config_app_caches": CONFIG_CACHE_PATTERNS,
            "dev_tool_caches": DEV_TOOL_CACHE_PATTERNS,
            "user_lang_tool_caches": USER_LANG_TOOL_CACHE_PATTERNS,
            "python_artifacts": [],  # filled by refresh_sizes via find_python_artifact_dirs()
            "local_history": [],  # filled by refresh_sizes via find_local_history_dirs()
            "system_misc_caches": SYSTEM_MISC_CACHE_PATTERNS,
            "system_extra_caches": SYSTEM_EXTRA_CACHE_PATTERNS,
            "apt_cache": ["/var/cache/apt/archives/*", "/var/cache/apt/archives/partial/*"],
            "editor_caches": EDITOR_CACHE_PATTERNS,
            "editor_state": EDITOR_STATE_PATTERNS,
            "ai_assistant_logs": AI_ASSISTANT_LOG_PATTERNS,
            "go_mod_cache": [GO_MODULE_CACHE_PATH],
            "node_modules": [],       # filled by refresh_sizes via find_node_modules_dirs()
            "composer_vendor": [],    # filled by refresh_sizes via find_composer_vendor_dirs()
            "build_output": [],       # filled by refresh_sizes via find_build_output_dirs()
            "old_kernels": [],        # filled by refresh_sizes via kernel_package_paths()
        }

        # Bookkeeping
        self.sizes_before: Dict[str, int] = {}
        self.widgets: Dict[str, tk.Checkbutton] = {}
        self.base_text: Dict[str, str] = {}

        # Background job state
        self._job_active: bool = False
        self._interactive_widgets: List[Tuple[tk.Widget, str]] = []
        self.disk_now: Dict[str, Tuple[int, int]] = {}
        self.last_cleanup: Optional[Dict[str, Any]] = None

        self._build_ui()

        # Log that helper is ready and authenticated (already done by main())
        log_append(self.log, "[OK] Privileged helper ready, authentication done at startup.")
        # Start the analysis once the main loop runs, so the progress dialog can
        # grab the window and stay responsive while data is collected.
        self.after(100, self.refresh_sizes)

    def _setup_styles(self) -> None:
        """
        Configure ttk styles for a cleaner, more user-friendly layout.
        """
        style = ttk.Style()
        available_themes = style.theme_names()
        if "clam" in available_themes:
            style.theme_use("clam")
        elif "vista" in available_themes:
            style.theme_use("vista")
        elif "alt" in available_themes:
            style.theme_use("alt")

        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("CardTitle.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Hint.TLabel", font=("Segoe UI", 9))
        style.configure("TLabelframe", font=("Segoe UI", 10, "bold"))
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 9), padding=(10, 6))
        style.configure("TCheckbutton", font=("Segoe UI", 9))
        # Slightly larger checkboxes with a clearer indicator.
        style.configure(
            "Big.TCheckbutton",
            font=("Segoe UI", 10),
            padding=(2, 4),
            indicatorsize=18,
            indicatormargin=(2, 2, 6, 2),
        )
        style.configure("TEntry", font=("Segoe UI", 9))
        # Footnotes below the disk space table.
        style.configure("ResultNote.TLabel", font=("Segoe UI", 9), foreground="#5b6673")
        style.configure("TNotebook.Tab", padding=(10, 6), font=("Segoe UI", 9))
        style.map(
            "TNotebook.Tab",
            padding=[("selected", (14, 10)), ("!selected", (10, 6))],
            font=[("selected", ("Segoe UI", 10, "bold")), ("!selected", ("Segoe UI", 9))],
        )
        # The main action is the only colored button, so it stands out clearly
        # against the secondary actions next to it.
        style.configure(
            "Primary.TButton",
            font=("Segoe UI", 11, "bold"),
            padding=(14, 11),
            foreground=PRIMARY_BUTTON_FG,
            background=PRIMARY_BUTTON_BG,
            bordercolor=PRIMARY_BUTTON_ACTIVE,
            lightcolor=PRIMARY_BUTTON_BG,
            darkcolor=PRIMARY_BUTTON_BG,
            focuscolor=PRIMARY_BUTTON_FG,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", PRIMARY_BUTTON_DISABLED),
                ("pressed", PRIMARY_BUTTON_PRESSED),
                ("active", PRIMARY_BUTTON_ACTIVE),
            ],
            foreground=[("disabled", "#f0f2f4")],
            lightcolor=[
                ("disabled", PRIMARY_BUTTON_DISABLED),
                ("pressed", PRIMARY_BUTTON_PRESSED),
                ("active", PRIMARY_BUTTON_ACTIVE),
            ],
            darkcolor=[
                ("disabled", PRIMARY_BUTTON_DISABLED),
                ("pressed", PRIMARY_BUTTON_PRESSED),
                ("active", PRIMARY_BUTTON_ACTIVE),
            ],
        )
        # Secondary actions stay quiet, they only support the main action.
        style.configure("Secondary.TButton", font=("Segoe UI", 9), padding=(10, 6))

    def _build_menubar(self) -> None:
        """
        Build the menu bar with File, Integration and Help.

        The actions that are not the main action live here, together with the
        desktop integration switches. Entries that must not run while a
        background job is active are collected in self._job_menu_entries.
        """
        menubar = tk.Menu(self, tearoff=False)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Clean Selected", command=self.on_clean_clicked)
        file_menu.add_command(label="Preview Commands", command=self.on_preview)
        file_menu.add_command(label="Refresh Sizes", command=self.refresh_sizes)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.on_quit)
        menubar.add_cascade(label="File", menu=file_menu)
        # Everything above the separator touches the analysis or the cleanup.
        self._job_menu_entries = [(file_menu, index) for index in range(3)]

        integration_menu = tk.Menu(menubar, tearoff=False)
        self.nemo_action_var = tk.BooleanVar(master=self, value=nemo_action_installed())
        integration_menu.add_checkbutton(
            label="Nemo context menu entry",
            variable=self.nemo_action_var,
            command=self.on_toggle_nemo_action,
        )
        self.desktop_shortcut_var = tk.BooleanVar(
            master=self, value=desktop_shortcut_installed()
        )
        integration_menu.add_checkbutton(
            label="Desktop shortcut",
            variable=self.desktop_shortcut_var,
            command=self.on_toggle_desktop_shortcut,
        )
        menubar.add_cascade(label="Integration", menu=integration_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self.on_about)
        help_menu.add_command(label="Developer", command=self.on_developer)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.configure(menu=menubar)
        self.menubar = menubar

    def _build_ui(self) -> None:
        """
        Build a redesigned, user-friendly interface with clear grouping and flow.
        """
        self._build_menubar()

        main_container = ttk.Frame(self, padding=14)
        main_container.pack(fill=tk.BOTH, expand=True)

        header_frame = ttk.Frame(main_container, padding=(2, 2, 2, 10))
        header_frame.pack(fill=tk.X)

        # Sizes live at the checkboxes, free space in the table at the bottom,
        # so the header only carries the application name.
        ttk.Label(header_frame, text="Mint Cleaner", style="Title.TLabel").pack(anchor="w")

        action_bar = ttk.Frame(main_container, padding=(0, 0, 0, 8))
        action_bar.pack(fill=tk.X)

        mode_frame = ttk.Frame(action_bar)
        mode_frame.pack(side=tk.RIGHT)
        ttk.Label(mode_frame, text="User deletion mode:").pack(side=tk.LEFT, padx=(0, 6))
        mode_combo = ttk.Combobox(
            mode_frame,
            state="readonly",
            values=[DELETE_MODE_LABELS["delete"], DELETE_MODE_LABELS["trash"]],
            width=18,
        )
        mode_combo.pack(side=tk.LEFT)
        mode_combo.set(DELETE_MODE_LABELS[self.delete_mode_var.get()])
        self.mode_combo = mode_combo

        def on_mode_change(event=None):
            """Apply the deletion mode chosen in the combobox."""
            chosen = mode_combo.get()
            for key, label in DELETE_MODE_LABELS.items():
                if label == chosen:
                    self.delete_mode_var.set(key)
                    break
            self.on_delete_mode_changed()

        mode_combo.bind("<<ComboboxSelected>>", on_mode_change)
        self._interactive_widgets.append((mode_combo, "readonly"))

        # Packed before the content area so it stays docked at the bottom edge.
        self._build_result_card(main_container)

        content = ttk.Panedwindow(main_container, orient=tk.HORIZONTAL)
        content.pack(fill=tk.BOTH, expand=True)
        self.content_pane = content

        left_panel = ttk.Frame(content, padding=(0, 4, 8, 0))
        right_panel = ttk.Frame(content, padding=(8, 4, 0, 0))
        content.add(left_panel, weight=3)
        content.add(right_panel, weight=2)

        task_card = ttk.LabelFrame(left_panel, text="Cleanup Categories", padding=10)
        task_card.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            task_card,
            text="Choose what should be cleaned. Sizes update automatically.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 8))

        # The cleanup result is reported by the disk space table at the bottom.
        notebook = ttk.Notebook(task_card)
        notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook = notebook

        sys_tab = self._create_scrollable_tab(notebook, "System (root)")
        user_tab = self._create_scrollable_tab(notebook, f"User ({self.username})")

        checkbox_opts = {
            "font": ("Segoe UI", 10),
            "indicatoron": True,
            "anchor": "w",
            "padx": 4,
            "pady": 2,
            "command": self.on_category_toggle,
        }

        row = 0
        self.widgets["tmp"] = tk.Checkbutton(sys_tab, text="/tmp and /var/tmp", variable=self.var_tmp, **checkbox_opts)
        self.widgets["tmp"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["tmp"] = "/tmp and /var/tmp"
        row += 1

        self.widgets["apt"] = tk.Checkbutton(sys_tab, text="APT cleanup (clean, autoclean, autoremove)", variable=self.var_apt, **checkbox_opts)
        self.widgets["apt"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["apt"] = "APT cleanup (clean, autoclean, autoremove)"
        row += 1

        self.widgets["apt_cache"] = tk.Checkbutton(sys_tab, text="APT package cache (/var/cache/apt/archives)", variable=self.var_apt_cache, **checkbox_opts)
        self.widgets["apt_cache"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["apt_cache"] = "APT package cache (/var/cache/apt/archives)"
        row += 1

        self.widgets["system_misc_caches"] = tk.Checkbutton(
            sys_tab,
            text="General system caches (/var/cache, /var/lib/apt/lists, /var/crash)",
            variable=self.var_system_misc_caches,
            **checkbox_opts,
        )
        self.widgets["system_misc_caches"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["system_misc_caches"] = "General system caches (/var/cache, /var/lib/apt/lists, /var/crash)"
        row += 1

        self.widgets["system_extra_caches"] = tk.Checkbutton(
            sys_tab,
            text="Additional system caches (PackageKit, fwupd, ldconfig, coredumps)",
            variable=self.var_system_extra_caches,
            **checkbox_opts,
        )
        self.widgets["system_extra_caches"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["system_extra_caches"] = "Additional system caches (PackageKit, fwupd, ldconfig, coredumps)"
        row += 1

        self.widgets["old_kernels"] = tk.Checkbutton(sys_tab, text="Remove old kernels (keeps the running one and one fallback)", variable=self.var_old_kernels, **checkbox_opts)
        self.widgets["old_kernels"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["old_kernels"] = "Remove old kernels (keeps the running one and one fallback)"
        row += 1

        self.widgets["flatpak_syscache"] = tk.Checkbutton(sys_tab, text="System Flatpak cache", variable=self.var_flatpak_syscache, **checkbox_opts)
        self.widgets["flatpak_syscache"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["flatpak_syscache"] = "System Flatpak cache"
        row += 1

        self.widgets["flatpak_repair_system"] = tk.Checkbutton(sys_tab, text="Flatpak repair system [size unknown]", variable=self.var_flatpak_repair_system, **checkbox_opts)
        self.widgets["flatpak_repair_system"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["flatpak_repair_system"] = "Flatpak repair system [size unknown]"
        row += 1

        journal_frame = ttk.Frame(sys_tab)
        journal_frame.grid(row=row, column=0, sticky="w", pady=4)
        self.widgets["journal"] = tk.Checkbutton(journal_frame, text="Systemd journal vacuum", variable=self.var_journal, **checkbox_opts)
        self.widgets["journal"].pack(side=tk.LEFT)
        self.base_text["journal"] = "Systemd journal vacuum"
        ttk.Label(journal_frame, text="Keep:").pack(side=tk.LEFT, padx=(8, 3))
        ttk.Entry(journal_frame, width=8, textvariable=self.journal_retention).pack(side=tk.LEFT)
        ttk.Label(journal_frame, text="(3d, 7d, 100M)").pack(side=tk.LEFT, padx=(6, 0))

        row = 0
        self.widgets["user_cache"] = tk.Checkbutton(
            user_tab,
            text="~/.cache/* (keeps dconf/session dirs — protects desktop icons)",
            variable=self.var_user_cache,
            **checkbox_opts,
        )
        self.widgets["user_cache"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["user_cache"] = "~/.cache/* (keeps dconf/session dirs — protects desktop icons)"
        row += 1

        self.widgets["thumbnails"] = tk.Checkbutton(user_tab, text="~/.thumbnails/*", variable=self.var_thumbnails, **checkbox_opts)
        self.widgets["thumbnails"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["thumbnails"] = "~/.thumbnails/*"
        row += 1

        self.widgets["trash"] = tk.Checkbutton(user_tab, text="~/.local/share/Trash/*", variable=self.var_trash, **checkbox_opts)
        self.widgets["trash"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["trash"] = "~/.local/share/Trash/*"
        row += 1

        self.widgets["flatpak_app_cache"] = tk.Checkbutton(user_tab, text="Flatpak application cache (~/.var/app/*/cache/*)", variable=self.var_flatpak_app_cache, **checkbox_opts)
        self.widgets["flatpak_app_cache"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["flatpak_app_cache"] = "Flatpak application cache (~/.var/app/*/cache/*)"
        row += 1

        self.widgets["firefox"] = tk.Checkbutton(user_tab, text="Firefox cache (all profiles)", variable=self.var_firefox, **checkbox_opts)
        self.widgets["firefox"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["firefox"] = "Firefox cache (all profiles)"
        row += 1

        self.widgets["chrome"] = tk.Checkbutton(user_tab, text="Chrome/Chromium cache (default profile)", variable=self.var_chrome, **checkbox_opts)
        self.widgets["chrome"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["chrome"] = "Chrome/Chromium cache (default profile)"
        row += 1

        self.widgets["config_app_caches"] = tk.Checkbutton(
            user_tab,
            text="Additional app caches in ~/.config (Code, Cursor, Chrome, Brave)",
            variable=self.var_config_app_caches,
            **checkbox_opts,
        )
        self.widgets["config_app_caches"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["config_app_caches"] = "Additional app caches in ~/.config (Code, Cursor, Chrome, Brave)"
        row += 1

        self.widgets["dev_tool_caches"] = tk.Checkbutton(
            user_tab,
            text="Developer tool caches (~/.npm, ~/.yarn, ~/.pnpm-store, ~/.cargo, ~/.gradle)",
            variable=self.var_dev_tool_caches,
            **checkbox_opts,
        )
        self.widgets["dev_tool_caches"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["dev_tool_caches"] = "Developer tool caches (~/.npm, ~/.yarn, ~/.pnpm-store, ~/.cargo, ~/.gradle)"
        row += 1

        self.widgets["user_lang_tool_caches"] = tk.Checkbutton(
            user_tab,
            text="Language and tool caches (pip, Poetry, uv, go-build, node-gyp, fontconfig, mesa)",
            variable=self.var_user_lang_tool_caches,
            **checkbox_opts,
        )
        self.widgets["user_lang_tool_caches"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["user_lang_tool_caches"] = "Language and tool caches (pip, Poetry, uv, go-build, node-gyp, fontconfig, mesa)"
        row += 1

        self.widgets["python_artifacts"] = tk.Checkbutton(
            user_tab,
            text="Python leftovers (__pycache__, .venv under home)",
            variable=self.var_python_artifacts,
            **checkbox_opts,
        )
        self.widgets["python_artifacts"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["python_artifacts"] = "Python leftovers (__pycache__, .venv under home)"
        row += 1

        self.widgets["local_history"] = tk.Checkbutton(
            user_tab,
            text="Editor Local History (.history) — deletes timeline/history data",
            variable=self.var_local_history,
            **checkbox_opts,
        )
        self.widgets["local_history"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["local_history"] = "Editor Local History (.history) — deletes timeline/history data"
        row += 1

        # Editor and assistant data, then rebuildable project directories.
        for key, variable, label in (
            (
                "editor_caches",
                self.var_editor_caches,
                "Editor caches (VS Code/Cursor extension archives, logs, snapshots)",
            ),
            (
                "editor_state",
                self.var_editor_state,
                "Editor workspace state and history — deletes undo/timeline data",
            ),
            (
                "ai_assistant_logs",
                self.var_ai_assistant_logs,
                "AI assistant transcripts and logs — keeps memory folders",
            ),
            (
                "go_mod_cache",
                self.var_go_mod_cache,
                "Go module cache (go clean -modcache)",
            ),
            (
                "node_modules",
                self.var_node_modules,
                "node_modules folders — restore with npm install",
            ),
            (
                "composer_vendor",
                self.var_composer_vendor,
                "Composer vendor folders — restore with composer install",
            ),
            (
                "build_output",
                self.var_build_output,
                "Project build output (dist, build, target) — restore by rebuilding",
            ),
        ):
            self.widgets[key] = tk.Checkbutton(
                user_tab, text=label, variable=variable, **checkbox_opts
            )
            self.widgets[key].grid(row=row, column=0, sticky="w", pady=4)
            self.base_text[key] = label
            row += 1

        self.widgets["flatpak_user_unused"] = tk.Checkbutton(user_tab, text="Flatpak user: uninstall unused [size unknown]", variable=self.var_flatpak_user, **checkbox_opts)
        self.widgets["flatpak_user_unused"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["flatpak_user_unused"] = "Flatpak user: uninstall unused [size unknown]"
        row += 1

        self.widgets["flatpak_repair_user"] = tk.Checkbutton(user_tab, text="Flatpak repair user [size unknown]", variable=self.var_flatpak_repair_user, **checkbox_opts)
        self.widgets["flatpak_repair_user"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["flatpak_repair_user"] = "Flatpak repair user [size unknown]"

        right_actions = ttk.LabelFrame(right_panel, text="Actions", padding=10)
        right_actions.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(
            right_actions,
            text="Select categories, then clean.\nPreview and Refresh Sizes: File menu.",
            style="Hint.TLabel",
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))

        self.clean_button = ttk.Button(
            right_actions,
            text="Clean Selected",
            style="Primary.TButton",
            command=self.on_clean_clicked,
        )
        # Broom glyph inside the button, rendered in memory so it needs no font
        # with emoji coverage and no extra file.
        self.clean_button_glyph = glyph_photo_image(
            self, PRIMARY_BUTTON_GLYPH_SIZE, (0xFF, 0xFF, 0xFF)
        )
        if self.clean_button_glyph is not None:
            self.clean_button.configure(
                image=self.clean_button_glyph, compound=tk.LEFT, padding=(12, 10)
            )
        self.clean_button.pack(fill=tk.X)
        self._interactive_widgets.append((self.clean_button, "normal"))

        log_card = ttk.LabelFrame(right_panel, text="Activity Log", padding=10)
        log_card.pack(fill=tk.BOTH, expand=True)
        self.log = ScrolledText(
            log_card,
            height=18,
            width=LOG_WIDTH_CHARS,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#f8f9fa",
            fg="#2c3e50",
            relief=tk.FLAT,
            bd=1,
            highlightthickness=0,
        )
        self.log.pack(fill=tk.BOTH, expand=True)

        log_append(self.log, "Ready. Select categories, preview, then clean.")
        # Category labels carry the sizes, so give them the larger share.
        self.after_idle(self._place_sash)

    def _create_scrollable_tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        """
        Add a notebook tab that scrolls vertically when its content does not fit.

        Keeps every category reachable on small screens.

        :param notebook: Notebook the tab is added to.
        :param title: Tab title.
        :return: Frame to place the tab content in.
        """
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=title)

        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        inner = ttk.Frame(canvas, padding=8)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind(
            "<Configure>",
            lambda event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )

        def wheel(event: tk.Event) -> None:
            """Scroll the tab content with the mouse wheel."""
            up = getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0
            canvas.yview_scroll(-2 if up else 2, "units")

        def bind_wheel(_event: tk.Event) -> None:
            """Route wheel events to this tab while the pointer is inside."""
            for sequence in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
                canvas.bind_all(sequence, wheel)

        def unbind_wheel(_event: tk.Event) -> None:
            """Stop routing wheel events when the pointer leaves the tab."""
            for sequence in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
                canvas.unbind_all(sequence)

        canvas.bind("<Enter>", bind_wheel)
        canvas.bind("<Leave>", unbind_wheel)
        return inner

    def _place_sash(self) -> None:
        """Give the category list roughly two thirds of the window width."""
        try:
            self.update_idletasks()
            width = self.content_pane.winfo_width()
            if width > 400:
                self.content_pane.sashpos(0, int(width * 0.64))
        except tk.TclError:
            pass

    def _build_result_card(self, parent: ttk.Frame) -> None:
        """
        Build the persistent disk space table docked at the bottom of the window.

        The table reads left to right: free space before the cleanup, the amount
        that was freed, and the free space available now. The values stay visible
        until the next cleanup run.

        :param parent: Container frame.
        """
        card = ttk.LabelFrame(parent, text="Disk space", padding=(12, 8, 12, 10))
        card.pack(fill=tk.X, side=tk.BOTTOM, pady=(10, 0))

        # The container background shows through the one pixel cell gaps and
        # forms the grid lines of the table.
        table = tk.Frame(
            card,
            bg=TABLE_BORDER_COLOR,
            highlightbackground=TABLE_BORDER_COLOR,
            highlightthickness=1,
            bd=0,
        )
        table.pack(fill=tk.X)

        self.result_vars: Dict[str, tk.StringVar] = {}
        self.result_caption_vars: List[tk.StringVar] = []
        for index, (caption, key, color) in enumerate(RESULT_TABLE_COLUMNS):
            table.columnconfigure(index, weight=1, uniform="result")
            caption_var = tk.StringVar(master=self, value=PREVIEW_TABLE_CAPTIONS[index])
            self.result_caption_vars.append(caption_var)
            tk.Label(
                table,
                textvariable=caption_var,
                bg=TABLE_HEADER_BG,
                fg=TABLE_HEADER_FG,
                font=("Segoe UI", 9, "bold"),
                anchor="w",
                padx=10,
                pady=5,
            ).grid(row=0, column=index, sticky="nsew",
                   padx=(0 if index == 0 else 1, 0), pady=(0, 1))

            var = tk.StringVar(master=self, value="—")
            self.result_vars[key] = var
            tk.Label(
                table,
                textvariable=var,
                bg=TABLE_CELL_BG,
                fg=color,
                font=("Segoe UI", 17, "bold"),
                anchor="w",
                padx=10,
                pady=8,
            ).grid(row=1, column=index, sticky="nsew", padx=(0 if index == 0 else 1, 0))

        self.disk_line_var = tk.StringVar(master=self, value="Reading disk usage ...")
        ttk.Label(card, textvariable=self.disk_line_var, style="ResultNote.TLabel").pack(
            anchor="w", pady=(8, 0)
        )

        self.result_note_var = tk.StringVar(
            master=self,
            value="Collecting sizes, the projection appears as soon as the analysis is done.",
        )
        ttk.Label(
            card,
            textvariable=self.result_note_var,
            style="ResultNote.TLabel",
            wraplength=1100,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(2, 0))

    def _set_controls_enabled(self, enabled: bool) -> None:
        """
        Enable or disable the action controls while a background job runs.

        :param enabled: True to restore the normal state.
        """
        for widget, normal_state in self._interactive_widgets:
            try:
                widget.configure(state=normal_state if enabled else "disabled")
            except tk.TclError:
                pass
        for menu, index in getattr(self, "_job_menu_entries", []):
            try:
                menu.entryconfigure(index, state=tk.NORMAL if enabled else tk.DISABLED)
            except tk.TclError:
                pass

    # ----------------------------- Menu actions -----------------------------

    def on_delete_mode_changed(self) -> None:
        """
        Show the current deletion mode in the combobox and refresh the projection.
        """
        try:
            self.mode_combo.set(DELETE_MODE_LABELS[self.delete_mode_var.get()])
        except (AttributeError, tk.TclError, KeyError):
            pass
        self._show_projection()

    def on_toggle_nemo_action(self) -> None:
        """
        Add or remove the Nemo context menu entry from the menu checkbox.
        """
        wanted = bool(self.nemo_action_var.get())
        succeeded = install_nemo_action() if wanted else remove_nemo_action()
        if not succeeded:
            # Show what is really on disk, not what was clicked.
            self.nemo_action_var.set(nemo_action_installed())
            log_append(self.log, "[ERR] Could not change the Nemo context menu entry.")
            return
        log_append(
            self.log,
            "[OK] Nemo context menu entry added."
            if wanted
            else "[OK] Nemo context menu entry removed.",
        )

    def on_toggle_desktop_shortcut(self) -> None:
        """
        Create or delete the desktop shortcut from the menu checkbox.
        """
        wanted = bool(self.desktop_shortcut_var.get())
        if wanted:
            succeeded, _path = install_desktop_shortcut()
        else:
            succeeded = remove_desktop_shortcut()
        if not succeeded:
            self.desktop_shortcut_var.set(desktop_shortcut_installed())
            log_append(self.log, "[ERR] Could not change the desktop shortcut.")
            return
        log_append(
            self.log,
            "[OK] Desktop shortcut created." if wanted else "[OK] Desktop shortcut removed.",
        )

    def on_about(self) -> None:
        """
        Show the About dialog.
        """
        messagebox.showinfo(
            "About Mint Cleaner",
            "Mint Cleaner\n\n"
            "Clean temporary files, caches and system leftovers on Linux Mint "
            "with a single authentication and a clear disk space report.",
            parent=self,
        )

    def on_developer(self) -> None:
        """
        Show the developer information.
        """
        messagebox.showinfo(
            "Developer",
            "Joachim Ruf\n"
            "Loresoft\n\n"
            "GitHub: https://github.com/joruf\n"
            "Web: https://www.loresoft.de/",
            parent=self,
        )

    def on_quit(self) -> None:
        """
        Close the application, but never in the middle of a running job.
        """
        if self._job_active:
            log_append(self.log, "[INFO] An operation is still running, please wait.")
            return
        self.destroy()

    # ----------------------------- Background jobs -----------------------------

    def _start_job(
        self,
        title: str,
        subtitle: str,
        steps: Sequence[str],
        work: Callable[[JobReporter], Any],
        on_success: Callable[[Any], None],
        list_height: int = 14,
    ) -> bool:
        """
        Run a worker function in a thread while showing a modal progress dialog.

        The worker receives a JobReporter and must not touch Tk widgets.

        :param title: Dialog title and headline.
        :param subtitle: Explanation shown below the headline.
        :param steps: Step labels known upfront.
        :param work: Callable executed in the background thread.
        :param on_success: Callback executed in the main thread with the result.
        :param list_height: Visible lines of the step checklist.
        :return: True when the job was started.
        """
        if self._job_active:
            log_append(self.log, "[INFO] Another operation is still running, please wait.")
            return False

        self._job_active = True
        self._set_controls_enabled(False)
        dialog = ProgressDialog(self, title, subtitle, steps, list_height=list_height)
        updates: "queue.Queue" = queue.Queue()
        reporter = JobReporter(updates)
        outcome: Dict[str, Any] = {}

        def runner() -> None:
            """Execute the worker and always signal completion."""
            try:
                outcome["result"] = work(reporter)
            except Exception as exc:  # keep the GUI usable on unexpected errors
                outcome["error"] = exc
            finally:
                updates.put(("finished", None, None))

        threading.Thread(target=runner, name="mint-cleaner-job", daemon=True).start()
        self._pump_job(updates, dialog, outcome, on_success)
        return True

    def _pump_job(
        self,
        updates: "queue.Queue",
        dialog: ProgressDialog,
        outcome: Dict[str, Any],
        on_success: Callable[[Any], None],
    ) -> None:
        """
        Drain worker updates into the dialog and finish the job when done.

        :param updates: Queue filled by the worker thread.
        :param dialog: Progress dialog to update.
        :param outcome: Dict filled by the worker with 'result' or 'error'.
        :param on_success: Callback executed after a successful run.
        """
        try:
            self._pump_job_step(updates, dialog, outcome, on_success)
        except tk.TclError:
            # The window was closed while the job was running, nothing to update.
            self._job_active = False

    def _pump_job_step(
        self,
        updates: "queue.Queue",
        dialog: ProgressDialog,
        outcome: Dict[str, Any],
        on_success: Callable[[Any], None],
    ) -> None:
        """
        Drain one batch of worker updates and schedule the next poll.

        :param updates: Queue filled by the worker thread.
        :param dialog: Progress dialog to update.
        :param outcome: Dict filled by the worker with 'result' or 'error'.
        :param on_success: Callback executed after a successful run.
        """
        finished = False
        while True:
            try:
                kind, payload, extra = updates.get_nowait()
            except queue.Empty:
                break

            if kind == "finished":
                finished = True
                break
            if kind == "add_steps":
                dialog.add_steps(payload)
            elif kind == "begin":
                label, note = extra
                dialog.begin_step(payload, label=label, note=note)
            elif kind == "end":
                result, failed = extra
                dialog.end_step(payload, result=result, failed=failed)
            elif kind == "subtitle":
                dialog.set_subtitle(payload)
            elif kind == "live":
                self._apply_live_scan(payload)
            elif kind == "log":
                log_append(self.log, payload)

        if not finished:
            self.after(60, lambda: self._pump_job(updates, dialog, outcome, on_success))
            return

        dialog.close()
        self._job_active = False
        self._set_controls_enabled(True)

        error = outcome.get("error")
        if error is not None:
            log_append(self.log, f"[ERR] Operation failed: {error}")
            return
        on_success(outcome.get("result"))

    def _selection_snapshot(self) -> Dict[str, bool]:
        """
        Read all category checkboxes into a plain dict for background use.

        :return: Mapping of selection name to bool.
        """
        return selection_from_vars(self)

    def _measure_key(self, key: str, patterns: Dict[str, List[str]]) -> int:
        """
        Measure one category, using the privileged helper for root paths.

        :param key: Category key.
        :param patterns: Patterns per category.
        :return: Size in bytes.
        """
        entries = patterns.get(key, [])
        if not entries:
            return 0
        if key in ROOT_MEASURABLE_KEYS:
            return HELPER.get_size_of_patterns(entries)
        return size_of_patterns(entries)

    def on_category_toggle(self) -> None:
        """
        Refresh the projection after a category checkbox click.
        """
        self._show_projection()

    def _measurable_key_vars(self) -> Dict[str, tk.BooleanVar]:
        """
        Return the selection variables the automatic select rules apply to.

        Single source for the threshold rule, the zero rule and the live update
        during the analysis, so all three tick exactly the same boxes.

        :return: Mapping of category key to its checkbox variable.
        """
        return {
            "tmp": self.var_tmp,
            "user_cache": self.var_user_cache,
            "thumbnails": self.var_thumbnails,
            "trash": self.var_trash,
            "firefox": self.var_firefox,
            "chrome": self.var_chrome,
            "flatpak_syscache": self.var_flatpak_syscache,
            "apt": self.var_apt,
            "journal": self.var_journal,
            "flatpak_app_cache": self.var_flatpak_app_cache,
            "config_app_caches": self.var_config_app_caches,
            "dev_tool_caches": self.var_dev_tool_caches,
            "user_lang_tool_caches": self.var_user_lang_tool_caches,
            "python_artifacts": self.var_python_artifacts,
            "local_history": self.var_local_history,
            "system_misc_caches": self.var_system_misc_caches,
            "system_extra_caches": self.var_system_extra_caches,
            "apt_cache": self.var_apt_cache,   # Now measurable via root
        }

    def _autoselect_threshold_bytes(self) -> int:
        """
        Return the size from which a category is ticked automatically.

        :return: Threshold in bytes, 0 when the automatic selection is off.
        """
        if AUTOCHECK_THRESHOLD_MB <= 0:
            return 0
        return int(AUTOCHECK_THRESHOLD_MB * 1024 * 1024)

    def _apply_autoselect_by_threshold(self, sizes_now: Dict[str, int]) -> None:
        """
        Auto select checkboxes whose measurable size exceeds AUTOCHECK_THRESHOLD_MB.
        Non measurable items are ignored.
        """
        threshold_bytes = self._autoselect_threshold_bytes()
        if threshold_bytes <= 0:
            return

        key_to_var = self._measurable_key_vars()
        for key, size in sizes_now.items():
            var = key_to_var.get(key)
            if var is not None and size >= threshold_bytes:
                var.set(True)

    def _apply_live_category_size(self, key: str, size: int) -> None:
        """
        Apply the automatic select rules to a single freshly measured category.

        Same rules as the full pass at the end of the analysis, only earlier, so
        the boxes tick while the scan is still running.

        :param key: Category key.
        :param size: Measured size in bytes.
        """
        var = self._measurable_key_vars().get(key)
        if var is None:
            return
        threshold_bytes = self._autoselect_threshold_bytes()
        if size <= 0:
            var.set(False)
        elif threshold_bytes > 0 and size >= threshold_bytes:
            var.set(True)

    def _apply_autodeselect_zero_or_unknown(self, sizes_now: Dict[str, int]) -> None:
        """
        Auto deselect all items that have size 0 or size unknown.
        Unknown size equals non measurable category or not present in sizes_now.
        """
        for key, var in self._measurable_key_vars().items():
            size = sizes_now.get(key, None)
            if size is None or size == 0:
                var.set(False)

        # Non measurable entries are always deselected
        self.var_flatpak_user.set(False)
        self.var_flatpak_repair_user.set(False)
        self.var_flatpak_repair_system.set(False)
        self.var_old_kernels.set(False)

    # ----------------------------- Size analysis -----------------------------

    def refresh_sizes(self) -> None:
        """
        Recalculate sizes for all measurable categories in the background.

        Shows a modal progress dialog listing every category, so the startup
        analysis and manual refreshes are visible instead of a frozen window.
        Root owned paths are measured by the privileged helper.
        """
        # Disk usage comes first, it is instant and fills the table right away.
        steps = ["Read disk usage"]
        steps += [CATEGORY_LABELS[key] for key in MEASURABLE_KEYS]
        self._start_job(
            "Collecting data",
            "Mint Cleaner is measuring what can be cleaned. "
            "Scanning the home directory can take a moment.",
            steps,
            self._scan_worker,
            self._apply_scan_result,
            list_height=len(steps),
        )

    def _scan_worker(self, reporter: JobReporter) -> Dict[str, Any]:
        """
        Measure every category and read the disk usage in a background thread.

        :param reporter: Progress reporter of the running job.
        :return: Result dict with sizes, discovered patterns and disk snapshot.
        """
        patterns = {key: list(value) for key, value in self.patterns.items()}
        sizes: Dict[str, int] = {}
        discovered: Dict[str, List[str]] = {}

        reporter.begin("Read disk usage")
        disk = disk_snapshot()
        reporter.live({"disk": disk})
        reporter.end(format_disk_line(disk))

        for key in MEASURABLE_KEYS:
            note = "(scanning your home directory)" if key in DISCOVERED_KEYS else ""
            reporter.begin(CATEGORY_LABELS[key], note)
            try:
                finder = DISCOVERY_FINDERS.get(key)
                if finder is not None:
                    discovered[key] = finder()
                    patterns[key] = discovered[key]
                size = self._measure_key(key, patterns)
                sizes[key] = size
                reporter.live({"sizes": {key: size}})
                reporter.end(human_size(size))
            except Exception as exc:
                sizes[key] = 0
                reporter.end(f"failed: {exc}", failed=True)

        return {"sizes": sizes, "discovered": discovered, "disk": disk}

    def _apply_scan_result(self, result: Optional[Dict[str, Any]]) -> None:
        """
        Apply a finished size analysis to the GUI.

        :param result: Result dict produced by _scan_worker().
        """
        if not result:
            return

        sizes: Dict[str, int] = result["sizes"]
        self.patterns.update(result["discovered"])

        self._apply_autoselect_by_threshold(sizes)
        self._apply_autodeselect_zero_or_unknown(sizes)
        self._update_category_labels(sizes)

        self.sizes_before = sizes
        self.disk_now = result["disk"]
        self._update_disk_line()
        self._show_projection()
        log_append(
            self.log,
            f"Sizes refreshed, total measurable: {human_size(sum(sizes.values()))}. "
            f"{format_disk_line(self.disk_now)}",
        )
        self._log_projection()

    def _apply_live_scan(self, payload: Dict[str, Any]) -> None:
        """
        Show partial analysis results while the scan is still running.

        Free space appears with the first message, and every measured category
        immediately raises the amount the current selection would free, so the
        table is readable long before "Collecting data" is finished.

        :param payload: Partial result published by the scan worker.
        """
        if "disk" in payload:
            self.disk_now = payload["disk"]
            self._update_disk_line()

        sizes = payload.get("sizes") or {}
        if sizes:
            self.sizes_before.update(sizes)
            for key, size in sizes.items():
                self._apply_live_category_size(key, size)
            self._update_category_labels(sizes)

        self._show_projection(measuring=True)

    def _update_category_labels(self, sizes: Dict[str, int]) -> None:
        """
        Write the measured sizes into the checkbox labels.

        :param sizes: Measured size in bytes per category key.
        """
        for key, size in sizes.items():
            if key in self.widgets and key in self.base_text:
                try:
                    self.widgets[key].configure(
                        text=f"{self.base_text[key]}  [{human_size(size)}]"
                    )
                except tk.TclError:
                    pass

        # Keep non measurable labels as is (they already have "[size unknown]")
        # old_kernels is measurable now: its module and header trees are sized
        # from the resolved package list.
        non_measurable = ["flatpak_user_unused", "flatpak_repair_user",
                          "flatpak_repair_system"]
        for key in non_measurable:
            if key in self.widgets and key in self.base_text:
                try:
                    self.widgets[key].configure(text=self.base_text[key])
                except tk.TclError:
                    pass

    def _update_disk_line(self) -> None:
        """Update the always current disk usage line of the report card."""
        if not hasattr(self, "disk_line_var"):
            return
        line = format_disk_line(self.disk_now)
        self.disk_line_var.set(line or "Disk usage unavailable.")

    # ----------------------------- Plan and execution -----------------------------

    def build_plan(self, rediscover: bool = True) -> dict:
        """
        Build a plan from the selected checkboxes.

        Thin wrapper around build_cleanup_plan() that reads the Tk variables,
        stores rediscovered paths and logs skipped optional commands.

        :param rediscover: Re-scan home for regeneratable directories when True.
        :return: Dict with user_py_delete, user_cmds, root_rm_patterns and root_cmds.
        """
        plan, discovered, notes = build_cleanup_plan(
            selection_from_vars(self),
            self.patterns,
            journal_retention=self.journal_retention.get(),
            rediscover=rediscover,
        )
        self.patterns.update(discovered)
        for note in notes:
            log_append(self.log, f"Note: {note}")
        return plan

    def on_preview(self) -> None:
        """
        Show a preview of planned actions.
        """
        # Uses the paths from the last analysis, so the preview stays instant.
        plan = self.build_plan(rediscover=False)
        mode_txt = "Move to Trash" if self.delete_mode_var.get() == "trash" else "Delete immediately"
        log_append(self.log, f"---- Preview ----\nUser deletion mode: {mode_txt}")
        if plan["user_py_delete"]:
            log_append(self.log, "User deletions, paths:")
            for p in plan["user_py_delete"]:
                log_append(self.log, f"  - {p}")
            if self.var_local_history.get():
                log_append(
                    self.log,
                    "Note: Removing .history deletes editor Local History "
                    "timeline/history data (not regeneratable from Git).",
                )
        if plan["user_cmds"]:
            log_append(self.log, "User commands:")
            for c in plan["user_cmds"]:
                log_append(self.log, f"  - {c}")
        if plan["root_rm_patterns"]:
            log_append(self.log, "Root deletions, patterns:")
            for p in plan["root_rm_patterns"]:
                log_append(self.log, f"  - {p}")
        if plan["root_cmds"]:
            log_append(self.log, "Root commands:")
            for c in plan["root_cmds"]:
                log_append(self.log, f"  - {c}")
        log_append(self.log, "-----------------")

    def on_clean_clicked(self) -> None:
        """
        Run the selected cleanup actions in a background job.

        Every selected category is measured before and after the run so the
        report card can show free space before, the deleted amount and free
        space afterwards. No confirmation popup, progress is shown in the
        progress dialog and in the activity log.
        """
        if self._job_active:
            log_append(self.log, "[INFO] Another operation is still running, please wait.")
            return

        selection = self._selection_snapshot()
        if not any(selection.values()):
            log_append(self.log, "[INFO] Nothing selected.")
            return

        selected_keys = [key for key in MEASURABLE_KEYS if selection.get(key)]
        context: Dict[str, Any] = {
            "selection": selection,
            "selected_keys": selected_keys,
            "patterns": {key: list(value) for key, value in self.patterns.items()},
            "retention": self.journal_retention.get(),
            "mode": self.delete_mode_var.get(),
        }

        if selection.get("local_history"):
            log_append(
                self.log,
                "[WARN] Local History (.history): editor timeline/history "
                "snapshots will be permanently removed.",
            )

        steps = ["Prepare cleanup plan", "Read disk usage before cleanup"]
        steps += [f"Measure before: {CATEGORY_LABELS[key]}" for key in selected_keys]

        self._start_job(
            "Cleaning up",
            "Mint Cleaner is removing the selected data. "
            "The checklist shows every step of this run.",
            steps,
            lambda reporter: self._cleanup_worker(reporter, context),
            self._apply_cleanup_result,
            list_height=16,
        )

    def _cleanup_worker(self, reporter: JobReporter, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the cleanup plan in a background thread.

        :param reporter: Progress reporter of the running job.
        :param context: Snapshot of the GUI state taken in the main thread.
        :return: Result dict with sizes, reclaimed bytes and disk snapshots.
        """
        selection: Dict[str, bool] = context["selection"]
        patterns: Dict[str, List[str]] = context["patterns"]
        selected_keys: List[str] = context["selected_keys"]

        reporter.begin("Prepare cleanup plan", "(locating regeneratable folders)")
        plan, discovered, notes = build_cleanup_plan(
            selection,
            patterns,
            journal_retention=context["retention"],
            rediscover=True,
        )
        patterns.update(discovered)
        for note in notes:
            reporter.log(f"Note: {note}")
        to_trash, to_delete_user = split_user_targets(plan["user_py_delete"], context["mode"])
        reporter.end("done")

        reporter.begin("Read disk usage before cleanup")
        disk_before = disk_snapshot()
        reporter.end(format_disk_line(disk_before))

        sizes_before: Dict[str, int] = {}
        for key in selected_keys:
            reporter.begin(f"Measure before: {CATEGORY_LABELS[key]}")
            sizes_before[key] = self._measure_key(key, patterns)
            reporter.end(human_size(sizes_before[key]))

        # Announce the remaining work now that the plan is known. Immediate
        # deletions run first: emptying the Trash must not wipe out the files
        # that are moved into it in the same run.
        remaining: List[str] = []
        if to_delete_user:
            remaining.append(f"Delete {len(to_delete_user)} user path patterns")
        if to_trash:
            remaining.append(f"Move {len(to_trash)} user path patterns to Trash")
        remaining += [f"Run: {cmd}" for cmd in plan["user_cmds"]]
        if plan["root_rm_patterns"]:
            remaining.append(
                f"Delete {len(plan['root_rm_patterns'])} system path patterns (root)"
            )
        remaining += [f"Root: {cmd}" for cmd in plan["root_cmds"]]
        remaining += [f"Measure after: {CATEGORY_LABELS[key]}" for key in selected_keys]
        remaining += ["Read disk usage after cleanup", "Check desktop icon theme"]
        reporter.add_steps(remaining)

        reporter.log("=== Cleanup started ===")
        if plan_is_empty(plan):
            reporter.log("[INFO] Nothing to delete, the selected categories are empty.")

        if to_delete_user:
            reporter.begin(f"Delete {len(to_delete_user)} user path patterns")
            reporter.log("[User] Deleting selected paths ...")
            removed, log_text = rm_paths(to_delete_user)
            if log_text.strip():
                reporter.log(log_text)
            reporter.log(f"[User] Removed entries: {removed}")
            reporter.end(f"{removed} entries")

        if to_trash:
            reporter.begin(f"Move {len(to_trash)} user path patterns to Trash")
            reporter.log("[User] Moving selected paths to Trash ...")
            moved, log_text = trash_paths(to_trash)
            if log_text.strip():
                reporter.log(log_text)
            reporter.log(f"[User] Trashed entries: {moved}")
            reporter.end(f"{moved} entries")

        for cmd in plan["user_cmds"]:
            reporter.begin(f"Run: {cmd}")
            reporter.log(f"[User] Running: {cmd}")
            proc = subprocess.run(
                cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            if proc.stdout and proc.stdout.strip():
                reporter.log(proc.stdout.strip())
            reporter.log(f"[User] Exit code: {proc.returncode}")
            reporter.end(f"exit {proc.returncode}", failed=proc.returncode != 0)

        if plan["root_rm_patterns"]:
            reporter.begin(
                f"Delete {len(plan['root_rm_patterns'])} system path patterns (root)"
            )
            reporter.log("[Root] Deleting patterns with helper ...")
            rc, out = HELPER.rm_rf_patterns(plan["root_rm_patterns"])
            if out:
                reporter.log(out.strip())
            reporter.log(f"[Root] rm_rf exit code: {rc}")
            reporter.end(f"exit {rc}", failed=rc != 0)

        for cmd in plan["root_cmds"]:
            reporter.begin(f"Root: {cmd}")
            last_rc = 0
            for executed, rc, out in HELPER.run_root_cmds([cmd]):
                reporter.log(f"[Root] Running: {executed}")
                if out.strip():
                    reporter.log(out.strip())
                reporter.log(f"[Root] Exit code: {rc}")
                last_rc = rc
            reporter.end(f"exit {last_rc}", failed=last_rc != 0)

        sizes_after: Dict[str, int] = {}
        reclaimed_total = 0
        for key in selected_keys:
            reporter.begin(f"Measure after: {CATEGORY_LABELS[key]}")
            after = self._measure_key(key, patterns)
            before = sizes_before.get(key, 0)
            reclaimed = max(0, before - after)
            sizes_after[key] = after
            reclaimed_total += reclaimed
            reporter.log(
                f"[{key}] before {human_size(before)}, after {human_size(after)}, "
                f"reclaimed {human_size(reclaimed)}"
            )
            reporter.end(f"{human_size(reclaimed)} freed, {human_size(after)} left")

        reporter.begin("Read disk usage after cleanup")
        disk_after = disk_snapshot()
        reporter.end(format_disk_line(disk_after))

        reporter.begin("Check desktop icon theme")
        changed, message = repair_user_hicolor_shadow()
        if changed:
            reporter.log(f"[FIX] {message}")
        reporter.end("repaired" if changed else "ok")

        reporter.log("=== Cleanup finished ===")

        return {
            "selected_keys": selected_keys,
            "sizes_before": sizes_before,
            "sizes_after": sizes_after,
            "reclaimed_total": reclaimed_total,
            "disk_before": disk_before,
            "disk_after": disk_after,
            "discovered": discovered,
            "trash_used": bool(to_trash),
        }

    def _apply_cleanup_result(self, result: Optional[Dict[str, Any]]) -> None:
        """
        Apply a finished cleanup run to the GUI.

        Updates the category labels with the new sizes and fills the persistent
        disk space report. No full re-analysis is triggered, the numbers of this
        run stay visible until the user refreshes or cleans again.

        :param result: Result dict produced by _cleanup_worker().
        """
        if not result:
            return

        self.patterns.update(result.get("discovered", {}))

        sizes = dict(self.sizes_before)
        sizes.update(result["sizes_after"])
        self.sizes_before = sizes
        self.disk_now = result["disk_after"]

        # Categories that are empty now do not need to stay selected.
        self._apply_autodeselect_zero_or_unknown(result["sizes_after"])
        self._update_category_labels(result["sizes_after"])
        self._update_disk_line()
        self._render_cleanup_result(result)
        self._log_cleanup_success(result)
        log_append(self.log, "Use Refresh Sizes to re-measure all categories.")

    def _set_table_captions(self, captions: Sequence[str]) -> None:
        """
        Switch the table headers between projection and result wording.

        :param captions: One caption per column, in table order.
        """
        for caption_var, caption in zip(self.result_caption_vars, captions):
            caption_var.set(caption)

    def _show_projection(self, measuring: bool = False) -> None:
        """
        Show what the current selection would free in the disk space table.

        Runs after every analysis and after every selection change, so the table
        answers "how much space do I have, how much can I free, what is left
        afterwards" before anything is deleted. A finished cleanup replaces these
        numbers with the values that were really measured.

        :param measuring: True while the analysis is still running, the note
            then says that the amount is still growing.
        """
        if not hasattr(self, "result_vars") or not self.result_vars:
            return

        free_now = primary_free_bytes(self.disk_now)
        selection = selection_from_vars(self)
        potential = selected_potential_bytes(self.sizes_before, selection)

        self._set_table_captions(PREVIEW_TABLE_CAPTIONS)
        for key, value in projection_table_row(free_now, potential).items():
            self.result_vars[key].set(value)

        selected_count = sum(1 for value in selection.values() if value)
        notes = [
            f"Projection for {selected_count} selected "
            f"{'category' if selected_count == 1 else 'categories'}",
            "still measuring, the value keeps growing"
            if measuring
            else "updates with every change of the selection",
        ]
        if trash_mode_delays_space(selection, self.delete_mode_var.get()):
            notes.append(
                "in Trash mode the space is released after emptying the Trash"
            )
        if self.last_cleanup is not None:
            notes.append(
                f"last cleanup freed {human_size(self.last_cleanup['reclaimed_total'])}"
            )
        self.result_note_var.set("  ·  ".join(notes))

    def _log_projection(self) -> None:
        """
        Write the projection for the current selection as a table into the log.
        """
        free_now = primary_free_bytes(self.disk_now)
        potential = selected_potential_bytes(self.sizes_before, selection_from_vars(self))
        row = projection_table_row(free_now, potential)

        log_append(self.log, "")
        log_append(self.log, "CURRENT SELECTION")
        for line in format_text_table(
            PREVIEW_TABLE_LOG_HEADERS,
            [[row[key] for _caption, key, _color in RESULT_TABLE_COLUMNS]],
        ):
            log_append(self.log, line)
        log_append(self.log, "")

    def _render_cleanup_result(self, result: Dict[str, Any]) -> None:
        """
        Fill the persistent disk space table with the values of a run.

        :param result: Result dict produced by _cleanup_worker().
        """
        self._set_table_captions(
            [caption for caption, _key, _color in RESULT_TABLE_COLUMNS]
        )
        for key, value in cleanup_table_row(result).items():
            self.result_vars[key].set(value)

        notes = [
            f"Last cleanup at {datetime.now().strftime('%H:%M:%S')}",
            f"{len(result['selected_keys'])} categories processed",
            f"deleted {human_size(result['reclaimed_total'])} of data",
        ]
        if result.get("trash_used"):
            notes.append(
                "paths were moved to Trash, empty the Trash to release that space on disk"
            )
        self.result_note_var.set("  ·  ".join(notes))
        self.last_cleanup = result

    def _log_cleanup_success(self, result: Dict[str, Any]) -> None:
        """
        Write the closing cleanup report as a table into the activity log.

        :param result: Result dict produced by _cleanup_worker().
        """
        reclaimed = result["reclaimed_total"]
        free_before = primary_free_bytes(result["disk_before"])
        free_after = primary_free_bytes(result["disk_after"])
        row = cleanup_table_row(result)

        log_append(self.log, "")
        log_append(self.log, "CLEANUP COMPLETE")
        for line in format_text_table(
            RESULT_TABLE_LOG_HEADERS,
            [[row[key] for _caption, key, _color in RESULT_TABLE_COLUMNS]],
        ):
            log_append(self.log, line)
        log_append(self.log, f"Deleted data: {human_size(reclaimed)}")
        log_append(
            self.log,
            f"Processed measurable categories: {len(result['selected_keys'])}",
        )
        if reclaimed > 0 and free_after <= free_before:
            log_append(
                self.log,
                "Data was deleted, but the free space did not grow. Files moved to "
                "the Trash still occupy space, and deleted files that a process "
                "still holds open are released later.",
            )
        if reclaimed >= 500 * 1024 * 1024:
            log_append(self.log, "Trophy unlocked: Great cleanup!")
        log_append(self.log, "")

# ----------------------------- Helper entrypoint (runs as root) -----------------------------

def helper_main() -> None:
    """
    Run the privileged JSON line helper handling a minimal set of safe actions:
    - ping
    - rm_rf_patterns
    - run_root_cmds
    - get_size (compute total size of root patterns)
    """
    # Belt-and-suspenders: sanitize again in case imports restored session vars.
    sanitize_helper_environment()

    def send_ok(data: Any = True) -> None:
        print(json.dumps({"status": "ok", "data": data}), flush=True)

    def send_err(msg: str) -> None:
        print(json.dumps({"status": "err", "error": msg}), flush=True)

    def _expand_patterns(patterns: List[str]) -> List[str]:
        out: List[str] = []
        for pat in patterns:
            for p in glob.glob(pat, recursive=False):
                if is_protected_path(p):
                    continue
                out.append(p)
        return out

    def _size_of_path(p: str) -> int:
        """Compute size of a single file/directory (no glob)."""
        if not os.path.exists(p) or is_protected_path(p):
            return 0
        if os.path.isdir(p) and not os.path.islink(p):
            total = 0
            for root, dirnames, files in os.walk(p, onerror=lambda e: None):
                dirnames[:] = [
                    d for d in dirnames
                    if not is_protected_path(os.path.join(root, d))
                ]
                for f in files:
                    fp = os.path.join(root, f)
                    if is_protected_path(fp):
                        continue
                    try:
                        if not os.path.islink(fp):
                            total += os.path.getsize(fp)
                    except Exception:
                        pass
            return total
        if os.path.isfile(p) and not os.path.islink(p):
            try:
                return os.path.getsize(p)
            except Exception:
                return 0
        return 0

    def _size_of_patterns(patterns: List[str]) -> int:
        total = 0
        for pat in patterns:
            for p in glob.glob(pat, recursive=False):
                if is_protected_path(p):
                    continue
                total += _size_of_path(p)
        return total

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
            action = req.get("action")
            args = req.get("args") or {}

            if action == "ping":
                send_ok(True)

            elif action == "get_size":
                patterns: List[str] = args.get("patterns") or []
                size = _size_of_patterns(patterns)
                send_ok(size)

            elif action == "rm_rf_patterns":
                pats: List[str] = args.get("patterns") or []
                expanded = _expand_patterns(pats)
                log_lines: List[str] = []
                # Also report any protected matches that were skipped.
                for pat in pats:
                    for p in glob.glob(pat, recursive=False):
                        if is_protected_path(p):
                            log_lines.append(f"Skipped protected path: {p}")
                rc_global = 0
                for p in expanded:
                    try:
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p, ignore_errors=True)
                            log_lines.append(f"Removed directory: {p}")
                        elif os.path.isfile(p) or os.path.islink(p):
                            os.remove(p)
                            log_lines.append(f"Removed file: {p}")
                    except Exception as e:
                        rc_global = 1
                        log_lines.append(f"Failed to remove {p}: {e}")
                send_ok((rc_global, "\n".join(log_lines)))

            elif action == "run_root_cmds":
                cmds: List[str] = args.get("cmds") or []
                results: List[Tuple[str, int, str]] = []
                for cmd in cmds:
                    p = subprocess.run(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    results.append((cmd, p.returncode, p.stdout or ""))
                send_ok(results)

            else:
                send_err("unknown action")

        except Exception as e:
            send_err(str(e))

# ----------------------------- Main -----------------------------

def main() -> None:
    """
    Application entry point:
    1) Start privileged helper via pkexec BEFORE showing any window.
    2) If authentication fails, exit with error, no GUI shown.
    3) If ok, create and show the Tk GUI.
    """
    ok = False
    try:
        ok = HELPER.start(None)
    except Exception:
        ok = False

    if not ok:
        sys.stderr.write("Authentication failed, could not start privileged helper. Exiting.\n")
        sys.exit(1)

    # Only now create and show the GUI
    app = MintCleanerApp(start_helper=False)
    app.mainloop()


if __name__ == "__main__":
    if "--helper" in sys.argv:
        helper_main()
    else:
        # Keep launchers of older versions pointing at run.py. Whether they
        # exist at all is controlled by the checkboxes in the Integration menu.
        refresh_desktop_shortcut()
        refresh_nemo_action()
        # Fix incomplete user hicolor overlays that hide Nemo toolbar icons.
        repair_user_hicolor_shadow()
        main()
