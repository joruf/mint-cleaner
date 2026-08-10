"""Shared filesystem paths for Mint Cleaner."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RESOURCES_DIR = PROJECT_ROOT / "resources"

MAIN_SCRIPT = PROJECT_ROOT / "run.py"

# The .desktop template ships with the app. The icons that sit next to it are
# rendered on first start by ui.window_icon, so they are not in version control.
DESKTOP_TEMPLATE = RESOURCES_DIR / "mint-cleaner.desktop"
DESKTOP_FILENAME = "Mint Cleaner.desktop"

ICON_BASENAME = "mint-cleaner"

# Written once the first-start setup ran, so the prompts appear only once.
INIT_FILE = PROJECT_ROOT / ".initialized"
