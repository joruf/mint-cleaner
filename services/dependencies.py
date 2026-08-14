#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Runtime dependency checks and optional installation for Mint Cleaner.

Uses only the Python standard library so it can run before tkinter is imported.
Missing required packages are offered for installation via apt (pkexec, sudo, or
direct when already root).
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass(frozen=True)
class Dependency:
    """
  Describe a runtime dependency with its check and Debian/Ubuntu package names.
    """

    name: str
    description: str
    packages: tuple[str, ...]
    check: Callable[[], bool]
    required: bool = True


def _check_tkinter() -> bool:
    """
    Return True when the current Python interpreter can import tkinter.

    @return bool
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import tkinter"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def _check_command(command: str) -> bool:
    """
    Return True when a command is available on PATH.

    @param command Command name to look up
    @return bool
    """
    return shutil.which(command) is not None


DEPENDENCIES: List[Dependency] = [
    Dependency(
        name="tkinter",
        description="Python GUI toolkit (python3-tk)",
        packages=("python3-tk",),
        check=_check_tkinter,
        required=True,
    ),
    Dependency(
        name="pkexec",
        description="PolicyKit privilege helper (policykit-1)",
        packages=("policykit-1",),
        check=lambda: _check_command("pkexec"),
        required=True,
    ),
    Dependency(
        name="gio",
        description="GLib file utilities for moving files to Trash (glib2.0-bin)",
        packages=("glib2.0-bin",),
        check=lambda: _check_command("gio"),
        required=False,
    ),
    Dependency(
        name="flatpak",
        description="Flatpak application manager",
        packages=("flatpak",),
        check=lambda: _check_command("flatpak"),
        required=False,
    ),
]


def missing_dependencies(required_only: bool = False) -> List[Dependency]:
    """
    Return dependencies whose check currently fails.

    @param required_only When True, include only required dependencies
    @return List[Dependency] Missing dependency definitions
    """
    missing: List[Dependency] = []
    for dep in DEPENDENCIES:
        if required_only and not dep.required:
            continue
        if not dep.check():
            missing.append(dep)
    return missing


def _unique_packages(deps: List[Dependency]) -> List[str]:
    """
    Collect unique apt package names from a list of dependencies.

    @param deps Dependency list
    @return List[str] Sorted unique package names
    """
    packages: set[str] = set()
    for dep in deps:
        packages.update(dep.packages)
    return sorted(packages)


def _ask_yes_no(title: str, message: str) -> bool:
    """
    Ask the user for confirmation without requiring tkinter.

    @param title Dialog title
    @param message Dialog body text
    @return bool True when the user agrees
    """
    if shutil.which("zenity"):
        try:
            result = subprocess.run(
                ["zenity", "--question", f"--title={title}", f"--text={message}", "--width=420"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return result.returncode == 0
        except OSError:
            pass

    print(f"\n{title}\n{message}\n", file=sys.stderr)
    while True:
        try:
            answer = input("Install missing packages now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print("Please answer y or n.", file=sys.stderr)


def _run_apt_install(packages: List[str]) -> tuple[bool, str]:
    """
    Install packages with apt-get using pkexec, sudo, or direct execution.

    @param packages Apt package names to install
    @return tuple[bool, str] Success flag and combined output or error text
    """
    if not packages:
        return True, ""

    if not shutil.which("apt-get"):
        return False, "apt-get was not found. Install the packages manually: " + ", ".join(packages)

    apt_cmd = ["apt-get", "install", "-y"] + packages

    prefixes: List[List[str]] = []
    if shutil.which("pkexec"):
        prefixes.append(["pkexec"])
    if shutil.which("sudo"):
        prefixes.append(["sudo"])
    prefixes.append([])

    output_parts: List[str] = []
    for prefix in prefixes:
        cmd = prefix + apt_cmd
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError as exc:
            output_parts.append(f"{' '.join(cmd)}: {exc}")
            continue

        if proc.stdout:
            output_parts.append(proc.stdout.strip())

        if proc.returncode == 0:
            return True, "\n".join(part for part in output_parts if part)

    return False, "\n".join(part for part in output_parts if part) or "apt-get install failed."


def install_dependencies(deps: List[Dependency]) -> tuple[bool, str]:
    """
    Install apt packages for the given dependencies.

    @param deps Dependencies to satisfy
    @return tuple[bool, str] Success flag and installer output
    """
    packages = _unique_packages(deps)
    return _run_apt_install(packages)


def ensure_runtime_dependencies() -> None:
    """
    Verify every dependency and install what is missing, showing the work in a window.

    The window is `bootstrap_ui`, shared with the other applications in this family, so what
    installing looks like is the same everywhere. It appears only when something is actually
    missing, and it shows the output of the package manager as it arrives — a yes/no box followed
    by four silent minutes tells nobody whether anything is happening.

    Exits the process with status 1 when a required dependency is still missing afterwards.
    """
    from bootstrap_ui import Need, ensure

    # The table's own checks come along: tkinter is tested in a subprocess, because importing it
    # here to find out would be the very thing that fails.
    needs = [
        Need(
            label=dependency.description,
            packages=tuple(dependency.packages),
            optional=not dependency.required,
            check=dependency.check,
        )
        for dependency in DEPENDENCIES
    ]

    if not ensure("Mint Cleaner", needs, force="--setup" in sys.argv):
        package_list = ", ".join(_unique_packages(missing_dependencies(required_only=True)))
        sys.stderr.write(
            "Mint Cleaner cannot start without its required components.\n"
            f"Try manually: sudo apt-get install {package_list}\n"
        )
        sys.exit(1)
