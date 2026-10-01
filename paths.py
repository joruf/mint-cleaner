"""Shared filesystem paths for Mint Cleaner."""

import os
import platform
import sys
from pathlib import Path

#: ``True`` inside the single-file executable that ``build-exe.py`` produces.
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _project_root() -> Path:
    """
    Return the directory that holds run.py and resources/.

    The executable unpacks itself into a temporary directory on every start;
    its read-only data lies there in the same layout a checkout has. That
    directory is deleted on exit, so nothing may ever be written into it.

    @return Path Project root
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", "."))
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    """
    Return the per-user data directory the executable writes to.

    Not ~/.cache: the cleaner's own "User cache" category empties that.

    @return Path ``$XDG_DATA_HOME/mint-cleaner``
    """
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "mint-cleaner"


PROJECT_ROOT = _project_root()
RESOURCES_DIR = PROJECT_ROOT / "resources"

MAIN_SCRIPT = PROJECT_ROOT / "run.py"

# The .desktop launcher ships next to run.py. The PNG window icons in resources/
# are still rendered on first start by ui.window_icon.
DESKTOP_TEMPLATE = PROJECT_ROOT / "Mint-Cleaner.desktop"
DESKTOP_FILENAME = "Mint Cleaner.desktop"

ICON_BASENAME = "mint-cleaner"

# Written once the first-start setup ran, so the prompts appear only once.
INIT_FILE = (user_data_dir() if IS_FROZEN else PROJECT_ROOT) / ".initialized"


def executable() -> Path:
    """
    Return the single-file executable this process was started from.

    Only meaningful when IS_FROZEN is set; otherwise it is the Python interpreter.

    @return Path Absolute path of the running file
    """
    return Path(sys.executable).resolve()


def executable_name(version: str = "", build: int = 0) -> str:
    """
    Return the file name of the executable for this machine and version.

    ``build-exe.py`` writes the executable under this name. The version is part
    of it so a downloaded file says which one it is. Mint Cleaner is Linux only.

    @param version ``major.minor.patch``; defaults to this program's
    @param build The build number; defaults to this program's
    @return str For example ``mint-cleaner-linux-x86_64-0.1.9-build24``
    """
    if not version:
        from version import current

        found = current()
        version, build = found.name, int(found.build or 0)
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return "mint-cleaner-linux-{0}-{1}-build{2}".format(machine, version, build)


def child_environment(env: "dict | None" = None) -> dict:
    """
    Return the environment a child process should get.

    The executable runs with ``LD_LIBRARY_PATH`` - and, through PyInstaller's
    Tcl/Tk hook, ``TCL_LIBRARY`` and ``TK_LIBRARY`` - pointing into its unpacked
    files. Inherited, they make apt, pkexec, gio or flatpak load the program's
    libraries instead of their own, which ends anywhere between odd warnings and
    crashes. Every entry that points into the unpacked files is removed;
    ``LD_LIBRARY_PATH`` gets the value it had before the program started.

    @param env The environment to clean; defaults to this process's
    @return dict A new dict; a plain copy outside the executable
    """
    source = os.environ if env is None else env
    bundle = getattr(sys, "_MEIPASS", "")
    if not IS_FROZEN or not bundle:
        return dict(source)
    clean = {}
    for key, value in source.items():
        if key.startswith("_PYI_") or key == "LD_LIBRARY_PATH_ORIG":
            continue
        if bundle in value:
            kept = [part for part in value.split(os.pathsep) if part and bundle not in part]
            if not kept:
                continue
            value = os.pathsep.join(kept)
        clean[key] = value
    original = source.get("LD_LIBRARY_PATH_ORIG")
    if original:
        clean["LD_LIBRARY_PATH"] = original
    return clean


def use_system_environment_for_children() -> None:
    """
    Start every child process with child_environment().

    One place instead of every ``subprocess`` call: the program starts dozens
    of tools, and a single forgotten ``env=`` would bring the problem back.
    Does nothing outside the executable.

    @return None
    """
    import subprocess

    if not IS_FROZEN or getattr(subprocess.Popen, "_mint_cleaner_clean_env", False):
        return

    class _SystemPopen(subprocess.Popen):  # type: ignore[misc, valid-type]
        """``Popen`` that never hands the bundled libraries to a child."""

        _mint_cleaner_clean_env = True

        def __init__(self, *args, **kwargs) -> None:
            kwargs["env"] = child_environment(kwargs.get("env"))
            super().__init__(*args, **kwargs)

    subprocess.Popen = _SystemPopen  # type: ignore[misc]
