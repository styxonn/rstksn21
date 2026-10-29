#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MojeKino Catalog Builder (GitHub Actions Indexer)
Fetches movies and TV shows from TMDB, checks their actual availability on
Webshare.cz and Přehraj.to, and generates an optimized catalog.json.
"""

import os
import sys
import re
import json
import time
import datetime
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "ee1b8b6bf7db9fc09923c52464fae2d1").strip()
TMDB_BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Minimum file size for movies in bytes (150 MB)
MIN_MOVIE_SIZE_BYTES = 150 * 1024 * 1024

# Ignored extensions
IGNORED_EXTENSIONS = {".rar", ".zip", ".srt", ".sub", ".txt", ".nfo", ".jpg", ".png", ".torrent", ".sample"}

# Supported video extensions
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".wmv", ".mov", ".ts", ".iso"}

GENRE_MAP = {
    28: "Akční",
    12: "Dobrodružný",
    16: "Animovaný",
    35: "Komedie",
    80: "Krimi",
    99: "Dokumentární",
    18: "Drama",
    10751: "Rodinný",
    14: "Fantasy",
    36: "Historický",
    27: "Horor",
    10402: "Hudební",
    9648: "Mysteriózní",
    10749: "Romantický",
    878: "Sci-Fi",
    10770: "TV film",
    53: "Thriller",
    10752: "Válečný",
    37: "Western",
    10759: "Akční a dobrodružný",
    10762: "Pro děti",
    10763: "Zprávy",
    10764: "Reality-TV",
    10765: "Sci-Fi a Fantasy",
    10766: "Mýdlová opera",
    10767: "Talk show",
    10768: "Válka a politika"
}


def normalize_title(text: str) -> str:
    """Normalize title for searching and filename matching."""
    if not text:
        return ""
    # Remove punctuation, symbols, extra spaces
    cleaned = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def parse_size_string(size_str: str) -> int:
    """Parse human readable size string like '2.68 GB' or '850 MB' to bytes."""
    if not size_str:
        return 0
    m = re.search(r"([\d\.,]+)\s*([KkMmGgTt]?[Bb])", size_str.replace(" ", ""))
    if not m:
        return 0
    val = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    if "TB" in unit:
        return int(val * (1024 ** 4))
    if "GB" in unit:
        return int(val * (1024 ** 3))
    if "MB" in unit:
        return int(val * (1024 ** 2))
    if "KB" in unit:
        return int(val * 1024)
    return int(val)


def detect_quality(filename: str) -> str:
    """Detect quality tags from filename (e.g. 4K, 1080p, 720p, CZ)."""
    fn_lower = filename.lower()
    quality = ""
    if re.search(r"\b(2160p|4k|uhd)\b", fn_lower):
        quality = "4K"
    elif re.search(r"\b(1080p|1080i|fhd|bluray|bdrip|remux)\b", fn_lower):
        quality = "1080p"
    elif re.search(r"\b(720p|hd)\b", fn_lower):
        quality = "720p"
    elif re.search(r"\b(480p|576p|dvd|dvdr|dvdrip|xvid|sd)\b", fn_lower):
        quality = "SD"

    is_cz = bool(re.search(r"\b(cz|cesky|česky|czdab|czdabing|dabing|dab|cztit)\b", fn_lower))
    if is_cz:
        quality = f"{quality} CZ".strip() if quality else "CZ"

    return quality or "HD"


def has_cz_audio(filename: str) -> bool:
    """Detect whether filename has Czech or Slovak audio dubbing."""
    fn_lower = filename.lower()
    has_cz_tit = bool(re.search(r"\b(cztit|sktit|titulky|cz_tit|tit|sub|subs)\b", fn_lower))
    has_dabing = bool(re.search(r"\b(czdab|czdabing|dabing|dab|cz_sk|dual|skdab|skdabing)\b", fn_lower))
    has_cz_word = bool(re.search(r"\b(cz|cesky|česky|sk|slovensky|slovenský)\b", fn_lower))
    if has_cz_tit and not (has_cz_word and has_dabing) and not ("czdab" in fn_lower):
        return False
    return has_cz_word or has_dabing


SEQUEL_WORDS_MAP = {
    "2": "2", "ii": "2", "two": "2", "druhy": "2", "druha": "2", "druhe": "2",
    "3": "3", "iii": "3", "three": "3", "treti": "3",
    "4": "4", "iv": "4", "four": "4", "ctvrty": "4", "ctvrta": "4",
    "5": "5", "v": "5", "five": "5", "paty": "5", "pata": "5",
    "6": "6", "vi": "6", "six": "6", "sesty": "6", "sesta": "6",
    "7": "7", "vii": "7", "seven": "7", "sedmy": "7", "sedma": "7",
    "8": "8", "viii": "8", "eight": "8", "osmy": "8", "osma": "8",
    "9": "9", "ix": "9", "nine": "9", "devaty": "9", "devata": "9",
    "10": "10", "x": "10", "ten": "10", "desaty": "10", "desata": "10"
}


def extract_sequel_number(text: str) -> str:
    """Extract normalized sequel identifier ('2', '3', etc.) from title or filename."""
    if not text:
        return ""
    norm = normalize_title(text).lower()
    clean = " " + " ".join(norm.split()) + " "

    m = re.search(r"\b(cast|dil|part|chapter|volume|vol)\s*([0-9]+|[ivx]+|druh[ay]|tret[i]|ctvrt[ay])\b", clean)
    if m:
        num = m.group(2)
        return SEQUEL_WORDS_MAP.get(num, num)

    m = re.search(r"\b(druh[ay]|tret[i]|ctvrt[ay])\s*(cast|dil)\b", clean)
    if m:
        word = m.group(1)
        return SEQUEL_WORDS_MAP.get(word, "")

    m = re.search(r"\b(ii|iii|iv|vi|vii|viii|ix)\b", clean)
    if m:
        return SEQUEL_WORDS_MAP.get(m.group(1), "")

    for token in norm.split():
        if token in SEQUEL_WORDS_MAP:
            return SEQUEL_WORDS_MAP[token]
        if token.isdigit() and 2 <= int(token) <= 10:
            return token
    return ""


def is_valid_file(filename: str, size_bytes: int, is_tv: bool, title_variants: list, year: int) -> bool:
    """Check if the given filename and size represent a valid match for the title."""
    fn_lower = filename.lower()
    _, ext = os.path.splitext(fn_lower)

    if ext in IGNORED_EXTENSIONS:
        return False

    # Check minimum size for movies
    if not is_tv and size_bytes > 0 and size_bytes < MIN_MOVIE_SIZE_BYTES:
        return False

    # Normalized filename tokens
    norm_fn = re.sub(r"[._\-]", " ", fn_lower)

    # TV show check: must have season 1 indicator (S01, S01E01, 1x01, Season 1, etc.)
    if is_tv:
        if not re.search(r"(s01e\d+|s01\b|1x\d+|season\s*0?1\b|serie\s*0?1\b|1\.\s*s[eé]rie)", norm_fn):
            return False

    # Check title match
    matched_title = False
    for t in title_variants:
        norm_t = normalize_title(t).lower()
        if not norm_t:
            continue
        words = norm_t.split()
        if not words:
            continue
        # If all words of title (or first 3 important words) appear in filename
        check_words = []
        for w in words:
            w_no_acc = "".join(c for c in unicodedata.normalize("NFD", w) if unicodedata.category(c) != "Mn")
            if len(w) > 2 and w_no_acc not in {"cast", "dil", "part", "chapter"}:
                check_words.append(w)
        if not check_words:
            check_words = words
        if all(w in norm_fn for w in check_words):
            matched_title = True
            break

    if not matched_title:
        return False

    # For movies, check sequel number match (prevents "Tiché místo 2" for "Tiché místo")
    if not is_tv:
        target_seq = ""
        for t in title_variants:
            seq = extract_sequel_number(t)
            if seq:
                target_seq = seq
                break
        fn_seq = extract_sequel_number(filename)
        if target_seq != fn_seq:
            return False

    # For movies, verify year if present in filename
    if not is_tv and year and year > 1900:
        year_str = str(year)
        prev_year = str(year - 1)
        next_year = str(year + 1)
        # If filename contains a 4-digit year, it should be close to release year
        year_matches = re.findall(r"\b(19\d\d|20\d\d)\b", norm_fn)
        if year_matches:
            if not any(y in (year_str, prev_year, next_year) for y in year_matches):
                return False

    return True


def check_webshare(query: str, is_tv: bool, title_variants: list, year: int) -> list:
    """Query Webshare.cz public search API."""
    url = "https://webshare.cz/api/search/"
    data = {
        "what": query,
        "category": "video",
        "sort": "rating",
        "limit": 25,
        "offset": 0
    }
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    valid_files = []
    try:
        resp = requests.post(url, data=data, headers=headers, timeout=12)
        if resp.status_code == 200 and resp.text.strip():
            root = ET.fromstring(resp.text)
            status = root.findtext("status")
            if status == "OK":
                for file_node in root.findall("file"):
                    name = file_node.findtext("name", "")
                    try:
                        size = int(file_node.findtext("size", "0"))
                    except ValueError:
                        size = 0
                    password = file_node.findtext("password", "0")
                    if password == "1":
                        continue
                    if is_valid_file(name, size, is_tv, title_variants, year):
                        valid_files.append({"name": name, "size": size, "source": "Webshare"})
    except Exception as ex:
        pass
    return valid_files


def check_prehrajto(query: str, is_tv: bool, title_variants: list, year: int) -> list:
    """Query Přehraj.to search HTML with retry on HTTP 429."""
    encoded_query = urllib.parse.quote_plus(query)
    url = f"https://prehraj.to/hledej/{encoded_query}"
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    valid_files = []

    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code == 429:
                time.sleep(1.5)
                continue
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")
                wrappers = soup.select(".video-wrapper, article")
                for w in wrappers:
                    a_tag = w.find("a")
                    href = a_tag.get("href", "") if a_tag else ""
                    if not href or "hledej" in href:
                        continue

                    texts = list(w.stripped_strings)
                    if not texts:
                        continue
                    # Title is typically the last element
                    title = texts[-1]
                    size_bytes = 0
                    for t in texts:
                        if re.search(r"\b\d+([.,]\d+)?\s*(GB|MB)\b", t, re.IGNORECASE):
                            size_bytes = parse_size_string(t)
                            break

                    if is_valid_file(title, size_bytes, is_tv, title_variants, year):
                        valid_files.append({"name": title, "size": size_bytes, "source": "Přehraj.to"})
                break
        except Exception:
            pass
    return valid_files


def verify_availability(item: dict) -> tuple:
    """Verify if at least one valid video file exists on Webshare or Přehraj.to."""
    is_tv = item["media_type"] == "tv"
    title_cz = item.get("title", "")
    title_orig = item.get("original_title", "")
    year = item.get("year", 0)

    title_variants = [t for t in [title_cz, title_orig] if t]

    queries = []
    if is_tv:
        if title_cz:
            queries.append(f"{title_cz} S01")
        if title_orig and title_orig != title_cz:
            queries.append(f"{title_orig} S01")
    else:
        if title_cz:
            queries.append(f"{title_cz} {year}" if year else title_cz)
        if title_orig and title_orig != title_cz:
            queries.append(f"{title_orig} {year}" if year else title_orig)

    found_files = []
    for q in queries:
        ws_files = check_webshare(q, is_tv, title_variants, year)
        found_files.extend(ws_files)
        pt_files = check_prehrajto(q, is_tv, title_variants, year)
        found_files.extend(pt_files)
        # If we already found files AND at least one has confirmed CZ dubbing, we can stop early
        if found_files and any(has_cz_audio(f["name"]) for f in found_files):
            break

    if not found_files:
        return False, None, False

    has_cz_dub = any(has_cz_audio(f["name"]) for f in found_files)

    # Detect best quality
    qualities = [detect_quality(f["name"]) for f in found_files]

    if has_cz_dub:
        # CZ audio is confirmed! Prioritize CZ quality tags so English 1080p never hides CZ
        cz_qualities = [q for q in qualities if "CZ" in q]
        best_q = "CZ"
        for q in ["4K CZ", "1080p CZ", "720p CZ", "SD CZ", "CZ"]:
            if q in cz_qualities:
                best_q = q
                break
    else:
        # No CZ audio found on any file
        best_q = "HD"
        for q in ["4K", "1080p", "720p", "SD"]:
            if q in qualities:
                best_q = q
                break

    return True, best_q, has_cz_dub


class TmdbFetcher:
    """Handles communication with The Movie Database API."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    def get(self, endpoint: str, params: dict = None) -> dict:
        if not self.api_key:
            return {}
        p = {"api_key": self.api_key, "language": "cs-CZ"}
        if params:
            p.update(params)
        url = f"{TMDB_BASE_URL}{endpoint}"
        try:
            r = self.session.get(url, params=p, timeout=15)
            if r.status_code == 200:
                return r.json()
            print(f"[TMDB] Request error: {r.status_code} for {endpoint}")
        except Exception as e:
            print(f"[TMDB] Connection error: {e}")
        return {}

    def fetch_items_for_category(self, endpoint: str, params: dict, media_type: str, category_key: str) -> list:
        data = self.get(endpoint, params)
        results = data.get("results", [])
        items = []
        for r in results:
            tmdb_id = r.get("id")
            if not tmdb_id:
                continue

            if media_type == "movie":
                title = r.get("title", "")
                orig_title = r.get("original_title", "")
                rel_date = r.get("release_date", "")
            else:
                title = r.get("name", "")
                orig_title = r.get("original_name", "")
                rel_date = r.get("first_air_date", "")

            year = 0
            if rel_date and len(rel_date) >= 4:
                try:
                    year = int(rel_date[:4])
                except ValueError:
                    year = 0

            genre_ids = r.get("genre_ids", [])
            genres = [GENRE_MAP[g] for g in genre_ids if g in GENRE_MAP]

            items.append({
                "id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "original_title": orig_title,
                "year": year,
                "overview": r.get("overview", ""),
                "vote_average": round(float(r.get("vote_average", 0.0)), 1),
                "poster_path": r.get("poster_path", ""),
                "backdrop_path": r.get("backdrop_path", ""),
                "genres": genres,
                "category": category_key
            })
        return items


def main():
    print("=" * 60)
    print("MojeKino Catalog Builder starting...")
    print(f"Timestamp: {datetime.datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    if not TMDB_API_KEY:
        print("[ERROR] TMDB_API_KEY environment variable is not set!")
        print("Please configure TMDB_API_KEY in your GitHub Secrets or environment.")
        # If running locally for testing, we can exit or create sample catalog
        if len(sys.argv) > 1 and sys.argv[1] == "--demo":
            print("[INFO] Creating demo catalog for local testing...")
            demo_catalog = {
                "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
                "categories": {
                    "movies_trending": "Filmy - Trendy týdne",
                    "movies_popular": "Filmy - Populární",
                    "movies_online": "Filmy - Nově online",
                    "movies_action": "Filmy - Akční",
                    "movies_scifi": "Filmy - Sci-Fi",
                    "tv_trending": "Seriály - Trendy týdne",
                    "tv_popular": "Seriály - Populární"
                },
                "items": [
                    {
                        "id": 603,
                        "media_type": "movie",
                        "title": "Matrix",
                        "original_title": "The Matrix",
                        "year": 1999,
                        "overview": "Počítačový hacker Neo zjistí, že jeho svět je ve skutečnosti počítačovou simulací.",
                        "vote_average": 8.2,
                        "poster_path": "/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg",
                        "backdrop_path": "/hZkgoQYus5vegHoetLkCJzb17zJ.jpg",
                        "genres": ["Akční", "Sci-Fi"],
                        "categories": ["movies_trending", "movies_popular", "movies_action", "movies_scifi"],
                        "quality": "4K CZ"
                    }
                ]
            }
            with open("catalog.json", "w", encoding="utf-8") as f:
                json.dump(demo_catalog, f, ensure_ascii=False, indent=2)
            print("[INFO] Demo catalog written to catalog.json")
            sys.exit(0)
        sys.exit(1)

    tmdb = TmdbFetcher(TMDB_API_KEY)
    today = datetime.date.today().isoformat()

    category_definitions = [
        # Movies
        {
            "key": "movies_trending",
            "name": "Filmy - Trendy týdne",
            "endpoint": "/trending/movie/week",
            "params": {},
            "media_type": "movie"
        },
        {
            "key": "movies_popular",
            "name": "Filmy - Populární",
            "endpoint": "/movie/popular",
            "params": {"page": 1},
            "media_type": "movie"
        },
        {
            "key": "movies_online",
            "name": "Filmy - Nově online",
            "endpoint": "/discover/movie",
            "params": {
                "with_release_type": "4|5|6",
                "release_date.lte": today,
                "sort_by": "popularity.desc"
            },
            "media_type": "movie"
        },
        {
            "key": "movies_action",
            "name": "Filmy - Akční",
            "endpoint": "/discover/movie",
            "params": {"with_genres": 28, "sort_by": "popularity.desc"},
            "media_type": "movie"
        },
        {
            "key": "movies_comedy",
            "name": "Filmy - Komedie",
            "endpoint": "/discover/movie",
            "params": {"with_genres": 35, "sort_by": "popularity.desc"},
            "media_type": "movie"
        },
        {
            "key": "movies_scifi",
            "name": "Filmy - Sci-Fi",
            "endpoint": "/discover/movie",
            "params": {"with_genres": 878, "sort_by": "popularity.desc"},
            "media_type": "movie"
        },
        {
            "key": "movies_thriller",
            "name": "Filmy - Thriller",
            "endpoint": "/discover/movie",
            "params": {"with_genres": 53, "sort_by": "popularity.desc"},
            "media_type": "movie"
        },
        {
            "key": "movies_animation",
            "name": "Filmy - Animované",
            "endpoint": "/discover/movie",
            "params": {"with_genres": 16, "sort_by": "popularity.desc"},
            "media_type": "movie"
        },
        {
            "key": "movies_horror",
            "name": "Filmy - Horor",
            "endpoint": "/discover/movie",
            "params": {"with_genres": 27, "sort_by": "popularity.desc"},
            "media_type": "movie"
        },
        # TV Shows
        {
            "key": "tv_trending",
            "name": "Seriály - Trendy týdne",
            "endpoint": "/trending/tv/week",
            "params": {},
            "media_type": "tv"
        },
        {
            "key": "tv_popular",
            "name": "Seriály - Populární",
            "endpoint": "/tv/popular",
            "params": {"page": 1},
            "media_type": "tv"
        }
    ]

    all_raw_items = {}
    categories_dict = {}

    print("[1/3] Fetching titles from TMDB...")
    for cat in category_definitions:
        cat_key = cat["key"]
        cat_name = cat["name"]
        categories_dict[cat_key] = cat_name
        print(f"  -> Fetching category: {cat_name} ({cat['endpoint']})")
        items = tmdb.fetch_items_for_category(cat["endpoint"], cat["params"], cat["media_type"], cat_key)
        for it in items:
            tid = (it["media_type"], it["id"])
            if tid not in all_raw_items:
                all_raw_items[tid] = it
                all_raw_items[tid]["categories"] = [cat_key]
            else:
                if cat_key not in all_raw_items[tid]["categories"]:
                    all_raw_items[tid]["categories"].append(cat_key)

    total_candidates = len(all_raw_items)
    print(f"\n[2/3] Total unique candidates to verify: {total_candidates}")
    print("Checking availability on Webshare.cz and Přehraj.to in parallel (max 3 threads)...")

    verified_items = []
    items_list = list(all_raw_items.values())

    start_time = time.time()
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_item = {executor.submit(verify_availability, item): item for item in items_list}
        done_count = 0
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            done_count += 1
            try:
                has_stream, quality, has_cz_dub = future.result()
                status_str = f"AVAILABLE ({quality}, CZ Dub={has_cz_dub})" if has_stream else "SKIPPED (no streams)"
                title_disp = item.get("title") or item.get("original_title")
                print(f"  [{done_count}/{total_candidates}] {item['media_type'].upper()}: {title_disp} ({item.get('year')}) -> {status_str}")
                if has_stream:
                    item_copy = dict(item)
                    item_copy.pop("category", None)
                    item_copy["quality"] = quality
                    item_copy["has_cz_dubbing"] = has_cz_dub
                    verified_items.append(item_copy)
            except Exception as e:
                print(f"  [{done_count}/{total_candidates}] Error checking {item.get('title')}: {e}")

    elapsed = time.time() - start_time
    print(f"\nVerification finished in {elapsed:.1f}s.")
    print(f"Kept {len(verified_items)} of {total_candidates} titles (available on storage).")

    # Sort items by vote_average descending
    verified_items.sort(key=lambda x: (x.get("vote_average", 0.0), x.get("year", 0)), reverse=True)

    catalog_data = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "total_items": len(verified_items),
        "categories": categories_dict,
        "items": verified_items
    }

    output_path = os.path.abspath("catalog.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, ensure_ascii=False, indent=2)

    print(f"[3/3] Successfully generated catalog at: {output_path}")
    print(f"File size: {os.path.getsize(output_path) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
