# ani-cli-shelf 🎌

A local Streamlit frontend for **ani-cli** — browse your watch history, search anime, manage favourites and playlists, and launch episodes directly into your player. Metadata and posters via Jikan (MyAnimeList).

Current version: **v0.77**

---

## Credits

- **[ani-cli](https://github.com/pystardust/ani-cli)** by [pystardust](https://github.com/pystardust) and contributors — the CLI tool that powers all search and playback
- **[Jikan API](https://docs.api.jikan.moe)** — unofficial MyAnimeList REST API used for metadata, posters, scores, and reviews
- **[ani-cli-shelf](https://github.com/djapexlo/ani-cli-shelf)** — this project, by [djapexlo](https://github.com/djapexlo)

---

## Requirements

- Python 3.10+
- [ani-cli](https://github.com/pystardust/ani-cli) installed and in PATH
- `mpv` (or `vlc`) for playback
- `fzf` in PATH (ani-cli-shelf bypasses it for search, but ani-cli checks for it on startup)

---

## Installation

```bash
git clone https://github.com/djapexlo/ani-cli-shelf
cd ani-cli-shelf
pip install .
```

Then launch from anywhere:

```bash
ani-cli-shelf
```

And open `http://localhost:8501` in your browser.

**Using a virtual environment (recommended):**

```bash
git clone https://github.com/djapexlo/ani-cli-shelf
cd ani-cli-shelf
python -m venv .venv && source .venv/bin/activate
pip install .
ani-cli-shelf
```

**Updating:**

```bash
cd ani-cli-shelf
git pull
pip install .
```

---

## Features

### Tabs
- **🔍 Search Online** — search via ani-cli-print in the background; results appear as cards with Jikan metadata fetched in batches
- **🌸 Seasonal** — browse airing, upcoming, top all-time, top TV, top movies; includes a Jikan search bar and pagination
- **⭐ Favourites** — starred titles with full metadata, always available even if not in current history
- **📺 History** — recent watch history from `ani-hsts`; cards show episode progress bars from mpv
- **📋 Playlists** — custom lists with reordering (↑/↓) and per-item remove
- **⚙ Settings** — all configuration lives here, auto-saved

### Cards
Each card shows a poster, score, genres, synopsis, and a progress bar (purple = in progress, green = complete) sourced from mpv's watch_later data.

Card actions:
- **♡** — toggle favourite
- **▶ Ep N** — launch episode immediately
- **☰** — open episode panel
- **🗑** — remove from history/playlist

### Episode panel
Expands below the card row. Contains:
- Trailer (above synopsis)
- Episode list from allanime API with watched episodes highlighted
- **✔ Mark Ep N watched** button — writes directly to `ani-hsts` without needing to play
- Edit panel (✎) — change display title, search Jikan for top 5 candidates and pick which metadata to apply, upload custom thumbnail
- Playlist picker (＋ List)
- Links to MAL

### Full info panel (⊞)
Opened from the episode panel. Shows full stats, relations, reviews, and trailer below the Back button.

### Live playback status
The sidebar shows real-time output from ani-cli-print while an episode is loading — e.g. `Checking dependencies...`, `youtube Links Fetched`. Clears automatically once the player opens.

---

## Runtime files

| File | Description |
|---|---|
| `jikan_cache.json` | Jikan metadata cache — entries expire after 7 days |
| `favourites.json` | Starred titles with embedded card data |
| `playlists.json` | Named playlists |
| `overrides.json` | Per-title display name and Jikan search title overrides |
| `settings.json` | All user settings |
| `ep_cache.json` | Episode lists from allanime API |
| `.anicli_pid` | PID of last ani-cli-print process |
| `.anicli_log` | Live stderr from ani-cli-print (auto-deleted after playback) |
| `thumbnails/` | Custom poster images uploaded via the edit panel |

---

## Settings

All settings are in the ⚙ Settings tab — no need to edit `app.py`. Key options:

| Setting | Default | Description |
|---|---|---|
| Terminal | `kitty` | Terminal emulator for playback |
| Player | `mpv` | Video player (`mpv` or `vlc`) |
| History limit | `50` | How many recent titles to load from `ani-hsts` |
| DUB | off | Toggle in sidebar for instant switch |
| Quality | `best` | Stream quality passed to ani-cli |
| Jikan batch size/delay | 3 / 3.02s | Rate limiting for Jikan API |
| Light mode | off | Also writes `.streamlit/config.toml` (restart to apply to native Streamlit elements) |

---

## Notes

- **ani-cli stores the *next* episode**, not the last watched. All progress logic accounts for this.
- Metadata is shared across all tabs — favourites, playlists, and history all read from the same `jikan_cache.json`.
- If a card shows wrong metadata, use ✎ Edit in the episode panel to search Jikan and pick the correct entry.
- The sidebar DUB toggle saves instantly and applies to the next launch.
