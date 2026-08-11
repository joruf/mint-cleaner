# Mint Cleaner – Selective Temp & Cache Cleanup for Linux Mint

A modern GUI tool to clean temporary files, caches, and system leftovers on Linux Mint (and other Debian‑based distributions).  
Uses a single `pkexec` authentication at startup – no repeated password prompts.

## Features

- **Single authentication** – privileged helper runs with `pkexec`, one‑time password entry.
- **Startup progress** – the size analysis runs in the background behind a modal
  progress bar that lists every category with its status (done / running / pending),
  the measured size, a step counter and a percentage. The window never freezes.
- **Live size analysis** – shows the current MB/GB usage of all measurable categories.
- **Auto‑select by threshold** – automatically ticks items larger than 100 MB (configurable).
- **Auto‑deselect** – untick items that are 0 MB or have unknown size.
- **User deletion mode** – **Delete immediately** (default) or **Move to Trash**;
  selectable in the window header. Trash contents are always
  emptied before new files are moved in, so moving to Trash really keeps them.
- **Background cleanup** – the cleanup runs with the same progress checklist
  (measure → delete → measure → read disk usage).
- **Projection before cleaning** – right after the startup analysis the table at
  the bottom shows, left to right: the free space you have now, how much the
  currently ticked categories would free, and how much free space would be
  available afterwards. It updates with every change of the selection.
- **Persistent result table** – after a cleanup the very same table switches to
  the measured values: free space before the cleanup, the space that was really
  freed on disk, and the free space available now. The row adds up, and the
  deleted data volume is reported next to it — the two differ when files went to
  the Trash or a process still holds deleted files open. Both tables are also
  written into the activity log, and the result stays visible until the next run.
- **Taskbar icon** – window and panel icon, generated as PNG without any image
  library (see `ui/window_icon.py`), plus `StartupWMClass` so the panel matches the launcher.
- **Modern UI** – grouped categories (System / User) in scrollable tabs and a detailed log area.
  **Clean Selected** is the only colored button and carries the broom glyph, so the
  main action is unmistakable next to the secondary ones.
- **No confirmation popups** – all progress is shown in the progress dialog and in the log.

## Requirements

- Linux with `pkexec` (part of `policykit-1`)
- Python 3.6+ with `tkinter` (`python3-tk` on Debian/Ubuntu/Mint)
- Tested on Linux Mint, but works on Ubuntu, Debian, and similar distributions.

On first start, Mint Cleaner checks required dependencies and offers to install
missing packages automatically via `apt` (using `pkexec` or `sudo`). Optional
components such as `gio` (Trash integration) and `flatpak` are installed only
when you agree to the prompt.

## What gets deleted?
System tasks (require root privileges)
- /tmp/* and /var/tmp/* – temporary files (safe to delete)
- APT cleanup – runs apt clean, apt autoclean, apt autoremove
(removes downloaded .deb packages, obsolete dependencies)
- APT package cache – /var/cache/apt/archives/* (all cached .deb files)
- General system caches:
  - /var/cache/fontconfig/* and /var/cache/man/*
  - /var/lib/apt/lists/* (recreated on apt update)
  - /var/lib/snapd/cache/* and /var/cache/snapd/*
  - /var/crash/* (old crash dumps)
- Additional system caches:
  - /var/cache/PackageKit/*, /var/cache/fwupd/*, /var/cache/ldconfig/*
  - /var/lib/systemd/coredump/* (old coredumps)
- Remove old kernels – apt autoremove --purge
(uninstalls older Linux kernels and headers, keeps the current one)
- System Flatpak cache – /var/tmp/flatpak-cache/*
- Flatpak repair system – flatpak repair --system -y
(repairs system Flatpak installations, removes broken references)
- Systemd journal vacuum – journalctl --vacuum-time=…
(configurable retention, e.g., 3d / 100M)

User tasks (run as your user)
- ~/.cache/* – application caches (browser caches, etc.)
  **Protected (never deleted):** `dconf` and other session dirs, plus `~/.icons`,
  `~/.themes`, and `~/.local/share/icons` — this prevents Cinnamon/Nemo icons
  from disappearing after a cleanup.
- ~/.thumbnails/* – thumbnail cache of the file manager
- ~/.local/share/Trash/* – your Trash folder (files you already deleted once)
- Flatpak application cache – ~/.var/app/*/cache/* (caches of Flatpak apps)
- Firefox cache – all profiles: ~/.mozilla/firefox/*.default*/cache2/* and ~/.cache/mozilla/firefox/*.default*/cache2/*
- Chrome / Chromium cache – default profile:
~/.config/google-chrome/Default/Cache/*, ~/.cache/google-chrome/Default/Cache/*,
~/.config/chromium/Default/Cache/*, ~/.cache/chromium/Default/Cache/*
- Additional app caches in ~/.config (safe cache-only paths):
  - VS Code / Cursor: Cache, CachedData, Code Cache, GPUCache, Service Worker CacheStorage
  - Google Chrome / Brave: Code Cache, GPUCache, ShaderCache, Service Worker CacheStorage
- Developer tool caches:
  - ~/.npm/_cacache/*, ~/.yarn/cache/*, ~/.yarn/berry/cache/*
  - ~/.pnpm-store/*, ~/.cargo/registry/cache/*, ~/.gradle/caches/*
- Language and package tool caches:
  - ~/.cache/pip/*, ~/.cache/pypoetry/*, ~/.cache/uv/*
  - ~/.cache/go-build/*, ~/.cache/node-gyp/*
  - ~/.cache/fontconfig/*, ~/.cache/mesa_shader_cache/*
- Python leftovers under home (safe to recreate):
  - `__pycache__` directories (bytecode cache)
  - `.venv` virtual environments
  (scans ~ with skip rules for caches/app data; does not remove an active interpreter venv)
- Editor Local History under home:
  - `.history` folders (VS Code / Cursor Local History extension)
  - Warning: deleting these removes timeline/history snapshots permanently
    (not recoverable from Git; extension only creates new snapshots going forward)
- Flatpak user: uninstall unused – flatpak uninstall --unused -y
(removes unused Flatpak runtimes and extensions)
- Flatpak repair user – flatpak repair --user -y
(repairs user‑level Flatpak installations)
    
## Usage

```bash
git clone https://github.com/joruf/mint-cleaner.git
cd mint-cleaner
python3 run.py
```

`run.py` is the entry point. The **Integration** menu switches the desktop
shortcut and the Nemo context menu entry on and off — ticking writes the entry,
unticking removes it again. Launchers created by older versions are updated
automatically. The application icons are rendered once into `resources/` (or
`~/.cache/mint-cleaner/` when the program directory is read-only).

### Menu

```
File                    Integration                  Help
  Clean Selected          [ ] Nemo context menu        About
  Preview Commands        [ ] Desktop shortcut         Developer
  Refresh Sizes
  ----------------
  Quit
```

The user deletion mode is set directly in the window header, not in the menu.

## Project structure

```
mint-cleaner/
├── run.py                          # Entry point; cleanup logic, GUI class and privileged helper
├── paths.py                        # Central path constants for resources, desktop file and markers
├── README.md                       # Project documentation
├── .gitignore                      # Git ignore rules for local and generated files
├── .gitattributes                  # Git line-ending and file attribute rules
│
├── ui/                             # Everything that draws or prompts
│   ├── __init__.py                 # Package marker
│   ├── window_icon.py              # Renders the PNG icon and applies _NET_WM_ICON
│   ├── progress_dialog.py          # Modal progress dialog for scan and cleanup jobs
│   ├── desktop_setup.py            # Creates and refreshes the desktop shortcut
│   └── nemo_setup.py               # Creates and refreshes the Nemo context menu action
│
├── services/                       # Logic without GUI
│   ├── __init__.py                 # Package marker
│   └── dependencies.py             # Checks runtime dependencies and offers apt install
│
├── resources/                      # Shipped and generated resources
│   ├── mint-cleaner.desktop        # Desktop entry template
│   └── mint-cleaner-*.png          # Icons, generated on first start (not in version control)
│
├── tests/                          # Unit tests, run with unittest or pytest
└── .github/workflows/              # CI and multi-OS matrix
```

## Testing

```bash
python3 -m unittest discover -s tests -v
```

CI runs the unit suite on Ubuntu 22.04/24.04 (Python 3.11 and 3.12) on every push and
pull request. **Windows is not supported** — this tool targets Linux Mint / Debian-based
systems only.

### Multi-OS matrix (local Linux host)

```bash
~/os-test-matrix/bin/test-project /path/to/mint-cleaner
~/os-test-matrix/bin/test-project "$PWD" --only ubuntu-2404
```

On-demand Linux runners: [`OS Matrix`](.github/workflows/os-matrix.yml).
Results: `~/os-test-matrix/results/`.
