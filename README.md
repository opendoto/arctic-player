# ❄️ Arctic Player 🏔️

Arctic Player is a lightweight, terminal-based music player written in Python. It uses `mpv` as the audio backend via JSON-IPC and `curses` for the user interface, focusing on low resource usage and vi-like navigation.

<p align="center">
  <img src="assets/screenshot1.png" width="48%" alt="Arctic Player Main View">
  <img src="assets/screenshot2.png" width="48%" alt="Arctic Player Lyrics View">
</p>
<p align="center">
  <img src="assets/screenshot3.png" width="48%" alt="Arctic Player Zen Mode">
  <img src="assets/screenshot4.png" width="48%" alt="Arctic Player Help Menu">
</p>

## ⚠️ Project Status & Known Issues

This player is a work in progress (WIP) and not yet 100% finished. It was built as a utility and learning project, combining personal development with AI assistance. 

- **Lyrics Fetching:** The on-the-fly lyrics feature (via `lrclib.net`) is currently in beta. It may occasionally fail, time out, or fetch mismatched lyrics depending on the track's metadata tags.
- Expect occasional bugs or unhandled exceptions during unusual edge cases. Feel free to report issues or contribute.

## ✨ Features

- Dual-pane interface (File browser and Playlist).
- Vi-style keybindings for navigation.
- Native `mpv` IPC audio engine integration.
- On-the-fly lyrics integration.
- Zen Mode for a minimal, distraction-free UI.
- Local SQLite metadata caching (WAL mode) for fast directory loading.
- Playback modes: Sequence, Repeat One, Random.

## 🛠️ Dependencies

Ensure the following system packages are installed and available in your `$PATH`:

| Package | Purpose | Debian / Ubuntu | Arch Linux | Fedora | Void Linux |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Python 3.8+** | Runtime | `sudo apt install python3` | `sudo pacman -S python` | `sudo dnf install python3` | `sudo xbps-install -Syu python3` |
| **mpv** | Audio Engine | `sudo apt install mpv` | `sudo pacman -S mpv` | `sudo dnf install mpv` | `sudo xbps-install -Syu mpv` |
| **ffmpeg** | Metadata (`ffprobe`) | `sudo apt install ffmpeg` | `sudo pacman -S ffmpeg` | `sudo dnf install ffmpeg` | `sudo xbps-install -Syu ffmpeg` |

## 📦 Installation

Clone the repository and run the main script directly:

```bash
git clone https://github.com/opendoto/arctic-player.git
cd arctic-player
python3 src/arctic.py
```

## 🎮 Keybindings

**Navigation**
| Key | Action |
| :---: | :--- |
| `h` / `l` / `Left` / `Right` | Switch panel (Browser ↔ Playlist) |
| `j` / `k` / `Up` / `Down` | Navigate through files or tracks |
| `Enter` | Play selected track / Open directory |
| `:` | Search current view |

**Playback & Volume**
| Key | Action |
| :---: | :--- |
| `p` / `Space` | Toggle Play / Pause |
| `,` / `.` | Previous / Next track |
| `1` / `2` | Rewind / Fast Forward (10s) |
| `+` / `-` | Increase / Decrease Volume |
| `m` | Toggle Playback Mode (SEQ ➔ ONE ➔ RND) |

**UI & System**
| Key | Action |
| :---: | :--- |
| `3` | Toggle Lyrics view |
| `z` / `Esc` | Toggle Zen Mode |
| `?` | Toggle Help menu |
| `q` | Save state, cleanup, and Quit |

## ⚙️ Architecture & Data

- **Config Path:** `~/.config/arctic/` (XDG compliant).
- **Database:** `arctic.db` (SQLite) stores metadata for instant loads.
- **State:** `state.json` persists your volume and current playback mode.
