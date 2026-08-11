#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Desktop shortcut creation.

The shortcut on the user's desktop is switched on and off by the checkbox in the
Integration menu, so there is exactly one place that controls it. A shortcut
created by an older version is refreshed on every start so it keeps pointing at
run.py and uses the generated application icon.
"""

import stat
from pathlib import Path

from paths import (
    DESKTOP_FILENAME,
    DESKTOP_TEMPLATE,
    MAIN_SCRIPT,
)
from ui.window_icon import WM_CLASS_PUBLISHED, desktop_icon_value

MINT_CLEANER_SCRIPT = MAIN_SCRIPT


def user_desktop_dir() -> Path:
    """
    Return the user's desktop directory.

    Reads XDG user-dirs when available and falls back to Desktop or Schreibtisch.

    @return Path Desktop directory path
    """
    config = Path.home() / ".config" / "user-dirs.dirs"
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("XDG_DESKTOP_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                if value.startswith("$HOME/"):
                    return Path.home() / value[len("$HOME/"):]
                if value == "$HOME":
                    return Path.home()
                return Path(value).expanduser()

    for name in ("Desktop", "Schreibtisch"):
        desktop = Path.home() / name
        if desktop.is_dir():
            return desktop

    return Path.home() / "Desktop"


DEFAULT_DESKTOP_TEMPLATE = (
    "[Desktop Entry]\n"
    "Version=1.0\n"
    "Type=Application\n"
    "Name=Mint Cleaner\n"
    "Comment=Selective temp and cache cleanup for Linux Mint\n"
    "Icon=edit-clear-symbolic\n"
    "Exec=python3 run.py\n"
    "Terminal=false\n"
    "Categories=Utility;System;\n"
    "StartupNotify=true\n"
    f"StartupWMClass={WM_CLASS_PUBLISHED}\n"
)


def build_desktop_entry_content() -> str:
    """
    Build the .desktop file contents with absolute Exec and Icon paths.

    StartupWMClass matches the WM_CLASS of the application window, so the panel
    shows a single taskbar entry with the correct icon.

    @return str Desktop entry definition
    """
    exec_line = f"Exec=python3 {MINT_CLEANER_SCRIPT}"
    icon_line = f"Icon={desktop_icon_value()}"
    wm_class_line = f"StartupWMClass={WM_CLASS_PUBLISHED}"

    if DESKTOP_TEMPLATE.is_file():
        template = DESKTOP_TEMPLATE.read_text(encoding="utf-8")
    else:
        template = DEFAULT_DESKTOP_TEMPLATE

    lines: list[str] = []
    has_wm_class = False
    for line in template.splitlines():
        if line.startswith("Exec="):
            lines.append(exec_line)
        elif line.startswith("Icon="):
            lines.append(icon_line)
        elif line.startswith("StartupWMClass="):
            lines.append(wm_class_line)
            has_wm_class = True
        else:
            lines.append(line)
    if not has_wm_class:
        lines.append(wm_class_line)

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def install_desktop_shortcut() -> tuple[bool, Path | None]:
    """
    Install the desktop shortcut on the user's desktop.

    @return tuple[bool, Path | None] Success flag and created shortcut path
    """
    try:
        desktop_dir = user_desktop_dir()
        desktop_dir.mkdir(parents=True, exist_ok=True)
        shortcut_path = desktop_dir / DESKTOP_FILENAME
        shortcut_path.write_text(build_desktop_entry_content(), encoding="utf-8")
        shortcut_path.chmod(
            shortcut_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        return True, shortcut_path
    except OSError:
        return False, None


def refresh_desktop_shortcut() -> bool:
    """
    Update an already created desktop shortcut when its content is outdated.

    Keeps shortcuts created by earlier versions working after the start script
    was renamed to run.py and gives them the generated application icon.

    @return bool True when the shortcut was rewritten
    """
    try:
        shortcut_path = user_desktop_dir() / DESKTOP_FILENAME
        if not shortcut_path.is_file():
            return False
        expected = build_desktop_entry_content()
        if shortcut_path.read_text(encoding="utf-8") == expected:
            return False
        shortcut_path.write_text(expected, encoding="utf-8")
        shortcut_path.chmod(
            shortcut_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        return True
    except OSError:
        return False


def desktop_shortcut_path() -> Path:
    """
    Return the path of the desktop shortcut.

    @return Path Shortcut location on the user's desktop
    """
    return user_desktop_dir() / DESKTOP_FILENAME


def desktop_shortcut_installed() -> bool:
    """
    Return True when the desktop shortcut currently exists.

    @return bool Installation state
    """
    return desktop_shortcut_path().is_file()


def remove_desktop_shortcut() -> bool:
    """
    Remove the desktop shortcut again.

    @return bool True when no shortcut is left behind
    """
    shortcut_path = desktop_shortcut_path()
    if not shortcut_path.exists():
        return True
    try:
        shortcut_path.unlink()
        return True
    except OSError:
        return False
