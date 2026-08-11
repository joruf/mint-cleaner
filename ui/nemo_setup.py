#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Nemo context menu integration.

The action lives in ~/.local/share/nemo/actions/ and adds a Mint Cleaner entry
to the Nemo context menu. It is switched on and off by the checkbox in the
Integration menu, so there is exactly one place that controls it. An action that
was installed by an older version is refreshed on every start so it keeps
pointing at run.py.
"""

from pathlib import Path

from paths import MAIN_SCRIPT
from ui.window_icon import desktop_icon_value

NEMO_ACTIONS_DIR = Path.home() / ".local" / "share" / "nemo" / "actions"
ACTION_FILENAME = "mint-cleaner.nemo_action"
MINT_CLEANER_SCRIPT = MAIN_SCRIPT


def build_nemo_action_content() -> str:
    """
    Build the contents of the mint-cleaner.nemo_action file.

    @return str Nemo action definition for context menu integration
    """
    return (
        "[Nemo Action]\n"
        "Active=true\n"
        "Name=Mint Cleaner\n"
        "Comment=Starts Mint Cleaner for selective temp and cache cleanup\n"
        f"Exec=python3 {MINT_CLEANER_SCRIPT}\n"
        f"Icon={desktop_icon_value()}\n"
        "\n"
        "# Shown on right-click on folders or in an empty window\n"
        "Selection=any\n"
        "Extensions=dir;\n"
    )


def nemo_action_path() -> Path:
    """
    Return the path of the Nemo action file.

    @return Path Action file location
    """
    return NEMO_ACTIONS_DIR / ACTION_FILENAME


def nemo_action_installed() -> bool:
    """
    Return True when the Nemo context menu entry is currently installed.

    @return bool Installation state
    """
    return nemo_action_path().is_file()


def install_nemo_action() -> bool:
    """
    Install the Nemo action file in the user's actions directory.

    @return bool True when the action file was written successfully
    """
    try:
        NEMO_ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
        nemo_action_path().write_text(build_nemo_action_content(), encoding="utf-8")
        return True
    except OSError:
        return False


def remove_nemo_action() -> bool:
    """
    Remove the Nemo context menu entry again.

    @return bool True when no action file is left behind
    """
    action_path = nemo_action_path()
    if not action_path.exists():
        return True
    try:
        action_path.unlink()
        return True
    except OSError:
        return False


def refresh_nemo_action() -> bool:
    """
    Update an already installed Nemo action when its content is outdated.

    Keeps context menu entries created by earlier versions working after the
    start script was renamed to run.py.

    @return bool True when the action file was rewritten
    """
    action_path = nemo_action_path()
    if not action_path.is_file():
        return False

    expected = build_nemo_action_content()
    try:
        if action_path.read_text(encoding="utf-8") == expected:
            return False
        action_path.write_text(expected, encoding="utf-8")
        return True
    except OSError:
        return False
