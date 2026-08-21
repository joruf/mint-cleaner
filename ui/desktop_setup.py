#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Desktop shortcut creation.

The shortcut on the user's desktop is switched on and off by the checkbox in the
Integration menu, so there is exactly one place that controls it. The project
``.desktop`` file locates ``run.py`` via ``%k``. A symlink on the desktop keeps
that true.
"""

import stat
from pathlib import Path

from paths import (
    DESKTOP_FILENAME,
    DESKTOP_TEMPLATE,
    MAIN_SCRIPT,
    RESOURCES_DIR,
)
from ui.window_icon import WM_CLASS_PUBLISHED

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


def _install_theme_icon() -> None:
    """
    Copy the SVG into the user hicolor theme as ``mint-cleaner``.

    Icon names in ``.desktop`` files cannot be relative paths, so the file is
    installed under the standard theme directory.
    """
    dest_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        src = RESOURCES_DIR / "mint-cleaner.svg"
        if src.is_file():
            (dest_dir / "mint-cleaner.svg").write_bytes(src.read_bytes())
    except OSError:
        pass


DEFAULT_DESKTOP_TEMPLATE = (
    "[Desktop Entry]\n"
    "Version=1.0\n"
    "Type=Application\n"
    "Name=Mint Cleaner\n"
    "Comment=Selective temp and cache cleanup for Linux Mint\n"
    "Icon=mint-cleaner\n"
    'Exec=/bin/bash -c "cd \\\\$(dirname \\\\$(readlink -f \\\\$0)) && exec python3 ./run.py" %k\n'
    "Terminal=false\n"
    "Categories=Utility;System;\n"
    "StartupNotify=true\n"
    f"StartupWMClass={WM_CLASS_PUBLISHED}\n"
)


def build_desktop_entry_content() -> str:
    """
    Return the project desktop entry, with StartupWMClass kept in sync.

    Exec stays relative (``%k`` plus ``./run.py``). The icon name ``mint-cleaner``
    is resolved from the user icon theme after the SVG is installed.

    @return str Desktop entry definition
    """
    wm_class_line = f"StartupWMClass={WM_CLASS_PUBLISHED}"

    if DESKTOP_TEMPLATE.is_file():
        template = DESKTOP_TEMPLATE.read_text(encoding="utf-8")
    else:
        template = DEFAULT_DESKTOP_TEMPLATE

    lines: list[str] = []
    has_wm_class = False
    for line in template.splitlines():
        if line.startswith("StartupWMClass="):
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
        if not DESKTOP_TEMPLATE.is_file():
            return False, None
        _install_theme_icon()
        desktop_dir = user_desktop_dir()
        desktop_dir.mkdir(parents=True, exist_ok=True)
        shortcut_path = desktop_dir / DESKTOP_FILENAME
        if shortcut_path.is_symlink() or shortcut_path.exists():
            shortcut_path.unlink()
        shortcut_path.symlink_to(DESKTOP_TEMPLATE.resolve())
        DESKTOP_TEMPLATE.chmod(
            DESKTOP_TEMPLATE.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        return True, shortcut_path
    except OSError:
        return False, None


def refresh_desktop_shortcut() -> bool:
    """
    Replace a leftover copy with a symlink to the project desktop file.

    @return bool True when the shortcut was rewritten
    """
    try:
        shortcut_path = user_desktop_dir() / DESKTOP_FILENAME
        if not shortcut_path.exists() and not shortcut_path.is_symlink():
            return False
        expected = DESKTOP_TEMPLATE.resolve()
        if shortcut_path.is_symlink() and shortcut_path.resolve() == expected:
            return False
        _install_theme_icon()
        if shortcut_path.is_symlink() or shortcut_path.exists():
            shortcut_path.unlink()
        shortcut_path.symlink_to(expected)
        DESKTOP_TEMPLATE.chmod(
            DESKTOP_TEMPLATE.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
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
    if not shortcut_path.exists() and not shortcut_path.is_symlink():
        return True
    try:
        shortcut_path.unlink()
        return True
    except OSError:
        return False
