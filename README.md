# Mint Cleaner – Selective Temp & Cache Cleanup for Linux Mint

A modern GUI tool to clean temporary files, caches, and system leftovers on Linux Mint (and other Debian‑based distributions).  
Uses a single `pkexec` authentication at startup – no repeated password prompts.

## Features

- **Single authentication** – privileged helper runs with `pkexec`, one‑time password entry.
- **Live size analysis** – shows current MB usage for all measurable categories.
- **Auto‑select by threshold** – automatically ticks items larger than 100 MB (configurable).
- **Auto‑deselect** – untick items that are 0 MB or have unknown size.
- **User deletion mode** – choose **Move to Trash** (default) or **Delete immediately**.
- **Modern UI** – grouped categories (System / User), clear icons, and a detailed log area.
- **No confirmation popups** – all progress is shown directly in the log.

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
chmod +x mint-cleaner.py
./mint-cleaner.py
