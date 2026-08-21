"""Shared filesystem paths for Mint Cleaner."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
RESOURCES_DIR = PROJECT_ROOT / "resources"

MAIN_SCRIPT = PROJECT_ROOT / "run.py"

# The .desktop launcher ships next to run.py. The PNG window icons in resources/
# are still rendered on first start by ui.window_icon.
DESKTOP_TEMPLATE = PROJECT_ROOT / "Mint-Cleaner.desktop"
DESKTOP_FILENAME = "Mint Cleaner.desktop"

ICON_BASENAME = "mint-cleaner"

# Written once the first-start setup ran, so the prompts appear only once.
INIT_FILE = PROJECT_ROOT / ".initialized"
