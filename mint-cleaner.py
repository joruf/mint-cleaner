#!/usr/bin/env python3
"""
MintCleaner – selective temp and cache cleanup for Linux Mint.

Features:
- Single privileged helper via pkexec for root tasks, one authentication at startup.
- GUI becomes visible only AFTER authentication succeeded.
- Live size analysis per measurable category, labels show current MB.
- Auto select items above a configured threshold in MB.
- Auto deselect items that are 0 MB or unknown size.
- User deletion mode selectable: Move to Trash (default) or Delete immediately.
  Note: Mode applies only to user scoped paths.
  Trash contents (~/.local/share/Trash/*) are always deleted when selected.
- New options: Flatpak app cache, APT package cache (with size), old kernel removal.

No popups before or after deletion, progress is logged in the UI.
"""

import os
import sys
import shlex
import glob
import json
import shutil
import getpass
import subprocess
from typing import Tuple, List, Dict, Any, Optional

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
    from dependencies import ensure_runtime_dependencies

    ensure_runtime_dependencies()

import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from desktop_setup import maybe_prompt_desktop_setup
from nemo_setup import maybe_prompt_nemo_setup
from datetime import datetime
from urllib.parse import quote

# ----------------------------- Config -----------------------------

AUTOCHECK_THRESHOLD_MB = 100  # Auto select items larger than this many MB; set 0 to disable

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
    "~/.yarn/cache/*",
    "~/.yarn/berry/cache/*",
    "~/.pnpm-store/*",
    "~/.cargo/registry/cache/*",
    "~/.gradle/caches/*",
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
) -> List[str]:
    """
    Find directories with exact target names under root.

    Walks with skip rules so caches, app data and VCS dirs are not scanned.
    Does not follow directory symlinks.

    :param target_names: Directory basenames to collect.
    :param root: Directory to scan (default: ~).
    :param max_depth: Maximum directory depth relative to root.
    :param skip_active_prefix: If True, skip sys.prefix when it matches a hit.
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

        :param payload: Request dictionary with 'action' and optional 'args'.
        :return: Response data on success.
        :raises RuntimeError: On transport or helper error.
        """
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise RuntimeError("helper not running")
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
        super().__init__()
        self.title("Mint Cleaner, Selective Temp and Cache Cleanup")
        self.geometry("1180x860")
        self.minsize(1080, 760)

        # Configure modern style
        self._setup_styles()

        self.username = getpass.getuser()

        # Deletion mode for user scoped actions
        self.delete_mode_var = tk.StringVar(master=self, value="trash")  # "trash" or "delete"

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
        self.var_old_kernels = tk.BooleanVar(master=self, value=False)         # apt autoremove --purge

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
        }

        # Bookkeeping
        self.sizes_before: Dict[str, int] = {}
        self.widgets: Dict[str, tk.Checkbutton] = {}
        self.base_text: Dict[str, str] = {}
        self.success_clear_after_id: Optional[str] = None

        self._build_ui()

        # Log that helper is ready and authenticated (already done by main())
        log_append(self.log, "[OK] Privileged helper ready, authentication done at startup.")
        self.refresh_sizes()

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
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
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
        style.configure("TNotebook.Tab", padding=(10, 6), font=("Segoe UI", 9))
        style.map(
            "TNotebook.Tab",
            padding=[("selected", (14, 10)), ("!selected", (10, 6))],
            font=[("selected", ("Segoe UI", 10, "bold")), ("!selected", ("Segoe UI", 9))],
        )
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8))

    def _build_ui(self) -> None:
        """
        Build a redesigned, user-friendly interface with clear grouping and flow.
        """
        main_container = ttk.Frame(self, padding=14)
        main_container.pack(fill=tk.BOTH, expand=True)

        header_frame = ttk.Frame(main_container, padding=(2, 2, 2, 10))
        header_frame.pack(fill=tk.X)

        ttk.Label(header_frame, text="Mint Cleaner", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header_frame,
            text="Clean temporary files and caches safely with one-click actions.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        self.summary_var = tk.StringVar(value="No data loaded yet.")
        ttk.Label(header_frame, textvariable=self.summary_var, style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

        action_bar = ttk.Frame(main_container, padding=(0, 0, 0, 8))
        action_bar.pack(fill=tk.X)

        mode_frame = ttk.Frame(action_bar)
        mode_frame.pack(side=tk.RIGHT)
        ttk.Label(mode_frame, text="User deletion mode:").pack(side=tk.LEFT, padx=(0, 6))
        mode_combo = ttk.Combobox(
            mode_frame,
            state="readonly",
            values=["Move to Trash", "Delete immediately"],
            width=18,
        )
        mode_combo.pack(side=tk.LEFT)
        mode_combo.set("Move to Trash")

        def on_mode_change(event=None):
            val = mode_combo.get()
            self.delete_mode_var.set("trash" if val == "Move to Trash" else "delete")

        mode_combo.bind("<<ComboboxSelected>>", on_mode_change)

        content = ttk.Panedwindow(main_container, orient=tk.HORIZONTAL)
        content.pack(fill=tk.BOTH, expand=True)

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

        self.success_var = tk.StringVar(master=self, value="")
        self.success_label = ttk.Label(
            task_card,
            textvariable=self.success_var,
            style="CardTitle.TLabel",
            foreground="#1e7f3b",
            wraplength=700,
            justify=tk.LEFT,
        )
        self.success_label.pack(anchor="w", pady=(0, 10))

        notebook = ttk.Notebook(task_card)
        notebook.pack(fill=tk.BOTH, expand=True)

        sys_tab = ttk.Frame(notebook, padding=8)
        user_tab = ttk.Frame(notebook, padding=8)
        notebook.add(sys_tab, text="System (root)")
        notebook.add(user_tab, text=f"User ({self.username})")

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

        self.widgets["old_kernels"] = tk.Checkbutton(sys_tab, text="Remove old kernels (apt autoremove --purge) [size unknown]", variable=self.var_old_kernels, **checkbox_opts)
        self.widgets["old_kernels"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["old_kernels"] = "Remove old kernels (apt autoremove --purge) [size unknown]"
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

        self.widgets["flatpak_user_unused"] = tk.Checkbutton(user_tab, text="Flatpak user: uninstall unused [size unknown]", variable=self.var_flatpak_user, **checkbox_opts)
        self.widgets["flatpak_user_unused"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["flatpak_user_unused"] = "Flatpak user: uninstall unused [size unknown]"
        row += 1

        self.widgets["flatpak_repair_user"] = tk.Checkbutton(user_tab, text="Flatpak repair user [size unknown]", variable=self.var_flatpak_repair_user, **checkbox_opts)
        self.widgets["flatpak_repair_user"].grid(row=row, column=0, sticky="w", pady=4)
        self.base_text["flatpak_repair_user"] = "Flatpak repair user [size unknown]"

        right_actions = ttk.LabelFrame(right_panel, text="Actions", padding=10)
        right_actions.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(right_actions, text="1) Select categories  2) Preview  3) Clean", style="Hint.TLabel").pack(anchor="w", pady=(0, 8))

        ttk.Button(right_actions, text="Clean Selected", style="Primary.TButton", command=self.on_clean_clicked).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(right_actions, text="Preview Commands", command=self.on_preview).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(right_actions, text="Refresh Sizes", command=self.refresh_sizes).pack(fill=tk.X)

        log_card = ttk.LabelFrame(right_panel, text="Activity Log", padding=10)
        log_card.pack(fill=tk.BOTH, expand=True)
        self.log = ScrolledText(
            log_card,
            height=18,
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

    def on_category_toggle(self) -> None:
        """
        Refresh summary after a category checkbox click.
        """
        self._update_summary()

    def _category_vars(self) -> List[tk.BooleanVar]:
        """
        Return all category selection variables.
        """
        return [
            self.var_tmp, self.var_user_cache, self.var_thumbnails, self.var_trash,
            self.var_firefox, self.var_chrome, self.var_config_app_caches,
            self.var_dev_tool_caches, self.var_user_lang_tool_caches,
            self.var_python_artifacts, self.var_local_history,
            self.var_flatpak_user, self.var_flatpak_repair_user,
            self.var_flatpak_syscache, self.var_flatpak_repair_system, self.var_apt,
            self.var_journal, self.var_flatpak_app_cache, self.var_apt_cache,
            self.var_system_misc_caches, self.var_system_extra_caches, self.var_old_kernels,
        ]

    def _update_summary(self) -> None:
        """
        Update top summary line with selected categories and measurable cache size.
        """
        if not hasattr(self, "summary_var"):
            return

        selected_count = sum(1 for var in self._category_vars() if var.get())

        measurable_total = sum(self.sizes_before.values()) if self.sizes_before else 0
        self.summary_var.set(
            f"Selected categories: {selected_count} | Measurable cache footprint: {human_mb(measurable_total)}"
        )

    def _apply_autoselect_by_threshold(self, sizes_now: Dict[str, int]) -> None:
        """
        Auto select checkboxes whose measurable size exceeds AUTOCHECK_THRESHOLD_MB.
        Non measurable items are ignored.
        """
        if AUTOCHECK_THRESHOLD_MB <= 0:
            return
        threshold_bytes = int(AUTOCHECK_THRESHOLD_MB * 1024 * 1024)

        key_to_var = {
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

        for key, size in sizes_now.items():
            var = key_to_var.get(key)
            if var is not None and size >= threshold_bytes:
                var.set(True)

    def _apply_autodeselect_zero_or_unknown(self, sizes_now: Dict[str, int]) -> None:
        """
        Auto deselect all items that have size 0 or size unknown.
        Unknown size equals non measurable category or not present in sizes_now.
        """
        key_to_var_measurable = {
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
            "apt_cache": self.var_apt_cache,
        }
        for key, var in key_to_var_measurable.items():
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
        Recalculate sizes for all measurable categories.
        For root‑owned paths (apt_cache) we ask the helper.
        """
        # Standard measurable keys (user accessible)
        measurable = [
            "tmp", "user_cache", "thumbnails", "trash",
            "firefox", "chrome", "flatpak_syscache", "apt", "journal",
            "flatpak_app_cache", "config_app_caches", "dev_tool_caches",
            "user_lang_tool_caches", "python_artifacts", "local_history",
            "system_misc_caches", "system_extra_caches", "apt_cache"
        ]
        sizes_now: Dict[str, int] = {}
        root_measurable_keys = {
            "tmp", "flatpak_syscache", "apt", "journal",
            "system_misc_caches", "system_extra_caches", "apt_cache"
        }
        # Rediscover regeneratable dirs before measuring.
        self.patterns["python_artifacts"] = find_python_artifact_dirs()
        self.patterns["local_history"] = find_local_history_dirs()
        for key in measurable:
            patterns = self.patterns.get(key, [])
            if key in root_measurable_keys:
                try:
                    sizes_now[key] = HELPER.get_size_of_patterns(patterns)
                except Exception:
                    sizes_now[key] = 0
            else:
                sizes_now[key] = size_of_patterns(patterns)

        # Auto select by threshold
        self._apply_autoselect_by_threshold(sizes_now)

        # Auto deselect 0 MB or unknown
        self._apply_autodeselect_zero_or_unknown(sizes_now)

        # Update checkbox texts for measurable items
        for key, size in sizes_now.items():
            if key in self.widgets and key in self.base_text:
                try:
                    if size == 0:
                        text = f"{self.base_text[key]}  [0 MB]"
                    else:
                        text = f"{self.base_text[key]}  [{human_mb(size)}]"
                    self.widgets[key].configure(text=text)
                except tk.TclError:
                    pass

        # Keep non measurable labels as is (they already have "[size unknown]")
        non_measurable = ["flatpak_user_unused", "flatpak_repair_user", "flatpak_repair_system",
                          "old_kernels"]
        for key in non_measurable:
            if key in self.widgets and key in self.base_text:
                try:
                    self.widgets[key].configure(text=self.base_text[key])
                except tk.TclError:
                    pass

        self.sizes_before = sizes_now
        self._update_summary()
        log_append(self.log, "Sizes refreshed (APT cache size obtained via helper).")

    # ----------------------------- Plan and execution -----------------------------

    def build_plan(self) -> dict:
        """
        Build a plan from selected checkboxes for user deletions, user commands and root actions.

        :return: Dict with user_py_deletes, user_cmds, root_rm_patterns and root_cmds.
        """
        plan = {"user_py_delete": [], "user_cmds": [], "root_rm_patterns": [], "root_cmds": []}

        # User deletions
        if self.var_user_cache.get():
            plan["user_py_delete"].append("~/.cache/*")
        if self.var_thumbnails.get():
            plan["user_py_delete"].append("~/.thumbnails/*")
        if self.var_trash.get():
            plan["user_py_delete"].append("~/.local/share/Trash/*")
        if self.var_firefox.get():
            plan["user_py_delete"] += [
                "~/.mozilla/firefox/*.default*/cache2/*",
                "~/.cache/mozilla/firefox/*.default*/cache2/*",
            ]
        if self.var_chrome.get():
            plan["user_py_delete"] += [
                "~/.config/google-chrome/Default/Cache/*",
                "~/.cache/google-chrome/Default/Cache/*",
                "~/.config/chromium/Default/Cache/*",
                "~/.cache/chromium/Default/Cache/*",
            ]
        # Flatpak app cache
        if self.var_flatpak_app_cache.get():
            plan["user_py_delete"].append("~/.var/app/*/cache/*")
        if self.var_config_app_caches.get():
            plan["user_py_delete"] += self.patterns.get("config_app_caches", [])
        if self.var_dev_tool_caches.get():
            plan["user_py_delete"] += self.patterns.get("dev_tool_caches", [])
        if self.var_user_lang_tool_caches.get():
            plan["user_py_delete"] += self.patterns.get("user_lang_tool_caches", [])
        if self.var_python_artifacts.get():
            # Rediscover at plan time so paths stay current.
            artifacts = find_python_artifact_dirs()
            self.patterns["python_artifacts"] = artifacts
            plan["user_py_delete"] += artifacts
        if self.var_local_history.get():
            history_dirs = find_local_history_dirs()
            self.patterns["local_history"] = history_dirs
            plan["user_py_delete"] += history_dirs

        # User commands
        if self.var_flatpak_user.get():
            if exists_in_path("flatpak"):
                plan["user_cmds"].append("flatpak uninstall --unused -y")
            else:
                log_append(self.log, "Note: flatpak not found, skipping user flatpak uninstall.")
        if self.var_flatpak_repair_user.get():
            if exists_in_path("flatpak"):
                plan["user_cmds"].append("flatpak repair --user -y")
            else:
                log_append(self.log, "Note: flatpak not found, skipping user flatpak repair.")

        # Root deletions as patterns handled by helper
        if self.var_tmp.get():
            plan["root_rm_patterns"] += ["/tmp/*", "/var/tmp/*"]
        if self.var_flatpak_syscache.get():
            plan["root_rm_patterns"] += ["/var/tmp/flatpak-cache/*"]
        if self.var_apt_cache.get():
            plan["root_rm_patterns"] += ["/var/cache/apt/archives/*", "/var/cache/apt/archives/partial/*"]
        if self.var_system_misc_caches.get():
            plan["root_rm_patterns"] += self.patterns.get("system_misc_caches", [])
        if self.var_system_extra_caches.get():
            plan["root_rm_patterns"] += self.patterns.get("system_extra_caches", [])

        # Root commands
        if self.var_flatpak_repair_system.get():
            plan["root_cmds"].append("flatpak repair --system -y")
        if self.var_apt.get():
            plan["root_cmds"] += ["apt clean", "apt autoclean", "apt autoremove -y"]
        if self.var_journal.get():
            retention = self.journal_retention.get().strip() or "3d"
            plan["root_cmds"].append(f"journalctl --vacuum-time={shlex.quote(retention)}")
        if self.var_old_kernels.get():
            plan["root_cmds"].append("apt autoremove --purge -y")

        return plan

    def on_preview(self) -> None:
        """
        Show a preview of planned actions.
        """
        plan = self.build_plan()
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
        Execute selected actions using user code and the single helper, then remeasure and log reclaimed sizes.
        No confirmation popup, progress only in log.
        """
        selected_keys = []
        if self.var_tmp.get(): selected_keys.append("tmp")
        if self.var_user_cache.get(): selected_keys.append("user_cache")
        if self.var_thumbnails.get(): selected_keys.append("thumbnails")
        if self.var_trash.get(): selected_keys.append("trash")
        if self.var_firefox.get(): selected_keys.append("firefox")
        if self.var_chrome.get(): selected_keys.append("chrome")
        if self.var_config_app_caches.get(): selected_keys.append("config_app_caches")
        if self.var_dev_tool_caches.get(): selected_keys.append("dev_tool_caches")
        if self.var_user_lang_tool_caches.get(): selected_keys.append("user_lang_tool_caches")
        if self.var_python_artifacts.get(): selected_keys.append("python_artifacts")
        if self.var_local_history.get(): selected_keys.append("local_history")
        if self.var_flatpak_syscache.get(): selected_keys.append("flatpak_syscache")
        if self.var_apt.get(): selected_keys.append("apt")
        if self.var_journal.get(): selected_keys.append("journal")
        if self.var_flatpak_app_cache.get(): selected_keys.append("flatpak_app_cache")
        if self.var_apt_cache.get(): selected_keys.append("apt_cache")
        if self.var_system_misc_caches.get(): selected_keys.append("system_misc_caches")
        if self.var_system_extra_caches.get(): selected_keys.append("system_extra_caches")

        sizes_before_local = {}
        root_measurable_keys = {
            "tmp", "flatpak_syscache", "apt", "journal",
            "system_misc_caches", "system_extra_caches", "apt_cache"
        }
        if "python_artifacts" in selected_keys:
            self.patterns["python_artifacts"] = find_python_artifact_dirs()
        if "local_history" in selected_keys:
            self.patterns["local_history"] = find_local_history_dirs()
        for k in selected_keys:
            if k in root_measurable_keys:
                patterns = self.patterns.get(k, [])
                if patterns:
                    sizes_before_local[k] = HELPER.get_size_of_patterns(patterns)
                else:
                    sizes_before_local[k] = self.sizes_before.get(k, 0)
            else:
                sizes_before_local[k] = size_of_patterns(self.patterns.get(k, []))

        plan = self.build_plan()
        if not (plan["user_py_delete"] or plan["user_cmds"] or plan["root_rm_patterns"] or plan["root_cmds"]):
            selected_count = sum(1 for var in self._category_vars() if var.get())
            log_append(self.log, f"[DEBUG] Selected category vars at clean click: {selected_count}")
            log_append(self.log, "[INFO] Nothing selected.")
            return

        log_append(self.log, "=== Cleanup started ===")
        if self.var_local_history.get():
            log_append(
                self.log,
                "[WARN] Local History (.history): editor timeline/history "
                "snapshots will be permanently removed.",
            )

        to_trash: List[str] = []
        to_delete_user: List[str] = []
        trash_root = os.path.abspath(os.path.expanduser("~/.local/share/Trash/"))

        for pat in plan["user_py_delete"]:
            expanded = os.path.expanduser(pat)
            if os.path.abspath(expanded).startswith(trash_root):
                to_delete_user.append(pat)
            else:
                if self.delete_mode_var.get() == "trash":
                    to_trash.append(pat)
                else:
                    to_delete_user.append(pat)

        if to_trash:
            log_append(self.log, "[User] Moving selected paths to Trash ...")
            moved, logtxt = trash_paths(to_trash)
            if logtxt.strip():
                log_append(self.log, logtxt)
            log_append(self.log, f"[User] Trashed entries: {moved}")
        if to_delete_user:
            log_append(self.log, "[User] Deleting selected paths ...")
            removed, logtxt = rm_paths(to_delete_user)
            if logtxt.strip():
                log_append(self.log, logtxt)
            log_append(self.log, f"[User] Removed entries: {removed}")

        for cmd in plan["user_cmds"]:
            log_append(self.log, f"[User] Running: {cmd}")
            p = subprocess.run(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if p.stdout.strip():
                log_append(self.log, p.stdout.strip())
            log_append(self.log, f"[User] Exit code: {p.returncode}")

        if plan["root_rm_patterns"]:
            log_append(self.log, "[Root] Deleting patterns with helper ...")
            rc, out = HELPER.rm_rf_patterns(plan["root_rm_patterns"])
            if out:
                log_append(self.log, out.strip())
            log_append(self.log, f"[Root] rm_rf exit code: {rc}")

        if plan["root_cmds"]:
            results = HELPER.run_root_cmds(plan["root_cmds"])
            for cmd, rc, out in results:
                log_append(self.log, f"[Root] Running: {cmd}")
                if out.strip():
                    log_append(self.log, out.strip())
                log_append(self.log, f"[Root] Exit code: {rc}")

        log_append(self.log, "Recalculating sizes after cleanup ...")
        reclaimed_total = 0
        for key in selected_keys:
            if key in root_measurable_keys:
                after = HELPER.get_size_of_patterns(self.patterns.get(key, []))
            else:
                after = size_of_patterns(self.patterns.get(key, []))
            before = sizes_before_local.get(key, 0)
            reclaimed = max(0, before - after)
            reclaimed_total += reclaimed
            log_append(self.log, f"[{key}] before {human_mb(before)}, after {human_mb(after)}, reclaimed {human_mb(reclaimed)}")

        self._log_cleanup_success(reclaimed_total, len(selected_keys))
        log_append(self.log, "Refreshing size view ...")
        self.refresh_sizes()
        log_append(self.log, "=== Cleanup finished ===")
        changed, msg = repair_user_hicolor_shadow()
        if changed:
            log_append(self.log, f"[FIX] {msg}")
        elif "shadow" not in msg:
            pass
        else:
            log_append(self.log, f"[OK] {msg}")

    def _log_cleanup_success(self, reclaimed_total_bytes: int, selected_count: int) -> None:
        """
        Write a clear success block after cleanup and update visible success text.

        :param reclaimed_total_bytes: Total reclaimed size in bytes.
        :param selected_count: Number of selected measurable categories.
        """
        reclaimed_text = human_mb(reclaimed_total_bytes)
        disk_free_now = shutil.disk_usage(os.path.expanduser("~")).free
        free_now_text = human_gb(disk_free_now)

        if reclaimed_total_bytes > 0:
            trophy_text = ""
            if reclaimed_total_bytes >= 500 * 1024 * 1024:
                trophy_text = " Trophy unlocked: Great cleanup!"

            self._show_timed_success_message(
                f"Cleanup completed. You reclaimed {reclaimed_text}. "
                f"Free space now: {free_now_text}.{trophy_text}"
            )
            log_append(self.log, "")
            log_append(self.log, "========================================")
            log_append(self.log, "CLEANUP SUCCESS")
            log_append(self.log, f"You reclaimed: {reclaimed_text}")
            log_append(self.log, f"Free space now: {free_now_text}")
            log_append(self.log, f"Processed measurable categories: {selected_count}")
            if trophy_text:
                log_append(self.log, trophy_text.strip())
            log_append(self.log, "Your system now has more free space.")
            log_append(self.log, "========================================")
            log_append(self.log, "")
        else:
            self._clear_success_message()
            log_append(self.log, "Cleanup completed, but no measurable space was reclaimed.")

    def _show_timed_success_message(self, message: str) -> None:
        """
        Show a green success message and hide it after 10 seconds.
        """
        if self.success_clear_after_id is not None:
            try:
                self.after_cancel(self.success_clear_after_id)
            except Exception:
                pass
            self.success_clear_after_id = None

        self.success_var.set(message)
        self.success_clear_after_id = self.after(10_000, self._clear_success_message)

    def _clear_success_message(self) -> None:
        """
        Clear the timed success message from the UI.
        """
        self.success_var.set("")
        self.success_clear_after_id = None

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
        _root = tk.Tk()
        _root.withdraw()
        maybe_prompt_nemo_setup(_root)
        maybe_prompt_desktop_setup(_root)
        _root.destroy()
        # Fix incomplete user hicolor overlays that hide Nemo toolbar icons.
        repair_user_hicolor_shadow()
        main()
