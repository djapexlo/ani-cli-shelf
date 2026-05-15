# Contributing to ani-cli-shelf

Thanks for your interest in contributing! This is a small personal project but PRs and issues are welcome.

---

## Setup

```bash
git clone https://github.com/djapexlo/ani-cli-shelf
cd ani-cli-shelf
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .   # editable install — changes to app.py take effect immediately
pip install ruff   # for linting

ani-cli-shelf
```

**Dependencies outside Python:**
- [ani-cli](https://github.com/pystardust/ani-cli) — must be in `PATH`
- `mpv` or `vlc` — for playback
- `fzf` — must exist in `PATH` (ani-cli-shelf bypasses it but ani-cli checks for it)

---

## Before submitting a PR

**Run the linter and make sure it passes with no errors:**
```bash
ruff check app.py
```

If you introduce new style issues, fix them before opening the PR. The project follows ruff's default ruleset — no configuration file needed.

**Version bump:**
Increment `ANISHELF_VERSION` in `app.py` by `0.01` for every change session. It's at the top of the file:
```python
ANISHELF_VERSION = "0.76"
```

---

## Project structure

```
ani_shelf/
├── app.py              # Everything — Streamlit app, all tabs, all logic
├── ani-cli-print       # Modified ani-cli with --direct flag and anime_list.txt output
├── requirements.txt    # streamlit, httpx
├── thumbnails/         # Custom posters uploaded by the user (gitignored)
└── README.md
```

Runtime JSON files (`jikan_cache.json`, `favourites.json`, etc.) are created on first use and are gitignored — don't commit them.

---

## Architecture notes

- **Single-file app** — `app.py` is intentionally one file. It's long (~2900 lines) but searchable. Don't split it into modules unless there's a strong reason.
- **Session state keys** — all initialized in one block near the top (search for `ss("`). Add new keys there.
- **Jikan rate limits** — 3 req/s, 60 req/min. The `BATCH_SIZE` / `BATCH_DELAY` defaults in settings respect this. Don't add Jikan calls outside the existing batch flow.
- **`st.rerun()` scope** — inside a `@st.fragment`, `st.rerun()` only re-renders the fragment. Outside, it's a full page reload. The search tab is particularly sensitive — any full rerun resets the `global_search` widget.
- **ani-cli stores NEXT episode** in `ani-hsts`, not the last watched. All episode progress comparisons use strict `<` not `<=`.
- **Cross-platform paths** — default file paths are set via `_platform_default_paths()` which handles Linux, macOS, and Windows separately.

---

## What's in scope

- Bug fixes for any tab or feature
- New browse/filter modes in the Seasonal tab (Jikan has many endpoints)
- UX improvements to existing panels
- Performance improvements to Jikan fetching or caching

## What's out of scope

- Replacing ani-cli with a different backend
- Cloud sync or multi-user support
- A non-Streamlit frontend

---

## Reporting bugs

Open a GitHub issue with:
1. What you did
2. What you expected
3. What happened instead
4. Your OS and Python version (`python --version`)
5. Your Streamlit version (`pip show streamlit`)

If it's a crash, paste the full traceback from the terminal where you ran `streamlit run app.py`.
