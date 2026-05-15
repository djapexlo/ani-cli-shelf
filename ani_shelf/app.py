import streamlit as st
import hashlib
import httpx
import json
import os
import re
import subprocess
import sys
import time
import base64
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG  —  defaults, overridden by settings.json
# ═══════════════════════════════════════════════════════════════════════════════
APP_DIR        = Path(__file__).parent
ANISHELF_VERSION = "0.88"
CACHE_FILE     = APP_DIR / "jikan_cache.json"
FAV_FILE       = APP_DIR / "favourites.json"
OVERRIDES_FILE = APP_DIR / "overrides.json"
SETTINGS_FILE  = APP_DIR / "settings.json"
PLAYLISTS_FILE = APP_DIR / "playlists.json"
THUMB_DIR      = APP_DIR / "thumbnails"
THUMB_DIR.mkdir(exist_ok=True)
JIKAN_CACHE_TTL_DAYS = 7   # re-fetch metadata older than this

def _platform_default_paths() -> dict:
    """Return platform-appropriate default paths for ani-cli and mpv."""
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return {
            "history_file":        str(home / ".local" / "state" / "ani-cli" / "ani-hsts"),
            "mpv_history_file":    str(appdata / "mpv" / "watch_history.jsonl"),
            "mpv_watch_later_dir": str(appdata / "mpv" / "watch_later"),
        }
    elif sys.platform == "darwin":
        state = home / "Library" / "Application Support"
        return {
            "history_file":        str(home / ".local" / "state" / "ani-cli" / "ani-hsts"),
            "mpv_history_file":    str(state / "mpv" / "watch_history.jsonl"),
            "mpv_watch_later_dir": str(state / "mpv" / "watch_later"),
        }
    else:  # Linux / POSIX
        state = home / ".local" / "state"
        return {
            "history_file":        str(state / "ani-cli" / "ani-hsts"),
            "mpv_history_file":    str(state / "mpv" / "watch_history.jsonl"),
            "mpv_watch_later_dir": str(state / "mpv" / "watch_later"),
        }

_PATHS = _platform_default_paths()

DEFAULTS = {
    "history_file":        _PATHS["history_file"],
    "terminal":            "kitty",
    "history_limit":       3,
    "search_result_limit": 30,
    "light_mode":          False,
    "dub":                 False,
    "player":              "mpv",
    "quality":             "best",
    "sub_lang":            "English",
    "title_prefer":        "available",
    "jikan_batch_size":    3,
    "jikan_batch_delay":   3.02,
    "mpv_history_file":    _PATHS["mpv_history_file"],
    "mpv_watch_later_dir": _PATHS["mpv_watch_later_dir"],
}
# These are overridden at runtime from cfg after settings load
BATCH_SIZE  = 3
BATCH_DELAY = 3.02

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# ── MPV progress helpers ──────────────────────────────────────────────────────
def _mpv_url_hash(url: str) -> str:
    """Compute the filename mpv uses for watch_later entries (md5 uppercase)."""
    return hashlib.md5(url.encode()).hexdigest().upper()

@st.cache_data(ttl=30)   # re-read at most every 30 seconds
def _load_mpv_history(mpv_history_file: str) -> dict:
    """
    Parse watch_history.jsonl → {(anime_title, ep_num): url}
    Title format in mpv: "Anime Name Episode N"
    """
    path = Path(mpv_history_file)
    result = {}
    if not path.exists():
        return result
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            title_raw = entry.get("title", "")
            url       = entry.get("path", "")
            if not title_raw or not url.startswith("http"):
                continue
            # Parse "Anime Name Episode N" or "Anime NameEpisode N" (no space)
            m = re.match(r'^(.+?)\s*[Ee]pisode\s+(\d+(?:\.\d+)?)$', title_raw)
            if m:
                anime = m.group(1).strip()
                ep    = m.group(2)
                result[(anime, ep)] = url
        except Exception:
            continue
    return result

def get_episode_progress(anime_title: str, ep_num: str | int,
                         duration_secs: int, cfg: dict) -> float | None:
    """
    Returns progress 0.0-1.0, or None if episode never opened.
    - 1.0   = completed (in watch_history, no watch_later file)
    - 0-1.0 = in progress (watch_later file with start= position)
    - 0.5   = in progress but duration unknown (no duration_secs)
    - None  = never opened
    """
    history = _load_mpv_history(cfg.get("mpv_history_file",
                                         DEFAULTS["mpv_history_file"]))
    ep_str = str(ep_num)
    url = history.get((anime_title, ep_str))
    if url is None:
        title_lower = anime_title.lower()
        for (a, e), u in history.items():
            if e != ep_str:
                continue
            a_lower = a.lower()
            # Require the shorter title to match at a word boundary in the longer
            short, long = (title_lower, a_lower) if len(title_lower) <= len(a_lower) \
                          else (a_lower, title_lower)
            pattern = r'(?<![a-z0-9])' + re.escape(short) + r'(?![a-z0-9])'
            if re.search(pattern, long):
                url = u
                break
    if url is None:
        return None   # never watched

    wl_dir  = Path(cfg.get("mpv_watch_later_dir", DEFAULTS["mpv_watch_later_dir"]))
    wl_file = wl_dir / _mpv_url_hash(url)
    if wl_file.exists():
        content = wl_file.read_text()
        m = re.search(r'start=([0-9.]+)', content)
        if m and duration_secs:
            pos = float(m.group(1))
            return min(pos / duration_secs, 1.0)
        return 0.5   # in progress but duration unknown
    else:
        return 1.0   # completed

def sort_items(items: list[dict], sort_by: str, cache: dict) -> list[dict]:
    """Sort a list of {title, episode, ...} dicts by the given sort key."""
    def score_key(item):
        m = cache.get(item["title"])
        return float((m or {}).get("score") or 0)
    if sort_by == "A→Z":
        return sorted(items, key=lambda h: h["title"].lower())
    if sort_by == "Z→A":
        return sorted(items, key=lambda h: h["title"].lower(), reverse=True)
    if sort_by == "Score↓":
        return sorted(items, key=score_key, reverse=True)
    if sort_by == "Score↑":
        return sorted(items, key=score_key)
    return items   # "Recent" = original order (history file order)

def get_settings() -> dict:
    saved = load_json(SETTINGS_FILE, {})
    return {**DEFAULTS, **saved}

def save_settings(s: dict):
    save_json(SETTINGS_FILE, s)

# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY
# ═══════════════════════════════════════════════════════════════════════════════
def _clean_title(raw: str) -> str:
    return re.sub(r'\s*\(\d+\s+episodes?\)\s*$', '', raw, flags=re.IGNORECASE).strip()

def read_history(cfg: dict) -> list[dict]:
    path  = Path(cfg["history_file"])
    limit = int(cfg["history_limit"])
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    items, seen = [], set()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        parts  = line.split("\t")
        raw    = parts[2] if len(parts) >= 3 else parts[-1]
        title  = _clean_title(raw)
        raw_id = parts[1] if len(parts) >= 3 else ""
        try:
            episode = int(parts[0]) if parts[0].strip().isdigit() else 1
        except Exception:
            episode = 1
        if title and title not in seen:
            seen.add(title)
            items.append({"title": title, "episode": episode, "raw_id": raw_id})
        if len(items) >= limit:
            break
    return items

def mark_episode_watched(title: str, ep_num: int, raw_id: str, cfg: dict):
    """Write/update an entry in ani-hsts marking ep_num as watched (stores next ep)."""
    path    = Path(cfg["history_file"])
    next_ep = ep_num + 1
    new_line = f"{next_ep}\t{raw_id}\t{title}"
    lines = path.read_text().splitlines() if path.exists() else []
    kept  = [ln for ln in lines
             if _clean_title((ln.split("\t") + ["", "", ""])[2]) != title]
    kept.append(new_line)
    path.write_text("\n".join(kept) + "\n")

def delete_from_history(title: str, cfg: dict):
    path = Path(cfg["history_file"])
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    kept  = [ln for ln in lines
             if _clean_title((ln.split("\t") + [""])[2] if len(ln.split("\t")) >= 3
                             else ln.split("\t")[-1]) != title]
    path.write_text("\n".join(kept) + ("\n" if kept else ""))

# ═══════════════════════════════════════════════════════════════════════════════
# ANI-CLI SEARCH  (non-interactive via fzf passthrough)
# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# ANI-CLI SEARCH  —  runs ani-cli-print which writes results to anime_list.txt
# before opening fzf. We read that file for the list, same format as history:
#   id TAB title (N episodes)
# ani-cli-print must be in the same folder as app.py.
# ═══════════════════════════════════════════════════════════════════════════════
ANICLI_PRINT = APP_DIR / "ani-cli-print"
ANIME_LIST   = APP_DIR / "anime_list.txt"
_FAKE_FZF    = APP_DIR / ".fake_fzf.sh"    # temp fzf shim that exits immediately

def _ensure_fake_fzf():
    """Write a tiny fzf shim that just exits — prevents any interactive picker."""
    if not _FAKE_FZF.exists():
        _FAKE_FZF.write_text("#!/bin/sh\nexit 1\n")
        _FAKE_FZF.chmod(0o755)

def anicli_search(query: str, prefer: str = "available", limit: int = 30) -> tuple[list[dict], str]:
    """
    Runs ani-cli-print fully in the background:
      1. A fake fzf shim is put first on PATH so the picker exits immediately
         after ani-cli-print writes anime_list.txt — no terminal window needed.
      2. We poll anime_list.txt until populated (max 15s).
      3. Parse id TAB title (N episodes) lines, strip episode counts.
    Returns (entries, debug_raw).
    """
    if not ANICLI_PRINT.exists():
        return [], f"ani-cli-print not found at {ANICLI_PRINT}"

    _ensure_fake_fzf()
    ANIME_LIST.write_text("")   # clear stale results

    # Build env with fake fzf first on PATH
    env = {
        **os.environ,
        "PATH":     f"{APP_DIR}:{os.environ.get('PATH', '')}",
        "NO_COLOR": "1",
        "TERM":     "dumb",
    }
    # Symlink fake_fzf as "fzf" in APP_DIR so it shadows the real fzf
    fake_link = APP_DIR / "fzf"
    if not fake_link.exists():
        fake_link.symlink_to(_FAKE_FZF)

    try:
        proc = subprocess.Popen(
            [str(ANICLI_PRINT), query],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Poll for file population — ani-cli-print writes before calling fzf
        deadline = time.time() + 15
        while time.time() < deadline:
            content = ANIME_LIST.read_text().strip()
            if content:
                break
            if proc.poll() is not None:
                break   # process exited
            time.sleep(0.3)
        # Kill the process — fzf already exited but parent may still be waiting
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception as e:
        return [], f"Error running ani-cli-print: {e}"
    finally:
        # Remove the fzf symlink so real fzf is used when launching anime
        try:
            fake_link.unlink(missing_ok=True)
        except Exception:
            pass

    raw = ANIME_LIST.read_text()
    if not raw.strip():
        return [], "anime_list.txt was empty — check that ani-cli-print can reach the API"

    ansi_re = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][AB0-9]|\x1b[=>]|\r')
    entries, seen = [], set()
    for line in raw.splitlines():
        line = ansi_re.sub('', line).strip()
        if not line:
            continue
        parts = line.split("\t")
        # Format: allanime_id TAB title (N episodes)
        allanime_id = parts[0] if len(parts) >= 2 else ""
        raw_title   = parts[-1] if parts else line
        title = _clean_title(raw_title)
        if not title or title in seen:
            continue
        seen.add(title)
        entries.append({"display": title, "japanese": title, "english": None,
                        "allanime_id": allanime_id})

    query_words = [w.lower() for w in re.split(r'\W+', query) if len(w) > 2]
    if query_words:
        filtered = [e for e in entries
                    if any(w in e["display"].lower() for w in query_words)]
        entries = filtered if filtered else entries

    return entries[:limit], raw


def search_jikan_query(query: str, limit: int = 10) -> tuple[list[dict], str]:
    """Fallback: search Jikan directly by title."""
    try:
        r = httpx.get("https://api.jikan.moe/v4/anime",
                      params={"q": query, "limit": limit}, timeout=10)
        r.raise_for_status()
        results = []
        for d in r.json().get("data", []):
            results.append({"anicli_title": d["title"], **_parse_jikan(d)})
        return results, ""
    except Exception as e:
        return [], str(e)

@st.cache_data(ttl=300)
def fetch_jikan_seasonal(page: int = 1) -> tuple[list[dict], bool]:
    """Fetch currently airing anime. Returns (items, has_next_page)."""
    try:
        r = httpx.get("https://api.jikan.moe/v4/seasons/now",
                      params={"limit": 25, "page": page}, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = [_parse_jikan(d) for d in data.get("data", [])]
        has_next = data.get("pagination", {}).get("has_next_page", False)
        return items, has_next
    except Exception:
        return [], False

@st.cache_data(ttl=300)
def fetch_jikan_top(filter_type: str = "airing", page: int = 1) -> tuple[list[dict], bool]:
    """Fetch top anime. Returns (items, has_next_page).
    filter_type can be a filter value (airing, upcoming, bypopularity, favorite)
    or a type value prefixed with 'type:' (type:movie, type:tv).
    """
    try:
        params = {"limit": 25, "page": page}
        if filter_type.startswith("type:"):
            params["type"] = filter_type[5:]
        elif filter_type not in ("all", "bypopularity"):
            params["filter"] = filter_type
        r = httpx.get("https://api.jikan.moe/v4/top/anime", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = [_parse_jikan(d) for d in data.get("data", [])]
        has_next = data.get("pagination", {}).get("has_next_page", False)
        return items, has_next
    except Exception:
        return [], False


@st.cache_data(ttl=300)
def fetch_jikan_search_browse(query: str, page: int = 1) -> tuple[list[dict], bool]:
    """Browse Jikan by search query. Returns (items, has_next_page)."""
    try:
        r = httpx.get("https://api.jikan.moe/v4/anime",
                      params={"q": query, "limit": 25, "page": page,
                              "order_by": "score", "sort": "desc"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = [_parse_jikan(d) for d in data.get("data", [])]
        has_next = data.get("pagination", {}).get("has_next_page", False)
        return items, has_next
    except Exception:
        return [], False

@st.cache_data(ttl=3600)
def fetch_jikan_producers(query: str) -> list[dict]:
    """Search Jikan for studios/producers by name. Returns list of {id, name}."""
    try:
        r = httpx.get("https://api.jikan.moe/v4/producers",
                      params={"q": query, "limit": 10}, timeout=10)
        r.raise_for_status()
        return [{"id": d["mal_id"],
                 "name": d["titles"][0]["title"] if d.get("titles") else d.get("name", "?")}
                for d in r.json().get("data", [])]
    except Exception:
        return []

@st.cache_data(ttl=300)
def fetch_jikan_by_studio(producer_id: int, page: int = 1) -> tuple[list[dict], bool]:
    """Fetch anime by producer/studio ID, sorted by score."""
    try:
        r = httpx.get("https://api.jikan.moe/v4/anime",
                      params={"producers": producer_id, "limit": 25, "page": page,
                              "order_by": "score", "sort": "desc"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [_parse_jikan(d) for d in data.get("data", [])], \
               data.get("pagination", {}).get("has_next_page", False)
    except Exception:
        return [], False

@st.cache_data(ttl=3600)
def fetch_jikan_people(query: str) -> list[dict]:
    """Search Jikan for people (directors, authors) by name."""
    try:
        r = httpx.get("https://api.jikan.moe/v4/people",
                      params={"q": query, "limit": 10}, timeout=10)
        r.raise_for_status()
        return [{"id": d["mal_id"], "name": d["name"]}
                for d in r.json().get("data", [])]
    except Exception:
        return []

@st.cache_data(ttl=300)
def fetch_jikan_by_person(person_id: int, page: int = 1) -> tuple[list[dict], bool]:
    """Fetch anime associated with a person, sorted by score, paginated client-side."""
    try:
        r = httpx.get(f"https://api.jikan.moe/v4/people/{person_id}/anime", timeout=15)
        r.raise_for_status()
        seen, items = set(), []
        for entry in r.json().get("data", []):
            anime = entry.get("anime", {})
            if not anime or anime.get("mal_id") in seen:
                continue
            seen.add(anime["mal_id"])
            items.append(_parse_jikan(anime))
        items.sort(key=lambda x: x.get("score") or 0, reverse=True)
        page_size = 25
        start = (page - 1) * page_size
        return items[start:start + page_size], start + page_size < len(items)
    except Exception:
        return [], False

# ═══════════════════════════════════════════════════════════════════════════════
# JIKAN
# ═══════════════════════════════════════════════════════════════════════════════
def _parse_duration(dur_str: str) -> int:
    """Parse Jikan duration string to seconds. e.g. '24 min per ep' -> 1440."""
    if not dur_str:
        return 0
    total = 0
    m = re.search(r'(\d+)\s*hr', dur_str, re.IGNORECASE)
    if m:
        total += int(m.group(1)) * 3600
    m = re.search(r'(\d+)\s*min', dur_str, re.IGNORECASE)
    if m:
        total += int(m.group(1)) * 60
    m = re.search(r'(\d+)\s*sec', dur_str, re.IGNORECASE)
    if m:
        total += int(m.group(1))
    return total

def _parse_jikan(d: dict) -> dict:
    synopsis = (d.get("synopsis") or "")
    # Trailer — Jikan provides embed_url (YouTube iframe)
    trailer_url = (d.get("trailer") or {}).get("embed_url") or ""
    # Full genre/theme/demographic tag lists
    all_genres = [g["name"] for g in d.get("genres", [])]
    themes     = [g["name"] for g in d.get("themes", [])]
    demogs     = [g["name"] for g in d.get("demographics", [])]
    studios    = [s["name"] for s in d.get("studios", [])]
    aired      = (d.get("aired") or {}).get("string", "")
    return {
        "mal_id":        d["mal_id"],
        "title":         d["title"],
        "image":         d["images"]["jpg"]["large_image_url"],
        "score":         d.get("score"),
        "scored_by":     d.get("scored_by"),
        "rank":          d.get("rank"),
        "popularity":    d.get("popularity"),
        "members":       d.get("members"),
        "favorites":     d.get("favorites"),
        "episodes":      d.get("episodes"),
        "synopsis":      synopsis,
        "genres":        all_genres[:3],       # short list for card
        "all_genres":    all_genres,
        "themes":        themes,
        "demographics":  demogs,
        "studios":       studios,
        "source":        d.get("source", ""),
        "rating":        d.get("rating", ""),
        "status":        d.get("status", ""),
        "season":        d.get("season", ""),
        "year":          d.get("year"),
        "aired":         aired,
        "duration":      d.get("duration", ""),
        "duration_secs": _parse_duration(d.get("duration", "")),
        "trailer_url":   trailer_url,
        "type":          d.get("type", ""),
    }

@st.cache_data(ttl=300)
def fetch_jikan_reviews(mal_id: int) -> list[dict]:
    """Fetch reviews for an anime. Returns list of {score, review, reviewer}."""
    try:
        r = httpx.get(f"https://api.jikan.moe/v4/anime/{mal_id}/reviews",
                      params={"page": 1}, timeout=10)
        r.raise_for_status()
        reviews = []
        for rv in r.json().get("data", []):
            reviews.append({
                "reviewer": rv.get("user", {}).get("username", "Anonymous"),
                "score":    rv.get("score"),
                "review":   (rv.get("review") or "")[:600],
                "tags":     rv.get("tags", []),
            })
        return reviews
    except Exception:
        return []

@st.cache_data(ttl=300)
def fetch_jikan_relations(mal_id: int) -> list[dict]:
    """Fetch related anime (prequels, sequels, etc.)."""
    try:
        r = httpx.get(f"https://api.jikan.moe/v4/anime/{mal_id}/relations",
                      timeout=10)
        r.raise_for_status()
        relations = []
        for rel in r.json().get("data", []):
            rel_type = rel.get("relation", "")
            for entry in rel.get("entry", []):
                if entry.get("type") == "anime":
                    relations.append({
                        "relation": rel_type,
                        "title":    entry.get("name", ""),
                        "mal_id":   entry.get("mal_id"),
                    })
        return relations
    except Exception:
        return []


def _clean_search_title(title: str) -> list[str]:
    """
    Return a list of progressively simpler search strings to try.
    e.g. 'Berserk (2016)' -> ['Berserk (2016)', 'Berserk']
         'Some Show Season 3' -> ['Some Show Season 3', 'Some Show']
    """
    attempts = [title]
    # Strip trailing year like (2016)
    t = re.sub(r'\s*\(\d{4}\)\s*$', '', title).strip()
    if t and t != title:
        attempts.append(t)
    # Strip season suffix: " Season 3", " 2nd Season", ": 2-nensei-hen..."
    t2 = re.sub(r'\s*(Season\s*\d+|[0-9]+(?:st|nd|rd|th)\s*Season).*$', '',
                t or title, flags=re.IGNORECASE).strip()
    if t2 and t2 not in attempts:
        attempts.append(t2)
    # Take only first colon-separated part
    if ':' in (t2 or title):
        t3 = (t2 or title).split(':')[0].strip()
        if t3 and t3 not in attempts:
            attempts.append(t3)
    return attempts

def fetch_jikan(title: str) -> dict | None:
    for attempt in _clean_search_title(title):
        try:
            r = httpx.get("https://api.jikan.moe/v4/anime",
                          params={"q": attempt, "limit": 1}, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                result = _parse_jikan(data[0])
                result["_fetched_at"] = time.time()
                return result
        except Exception:
            pass
    return None

def fetch_jikan_candidates(query: str, limit: int = 5) -> list[dict]:
    """Fetch top N Jikan results for a query. Used in the edit panel picker."""
    try:
        r = httpx.get("https://api.jikan.moe/v4/anime",
                      params={"q": query, "limit": limit}, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        results = []
        for d in data:
            parsed = _parse_jikan(d)
            parsed["_fetched_at"] = time.time()
            results.append(parsed)
        return results
    except Exception:
        return []

def search_jikan_multi(entries: list[dict]) -> list[dict]:
    """
    entries: list of {japanese, english, display} from anicli_search.
    Looks up Jikan using Japanese title (best MAL match).
    Stores both anicli_title (display) and anicli_jp (japanese) so we can
    always launch with the Japanese title which ani-cli sources use internally.
    """
    results = []
    for i in range(0, len(entries), BATCH_SIZE):
        batch = entries[i:i+BATCH_SIZE]
        for e in batch:
            search_term = e.get("japanese") or e.get("english") or e["display"]
            meta  = fetch_jikan(search_term)
            entry = {
                "anicli_title":  e["display"],
                "anicli_jp":     e.get("japanese") or e["display"],
                "allanime_id":   e.get("allanime_id", ""),
            }
            entry.update(meta if meta else
                         {"title": e["display"], "image": "", "score": None,
                          "episodes": None, "synopsis": "", "genres": [], "status": ""})
            results.append(entry)
        if i + BATCH_SIZE < len(entries):
            time.sleep(BATCH_DELAY)
    return results

def get_metadata(titles: list[str], overrides: dict) -> dict:
    """
    Returns cached metadata immediately (may be incomplete).
    Missing entries are first seeded from favourites.json (free, no API call),
    then fetched from Jikan if still missing.
    """
    cache: dict = load_json(CACHE_FILE, {})

    # Expire stale entries
    cutoff = time.time() - JIKAN_CACHE_TTL_DAYS * 86400
    stale  = [t for t, v in cache.items()
              if isinstance(v, dict) and v.get("_fetched_at", 0) < cutoff]
    for t in stale:
        del cache[t]

    # Seed from favourites — if we already have the data there, use it
    favs = load_json(FAV_FILE, {})
    for title in titles:
        if title not in cache and title in favs:
            fav_data = favs[title]
            if fav_data.get("image"):   # only use if it has real metadata
                cache[title] = fav_data

    missing = [t for t in titles if t not in cache]

    if missing:
        if not st.session_state.get("history_jikan_pending"):
            # First pass — flag and rerun so cards render first, then we fetch
            st.session_state["history_jikan_pending"] = True
            st.rerun()
        else:
            # Second pass — actually fetch
            st.session_state["history_jikan_pending"] = False
            bar = st.progress(0, text="Fetching metadata from Jikan…")
            total = len(missing)
            for i in range(0, total, BATCH_SIZE):
                batch = missing[i:i+BATCH_SIZE]
                for j, title in enumerate(batch):
                    stitle = overrides.get(title, {}).get("search_title", title)
                    cache[title] = fetch_jikan(stitle)
                    bar.progress((i+j+1)/total, text=f"Fetching {i+j+1}/{total}…")
                if i + BATCH_SIZE < total:
                    time.sleep(BATCH_DELAY)
            save_json(CACHE_FILE, cache)
            bar.empty()

    return cache

# ═══════════════════════════════════════════════════════════════════════════════
# EPISODE LIST  —  fetches real available episodes from allanime (same as ani-cli)
# ═══════════════════════════════════════════════════════════════════════════════
EP_CACHE_FILE = APP_DIR / "ep_cache.json"   # {allanime_id: [ep_num_strings]}

def fetch_episodes_allanime(allanime_id: str, mode: str = "sub") -> list[str]:
    """
    Fetch real available episode list from allanime API — same query ani-cli uses.
    Returns sorted list of episode strings e.g. ["1","2","3","3.5",...].
    Results are cached in ep_cache.json.
    """
    if not allanime_id:
        return []
    ep_cache: dict = load_json(EP_CACHE_FILE, {})
    cache_key = f"{allanime_id}:{mode}"
    if cache_key in ep_cache:
        return ep_cache[cache_key]

    gql = ('query ($showId: String!) { show( _id: $showId ) '
           '{ _id availableEpisodesDetail }}')
    try:
        r = httpx.post(
            "https://api.allanime.day/api",
            headers={
                "Content-Type": "application/json",
                "Referer": "https://allmanga.to",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
            },
            json={"variables": {"showId": allanime_id}, "query": gql},
            timeout=10,
        )
        r.raise_for_status()
        detail = r.json()["data"]["show"]["availableEpisodesDetail"]
        # Try requested mode, fall back to sub, then dub
        raw_eps = detail.get(mode) or detail.get("sub") or detail.get("dub") or []
        eps = sorted(
            [str(e).strip('"') for e in raw_eps],
            key=lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0
        )
        ep_cache[cache_key] = eps
        save_json(EP_CACHE_FILE, ep_cache)
        return eps
    except Exception:
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# LAUNCH
# - When allanime_id is known: ani-cli-print --direct <id> <title> <ep>
#   skips all fzf/search and plays immediately.
# - Fallback (no id): plain ani-cli <title> -e <ep> — fzf opens for title pick.
# Both run in background; mpv/vlc open their own window.
# ═══════════════════════════════════════════════════════════════════════════════

def render_full_panel(display: str, meta: dict | None, key_prefix: str):
    """Full expanded panel: stats, all tags, relations, reviews. No episode list."""
    mal_id  = (meta or {}).get("mal_id")
    image   = (meta or {}).get("image", "")
    trailer = (meta or {}).get("trailer_url", "")

    # ── Header ────────────────────────────────────────────────────────────────
    h1, h2 = st.columns([1, 3])
    with h1:
        if image:
            st.image(image, width='stretch')
    with h2:
        st.markdown(f"### {display}")
        # Return to normal panel button
        if st.button("◀ Back to episodes", key=f"full_back_{key_prefix}",
                     width="stretch"):
            st.session_state.expanded_full = None
            st.session_state.expanded      = key_prefix
            st.rerun()

        if trailer:
            ft_key = f"_show_trailer_full_{key_prefix}"
            if st.session_state.get(ft_key):
                st.iframe(trailer, height=300)
                if st.button("✕ Hide trailer", key=f"_hide_trailer_full_{key_prefix}"):
                    st.session_state[ft_key] = False
                    st.rerun()
            else:
                if st.button("▶ Show trailer", key=f"_show_trailer_full_btn_{key_prefix}",
                             width='stretch'):
                    st.session_state[ft_key] = True
                    st.rerun()

    st.divider()

    # ── Stats grid ────────────────────────────────────────────────────────────
    def _fmt(val, suffix=""):
        if val is None:
            return "—"
        if isinstance(val, int) and val >= 1000:
            return f"{val:,}{suffix}"
        return f"{val}{suffix}"

    s1, s2, s3, s4 = st.columns(4)
    with s1:
        st.metric("Score",      _fmt((meta or {}).get("score")))
        st.metric("Ranked",     "#" + _fmt((meta or {}).get("rank")) if (meta or {}).get("rank") else "—")
    with s2:
        st.metric("Popularity", "#" + _fmt((meta or {}).get("popularity")) if (meta or {}).get("popularity") else "—")
        st.metric("Members",    _fmt((meta or {}).get("members")))
    with s3:
        st.metric("Episodes",   _fmt((meta or {}).get("episodes")))
        st.metric("Duration",   (meta or {}).get("duration") or "—")
    with s4:
        st.metric("Type",       (meta or {}).get("type") or "—")
        st.metric("Source",     (meta or {}).get("source") or "—")

    st.divider()

    # ── Details row ───────────────────────────────────────────────────────────
    d1, d2, d3 = st.columns(3)
    with d1:
        st.caption("**Status**")
        st.write((meta or {}).get("status") or "—")
        st.caption("**Aired**")
        st.write((meta or {}).get("aired") or "—")
    with d2:
        st.caption("**Season**")
        season = (meta or {}).get("season", "")
        year   = (meta or {}).get("year")
        st.write(f"{season.title()} {year}" if season and year else year or season or "—")
        st.caption("**Rating**")
        st.write((meta or {}).get("rating") or "—")
    with d3:
        st.caption("**Studios**")
        studios = (meta or {}).get("studios", [])
        st.write(", ".join(studios) if studios else "—")
        st.caption("**Favorites**")
        st.write(_fmt((meta or {}).get("favorites")))

    # ── All tags ──────────────────────────────────────────────────────────────
    all_tags = (
        (meta or {}).get("all_genres", []) +
        (meta or {}).get("themes", []) +
        (meta or {}).get("demographics", [])
    )
    if all_tags:
        st.divider()
        st.caption("**Genres / Themes / Demographics**")
        st.markdown(
            " ".join(f'<span class="genre-tag" style="font-size:0.72rem;padding:2px 8px">'
                     f'{t}</span>' for t in all_tags),
            unsafe_allow_html=True)

    # ── Relations ─────────────────────────────────────────────────────────────
    if mal_id:
        relations = fetch_jikan_relations(mal_id)
        if relations:
            st.divider()
            st.caption("**Related Anime**")
            for rel in relations:
                mid = rel.get("mal_id")
                url = f"https://myanimelist.net/anime/{mid}" if mid else "#"
                st.markdown(
                    f"<span style='color:{MUTED};font-size:0.72rem'>{rel['relation']}: </span>"
                    f"<a href='{url}' target='_blank' style='color:{ACCENT};font-size:0.72rem'>"
                    f"{rel['title']}</a>",
                    unsafe_allow_html=True)

    # ── Reviews ───────────────────────────────────────────────────────────────
    if mal_id:
        reviews = fetch_jikan_reviews(mal_id)
        if reviews:
            st.divider()
            st.caption("**Reviews** — 1 positive · 1 mixed · 1 negative")
            # Sort by score desc, pick top, middle, bottom
            scored = [r for r in reviews if r.get("score") is not None]
            scored.sort(key=lambda x: x["score"], reverse=True)
            picks = []
            if scored:
                picks.append(("🟢 Positive", scored[0]))
            if len(scored) >= 3:
                picks.append(("🟡 Mixed", scored[len(scored)//2]))
            if len(scored) >= 2:
                picks.append(("🔴 Critical", scored[-1]))
            for label, rv in picks:
                with st.expander(f"{label} — {rv['reviewer']} · {rv['score']}/10"):
                    st.markdown(
                        f"<div style='font-size:0.75rem;line-height:1.6;color:{MUTED}'>"
                        f"{rv['review']}</div>",
                        unsafe_allow_html=True)
                    if rv.get("tags"):
                        st.caption(" · ".join(rv["tags"]))
        else:
            st.divider()
            st.caption("No reviews available on MAL for this title yet.")

_ANICLI_PID_FILE = APP_DIR / ".anicli_pid"
_ANICLI_LOG_FILE = APP_DIR / ".anicli_log"

def _kill_previous_anicli():
    try:
        if _ANICLI_PID_FILE.exists():
            pid = int(_ANICLI_PID_FILE.read_text().strip())
            os.killpg(os.getpgid(pid), 9)
    except Exception:
        pass
    try:
        _ANICLI_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def launch_anime(title: str, episode: int | str | None, cfg: dict,
                 allanime_id: str = "") -> str:
    """Launch anime and return a status message describing what's happening."""
    extra = []
    if cfg.get("dub"):
        extra += ["--dub"]
    if cfg.get("player") == "vlc":
        extra += ["-v"]
    q = cfg.get("quality", "best")
    if q != "best":
        extra += ["-q", q]

    if allanime_id and episode is not None:
        # Use ani-cli-print --direct: bypasses fzf, but still resolves stream URL
        cmd = ([str(APP_DIR / "ani-cli-print")]
               + extra
               + ["--direct", allanime_id, str(title), str(episode)])
        status = (f"Resolving stream for ep {episode}… "
                  f"This may take a few seconds before {cfg.get('player', 'mpv')} opens.")
    else:
        # Fallback: plain ani-cli, fzf opens in background (hidden)
        ep_flags = ["-e", str(episode)] if episode is not None else []
        cmd = ["ani-cli"] + ep_flags + extra + [str(title)]
        status = (f"Searching for ep {episode} via ani-cli… "
                  f"Stream URL is being resolved, please wait.")

    _kill_previous_anicli()
    # Clear log and pipe stderr so the sidebar status widget can tail it live
    log_fh = open(_ANICLI_LOG_FILE, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=log_fh,
        start_new_session=True,
    )
    log_fh.close()  # parent closes its handle; child keeps writing
    try:
        _ANICLI_PID_FILE.write_text(str(proc.pid))
    except Exception:
        pass
    return status

# ═══════════════════════════════════════════════════════════════════════════════
# THUMBNAIL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def get_thumb_path(title: str) -> Path | None:
    safe = re.sub(r'[^\w\-]', '_', title)
    for ext in ("jpg","jpeg","png","webp"):
        p = THUMB_DIR / f"{safe}.{ext}"
        if p.exists():
            return p
    return None

def save_thumb(title: str, uploaded) -> Path:
    safe = re.sub(r'[^\w\-]', '_', title)
    ext  = uploaded.name.rsplit(".", 1)[-1].lower()
    dest = THUMB_DIR / f"{safe}.{ext}"
    dest.write_bytes(uploaded.read())
    return dest

def img_to_data_url(path: Path) -> str:
    ext = path.suffix.lstrip(".")
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:image/{ext};base64,{b64}"

# ═══════════════════════════════════════════════════════════════════════════════
# CARD HTML  —  2:3 aspect ratio, synopsis in body
# ═══════════════════════════════════════════════════════════════════════════════
def _short(text: str, limit: int = 80) -> str:
    text = text.replace("\n", " ").strip()
    return text[:limit] + "…" if len(text) > limit else text

def card_html(meta: dict | None, display_title: str, is_fav: bool,
              last_ep: int | None = None, custom_thumb: str | None = None,
              progress: float | None = None) -> str:
    score    = (meta or {}).get("score")
    genres   = list((meta or {}).get("genres", []))
    image    = custom_thumb or (meta or {}).get("image", "")
    synopsis = _short((meta or {}).get("synopsis") or "", 80)

    score_html = (f'<span class="score-pill">★ {score}</span>'
                  if score else '<span class="score-pill invis">★–</span>')

    genre_tags = [f'<span class="genre-tag">{g}</span>' for g in genres[:3]]
    while len(genre_tags) < 3:
        genre_tags.append('<span class="genre-tag invis">–</span>')

    # Progress bar — sits between poster and card body, always visible
    if progress is None:
        prog_overlay = ""
    elif progress >= 0.98:
        prog_overlay = '<div class="prog-bar-wrap"><div class="prog-fill prog-done" style="width:100%"></div></div>'
    else:
        pct = int(progress * 100)
        prog_overlay = f'<div class="prog-bar-wrap"><div class="prog-fill" style="width:{pct}%"></div></div>'

    img_block = (
        f'<div class="poster-wrap"><img class="poster" src="{image}" /></div>'
        if image else
        '<div class="poster-wrap"><div class="poster-placeholder">🎌</div></div>'
    )

    fav_star  = '<span class="fav-badge">⭐</span>' if is_fav else ''
    ep_badge  = (f'<span class="watched-badge">EP {last_ep}</span>'
                 if last_ep else '')
    synopsis_block = (f'<div class="card-synopsis">{synopsis}</div>'
                      if synopsis else '<div class="card-synopsis invis">…</div>')

    return f"""
<div class="anime-card">
  {fav_star}{ep_badge}
  {img_block}
  {prog_overlay}
  <div class="card-body">
    <div class="anime-title" title="{display_title}">{display_title}</div>
    <div class="pill-row">{score_html}{"".join(genre_tags)}</div>
    {synopsis_block}
  </div>
</div>"""

# ═══════════════════════════════════════════════════════════════════════════════
# EPISODE PANEL  (shared between history and search)
# ═══════════════════════════════════════════════════════════════════════════════
def render_episode_panel(title: str, display: str, episode: int,
                         meta: dict | None, cfg: dict, key_prefix: str,
                         allanime_id: str = "", show_edit: bool = True):
    synopsis = (meta or {}).get("synopsis", "")
    image    = (meta or {}).get("image", "")

    # Header: poster + synopsis, edit button top-right
    hc1, hc2 = st.columns([1, 3])
    with hc1:
        if image:
            st.image(image, width='stretch')
    with hc2:
        mal_id = (meta or {}).get("mal_id")
        th1, th2, th3, th4, th5 = st.columns([4, 1, 1, 1, 1])
        with th1:
            st.markdown(f"**{display}**")
            score  = (meta or {}).get("score")
            genres = (meta or {}).get("genres", [])
            if score:
                st.caption(f"★ {score}  ·  " + "  ".join(genres))
        with th2:
            if show_edit:
                mod_open = st.session_state.modify_open == key_prefix
                if st.button("✎ Edit", key=f"ep_edit_{key_prefix}",
                             width='stretch'):
                    st.session_state.modify_open = None if mod_open else key_prefix
                    st.session_state.playlist_picker = None
                    st.rerun()
        with th3:
            pl_open = st.session_state.playlist_picker == key_prefix
            if st.button("＋ List", key=f"ep_pl_{key_prefix}",
                         width='stretch'):
                st.session_state.playlist_picker = None if pl_open else key_prefix
                st.session_state.modify_open = None
                st.rerun()
        with th4:
            if mal_id:
                # Regular button styled same as others, opens MAL in new tab via JS
                if st.button("🔗 MAL", key=f"ep_mal_{key_prefix}", width='stretch'):
                    st.markdown(
                        f"<script>window.open('https://myanimelist.net/anime/{mal_id}','_blank');</script>",
                        unsafe_allow_html=True)
        with th5:
            full_open = st.session_state.expanded_full == key_prefix
            if st.button("⊞", key=f"ep_expand_{key_prefix}", width='stretch'):
                if full_open:
                    st.session_state.expanded_full = None
                else:
                    st.session_state.expanded_full = key_prefix
                    st.session_state.expanded = None  # close normal panel
                    st.rerun()
        # Trailer embed if available
        trailer_url = (meta or {}).get("trailer_url", "")
        if trailer_url:
            t_key = f"_show_trailer_{key_prefix}"
            if st.session_state.get(t_key):
                st.iframe(trailer_url, height=280)
                if st.button("✕ Hide trailer", key=f"_hide_trailer_{key_prefix}"):
                    st.session_state[t_key] = False
                    st.rerun()
            else:
                if st.button("▶ Show trailer", key=f"_show_trailer_btn_{key_prefix}",
                             width='stretch'):
                    st.session_state[t_key] = True
                    st.rerun()

        if synopsis:
            st.markdown(
                f"<div style='font-size:0.78rem;color:#b0b0d0;line-height:1.5'>"
                f"{synopsis}</div>", unsafe_allow_html=True)

    # ── Inline playlist picker ────────────────────────────────────────────────
    if st.session_state.playlist_picker == key_prefix:
        playlists = _load_playlists()
        card_data_for_pl = {"anicli_title": title, "anicli_jp": title,
                            "allanime_id": allanime_id, **(meta or {})}
        st.markdown('<div class="modify-panel">', unsafe_allow_html=True)
        st.markdown("**＋ Add to playlist**")
        if playlists:
            st.caption("Choose a playlist:")
            for pl_name, pl_items in playlists.items():
                already_in = title in pl_items
                btn_label  = f"{'✓ ' if already_in else ''}{pl_name}"
                if st.button(btn_label, key=f"pl_pick_{key_prefix}_{pl_name}",
                             width='stretch'):
                    if already_in:
                        remove_from_playlist(pl_name, title)
                        st.toast(f"Removed from '{pl_name}'", icon="❌")
                    else:
                        add_to_playlist(pl_name, title, card_data_for_pl)
                        st.toast(f"Added to '{pl_name}'", icon="✅")
                        st.rerun()
        else:
            st.caption("No playlists yet — create one below.")

        st.divider()
        # Create new playlist
        new_pl_name = st.text_input("New playlist name", key=f"new_pl_{key_prefix}",
                                     placeholder="e.g. Weekend watchlist")
        if st.button("＋ Create & add", key=f"pl_create_{key_prefix}",
                     width='stretch'):
            if new_pl_name.strip():
                add_to_playlist(new_pl_name.strip(), title, card_data_for_pl)
                st.session_state.playlist_picker = None
                st.toast(f"Created '{new_pl_name.strip()}' and added {display}", icon="✅")
                st.rerun()
            else:
                st.warning("Enter a playlist name first.")
        if st.button("✗ Close", key=f"pl_close_{key_prefix}", width='stretch'):
            st.session_state.playlist_picker = None
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    if show_edit and st.session_state.modify_open == key_prefix:
        load_json(OVERRIDES_FILE, {}).get(key_prefix.replace("r","").replace("c",""), {})
        s_title_val = load_json(OVERRIDES_FILE, {}).get(title, {}).get("search_title", title)
        st.markdown('<div class="modify-panel">', unsafe_allow_html=True)
        st.markdown(f"**✎ Edit — {display}**")
        new_display = st.text_input("Display title", value=display,
                                     key=f"ep_mod_disp_{key_prefix}")

        # ── Jikan search with candidate picker ───────────────────────────────
        st.caption("Jikan metadata")
        with st.form(key=f"ep_mod_srch_form_{key_prefix}"):
            srch_c1, srch_c2 = st.columns([4, 1])
            with srch_c1:
                new_search = st.text_input("Jikan search title", value=s_title_val,
                                            key=f"ep_mod_srch_{key_prefix}",
                                            label_visibility="collapsed",
                                            placeholder="Search Jikan…")
            with srch_c2:
                do_search = st.form_submit_button("🔍", use_container_width=True,
                                                   help="Search Jikan for top 5 results")

        cand_key = f"_jikan_cands_{key_prefix}"
        if do_search and new_search.strip():
            with st.spinner("Searching Jikan…"):
                st.session_state[cand_key] = fetch_jikan_candidates(new_search.strip())

        candidates = st.session_state.get(cand_key, [])
        if candidates:
            st.caption("Pick a result to apply its metadata:")
            for ci, cand in enumerate(candidates):
                cc1, cc2, cc3 = st.columns([1, 5, 2])
                with cc1:
                    if cand.get("image"):
                        st.image(cand["image"], width=50)
                with cc2:
                    year  = cand.get("year") or ""
                    score = f"★{cand['score']}" if cand.get("score") else ""
                    st.markdown(
                        f"<div style='font-size:0.78rem;font-weight:600'>{cand.get('title','?')}</div>"
                        f"<div style='font-size:0.68rem;color:#888'>{year}  {score}</div>",
                        unsafe_allow_html=True)
                with cc3:
                    if st.button("✔ Use this", key=f"ep_mod_pick_{key_prefix}_{ci}",
                                  width='stretch'):
                        # Write chosen metadata to cache, update search_title override
                        cd = load_json(CACHE_FILE, {})
                        cd[title] = cand
                        save_json(CACHE_FILE, cd)
                        od = load_json(OVERRIDES_FILE, {})
                        od.setdefault(title, {})
                        od[title]["display_title"] = new_display.strip() or title
                        od[title]["search_title"]  = cand.get("title", title)
                        save_json(OVERRIDES_FILE, od)
                        st.session_state.pop(cand_key, None)
                        st.session_state.modify_open = None
                        st.toast(f"Applied metadata: {cand.get('title','?')}", icon="✅")
                        st.rerun()
                st.divider()
            if st.button("✗ Clear results", key=f"ep_mod_clrcands_{key_prefix}"):
                st.session_state.pop(cand_key, None)
                st.rerun()

        # ── Thumbnail + save/cancel ───────────────────────────────────────────
        uploaded = st.file_uploader("Custom thumbnail",
                                     type=["jpg","jpeg","png","webp"],
                                     key=f"ep_mod_thumb_{key_prefix}")
        ec1, ec2, ec3 = st.columns(3)
        with ec1:
            if st.button("💾 Save", key=f"ep_mod_save_{key_prefix}",
                         width='stretch'):
                od = load_json(OVERRIDES_FILE, {})
                od.setdefault(title, {})
                od[title]["display_title"] = new_display.strip() or title
                od[title]["search_title"]  = new_search.strip() or title
                save_json(OVERRIDES_FILE, od)
                if uploaded:

                    save_thumb(title, uploaded)
                if new_search.strip() != s_title_val:
                    cd = load_json(CACHE_FILE, {})
                    cd.pop(title, None)
                    save_json(CACHE_FILE, cd)
                st.session_state.modify_open = None
                st.session_state.pop(cand_key, None)
                st.toast("Saved!", icon="💾")
                st.rerun()
        with ec2:
            if st.button("✗ Cancel", key=f"ep_mod_cancel_{key_prefix}",
                         width='stretch'):
                st.session_state.modify_open = None
                st.session_state.pop(cand_key, None)
                st.rerun()
        with ec3:
            thumb_p = get_thumb_path(title)
            if thumb_p and st.button("🖼 Clear thumb",
                                      key=f"ep_mod_clrthumb_{key_prefix}",
                                      width='stretch'):
                thumb_p.unlink()
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    st.divider()

    # Fetch real episode list from allanime if we have the ID
    mode = "dub" if cfg.get("dub") else "sub"
    real_eps: list[str] = []
    if allanime_id:
        with st.spinner("Loading episode list…"):
            real_eps = fetch_episodes_allanime(allanime_id, mode=mode)

    # ani-cli stores NEXT episode to watch in history, so episode N means
    # episodes 1..(N-1) are watched, episode N is next up (not yet watched).
    # Use strict < so the "next" episode shows as unwatched.
    if real_eps:
        cap_c1, cap_c2 = st.columns([3, 2])
        with cap_c1:
            st.caption(
                f"{len(real_eps)} episodes available · "
                f"next up: **Ep {episode}** · click to launch")
        with cap_c2:
            if st.button(f"✔ Mark Ep {episode} watched", key=f"mw_{key_prefix}",
                         width='stretch'):
                mark_episode_watched(title, episode, allanime_id, cfg)
                st.toast(f"Marked Ep {episode} as watched", icon="✔")
                st.rerun()
        EP_ROW = 12
        for ep_start in range(0, len(real_eps), EP_ROW):
            ep_cols = st.columns(EP_ROW)
            for ep_i, ep_str in enumerate(real_eps[ep_start:ep_start + EP_ROW]):
                try:
                    ep_num = float(ep_str)
                    watched = ep_num < float(episode)   # strict <, not <=
                except ValueError:
                    watched = False
                btn_key = f"ep_{key_prefix}_{ep_start + ep_i}"
                with ep_cols[ep_i]:
                    if watched:
                        st.markdown(
                            f"<style>.w-{btn_key} button{{"
                            f"background:#e040fb22!important;"
                            f"border-color:#e040fb88!important;"
                            f"color:#e040fb!important;}}</style>"
                            f'<div class="w-{btn_key}">',
                            unsafe_allow_html=True)
                    label = f"{'✓' if watched else ''}{ep_str}"
                    if st.button(label, key=btn_key, width='stretch'):
                        msg = launch_anime(title, ep_str, cfg, allanime_id=allanime_id)
                        st.toast(msg, icon="▶")
                    if watched:
                        st.markdown("</div>", unsafe_allow_html=True)
    else:
        # No allanime ID or fetch failed — fall back to Jikan count or manual
        ep_count = int(meta["episodes"]) if meta and meta.get("episodes") else None
        if ep_count:
            st.caption(
                f"{ep_count} episodes (from MAL) · "
                f"next up: **Ep {episode}** · "
                f"⚠ real availability unknown")
            EP_ROW = 12
            for ep_start in range(1, ep_count + 1, EP_ROW):
                ep_cols = st.columns(EP_ROW)
                for ep_i, ep_num in enumerate(
                        range(ep_start, min(ep_start + EP_ROW, ep_count + 1))):
                    watched = ep_num < episode   # strict <
                    btn_key = f"ep_{key_prefix}_{ep_num}"
                    with ep_cols[ep_i]:
                        if watched:
                            st.markdown(
                                f"<style>.w-{btn_key} button{{"
                                f"background:#e040fb22!important;"
                                f"border-color:#e040fb88!important;"
                                f"color:#e040fb!important;}}</style>"
                                f'<div class="w-{btn_key}">',
                                unsafe_allow_html=True)
                        if st.button(f"{'✓' if watched else ''}{ep_num}",
                                     key=btn_key, width='stretch'):
                            msg = launch_anime(title, ep_num, cfg, allanime_id=allanime_id)
                            st.toast(msg, icon="▶")
                            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.caption("Episode list unavailable — enter count manually:")
            manual_eps = st.number_input("Episode count", min_value=1,
                                         max_value=9999, value=24, step=1,
                                         key=f"meps_{key_prefix}")
            EP_ROW = 12
            for ep_start in range(1, int(manual_eps) + 1, EP_ROW):
                ep_cols = st.columns(EP_ROW)
                for ep_i, ep_num in enumerate(
                        range(ep_start, min(ep_start + EP_ROW, int(manual_eps) + 1))):
                    with ep_cols[ep_i]:
                        if st.button(str(ep_num), key=f"ep_{key_prefix}_{ep_num}",
                                     width='stretch'):
                            msg = launch_anime(title, ep_num, cfg, allanime_id=allanime_id)
                            st.toast(msg, icon="▶")

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE SETUP
# ═══════════════════════════════════════════════════════════════════════════════
st.set_page_config(page_title="ani-cli-shelf", page_icon="🎌",
                   layout="wide", initial_sidebar_state="expanded")

cfg = get_settings()
DARK = not cfg.get("light_mode", False)

# Keep .streamlit/config.toml in sync with saved light_mode setting
def _sync_streamlit_theme():
    light = cfg.get("light_mode", False)
    config_dir = APP_DIR / ".streamlit"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "config.toml"
    theme = "light" if light else "dark"
    config_file.write_text(
        f'[theme]\nbase="{theme}"\n'
        f'backgroundColor="{"#f4f4f8" if light else "#0d0d12"}"\n'
        f'secondaryBackgroundColor="{"#ffffff" if light else "#16161f"}"\n'
        f'textColor="{"#1a1a2e" if light else "#e8e6f0"}"\n'
        f'primaryColor="{"#9c27b0" if light else "#e040fb"}"\n'
    )
_sync_streamlit_theme()

# Override Jikan rate-limit settings from cfg (0 = no limit)
_bs = int(cfg.get("jikan_batch_size", 3))
_bd = float(cfg.get("jikan_batch_delay", 3.02))
BATCH_SIZE  = _bs  if _bs  > 0 else 9999
BATCH_DELAY = _bd  if _bd  > 0 else 0

# Palette
if DARK:
    BG, SURFACE, BORDER, TEXT, MUTED, ACCENT = \
        "#0d0d12","#16161f","#252535","#e8e6f0","#7070a0","#e040fb"
else:
    BG, SURFACE, BORDER, TEXT, MUTED, ACCENT = \
        "#f4f4f8","#ffffff","#d0d0e0","#1a1a2e","#6060a0","#9c27b0"

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Noto+Sans+JP:wght@300;400;700&display=swap');

html, body, [class*="css"] {{
    font-family: 'Noto Sans JP', sans-serif;
    background: {BG} !important;
    color: {TEXT};
}}
h1,h2,h3 {{ font-family: 'Space Mono', monospace; color: {TEXT}; }}

/* Sidebar — narrow icon-only nav */
[data-testid="stSidebar"] {{
    background: {"#12121a" if DARK else "#ebebf5"} !important;
    border-right: 1px solid {BORDER};
    min-width: 72px !important;
    max-width: 72px !important;
}}
[data-testid="stSidebarContent"] {{
    padding: 0.75rem 0.4rem !important;
}}
[data-testid="stSidebar"] .stButton > button {{
    font-size: 1.5rem !important;
    padding: 4px 2px !important;
    min-height: 44px !important;
    max-height: 44px !important;
    border-radius: 8px;
    text-align: center;
    justify-content: center;
    line-height: 1 !important;
}}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] .stMarkdown {{
    font-size: 0.60rem;
    word-break: break-all;
}}
[data-testid="stSidebar"] .stTextInput input {{
    font-size: 0.70rem;
    text-align: center;
}}

/* Global search bar at top */
.global-search-wrap {{
    background: {SURFACE};
    border-bottom: 1px solid {BORDER};
    padding: 8px 0 6px;
    margin-bottom: 12px;
}}

/* ── Card  (2:3 ratio poster) ── */
.card-wrap {{ margin-bottom: 4px; }}
.anime-card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    overflow: hidden;
    position: relative;
    display: flex;
    flex-direction: column;
    transition: border-color 0.15s, box-shadow 0.15s;
    cursor: pointer;
}}
.anime-card:hover {{
    border-color: {ACCENT};
    box-shadow: 0 0 12px {ACCENT}33;
}}

/* Poster: slightly taller than 2:3 — use padding-top trick for compat */
.poster-wrap {{
    width: 100%;
    aspect-ratio: 2 / 3.2;
    overflow: hidden;
    flex-shrink: 0;
    background: {"#1a1a28" if DARK else "#dde"};
    position: relative;
}}
img.poster {{
    width: 100%; height: 100%;
    object-fit: cover; object-position: center top;
    display: block;
}}
.poster-placeholder {{
    width: 100%; height: 100%;
    display: flex; align-items: center; justify-content: center;
    font-size: 2.5rem;
}}
.card-body {{
    padding: 6px 8px 5px;
    display: flex; flex-direction: column;
}}
.anime-title {{
    font-family: 'Space Mono', monospace;
    font-size: 0.68rem; font-weight: 700; color: {TEXT};
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    margin-bottom: 3px;
}}
.pill-row {{
    display: flex; gap: 3px; flex-wrap: wrap;
    margin-bottom: 3px; align-items: center; min-height: 16px;
}}
.score-pill {{
    background: {ACCENT}22; color: {ACCENT}; border: 1px solid {ACCENT}55;
    border-radius: 10px; padding: 0px 5px; font-size: 0.58rem;
    font-family: 'Space Mono', monospace; white-space: nowrap;
}}
.genre-tag {{
    background: {"#1e1e30" if DARK else "#e0e0f0"};
    color: {"#9090c0" if DARK else "#5050a0"};
    border-radius: 3px; padding: 0px 4px; font-size: 0.56rem;
}}
.card-synopsis {{
    font-size: 0.62rem; color: {MUTED}; line-height: 1.4;
    overflow: hidden;
}}
.invis {{ opacity: 0; pointer-events: none; }}

.fav-badge {{
    position: absolute; top: 5px; right: 5px; font-size: 0.80rem;
    z-index: 1; text-shadow: 0 0 4px #0008;
}}
.watched-badge {{
    position: absolute; top: 5px; left: 5px;
    background: {ACCENT}cc; color: #fff; border-radius: 3px;
    padding: 0px 4px; font-size: 0.56rem; font-family: 'Space Mono', monospace;
    z-index: 1;
}}

/* Hide deploy button and toolbar, keep sidebar toggle always visible */
[data-testid="stToolbar"] {{ visibility: hidden; }}
[data-testid="stDecoration"] {{ display: none; }}
#MainMenu {{ visibility: hidden; }}
.stAppDeployButton {{ display: none; }}

/* Sidebar always visible — disable collapse entirely */
[data-testid="stSidebar"] {{
    display: flex !important;
    visibility: visible !important;
    transform: none !important;
    min-width: 72px !important;
    max-width: 72px !important;
}}
[data-testid="stSidebarCollapsedControl"],
[data-testid="stSidebarCollapseButton"],
[data-testid="stHeader"] [data-testid="stBaseButton-header"],
[data-testid="stHeader"] button[kind="header"] {{
    display: none !important;
}}
[data-testid="stSidebar"] .stButton > button {{
    font-size: 0.82rem !important;
    padding: 7px 10px !important;
    min-height: 36px !important;
    max-height: 36px !important;
    border-radius: 6px;
    text-align: left;
    justify-content: flex-start;
}}

/* ── Episode progress bar — sits between poster and card body ── */
.prog-bar-wrap {{
    width: 100%;
    height: 4px;
    background: {BORDER};
    flex-shrink: 0;
}}
.prog-fill {{
    height: 100%;
    background: {ACCENT};
}}
.prog-done {{ background: #4caf50; }}

/* ── Uniform card buttons ── */
.stButton > button {{
    background: transparent;
    border: 1px solid {BORDER};
    color: {MUTED};
    font-size: 0.66rem !important;
    padding: 3px 6px !important;
    line-height: 1.2 !important;
    border-radius: 4px;
    width: 100%;
    min-height: 26px !important;
    max-height: 26px !important;
    white-space: nowrap;
    transition: border-color 0.15s, color 0.15s;
}}
.stButton > button:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}

/* Zero gap between card action button columns */
div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {{
    padding-left: 1px !important;
    padding-right: 1px !important;
}}
/* Force ALL buttons to identical height — prevents tall icon-only buttons */
div[data-testid="stHorizontalBlock"] .stButton > button {{
    height: 26px !important;
    min-height: 26px !important;
    max-height: 26px !important;
    padding: 0 4px !important;
    line-height: 26px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}}
/* Remove Streamlit's default gap between columns */
div[data-testid="stHorizontalBlock"] {{
    gap: 2px !important;
}}

/* Episode buttons slightly smaller */
.ep-btn button {{
    min-height: 22px !important;
    max-height: 22px !important;
    font-size: 0.62rem !important;
    padding: 0px 1px !important;
}}

/* ── Delete confirm ── */
.del-confirm {{
    background: {"#1a0d0d" if DARK else "#fff0f0"};
    border: 1px solid #6b1a1a;
    border-radius: 6px;
    padding: 10px 14px; font-size: 0.80rem;
    color: {"#e8c0c0" if DARK else "#8b0000"};
    margin: 6px 0;
}}

/* ── Modify panel ── */
.modify-panel {{
    background: {"#13131c" if DARK else "#f0f0f8"};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 14px; margin: 6px 0;
}}

/* ── Settings page ── */
.settings-section {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 16px; margin-bottom: 14px;
}}

/* Expander styling */
[data-testid="stExpander"] {{
    background: {"#13131c" if DARK else "#f5f5fb"} !important;
    border: 1px solid {BORDER} !important;
    border-radius: 8px !important;
}}
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION STATE
# ═══════════════════════════════════════════════════════════════════════════════
def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

def _load_favs() -> dict:
    """Load favourites as {title: card_data_dict}. Migrates old list format."""
    raw = load_json(FAV_FILE, {})
    if isinstance(raw, list):
        # Migrate old format (list of title strings) to new dict format
        raw = {t: {"anicli_title": t} for t in raw}
        save_json(FAV_FILE, raw)
    return raw

ss("favs_data",          _load_favs())
ss("search_results",     [])
ss("search_query",       "")
ss("search_pending",     False)
ss("jikan_pending",      False)
ss("expanded",           None)
ss("modify_open",        None)
ss("delete_confirm",     None)
ss("tab_view",           "⭐ Favourites")
ss("sort_by",            "Recent")
ss("playlist_picker",    None)   # key_prefix of card showing playlist picker
ss("new_playlist_mode",  False)  # True = show "create new" text input
ss("sea_prefill",        "")    # pre-fill search from Seasonal tab
ss("sea_page",           1)    # current page for Seasonal tab
ss("studio_candidates",  [])
ss("creator_candidates", [])
ss("selected_studio",    None)
ss("selected_creator",   None)
ss("expanded_full",      None)  # key_prefix of card in full-stats view

def _load_playlists() -> dict:
    return load_json(PLAYLISTS_FILE, {})

def save_playlists(pl: dict):
    save_json(PLAYLISTS_FILE, pl)

def add_to_playlist(playlist_name: str, title: str, card_data: dict):
    pl = _load_playlists()
    pl.setdefault(playlist_name, {})[title] = card_data
    save_playlists(pl)

def remove_from_playlist(playlist_name: str, title: str):
    pl = _load_playlists()
    if playlist_name in pl:
        pl[playlist_name].pop(title, None)
        if not pl[playlist_name]:   # remove empty playlists
            del pl[playlist_name]
    save_playlists(pl)

def reorder_playlist(playlist_name: str, title: str, direction: int):
    """Move title up (-1) or down (+1) in playlist order."""
    pl = _load_playlists()
    if playlist_name not in pl:
        return
    items = list(pl[playlist_name].items())
    idx   = next((i for i, (t, _) in enumerate(items) if t == title), None)
    if idx is None:
        return
    new_idx = max(0, min(len(items) - 1, idx + direction))
    if new_idx == idx:
        return
    items.insert(new_idx, items.pop(idx))
    pl[playlist_name] = dict(items)
    save_playlists(pl)

def _fav_titles() -> set:
    return set(st.session_state.favs_data.keys())

def toggle_fav(title: str, card_data: dict | None = None):
    """Add or remove a favourite. card_data stores the full entry for display."""
    favs = st.session_state.favs_data
    if title in favs:
        del favs[title]
    else:
        favs[title] = card_data or {"anicli_title": title}
    save_json(FAV_FILE, favs)

def _on_search_change():
    if st.session_state.global_search.strip():
        st.session_state.search_pending = True

# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════
def _save_setting(key, value):
    """Save a single setting immediately — no Save button needed."""
    s = get_settings()
    s[key] = value
    save_settings(s)

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m|\x1b\[[0-9;]*[A-Za-z]|\r')

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).strip()

with st.sidebar:
    # ── Title ────────────────────────────────────────────────────────────────
    st.markdown(
        "<div style='text-align:center;font-size:1.6rem;padding:6px 0 0'>🎌</div>",
        unsafe_allow_html=True)

    # ── Dub toggle ────────────────────────────────────────────────────────────
    dub_val = cfg.get("dub", False)
    st.markdown(
        f"<div style='text-align:center;font-size:0.62rem;color:{MUTED};margin:2px 0 0'>DUB</div>",
        unsafe_allow_html=True)
    new_dub = st.toggle("DUB", value=bool(dub_val), key="sidebar_dub",
                        label_visibility="collapsed")
    if new_dub != dub_val:
        _save_setting("dub", new_dub)
        st.rerun()

    # ── Navigation ───────────────────────────────────────────────────────────
    nav_items = [("🔍", "Search Online"), ("🌸", "Seasonal"),
                 ("⭐", "Favourites"),
                 ("📺", "History"), ("📋", "Playlists"), ("⚙", "Settings")]
    for icon, label in nav_items:
        active   = st.session_state.tab_view == f"{icon} {label}"
        slug     = label.lower().replace(' ', '-')
        if active:
            st.markdown(
                f"<style>.nav-btn-{slug} button{{"
                f"border-color:{ACCENT}!important;"
                f"background:{ACCENT}18!important;}}</style>"
                f'<div class="nav-btn-{slug}">',
                unsafe_allow_html=True)
        if st.button(icon, key=f"nav_{label}", width='stretch', help=label):
            st.session_state.tab_view = f"{icon} {label}"
            st.rerun()
        if active:
            st.markdown("</div>", unsafe_allow_html=True)

    tab_view = st.session_state.tab_view

    # ── Version info ─────────────────────────────────────────────────────────
    try:
        anicli_ver = subprocess.run(
            ["ani-cli", "--version"], capture_output=True, text=True, timeout=3
        ).stdout.strip() or "?"
    except Exception:
        anicli_ver = "?"
    try:
        anicliprint_ver = subprocess.run(
            [str(APP_DIR / "ani-cli-print"), "--version"],
            capture_output=True, text=True, timeout=3
        ).stdout.strip() or "?"
    except Exception:
        anicliprint_ver = "?"
    st.sidebar.markdown(
        f"<div style='text-align:center;font-size:0.52rem;color:{MUTED};line-height:1.6'>"
        f"v{ANISHELF_VERSION}<br>{anicli_ver}<br>{anicliprint_ver}"
        f"</div>",
        unsafe_allow_html=True)
# ── Page-level filter/sort state (set per-tab below) ──────────────────────────
filter_q = ""

# ═══════════════════════════════════════════════════════════════════════════════
# GLOBAL SEARCH BAR  —  only shown on Search Online tab
# ═══════════════════════════════════════════════════════════════════════════════
global_query     = ""
do_global_search = False
jikan_status     = st.empty()   # placeholder always exists for metadata bar

if tab_view == "🔍 Search Online":
    # Handle prefill from Seasonal tab — set the widget's session state key directly
    # before rendering so Streamlit picks it up as the initial value without conflict
    prefill = st.session_state.pop("sea_prefill", "")
    if prefill and not st.session_state.get("global_search"):
        st.session_state["global_search"] = prefill
        st.session_state.search_pending   = True

    gc1, gc2 = st.columns([6, 1])
    with gc1:
        global_query = st.text_input(
            "Search", placeholder="🔍  Search anime using ani-cli online… (Enter or click Search)",
            label_visibility="collapsed", key="global_search",
            on_change=_on_search_change)
    with gc2:
        st.markdown("<div style='height:2px'></div>", unsafe_allow_html=True)
        do_global_search = st.button("Search", width='stretch', key="global_search_btn")
    # Read back the current widget value
    global_query = st.session_state.get("global_search", "")

_trigger_search = (do_global_search or st.session_state.search_pending) and bool(global_query.strip())
if st.session_state.search_pending:
    st.session_state.search_pending = False

if _trigger_search:
    prefer = cfg.get("title_prefer", "available")

    # Step 1: get ani-cli results immediately
    with st.spinner("Searching via ani-cli…"):
        anicli_entries, debug_raw = anicli_search(
            global_query.strip(), prefer=prefer,
            limit=int(cfg.get("search_result_limit", 30)))

    if not anicli_entries:
        with st.expander("⚠️ ani-cli returned no results — debug output"):
            st.code(debug_raw or "(empty)", language=None)
        st.info("Falling back to Jikan search…")
        with st.spinner("Searching Jikan…"):
            fallback, err = search_jikan_query(global_query.strip(), limit=10)
        if fallback:
            st.session_state.search_results = fallback
            st.session_state.search_query   = global_query.strip()
        elif err:
            st.warning(f"Jikan also failed: {err}")
    else:
        # Show skeleton cards immediately — Jikan fetch deferred to next run
        skeleton_results = [
            {"anicli_title": e["display"],
             "anicli_jp":    e.get("japanese") or e["display"],
             "allanime_id":  e.get("allanime_id", ""),
             "image": "", "score": None, "episodes": None,
             "synopsis": "", "genres": [], "status": ""}
            for e in anicli_entries
        ]
        st.session_state.search_results = skeleton_results
        st.session_state.search_query   = global_query.strip()
        st.session_state.jikan_pending  = True   # enrich on next run
        # No st.rerun() here — let the page continue rendering skeleton cards
        # then the search results block below will show them immediately

# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS PAGE
# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# SEASONAL TAB
# ═══════════════════════════════════════════════════════════════════════════════
if tab_view == "🌸 Seasonal":
    st.markdown("### 🌸 Browse Anime")

    # ── Filter bar ────────────────────────────────────────────────────────────
    fc1, fc2 = st.columns([2, 3])
    with fc1:
        browse_mode = st.selectbox("Browse mode", [
            "🌸 Airing now", "🔥 Top airing", "⭐ Top all time",
            "🆕 Top upcoming", "📺 Top TV", "🎬 Top movies",
            "🏢 By studio", "🎨 By creator",
        ], label_visibility="collapsed", key="seasonal_mode")
    with fc2:
        if browse_mode in ("🏢 By studio", "🎨 By creator"):
            browse_query = ""
        else:
            browse_query = st.text_input("Jikan search", placeholder="🔍 Search Jikan directly…",
                                         label_visibility="collapsed", key="seasonal_query")

    # ── Studio picker ─────────────────────────────────────────────────────────
    if browse_mode == "🏢 By studio":
        with st.form("studio_search_form"):
            sp1, sp2 = st.columns([4, 1])
            with sp1:
                studio_q = st.text_input("Studio", placeholder="e.g. MAPPA, Bones, Kyoto Animation…",
                                         label_visibility="collapsed", key="studio_search_q")
            with sp2:
                studio_submitted = st.form_submit_button("🔍", use_container_width=True)
        if studio_submitted and studio_q.strip():
            with st.spinner("Searching studios…"):
                st.session_state.studio_candidates = fetch_jikan_producers(studio_q.strip())
                st.session_state.selected_studio   = None
                st.session_state.sea_page          = 1
        if st.session_state.studio_candidates and not st.session_state.selected_studio:
            st.caption("Pick a studio:")
            for prod in st.session_state.studio_candidates:
                if st.button(prod["name"], key=f"studio_pick_{prod['id']}"):
                    st.session_state.selected_studio   = prod
                    st.session_state.studio_candidates = []
                    st.session_state.sea_page          = 1
                    st.rerun()
        if st.session_state.selected_studio:
            sc1, sc2 = st.columns([5, 1])
            with sc1:
                st.caption(f"Showing: **{st.session_state.selected_studio['name']}**")
            with sc2:
                if st.button("✗", key="studio_clear", width='stretch'):
                    st.session_state.selected_studio   = None
                    st.session_state.studio_candidates = []
                    st.rerun()

    # ── Creator picker ────────────────────────────────────────────────────────
    if browse_mode == "🎨 By creator":
        with st.form("creator_search_form"):
            cp1, cp2 = st.columns([4, 1])
            with cp1:
                creator_q = st.text_input("Creator", placeholder="e.g. Kentaro Miura, Hayao Miyazaki…",
                                          label_visibility="collapsed", key="creator_search_q")
            with cp2:
                creator_submitted = st.form_submit_button("🔍", use_container_width=True)
        if creator_submitted and creator_q.strip():
            with st.spinner("Searching creators…"):
                st.session_state.creator_candidates = fetch_jikan_people(creator_q.strip())
                st.session_state.selected_creator   = None
                st.session_state.sea_page           = 1
        if st.session_state.creator_candidates and not st.session_state.selected_creator:
            st.caption("Pick a creator:")
            for person in st.session_state.creator_candidates:
                if st.button(person["name"], key=f"creator_pick_{person['id']}"):
                    st.session_state.selected_creator   = person
                    st.session_state.creator_candidates = []
                    st.session_state.sea_page           = 1
                    st.rerun()
        if st.session_state.selected_creator:
            cc1, cc2 = st.columns([5, 1])
            with cc1:
                st.caption(f"Showing: **{st.session_state.selected_creator['name']}**")
            with cc2:
                if st.button("✗", key="creator_clear", width='stretch'):
                    st.session_state.selected_creator   = None
                    st.session_state.creator_candidates = []
                    st.rerun()

    # Reset to page 1 when mode or query changes
    if "seasonal_mode_prev" not in st.session_state:
        st.session_state.seasonal_mode_prev = browse_mode
    if "seasonal_query_prev" not in st.session_state:
        st.session_state.seasonal_query_prev = browse_query
    if (st.session_state.seasonal_mode_prev != browse_mode or
            st.session_state.seasonal_query_prev != browse_query):
        st.session_state.sea_page = 1
        st.session_state.seasonal_mode_prev  = browse_mode
        st.session_state.seasonal_query_prev = browse_query
        st.session_state.pop("seasonal_items_cache", None)  # clear stale results
        if browse_mode != "🏢 By studio":
            st.session_state.selected_studio   = None
            st.session_state.studio_candidates = []
        if browse_mode != "🎨 By creator":
            st.session_state.selected_creator   = None
            st.session_state.creator_candidates = []

    current_page = st.session_state.sea_page

    st.caption(
        "ℹ️ This tab browses **MyAnimeList via Jikan** — not ani-cli. "
        "Availability on your streaming provider is not guaranteed. "
        "Press **🔍 Search** below a card to search for it on ani-cli and watch it."
    )
    st.divider()

    # ── Fetch ─────────────────────────────────────────────────────────────────
    with st.spinner("Loading from Jikan…"):
        if browse_query.strip():
            seasonal_items, has_next = fetch_jikan_search_browse(
                browse_query.strip(), page=current_page)
        elif browse_mode == "🌸 Airing now":
            seasonal_items, has_next = fetch_jikan_seasonal(page=current_page)
        elif browse_mode == "🔥 Top airing":
            seasonal_items, has_next = fetch_jikan_top("airing", page=current_page)
        elif browse_mode == "⭐ Top all time":
            seasonal_items, has_next = fetch_jikan_top("all", page=current_page)
        elif browse_mode == "🆕 Top upcoming":
            seasonal_items, has_next = fetch_jikan_top("upcoming", page=current_page)
        elif browse_mode == "📺 Top TV":
            seasonal_items, has_next = fetch_jikan_top("type:tv", page=current_page)
        elif browse_mode == "🎬 Top movies":
            seasonal_items, has_next = fetch_jikan_top("type:movie", page=current_page)
        elif browse_mode == "🏢 By studio":
            sel = st.session_state.selected_studio
            if sel:
                seasonal_items, has_next = fetch_jikan_by_studio(sel["id"], page=current_page)
            else:
                seasonal_items, has_next = [], False
        elif browse_mode == "🎨 By creator":
            sel = st.session_state.selected_creator
            if sel:
                seasonal_items, has_next = fetch_jikan_by_person(sel["id"], page=current_page)
            else:
                seasonal_items, has_next = [], False
        else:
            seasonal_items, has_next = [], False

    if not seasonal_items:
        if browse_mode == "🏢 By studio" and not st.session_state.selected_studio:
            st.info("Search for a studio above and pick one to browse its anime.")
        elif browse_mode == "🎨 By creator" and not st.session_state.selected_creator:
            st.info("Search for a creator above and pick one to browse their works.")
        else:
            st.warning("No results from Jikan — check your connection or try again.")
        st.stop()

    st.caption(f"Page {current_page} · {len(seasonal_items)} titles from MyAnimeList")

    COLS = 5
    sea_rows = [seasonal_items[i:i+COLS] for i in range(0, len(seasonal_items), COLS)]

    for sr_idx, sr_row in enumerate(sea_rows):
        cols = st.columns(COLS, gap="small")
        for sc_idx, item in enumerate(sr_row):
            sea_title   = item.get("title", "")
            is_fav      = sea_title in _fav_titles()
            sea_key     = f"sea{sr_idx}c{sc_idx}"
            sea_ep_open = st.session_state.expanded == sea_key

            with cols[sc_idx]:
                st.markdown(
                    f'<div class="card-wrap">'
                    f'{card_html(item, sea_title, is_fav)}'
                    f'</div>', unsafe_allow_html=True)

                b1, b2, b3 = st.columns([2, 5, 2])
                with b1:
                    if st.button("★" if is_fav else "♡", key=f"seafav_{sea_key}",
                                 width='stretch'):
                        toggle_fav(sea_title, card_data={"anicli_title": sea_title,
                                                          "anicli_jp": sea_title,
                                                          **item})
                        st.rerun()
                with b2:
                    if st.button("🔍 Search", key=f"seasearch_{sea_key}",
                                 width='stretch'):
                        st.session_state.tab_view    = "🔍 Search Online"
                        st.session_state.sea_prefill = sea_title
                        st.rerun()
                with b3:
                    sea_ep_open = st.session_state.expanded == sea_key
                    if st.button("☰", key=f"seaeps_{sea_key}", width='stretch'):
                        st.session_state.expanded      = None if sea_ep_open else sea_key
                        st.session_state.expanded_full = None
                        st.rerun()

        for sc_idx, item in enumerate(sr_row):
            sea_title = item.get("title", "")
            sea_key   = f"sea{sr_idx}c{sc_idx}"
            if st.session_state.expanded == sea_key:
                with st.expander(f"📋 {sea_title}", expanded=True):
                    render_episode_panel(sea_title, sea_title, 1, item,
                                         cfg, sea_key, allanime_id="",
                                         show_edit=False)
            if st.session_state.expanded_full == sea_key:
                with st.expander(f"⊞ {sea_title} — Full Info", expanded=True):
                    render_full_panel(sea_title, item, sea_key)

    # ── Pagination ────────────────────────────────────────────────────────────
    show_pagination = current_page > 1 or has_next
    if show_pagination:
        st.divider()

        # Scroll to top when page changes (injected JS runs on render)
        st.markdown("<script>window.scrollTo(0,0);</script>",
                    unsafe_allow_html=True)

        # Build up to 5 page numbers centred around current page
        # Always include page 1; pages are unknown total so we go by has_next
        visible = []
        if current_page <= 3:
            visible = list(range(1, min(current_page + 3, current_page + (2 if has_next else 1)) + 1))
        else:
            visible = list(range(current_page - 2, current_page + 1))
            if has_next:
                visible.append(current_page + 1)
        if 1 not in visible:
            visible = [1] + visible
        visible = sorted(set(visible))[:5]   # cap at 5

        # Fixed layout: « ‹ [up to 5 pages] [jump] › »
        # Total columns = 2 + len(visible) + 1 + 2 = len(visible) + 5
        n_cols = len(visible) + 5
        nav_cols = st.columns(n_cols)
        ci = 0

        with nav_cols[ci]:
            if st.button("«", width='stretch', key="pg_first",
                         disabled=current_page == 1):
                st.session_state.sea_page = 1
                st.rerun()
        ci += 1

        with nav_cols[ci]:
            if st.button("‹", width='stretch', key="pg_prev",
                         disabled=current_page == 1):
                st.session_state.sea_page = current_page - 1
                st.rerun()
        ci += 1

        for p in visible:
            with nav_cols[ci]:
                if p == current_page:
                    st.markdown(
                        f"<div style='text-align:center;font-weight:700;"
                        f"padding:4px 0;color:{ACCENT}'>{p}</div>",
                        unsafe_allow_html=True)
                else:
                    if st.button(str(p), width='stretch', key=f"pg_{p}"):
                        st.session_state.sea_page = p
                        st.rerun()
            ci += 1

        # Jump-to-page input
        with nav_cols[ci]:
            jump = st.text_input("Go", placeholder="…", label_visibility="collapsed",
                                  key="pg_jump", max_chars=4)
            if jump.strip().isdigit():
                jp = int(jump.strip())
                if jp >= 1:
                    st.session_state.sea_page = jp
                    st.rerun()
        ci += 1

        with nav_cols[ci]:
            if st.button("›", width='stretch', key="pg_next",
                         disabled=not has_next):
                st.session_state.sea_page = current_page + 1
                st.rerun()
        ci += 1

        with nav_cols[ci]:
            if st.button("»", width='stretch', key="pg_last",
                         disabled=not has_next):
                st.session_state.sea_page = current_page + 1
                st.rerun()

        st.caption(f"Page {current_page}" + (" · more available →" if has_next else " · last page"))

    st.stop()


if tab_view == "⚙ Settings":
    st.markdown("### ⚙ Settings")
    st.caption("All settings save automatically on change.")

    # ── Credits & links ───────────────────────────────────────────────────────
    st.markdown(
        "<div style='display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px'>"
        "<a href='https://github.com/djapexlo/ani-cli-shelf' target='_blank' "
        "style='font-size:0.78rem;text-decoration:none'>🎌 ani-cli-shelf on GitHub</a>"
        "<a href='https://github.com/pystardust/ani-cli' target='_blank' "
        "style='font-size:0.78rem;text-decoration:none'>⚡ ani-cli by pystardust</a>"
        "<a href='https://docs.api.jikan.moe' target='_blank' "
        "style='font-size:0.78rem;text-decoration:none'>📡 Jikan API (MyAnimeList)</a>"
        "</div>",
        unsafe_allow_html=True
    )

    s = get_settings()

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**🖥 Terminal & Launch**")

    def _on_terminal():  _save_setting("terminal", st.session_state._s_terminal)
    def _on_player():    _save_setting("player",   st.session_state._s_player)
    def _on_dub():       _save_setting("dub",      st.session_state._s_dub)

    st.text_input("Terminal emulator", value=s["terminal"], key="_s_terminal",
                  on_change=_on_terminal,
                  help="kitty | foot | alacritty | wezterm | gnome-terminal")
    st.selectbox("Media player", ["mpv", "vlc"],
                 index=0 if s["player"] == "mpv" else 1,
                 key="_s_player", on_change=_on_player)
    st.toggle("Prefer dub (--dub)", value=bool(s["dub"]),
              key="_s_dub", on_change=_on_dub)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**📁 History & Search**")

    def _on_hist_file():  _save_setting("history_file",       st.session_state._s_hist_file)
    def _on_hist_lim():   _save_setting("history_limit",      st.session_state._s_hist_lim)
    def _on_search_lim(): _save_setting("search_result_limit",st.session_state._s_search_lim)

    st.text_input("History file path", value=s["history_file"],
                  key="_s_hist_file", on_change=_on_hist_file)
    st.number_input("Max history titles to load", min_value=1, max_value=500,
                    value=int(s["history_limit"]), step=1,
                    key="_s_hist_lim", on_change=_on_hist_lim)
    st.number_input("Max search results", min_value=5, max_value=100,
                    value=int(s.get("search_result_limit", 30)), step=5,
                    key="_s_search_lim", on_change=_on_search_lim)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**🎬 mpv Progress Tracking**")
    st.caption("Used to show episode progress bars on cards.")

    def _on_mpv_hist():  _save_setting("mpv_history_file",    st.session_state._s_mpv_hist)
    def _on_mpv_wl():    _save_setting("mpv_watch_later_dir", st.session_state._s_mpv_wl)

    st.text_input("mpv watch_history.jsonl path",
                  value=s.get("mpv_history_file", DEFAULTS["mpv_history_file"]),
                  key="_s_mpv_hist", on_change=_on_mpv_hist)
    st.text_input("mpv watch_later/ directory",
                  value=s.get("mpv_watch_later_dir", DEFAULTS["mpv_watch_later_dir"]),
                  key="_s_mpv_wl", on_change=_on_mpv_wl)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**⏱ Jikan API Rate Limits**")

    _jikan_tooltip = (
        "Jikan limits requests to 3 per second and 60 per minute. "
        "Exceeding this returns HTTP 429 errors and skips metadata. "
        "Set 0 in either field to remove that limit (use carefully)."
    )

    def _on_jikan_bs():    _save_setting("jikan_batch_size",  st.session_state._s_jikan_bs)
    def _on_jikan_delay(): _save_setting("jikan_batch_delay", st.session_state._s_jikan_delay)

    st.number_input(
        "Cards per batch", min_value=0, max_value=60,
        value=int(s.get("jikan_batch_size", 3)), step=1,
        key="_s_jikan_bs", on_change=_on_jikan_bs,
        help=_jikan_tooltip)
    st.number_input(
        "Delay between batches (seconds)", min_value=0.0, max_value=10.0,
        value=float(s.get("jikan_batch_delay", 3.02)), step=0.1, format="%.2f",
        key="_s_jikan_delay", on_change=_on_jikan_delay,
        help=_jikan_tooltip)
    st.caption("ℹ Jikan limit: 3 req/s · 60 req/min · set 0 to remove limit")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**🎬 Stream Quality**")

    def _on_quality(): _save_setting("quality",  st.session_state._s_quality)
    def _on_sublang(): _save_setting("sub_lang", st.session_state._s_sublang)

    st.selectbox("Preferred quality", ["best","1080","720","480","360"],
                 index=["best","1080","720","480","360"].index(s.get("quality","best")),
                 key="_s_quality", on_change=_on_quality)
    st.text_input("Subtitle language", value=s.get("sub_lang","English"),
                  key="_s_sublang", on_change=_on_sublang,
                  help="e.g. English, Japanese")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**🌐 Search Title Preference**")

    pref_opts   = ["available", "english", "japanese"]
    pref_labels = ["Available (English if present, else Japanese)",
                   "Always English", "Always Japanese"]
    cur_pref    = s.get("title_prefer", "available")

    def _on_title_pref():
        _save_setting("title_prefer", pref_opts[st.session_state._s_titlepref])

    st.radio("Title preference", options=range(len(pref_opts)),
             format_func=lambda i: pref_labels[i],
             index=pref_opts.index(cur_pref) if cur_pref in pref_opts else 0,
             label_visibility="collapsed",
             key="_s_titlepref", on_change=_on_title_pref)
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**🎨 Appearance**")

    def _write_streamlit_theme(light: bool):
        config_dir = APP_DIR / ".streamlit"
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / "config.toml"
        theme = "light" if light else "dark"
        config_file.write_text(
            f'[theme]\nbase="{theme}"\n'
            f'backgroundColor="{"#f4f4f8" if light else "#0d0d12"}"\n'
            f'secondaryBackgroundColor="{"#ffffff" if light else "#16161f"}"\n'
            f'textColor="{"#1a1a2e" if light else "#e8e6f0"}"\n'
            f'primaryColor="{"#9c27b0" if light else "#e040fb"}"\n'
        )

    def _on_lightmode():
        val = st.session_state._s_lightmode
        _save_setting("light_mode", val)
        _write_streamlit_theme(val)

    st.toggle("Light mode", value=bool(s.get("light_mode", False)),
              key="_s_lightmode", on_change=_on_lightmode)
    st.caption("⚠ Restart ani-cli-shelf after changing this for Streamlit's own UI to update.")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**🗄 Cache**")
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        if st.button("🔄 Clear metadata cache", width='stretch'):
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
            st.success("Cache cleared.")
    with col_c2:
        cache_size = f"{CACHE_FILE.stat().st_size//1024} KB" if CACHE_FILE.exists() else "empty"
        st.caption(f"Cache: {cache_size}")

    if st.button("♻ Reload ALL metadata (keeps your custom search/display titles)",
                 width='stretch'):
        overrides_data = load_json(OVERRIDES_FILE, {})
        # Collect all titles from history + favourites + playlists
        all_titles = set()
        hist_items = read_history(cfg)
        for h in hist_items:
            all_titles.add(h["title"])
        for t in load_json(FAV_FILE, {}).keys():
            all_titles.add(t)
        for pl in load_json(PLAYLISTS_FILE, {}).values():
            for t in pl.keys():
                all_titles.add(t)

        total = len(all_titles)
        if total == 0:
            st.info("No titles found to reload.")
        else:
            new_cache = {}
            bar = st.progress(0, text=f"Reloading 0/{total}…")
            for i, title in enumerate(sorted(all_titles)):
                # Respect user-set search title override; fall back to title itself
                search_title = overrides_data.get(title, {}).get("search_title", title)
                new_cache[title] = fetch_jikan(search_title)
                bar.progress((i + 1) / total, text=f"Reloading {i+1}/{total}: {title[:30]}…")
                if (i + 1) % BATCH_SIZE == 0 and (i + 1) < total:
                    time.sleep(BATCH_DELAY)
            save_json(CACHE_FILE, new_cache)
            bar.empty()
            fetched = sum(1 for v in new_cache.values() if v)
            st.success(f"Reloaded {fetched}/{total} titles. "
                       f"{total - fetched} couldn't be found on Jikan.")
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<div class="settings-section">', unsafe_allow_html=True)
    st.markdown("**📦 Export / Import**")
    st.caption("Export saves your favourites, playlists, overrides, settings and episode cache. "
               "It does NOT export ani-cli history (that's managed by ani-cli itself).")

    ex1, ex2 = st.columns(2)
    with ex1:
        # Build export zip in memory
        if st.button("📤 Export data", width='stretch'):
            import zipfile
            import io
            buf = io.BytesIO()
            files_to_export = [
                (FAV_FILE,       "favourites.json"),
                (PLAYLISTS_FILE, "playlists.json"),
                (OVERRIDES_FILE, "overrides.json"),
                (SETTINGS_FILE,  "settings.json"),
                (EP_CACHE_FILE,  "ep_cache.json"),
            ]
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, name in files_to_export:
                    if path.exists():
                        zf.write(path, name)
                # Also include thumbnails folder
                if THUMB_DIR.exists():
                    for thumb in THUMB_DIR.iterdir():
                        zf.write(thumb, f"thumbnails/{thumb.name}")
            buf.seek(0)
            st.download_button(
                "⬇ Download anishelf_export.zip",
                data=buf,
                file_name="anishelf_export.zip",
                mime="application/zip",
                width='stretch',
            )
    with ex2:
        st.markdown("**Import**")
        uploaded_zip = st.file_uploader("Upload anishelf_export.zip",
                                         type=["zip"], key="import_zip",
                                         label_visibility="collapsed")
        if uploaded_zip and st.button("📥 Import data", width='stretch', key="do_import"):
            import zipfile
            import io
            try:
                with zipfile.ZipFile(io.BytesIO(uploaded_zip.read())) as zf:
                    name_to_path = {
                        "favourites.json": FAV_FILE,
                        "playlists.json":  PLAYLISTS_FILE,
                        "overrides.json":  OVERRIDES_FILE,
                        "settings.json":   SETTINGS_FILE,
                        "ep_cache.json":   EP_CACHE_FILE,
                    }
                    imported = []
                    for name in zf.namelist():
                        if name in name_to_path:
                            name_to_path[name].write_bytes(zf.read(name))
                            imported.append(name)
                        elif name.startswith("thumbnails/") and not name.endswith("/"):
                            dest = THUMB_DIR / Path(name).name
                            dest.write_bytes(zf.read(name))
                            imported.append(name)
                # Reload favourites into session state
                st.session_state.favs_data = _load_favs()
                st.success(f"Imported: {', '.join(imported)}")
                st.rerun()
            except Exception as e:
                st.error(f"Import failed: {e}")
    st.markdown('</div>', unsafe_allow_html=True)

    st.stop()

# ── Search Online tab — search bar already rendered above, just show results ──
if tab_view == "🔍 Search Online":
    if not st.session_state.search_results:
        st.markdown(
            f"<div style='text-align:center;padding:40px 0;color:{MUTED};font-size:0.9rem'>"
            f"🔍 Type a title above and press Enter or Search</div>",
            unsafe_allow_html=True)
    # Search results render below automatically — fall through to SEARCH RESULTS block
    # After results we stop so History/Favs don't appear

# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH RESULTS
# ═══════════════════════════════════════════════════════════════════════════════
if st.session_state.search_results:
    st.markdown(
        f"#### 🔍 Results for **{st.session_state.search_query}**"
        f" <span style='color:{MUTED};font-size:0.8rem'>"
        f"({len(st.session_state.search_results)} titles from ani-cli)</span>",
        unsafe_allow_html=True)

    if st.button("✕ Clear search results", key="clear_search"):
        st.session_state.search_results = []
        st.session_state.search_query   = ""
        st.rerun()

    COLS = 5
    # Group into rows so expanders can span full width below each row
    s_rows = [st.session_state.search_results[i:i+COLS]
              for i in range(0, len(st.session_state.search_results), COLS)]

    for s_row_idx, s_row in enumerate(s_rows):
        cols = st.columns(COLS, gap="small")
        for s_col_idx, entry in enumerate(s_row):
            # ── ani-cli is the source of truth ──────────────────────────
            anicli_title = entry["anicli_title"]   # display title
            anicli_jp    = entry.get("anicli_jp", anicli_title)  # JP title for launching
            has_meta     = bool(entry.get("image")) # Jikan found something
            is_fav       = anicli_title in _fav_titles()
            s_key        = f"sr{s_row_idx}c{s_col_idx}"
            s_ep_open    = st.session_state.expanded == s_key

            # Build a meta-like dict but NEVER let Jikan overwrite the title
            display_meta = {**entry, "title": anicli_title} if has_meta else None

            with cols[s_col_idx]:
                if has_meta:
                    st.markdown(
                        f'<div class="card-wrap">'
                        f'{card_html(display_meta, anicli_title, is_fav)}'
                        f'</div>', unsafe_allow_html=True)
                else:
                    # No Jikan metadata — show placeholder card with notice
                    st.markdown(f"""
<div class="card-wrap">
<div class="anime-card">
  <div class="poster-wrap"><div class="poster-placeholder">🎌</div></div>
  <div class="card-body">
    <div class="anime-title" title="{anicli_title}">{anicli_title}</div>
    <div class="card-synopsis" style="color:{ACCENT};font-size:0.60rem">
      ⚠ No metadata found — use Modify in history to fix, or title may differ on MAL
    </div>
  </div>
</div>
</div>""", unsafe_allow_html=True)

                b1, b2, b3 = st.columns([2, 5, 2])
                with b1:
                    if st.button("★" if is_fav else "♡", key=f"sfav_{s_key}",
                                 width='stretch'):
                        toggle_fav(anicli_title, card_data={**entry, "anicli_title": anicli_title})
                        st.rerun()
                with b2:
                    if st.button("▶ Ep 1", key=f"splay_{s_key}",
                                 width='stretch'):
                        msg = launch_anime(anicli_jp, 1, cfg,
                                     allanime_id=entry.get("allanime_id", ""))
                        st.toast(msg, icon="▶")
                with b3:
                    if st.button("☰", key=f"seps_{s_key}",
                                 width='stretch'):
                        st.session_state.expanded = None if s_ep_open else s_key
                        st.rerun()

        # Full-width episode expander below each search row
        for s_col_idx, entry in enumerate(s_row):
            anicli_title = entry["anicli_title"]
            anicli_jp    = entry.get("anicli_jp", anicli_title)
            s_key        = f"sr{s_row_idx}c{s_col_idx}"
            if st.session_state.expanded == s_key:
                panel_meta = {**entry, "title": anicli_title} if entry.get("image") else None
                with st.expander(f"📋 {anicli_title}", expanded=True):
                    render_episode_panel(anicli_jp, anicli_title, 1, panel_meta, cfg, s_key,
                                         allanime_id=entry.get("allanime_id", ""))

    # ── Jikan enrichment runs HERE — after all cards are already visible ──────
    @st.fragment
    def _enrich_jikan():
        if not st.session_state.jikan_pending:
            return
        st.session_state.jikan_pending = False
        skeletons = [e for e in st.session_state.search_results if not e.get("image")]
        if not skeletons:
            return
        jikan_status.progress(0, text=f"Fetching metadata 0/{len(skeletons)}…")
        enriched_map = {}
        for i in range(0, len(skeletons), BATCH_SIZE):
            batch = skeletons[i:i+BATCH_SIZE]
            for j, e in enumerate(batch):
                meta = fetch_jikan(e.get("anicli_jp") or e["anicli_title"])
                enriched_map[e["anicli_title"]] = meta
                jikan_status.progress((i+j+1)/len(skeletons),
                                      text=f"Fetching metadata {i+j+1}/{len(skeletons)}…")
            if i + BATCH_SIZE < len(skeletons):
                time.sleep(BATCH_DELAY)
        jikan_status.empty()
        updated = []
        for entry in st.session_state.search_results:
            meta = enriched_map.get(entry["anicli_title"])
            if meta:
                new_entry = {**entry, **meta,
                             "anicli_title": entry["anicli_title"],
                             "anicli_jp":    entry.get("anicli_jp", entry["anicli_title"]),
                             "allanime_id":  entry.get("allanime_id", "")}
                updated.append(new_entry)
            else:
                updated.append(entry)
        st.session_state.search_results = updated
        st.rerun()   # fragment-scoped rerun — search bar is unaffected

    _enrich_jikan()

    st.divider()

if tab_view == "🔍 Search Online":
    st.stop()

# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY / FAVOURITES / PLAYLISTS
# ═══════════════════════════════════════════════════════════════════════════════

# History auto-refresh — plain inline mtime check, no background thread.
# Triggers a rerun only when ani-hsts actually changes (e.g. episode just played).
if tab_view in ("📺 History", "⭐ Favourites", "📋 Playlists"):
    _hist_path = Path(cfg["history_file"])
    if _hist_path.exists():
        _mtime = _hist_path.stat().st_mtime
        if st.session_state.get("_hist_mtime_val") != _mtime:
            prev = st.session_state.get("_hist_mtime_val")
            st.session_state["_hist_mtime_val"] = _mtime
            if prev is not None:
                st.rerun()

# Launch status — read log inline, show in sidebar, no background polling.
if _ANICLI_LOG_FILE.exists():
    try:
        _log_lines = [_strip_ansi(ln)
                      for ln in _ANICLI_LOG_FILE.read_text().splitlines()
                      if _strip_ansi(ln)]
        if _log_lines:
            try:
                _pid = int(_ANICLI_PID_FILE.read_text().strip()) \
                       if _ANICLI_PID_FILE.exists() else None
                if _pid:
                    os.kill(_pid, 0)   # raises if process gone
                    with st.sidebar:
                        st.markdown(
                            f"<div style='font-size:0.58rem;color:{MUTED};"
                            f"text-align:center;word-break:break-word;"
                            f"padding:2px 0;line-height:1.4'>"
                            f"⟳ {_log_lines[-1]}</div>",
                            unsafe_allow_html=True)
                else:
                    _ANICLI_LOG_FILE.unlink(missing_ok=True)
            except (ProcessLookupError, ValueError):
                _ANICLI_LOG_FILE.unlink(missing_ok=True)
    except Exception:
        pass

history_items  = read_history(cfg)
history_titles = [h["title"] for h in history_items]
overrides: dict = load_json(OVERRIDES_FILE, {})

if not history_titles:
    st.warning(f"No history at `{cfg['history_file']}`. Watch something with ani-cli first!")
    st.stop()

# Build the full set of titles across all tabs so the cache covers everything,
# not just what's in the current history window.
# Seasonal tab items carry their own metadata — exclude to avoid redundant fetches.
_fav_titles_list = list(load_json(FAV_FILE, {}).keys())
_pl_titles_list  = [t for pl in load_json(PLAYLISTS_FILE, {}).values() for t in pl]
all_titles = list(dict.fromkeys(history_titles + _fav_titles_list + _pl_titles_list))
if tab_view != "🌸 Seasonal":
    cache = get_metadata(all_titles, overrides)
else:
    cache = load_json(CACHE_FILE, {})

if tab_view == "⭐ Favourites":
    fav_dict = st.session_state.favs_data
    if not fav_dict:
        st.info("No favourites yet — hit ♡ on any card.")
        st.stop()

    heading = "⭐ Favourites"

    # Filter + sort bar
    ff1, ff2 = st.columns([3, 2])
    with ff1:
        filter_q = st.text_input("Filter", placeholder="🔍 Filter titles…", label_visibility="collapsed", key="fav_filter")
    with ff2:
        SORT_OPTS = ["Recent", "A→Z", "Z→A", "Score↓", "Score↑"]
        new_sort = st.selectbox("Sort", SORT_OPTS, label_visibility="collapsed",
                                 index=SORT_OPTS.index(st.session_state.get("sort_by","Recent")),
                                 key="fav_sort")
        if new_sort != st.session_state.get("sort_by","Recent"):
            st.session_state.sort_by = new_sort
            st.rerun()

    fav_titles_list = list(fav_dict.keys())
    if filter_q:
        q = filter_q.lower()
        fav_titles_list = [t for t in fav_titles_list if q in t.lower()]
    # Convert to dicts for sort_items then back to title list
    fav_items_tmp = [{"title": t, "episode": 1} for t in fav_titles_list]
    fav_items_tmp = sort_items(fav_items_tmp, st.session_state.get("sort_by","Recent"), cache)
    fav_titles_list = [h["title"] for h in fav_items_tmp]

    st.markdown(
        f"### {heading} &nbsp;"
        f"<span style='color:{MUTED};font-size:0.85rem'>{len(fav_titles_list)} titles</span>",
        unsafe_allow_html=True)

    COLS = 5
    fav_rows = [fav_titles_list[i:i+COLS] for i in range(0, len(fav_titles_list), COLS)]

    for row_idx, row in enumerate(fav_rows):
        cols = st.columns(COLS, gap="small")
        for col_idx, fav_title in enumerate(row):
            card_data = fav_dict.get(fav_title, {})
            # Merge with Jikan cache if available
            cached_meta = cache.get(fav_title)
            meta = cached_meta or (card_data if card_data.get("image") else None)
            anicli_jp = card_data.get("anicli_jp", fav_title)
            allanime_id = card_data.get("allanime_id", "")
            # Episode from history if available
            hist_item = next((h for h in history_items if h["title"] == fav_title), None)
            episode = hist_item["episode"] if hist_item else 1
            thumb_path = get_thumb_path(fav_title)
            custom_src = img_to_data_url(thumb_path) if thumb_path else None
            fav_key = f"fv{row_idx}c{col_idx}"
            ep_open  = st.session_state.expanded == fav_key
            ov       = overrides.get(fav_title, {})
            display  = ov.get("display_title", fav_title)

            with cols[col_idx]:
                st.markdown(
                    f'<div class="card-wrap">'
                    f'{card_html(meta, display, True, last_ep=episode if hist_item else None, custom_thumb=custom_src, progress=get_episode_progress(display, episode, (meta or {}).get("duration_secs",0), cfg))}'
                    f'</div>', unsafe_allow_html=True)

                b1, b2, b3 = st.columns([2, 5, 2])
                with b1:
                    if st.button("★", key=f"ffav_{fav_key}", width='stretch'):
                        toggle_fav(fav_title)
                        st.rerun()
                with b2:
                    if st.button(f"▶ Ep {episode}", key=f"fplay_{fav_key}",
                                 width='stretch'):
                        msg = launch_anime(anicli_jp, episode, cfg,
                                     allanime_id=allanime_id)
                        st.toast(msg, icon="▶")
                with b3:
                    if st.button("☰", key=f"feps_{fav_key}", width='stretch'):
                        st.session_state.expanded      = None if ep_open else fav_key
                        st.session_state.expanded_full = None
                        st.session_state.modify_open   = None
                        st.rerun()

        # Full-width panels below each favourites row
        for col_idx, fav_title in enumerate(row):
            card_data   = fav_dict.get(fav_title, {})
            cached_meta = cache.get(fav_title)
            meta        = cached_meta or (card_data if card_data.get("image") else None)
            anicli_jp   = card_data.get("anicli_jp", fav_title)
            allanime_id = card_data.get("allanime_id", "")
            hist_item   = next((h for h in history_items if h["title"] == fav_title), None)
            episode     = hist_item["episode"] if hist_item else 1
            ov          = overrides.get(fav_title, {})
            display     = ov.get("display_title", fav_title)
            fav_key     = f"fv{row_idx}c{col_idx}"

            if st.session_state.expanded == fav_key:
                with st.expander(f"📋 {display}", expanded=True):
                    render_episode_panel(anicli_jp, display, episode, meta, cfg,
                                         fav_key, allanime_id=allanime_id)
            if st.session_state.expanded_full == fav_key:
                with st.expander(f"⊞ {display} — Full Info", expanded=True):
                    render_full_panel(display, meta, fav_key)
            if st.session_state.modify_open == fav_key:
                s_title = overrides.get(fav_title, {}).get("search_title", fav_title)
                st.markdown('<div class="modify-panel">', unsafe_allow_html=True)
                st.markdown(f"**✎ Modify — {display}**")
                new_display = st.text_input("Display title", value=display, key=f"fmod_disp_{fav_key}")
                new_search  = st.text_input("Jikan search title", value=s_title, key=f"fmod_srch_{fav_key}")
                uploaded    = st.file_uploader("Custom thumbnail", type=["jpg","jpeg","png","webp"], key=f"fmod_thumb_{fav_key}")
                mc1, mc2 = st.columns(2)
                with mc1:
                    if st.button("💾 Save", key=f"fmod_save_{fav_key}", width='stretch'):
                        od = load_json(OVERRIDES_FILE, {})
                        od.setdefault(fav_title, {})
                        od[fav_title]["display_title"] = new_display.strip() or fav_title
                        od[fav_title]["search_title"]  = new_search.strip() or fav_title
                        save_json(OVERRIDES_FILE, od)
                        if uploaded:

                            save_thumb(fav_title, uploaded)
                        if new_search.strip() != s_title:
                            cd = load_json(CACHE_FILE, {})
                            cd.pop(fav_title, None)
                            save_json(CACHE_FILE, cd)
                        st.session_state.modify_open = None
                        st.toast("Saved!", icon="💾")
                        st.rerun()
                with mc2:
                    if st.button("✗ Cancel", key=f"fmod_cancel_{fav_key}", width='stretch'):
                        st.session_state.modify_open = None
                        st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

# ── PLAYLISTS tab ─────────────────────────────────────────────────────────────
if tab_view == "📋 Playlists":
    playlists = _load_playlists()
    st.markdown("### 📋 Playlists")

    if not playlists:
        st.info("No playlists yet — open any anime's ☰ episode panel and hit ＋ List.")
        st.stop()

    # Playlist selector
    pl_names = list(playlists.keys())
    ss("active_playlist", pl_names[0] if pl_names else None)

    # Rename / delete controls
    pl_col1, pl_col2, pl_col3 = st.columns([3, 1, 1])
    with pl_col1:
        selected_pl = st.selectbox("Playlist", pl_names,
                                    index=pl_names.index(st.session_state.active_playlist)
                                    if st.session_state.active_playlist in pl_names else 0,
                                    label_visibility="collapsed",
                                    key="pl_selector")
        st.session_state.active_playlist = selected_pl
    with pl_col2:
        if st.button("✎ Rename", width='stretch', key="pl_rename_btn"):
            ss("pl_renaming", False)
            st.session_state.pl_renaming = not st.session_state.get("pl_renaming", False)
            st.rerun()
    with pl_col3:
        if st.button("🗑 Delete", width='stretch', key="pl_delete_btn"):
            pl = _load_playlists()
            pl.pop(selected_pl, None)
            save_playlists(pl)
            st.session_state.active_playlist = list(pl.keys())[0] if pl else None
            st.toast(f"Deleted '{selected_pl}'", icon="🗑")
            st.rerun()

    if st.session_state.get("pl_renaming"):
        new_name = st.text_input("New name", value=selected_pl, key="pl_new_name")
        rc1, rc2 = st.columns(2)
        with rc1:
            if st.button("💾 Save name", width='stretch', key="pl_rename_save"):
                if new_name.strip() and new_name.strip() != selected_pl:
                    pl = _load_playlists()
                    pl[new_name.strip()] = pl.pop(selected_pl, {})
                    save_playlists(pl)
                    st.session_state.active_playlist = new_name.strip()
                st.session_state.pl_renaming = False
                st.rerun()
        with rc2:
            if st.button("✗ Cancel", width='stretch', key="pl_rename_cancel"):
                st.session_state.pl_renaming = False
                st.rerun()

    st.divider()

    pl_items = playlists.get(selected_pl, {})
    if not pl_items:
        st.info(f"'{selected_pl}' is empty — add anime from the ☰ episode panel.")
        st.stop()

    st.caption(f"{len(pl_items)} titles in '{selected_pl}'")

    # Filter + sort bar
    pf1, pf2 = st.columns([3, 2])
    with pf1:
        filter_q = st.text_input("Filter", placeholder="🔍 Filter titles…", label_visibility="collapsed", key="pl_filter")
    with pf2:
        SORT_OPTS = ["Recent", "A→Z", "Z→A", "Score↓", "Score↑"]
        new_sort = st.selectbox("Sort", SORT_OPTS, label_visibility="collapsed",
                                 index=SORT_OPTS.index(st.session_state.get("sort_by","Recent")),
                                 key="pl_sort")
        if new_sort != st.session_state.get("sort_by","Recent"):
            st.session_state.sort_by = new_sort
            st.rerun()

    COLS = 5
    pl_list = list(pl_items.keys())
    if filter_q:
        q = filter_q.lower()
        pl_list = [t for t in pl_list if q in t.lower()]
    pl_tmp = [{"title": t, "episode": 1} for t in pl_list]
    pl_tmp = sort_items(pl_tmp, st.session_state.get("sort_by","Recent"), cache)
    pl_list = [h["title"] for h in pl_tmp]

    pl_rows = [pl_list[i:i+COLS] for i in range(0, len(pl_list), COLS)]
    for pr_idx, pr_row in enumerate(pl_rows):
        cols = st.columns(COLS, gap="small")
        for pc_idx, pl_title in enumerate(pr_row):
            card_data   = pl_items[pl_title]
            meta        = cache.get(pl_title) or (card_data if card_data.get("image") else None)
            anicli_jp   = card_data.get("anicli_jp", pl_title)
            allanime_id = card_data.get("allanime_id", "")
            hist_item   = next((h for h in history_items if h["title"] == pl_title), None)
            episode     = hist_item["episode"] if hist_item else 1
            ov          = overrides.get(pl_title, {})
            display     = ov.get("display_title", pl_title)
            is_fav      = pl_title in _fav_titles()
            thumb_path  = get_thumb_path(pl_title)
            custom_src  = img_to_data_url(thumb_path) if thumb_path else None
            pl_key      = f"pl{pr_idx}c{pc_idx}"
            ep_open     = st.session_state.expanded == pl_key

            with cols[pc_idx]:
                st.markdown(
                    f'<div class="card-wrap">'
                    f'{card_html(meta, display, is_fav, last_ep=episode if hist_item else None, custom_thumb=custom_src, progress=get_episode_progress(display, episode, (meta or {}).get("duration_secs",0), cfg))}'
                    f'</div>', unsafe_allow_html=True)
                b1, b2, b3, b4, b5, b6 = st.columns([2, 5, 1, 1, 1, 1])
                with b1:
                    if st.button("★" if is_fav else "♡", key=f"plfav_{pl_key}",
                                 width='stretch'):
                        toggle_fav(pl_title, card_data=card_data)
                        st.rerun()
                with b2:
                    if st.button(f"▶ Ep {episode}", key=f"plplay_{pl_key}",
                                 width='stretch'):
                        msg = launch_anime(anicli_jp, episode, cfg, allanime_id=allanime_id)
                        st.toast(msg, icon="▶")
                with b3:
                    if st.button("☰", key=f"pleps_{pl_key}", width='stretch'):
                        st.session_state.expanded      = None if ep_open else pl_key
                        st.session_state.expanded_full = None
                        st.rerun()
                with b4:
                    if st.button("↑", key=f"plup_{pl_key}", width='stretch',
                                 help="Move up in playlist"):
                        reorder_playlist(selected_pl, pl_title, -1)
                        st.rerun()
                with b5:
                    if st.button("↓", key=f"pldown_{pl_key}", width='stretch',
                                 help="Move down in playlist"):
                        reorder_playlist(selected_pl, pl_title, 1)
                        st.rerun()
                with b6:
                    if st.button("🗑", key=f"plrm_{pl_key}", width='stretch'):
                        remove_from_playlist(selected_pl, pl_title)
                        st.toast(f"Removed from '{selected_pl}'", icon="🗑")
                        st.rerun()

        for pc_idx, pl_title in enumerate(pr_row):
            card_data   = pl_items[pl_title]
            meta        = cache.get(pl_title) or (card_data if card_data.get("image") else None)
            anicli_jp   = card_data.get("anicli_jp", pl_title)
            allanime_id = card_data.get("allanime_id", "")
            hist_item   = next((h for h in history_items if h["title"] == pl_title), None)
            episode     = hist_item["episode"] if hist_item else 1
            ov          = overrides.get(pl_title, {})
            display     = ov.get("display_title", pl_title)
            pl_key      = f"pl{pr_idx}c{pc_idx}"

            if st.session_state.expanded == pl_key:
                with st.expander(f"📋 {display}", expanded=True):
                    render_episode_panel(anicli_jp, display, episode, meta, cfg, pl_key,
                                         allanime_id=allanime_id)
            if st.session_state.expanded_full == pl_key:
                with st.expander(f"⊞ {display} — Full Info", expanded=True):
                    render_full_panel(display, meta, pl_key)
    st.stop()

# ── HISTORY tab only below this point ────────────────────────────────────────
items_to_show = history_items
heading = "📺 Watch History"

# Filter + sort bar with reload button
SORT_OPTS = ["Recent", "A→Z", "Z→A", "Score↓", "Score↑"]
hf1, hf2, hf3 = st.columns([3, 2, 1])
with hf1:
    filter_q = st.text_input("Filter", placeholder="🔍 Filter titles…", label_visibility="collapsed", key="hist_filter")
with hf2:
    new_sort = st.selectbox("Sort", SORT_OPTS, label_visibility="collapsed",
                             index=SORT_OPTS.index(st.session_state.get("sort_by","Recent")),
                             key="hist_sort")
    if new_sort != st.session_state.get("sort_by","Recent"):
        st.session_state.sort_by = new_sort
        st.rerun()
with hf3:
    if st.button("🔄", width='stretch', help="Reload history from file"):
        st.session_state.pop("_hist_mtime_val", None)
        st.rerun()

if filter_q:
    q = filter_q.lower()
    items_to_show = [h for h in items_to_show
                     if q in h["title"].lower()
                     or q in (cache.get(h["title"]) or {}).get("title","").lower()]

items_to_show = sort_items(items_to_show, st.session_state.get("sort_by","Recent"), cache)

st.markdown(
    f"### {heading} &nbsp;"
    f"<span style='color:{MUTED};font-size:0.85rem'>{len(items_to_show)} titles</span>",
    unsafe_allow_html=True)

COLS = 5
rows = [items_to_show[i:i+COLS] for i in range(0, len(items_to_show), COLS)]

for row_idx, row in enumerate(rows):
    cols = st.columns(COLS, gap="small")

    for col_idx, item in enumerate(row):
        title   = item["title"]
        episode = item["episode"]
        meta    = cache.get(title)
        is_fav  = title in _fav_titles()
        ov      = overrides.get(title, {})
        display = ov.get("display_title", title)
        thumb_path = get_thumb_path(title)
        custom_src = img_to_data_url(thumb_path) if thumb_path else None
        card_key   = f"r{row_idx}c{col_idx}"

        with cols[col_idx]:
            ep_open  = st.session_state.expanded == card_key

            # ani-cli stores NEXT episode, so episode N means ep N-1 was last played.
            # We check progress for ep N-1. If episode=1 nothing played yet.
            _dur  = (meta or {}).get("duration_secs", 0)
            _prog = get_episode_progress(display, episode, _dur, cfg)

            # Auto-mark as watched when progress reaches 95%
            _auto_key = f"_auto_marked_{title}_{episode}"
            if (_prog is not None and _prog >= 0.95
                    and not st.session_state.get(_auto_key)):
                st.session_state[_auto_key] = True
                mark_episode_watched(title, episode, item.get("raw_id", ""), cfg)
                st.rerun()

            st.markdown(
                f'<div class="card-wrap">'
                f'{card_html(meta, display, is_fav, last_ep=episode, custom_thumb=custom_src, progress=_prog)}'
                f'</div>', unsafe_allow_html=True)

            # ── 4 buttons: ♡  |  ▶ Continue Watching (wide)  |  ☰  |  🗑 ──
            b1, b2, b3, b4 = st.columns([2, 5, 1, 1])

            with b1:
                if st.button("★" if is_fav else "♡", key=f"fav_{card_key}",
                             width='stretch'):
                    toggle_fav(title, card_data={"anicli_title": title, "anicli_jp": title, "allanime_id": item.get("raw_id",""), **(cache.get(title) or {})})
                    st.rerun()

            with b2:
                if st.button(f"▶ Ep {episode}", key=f"play_{card_key}",
                             width='stretch'):
                    msg = launch_anime(display, episode, cfg,
                                 allanime_id=item.get("raw_id", ""))
                    st.toast(msg, icon="▶")

            with b3:
                if st.button("☰", key=f"eps_{card_key}",
                             width='stretch'):
                    st.session_state.expanded      = None if ep_open else card_key
                    st.session_state.expanded_full = None
                    st.session_state.modify_open   = None
                    st.rerun()

            with b4:
                if st.button("🗑", key=f"del_{card_key}",
                             width='stretch'):
                    st.session_state.delete_confirm = title
                    st.rerun()

    # ── Full-width panels below each row ──────────────────────────────────────
    for col_idx, item in enumerate(row):
        title    = item["title"]
        episode  = item["episode"]
        meta     = cache.get(title)
        ov       = overrides.get(title, {})
        display  = ov.get("display_title", title)
        card_key = f"r{row_idx}c{col_idx}"

        # ── DELETE CONFIRM ─────────────────────────────────────────────
        if st.session_state.delete_confirm == title:
            st.markdown(f"""<div class="del-confirm">
⚠️ <b>Delete "{display}" from history?</b><br>
This will permanently remove it from <code>ani-hsts</code> — ani-cli's history file.
Metadata cache, overrides and favourites for this title will also be cleared.
</div>""", unsafe_allow_html=True)
            dc1, dc2, _ = st.columns([1,1,4])
            with dc1:
                if st.button("✓ Yes, delete", key=f"delyes_{card_key}"):
                    delete_from_history(title, cfg)
                    cd = load_json(CACHE_FILE, {})
                    cd.pop(title,None)
                    save_json(CACHE_FILE, cd)
                    od = load_json(OVERRIDES_FILE, {})
                    od.pop(title,None)
                    save_json(OVERRIDES_FILE, od)
                    st.session_state.favs_data.pop(title, None)
                    save_json(FAV_FILE, st.session_state.favs_data)
                    st.session_state.delete_confirm = None
                    st.toast(f"Deleted '{display}' from history", icon="🗑")
                    st.rerun()
            with dc2:
                if st.button("✗ Cancel", key=f"delno_{card_key}"):
                    st.session_state.delete_confirm = None
                    st.rerun()

        # ── EPISODE PANEL ──────────────────────────────────────────────
        if st.session_state.expanded == card_key:
            with st.expander(f"📋 {display}", expanded=True):
                render_episode_panel(title, display, episode, meta, cfg, card_key,
                                     allanime_id=item.get("raw_id", ""))
        # ── FULL STATS PANEL ────────────────────────────────────────────
        if st.session_state.expanded_full == card_key:
            with st.expander(f"⊞ {display} — Full Info", expanded=True):
                render_full_panel(display, meta, card_key)

        # ── MODIFY PANEL ───────────────────────────────────────────────
        if st.session_state.modify_open == card_key:
            ov      = overrides.get(title, {})
            display = ov.get("display_title", title)
            s_title = ov.get("search_title", title)

            st.markdown('<div class="modify-panel">', unsafe_allow_html=True)
            st.markdown(f"**✎ Modify — {display}**")

            new_display = st.text_input("Display title", value=display,
                                        key=f"mod_disp_{card_key}")
            new_search  = st.text_input(
                "Jikan search title",
                value=s_title, key=f"mod_srch_{card_key}",
                help="Change this if the wrong thumbnail or metadata is showing.")
            uploaded = st.file_uploader(
                "Custom thumbnail (overrides Jikan image)",
                type=["jpg","jpeg","png","webp"], key=f"mod_thumb_{card_key}")

            thumb_path = get_thumb_path(title)
            mc1, mc2, mc3, mc4 = st.columns(4)
            with mc1:
                if st.button("💾 Save", key=f"mod_save_{card_key}",
                             width='stretch'):
                    od = load_json(OVERRIDES_FILE, {})
                    od.setdefault(title, {})
                    od[title]["display_title"] = new_display.strip() or title
                    od[title]["search_title"]  = new_search.strip() or title
                    save_json(OVERRIDES_FILE, od)
                    if uploaded:

                        save_thumb(title, uploaded)
                    if new_search.strip() != s_title:
                        cd = load_json(CACHE_FILE, {})
                        cd.pop(title,None)
                        save_json(CACHE_FILE, cd)
                    st.session_state.modify_open = None
                    st.toast("Saved!", icon="💾")
                    st.rerun()
            with mc2:
                if st.button("✗ Cancel", key=f"mod_cancel_{card_key}",
                             width='stretch'):
                    st.session_state.modify_open = None
                    st.rerun()
            with mc3:
                if thumb_path and st.button("🖼 Clear thumb", key=f"mod_clrthumb_{card_key}",
                                             width='stretch'):
                    thumb_path.unlink()
                    st.rerun()
            with mc4:
                # 🗑 Delete moved here inside modify panel
                if st.button("🗑 Delete", key=f"del_{card_key}",
                             width='stretch'):
                    st.session_state.delete_confirm = title
                    st.session_state.modify_open    = None
                    st.rerun()

            st.markdown('</div>', unsafe_allow_html=True)
