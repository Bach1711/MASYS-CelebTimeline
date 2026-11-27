import os
import re
import time
import json
import shutil
import hashlib
import datetime
import threading
from collections import Counter
import requests
from typing import Dict, Any, List, Optional

HEADERS = {
    "User-Agent": "MASYS-CelebTimeline/1.0 (https://github.com/Bach1711/MASYS-CelebTimeline; contact@masys.local) python-requests/2.32",
    "Accept": "application/json",
}

_WIKI_SESSION = requests.Session()
_WIKI_SESSION.headers.update(HEADERS)
_WIKI_RATE_SEMAPHORE = threading.Semaphore(2)
_WIKI_LAST_REQ_LOCK = threading.Lock()
_WIKI_LAST_REQ_TS = 0.0


def _wiki_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 10, max_retries: int = 4) -> Optional[requests.Response]:
    """
    Rate-limited, retry-enabled GET wrapper for Wikipedia / Wikimedia Commons / upload.wikimedia.org
    so concurrent 4-thread scans NEVER fail due to HTTP 429 Too Many Requests.
    """
    global _WIKI_LAST_REQ_TS
    with _WIKI_RATE_SEMAPHORE:
        for attempt in range(max_retries):
            with _WIKI_LAST_REQ_LOCK:
                now = time.time()
                wait_gap = 0.14 - (now - _WIKI_LAST_REQ_TS)
                if wait_gap > 0:
                    time.sleep(wait_gap)
                _WIKI_LAST_REQ_TS = time.time()
            try:
                resp = _WIKI_SESSION.get(url, params=params, timeout=timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = float(resp.headers.get("Retry-After") or (0.8 * (attempt + 1)))
                    time.sleep(min(4.0, max(0.6, retry_after)))
                    continue
                return resp
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
    return None


def _parse_birth_death_years(text: str, current_year: int) -> tuple:
    """
    Accurately parse birth_year and death_year from Wikipedia short descriptions and intro paragraphs,
    supporting full dates like `(June 1, 1926 – August 4, 1962)`, `(9 August 1963 – 11 February 2012)`,
    `(1926–1962)`, and `(born February 16, 1961)`.
    """
    birth_year = None
    death_year = None
    src = (text or "")[:1200]

    # Pattern 1: Direct year range `1926–1962` or full date range `June 1, 1926 – August 4, 1962` / `9 August 1963 – 11 February 2012`
    full_range_match = re.search(
        r"\b(19\d{2}|20[01]\d)\b\s*(?:[–—-]|\bto\b)\s*(?:[A-Za-z]+\.?\s+\d{1,2},?\s+|\d{1,2}\s+[A-Za-z]+\.?\s+)?\b(19\d{2}|20\d{2})\b",
        src,
        flags=re.IGNORECASE,
    )
    if full_range_match:
        by_cand = int(full_range_match.group(1))
        dy_cand = int(full_range_match.group(2))
        if 1880 <= by_cand <= current_year - 10 and by_cand + 15 <= dy_cand <= current_year:
            birth_year = by_cand
            death_year = dy_cand

    if not birth_year:
        b_match = re.search(
            r"(?:born|b\.|\()\s*[^);\n]{0,55}?\b(19\d{2}|200\d)\b",
            src,
            flags=re.IGNORECASE,
        )
        if not b_match:
            b_match = re.search(r"\b(19\d{2})\b", src[:350])
        if b_match:
            birth_year = int(b_match.group(1))

    if not death_year:
        d_match = re.search(
            r"(?:died|d\.|passed away|–|—)[^);\n]{0,55}?\b(19\d{2}|20\d{2})\b",
            src[:900],
            flags=re.IGNORECASE,
        )
        if d_match:
            dy_cand = int(d_match.group(1))
            if not birth_year or dy_cand >= birth_year + 15:
                death_year = dy_cand

    return birth_year, death_year


def _score_wikipedia_human_candidate(
    title: str,
    desc: str,
    extract: str,
    query_name: str,
    rank_idx: int,
) -> float:
    """
    Score a Wikipedia page to ensure we select a REAL HUMAN CELEBRITY biography
    (and never a generic concept like 'Prince = Son of a ruler' or a company like 'Pizza Hut').
    """
    t_low = (title or "").lower()
    d_low = (desc or "").lower()
    e_low = (extract or "")[:600].lower()
    combined = f"{d_low} {e_low}"
    q_low = (query_name or "").strip().lower()

    # Strictly reject disambiguation pages, lists, death-of articles, stage-and-screen/filmography subpages, bands/companies/films/albums
    reject_title_patterns = [
        "disambiguation",
        "list of ",
        "death of ",
        "in popular culture",
        "on stage and screen",
        "filmography",
        "discography",
        "videography",
        "bibliography",
        "awards and nominations",
        "career of ",
        "personal life of ",
        " (film)",
        " (album)",
        " (song)",
        " (band)",
        " (tv series)",
    ]
    if any(pat in t_low for pat in reject_title_patterns):
        return -500.0

    hard_reject_desc = [
        "may refer to",
        "disambiguation",
        "restaurant chain",
        "fast food",
        "multinational",
        "corporation",
        "company",
        "son of a ruler",
        "title of nobility",
        "studio album",
        "discography",
        "filmography",
        "television series",
        "video game",
        "fictional character",
    ]
    if any(p in d_low for p in hard_reject_desc):
        return -500.0
    if any(p in e_low[:180] for p in ["may refer to", "restaurant chain", "fast food", "son of a ruler", "title of nobility"]):
        return -500.0

    score = max(0.0, 20.0 - rank_idx * 2.0)

    # Check if biography has birth/death years of a human
    curr_yr = datetime.datetime.now().year
    by, dy = _parse_birth_death_years(f"{desc}\n{extract}", curr_yr)
    if by and 1880 <= by <= 2015:
        score += 45.0
    if dy and by and dy >= by + 18:
        score += 20.0
        if dy <= curr_yr and (f"–{dy}" in f"{desc} {extract}" or f"-{dy}" in f"{desc} {extract}" or "died" in combined or "was a" in combined or "was an" in combined):
            score += 30.0

    # Celebrity / Human profession keywords
    person_keywords = [
        "actor", "actress", "singer", "musician", "songwriter", "rapper", "comedian",
        "entertainer", "filmmaker", "director", "producer", "composer", "artist",
        "dancer", "performer", "television presenter", "host", "journalist", "author",
        "writer", "politician", "player", "athlete", "boxer", "wrestler", "model",
    ]
    if any(k in combined for k in person_keywords):
        score += 35.0

    if any(tag in t_low for tag in ["(musician)", "(singer)", "(actor)", "(actress)", "(comedian)", "(entertainer)"]):
        score += 30.0

    # If user typed a single word like "Prince", prefer the musician/singer over British royalty unless user typed "Prince Charles/William/Philip"
    if " " not in q_low and q_low == "prince":
        if "musician" in combined or "singer" in combined:
            score += 65.0
        if any(r in combined for r in ["british royal", "duke of", "prince of wales", "queen elizabeth"]):
            score -= 35.0

    clean_title = re.sub(r"\s*\([^)]*\)\s*$", "", t_low).strip()
    if clean_title == q_low:
        score += 140.0
    elif q_low in clean_title:
        score += 20.0

    return score


_BASE_DIR = (
    os.path.dirname(os.path.abspath(__import__("sys").executable))
    if getattr(__import__("sys"), "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
_CELEB_CACHE_ROOT = os.path.join(_BASE_DIR, "workspace", "_cache_celebs")


def _get_cache_dir_for_query(clean_name: str, exact_wiki_title: Optional[str]) -> str:
    key_raw = (exact_wiki_title or clean_name).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", key_raw).strip("_")[:40]
    h = hashlib.md5(key_raw.encode("utf-8")).hexdigest()[:8]
    return os.path.join(_CELEB_CACHE_ROOT, f"{slug}_{h}")


def lookup_celebrity(
    name: str,
    save_dir: Optional[str] = None,
    target_clip_count: Optional[int] = None,
    exact_wiki_title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Look up celebrity info from Wikipedia, automatically correcting typos (e.g., 'Michale Jackson' -> 'Michael Jackson')
    and disambiguating human celebrities (e.g., 'Prince' -> 'Prince (musician)'), while returning alternative
    human candidates for the user's Scan & Approve step.
    Protected against Wikipedia HTTP 429 rate-limiting via request batching, rate-gated retries,
    direct fallback to batch candidate metadata, and persistent disk caching.
    """
    clean_name = name.strip()
    current_year = datetime.datetime.now().year
    search_url = "https://en.wikipedia.org/w/api.php"

    # 0. Check persistent disk cache first (only reuse if cache has valid portraits and non-empty bio)
    cache_dir = _get_cache_dir_for_query(clean_name, exact_wiki_title)
    cache_json_p = os.path.join(cache_dir, "celeb_info.json")
    if os.path.exists(cache_json_p):
        try:
            with open(cache_json_p, "r", encoding="utf-8") as cf:
                cached = json.load(cf)
            cached_imgs = [p for p in (cached.get("reference_images") or []) if p and os.path.exists(p)]
            has_real_bio = bool(cached.get("description") or cached.get("extract"))
            if cached_imgs and has_real_bio:
                out_imgs = list(cached_imgs)
                if save_dir and os.path.abspath(save_dir) != os.path.abspath(cache_dir):
                    os.makedirs(save_dir, exist_ok=True)
                    out_imgs = []
                    for idx, src_img in enumerate(cached_imgs):
                        ext = os.path.splitext(src_img)[1] or ".jpg"
                        dst_img = os.path.join(save_dir, f"ref_portrait_{idx}{ext}")
                        if not os.path.exists(dst_img):
                            shutil.copy2(src_img, dst_img)
                        out_imgs.append(dst_img)
                cached["reference_images"] = out_imgs
                c_by = int(cached.get("birth_year") or 1955)
                c_dy = int(cached.get("death_year") or current_year)
                c_yrs = [int(y) for y in (cached.get("recommended_years") or []) if c_by + 18 <= int(y) <= c_dy]
                if (target_clip_count is not None and target_clip_count > 0) or len(c_yrs) != len(cached.get("recommended_years") or []):
                    cached["recommended_years"] = generate_career_years(
                        birth_year=c_by,
                        death_year=c_dy,
                        notable_years=cached.get("notable_years") or [],
                        year_counts={int(k): int(v) for k, v in (cached.get("year_counts") or {}).items()},
                        count=target_clip_count or len(cached.get("recommended_years") or []) or 9,
                    )
                return cached
        except Exception:
            pass

    wiki_title = exact_wiki_title or clean_name
    summary_data: Dict[str, Any] = {}
    full_extract = ""
    candidates_list: List[Dict[str, Any]] = []
    chosen_cand: Dict[str, Any] = {}

    try:
        candidate_titles: List[str] = []
        if exact_wiki_title:
            candidate_titles.append(exact_wiki_title)

        # 1. Search Wikipedia with typo suggestion enabled
        r = _wiki_get(
            search_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": clean_name,
                "srinfo": "suggestion",
                "format": "json",
                "srlimit": 8,
            },
            timeout=10,
        )
        suggestion = None
        if r and r.ok:
            q_data = r.json().get("query", {})
            suggestion = (q_data.get("searchinfo") or {}).get("suggestion")
            for h in q_data.get("search", []):
                if h.get("title") and h["title"] not in candidate_titles:
                    candidate_titles.append(h["title"])

        # If Wikipedia suggested a typo fix (e.g. "Michale Jackson" -> "michael jackson"), append suggested hits
        has_exact_hit = any(re.sub(r"\s*\([^)]*\)\s*$", "", t.lower()).strip() == clean_name.lower() for t in candidate_titles)
        if suggestion and not has_exact_hit:
            r_sug = _wiki_get(
                search_url,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": suggestion,
                    "format": "json",
                    "srlimit": 5,
                },
                timeout=8,
            )
            if r_sug and r_sug.ok:
                for h in r_sug.json().get("query", {}).get("search", []):
                    if h.get("title") and h["title"] not in candidate_titles:
                        candidate_titles.insert(0, h["title"])

        # For single-word queries (like "Prince", "Madonna", "Cher"), also probe musician/actor disambiguations
        if " " not in clean_name:
            for suffix in ["(musician)", "(singer)", "(actor)", "(actress)"]:
                probe_t = f"{clean_name.capitalize()} {suffix}"
                if probe_t not in candidate_titles:
                    candidate_titles.insert(0, probe_t)

        effective_query = clean_name if has_exact_hit else (suggestion or clean_name)

        # 2. Batch-fetch intro extracts, descriptions, and high-res 600px thumbnails for candidate titles in 1 request
        batch_titles = candidate_titles[:8]
        if batch_titles:
            r_batch = _wiki_get(
                search_url,
                params={
                    "action": "query",
                    "prop": "pageprops|pageimages|extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "piprop": "thumbnail|original",
                    "pithumbsize": 600,
                    "redirects": 1,
                    "titles": "|".join(batch_titles),
                    "format": "json",
                },
                timeout=10,
            )
            if r_batch and r_batch.ok:
                pages = r_batch.json().get("query", {}).get("pages", {})
                scored_cands = []
                for pid, pinfo in pages.items():
                    if int(pid) < 0 or "missing" in pinfo:
                        continue
                    pt = pinfo.get("title") or ""
                    pdesc = (pinfo.get("pageprops") or {}).get("wikibase-shortdesc") or ""
                    pext = pinfo.get("extract") or ""
                    pthumb = (
                        (pinfo.get("thumbnail") or {}).get("source")
                        or (pinfo.get("original") or {}).get("source")
                        or ""
                    )
                    porig = (pinfo.get("original") or {}).get("source") or pthumb
                    sc = _score_wikipedia_human_candidate(
                        pt,
                        pdesc,
                        pext,
                        query_name=effective_query,
                        rank_idx=batch_titles.index(pt) if pt in batch_titles else 5,
                    )
                    if exact_wiki_title and pt.lower() == exact_wiki_title.lower():
                        sc += 1000.0
                    if sc > -100.0:
                        c_by, c_dy = _parse_birth_death_years(f"{pdesc}\n{pext}", current_year)
                        scored_cands.append(
                            {
                                "score": sc,
                                "title": pt,
                                "wiki_title": pt,
                                "name": re.sub(r"\s*\([^)]*\)\s*$", "", pt).strip() or pt,
                                "summary": pdesc or (pext[:140] + "..." if pext else ""),
                                "description": pdesc or (pext[:140] + "..." if pext else ""),
                                "raw_desc": pdesc,
                                "raw_extract": pext,
                                "parsed_by": c_by,
                                "parsed_dy": c_dy,
                                "birth_year": c_by or 1955,
                                "death_year": c_dy or current_year,
                                "thumb_url": pthumb,
                                "orig_url": porig,
                            }
                        )
                scored_cands.sort(key=lambda x: x["score"], reverse=True)
                candidates_list = scored_cands[:6]
                if candidates_list:
                    if exact_wiki_title:
                        exact_matches = [c for c in candidates_list if c["wiki_title"].lower() == exact_wiki_title.lower()]
                        chosen_cand = exact_matches[0] if exact_matches else candidates_list[0]
                    else:
                        chosen_cand = candidates_list[0]
                    wiki_title = chosen_cand["wiki_title"]

        # 3. Only call REST summary if chosen_cand is missing extract or thumbnail
        if not chosen_cand.get("raw_extract") or not chosen_cand.get("thumb_url"):
            rest_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(wiki_title)}"
            r_sum = _wiki_get(rest_url, timeout=8)
            if r_sum and r_sum.ok:
                summary_data = r_sum.json()

        # 4. Fetch full plain-text extract (including Career & Filmography sections)
        r_ext = _wiki_get(
            search_url,
            params={
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "titles": wiki_title,
                "format": "json",
            },
            timeout=10,
        )
        if r_ext and r_ext.ok:
            pages = r_ext.json().get("query", {}).get("pages", {})
            for _, pinfo in pages.items():
                full_extract = pinfo.get("extract", "")
                break
    except Exception:
        pass

    raw_wiki_title = summary_data.get("title") or chosen_cand.get("wiki_title") or wiki_title
    canonical_name = re.sub(r"\s*\([^)]*\)\s*$", "", raw_wiki_title).strip() or clean_name
    description = (
        summary_data.get("description")
        or chosen_cand.get("raw_desc")
        or chosen_cand.get("description")
        or ""
    )
    extract = (
        summary_data.get("extract")
        or chosen_cand.get("raw_extract")
        or (full_extract[:800] if full_extract else "")
    )
    combined_bio = f"{description}\n{extract}\n{full_extract[:2500]}"

    # Parse birth year & death year (with fallback to chosen_cand's already-parsed years!)
    birth_year, death_year = _parse_birth_death_years(combined_bio, current_year)
    if not birth_year and chosen_cand.get("parsed_by"):
        birth_year = int(chosen_cand["parsed_by"])
    if not death_year and chosen_cand.get("parsed_dy"):
        death_year = int(chosen_cand["parsed_dy"])

    if not birth_year or birth_year < 1880 or birth_year > current_year - 12:
        birth_year = 1955
    if not death_year or death_year < birth_year + 15 or death_year > current_year:
        death_year = current_year

    # Count frequency of career years mentioned in full biography & filmography
    year_counter: Counter = Counter()
    for ym in re.findall(r"\b(19\d{2}|20\d{2})\b", full_extract or extract):
        yr = int(ym)
        if birth_year + 18 <= yr <= death_year:
            year_counter[yr] += 1

    notable_years = sorted(year_counter.keys())

    if target_clip_count is not None and target_clip_count > 0:
        adaptive_count = target_clip_count
    else:
        career_span = max(10, death_year - (birth_year + 20))
        if len(notable_years) >= 12 and career_span >= 30:
            adaptive_count = 12
        elif len(notable_years) >= 9 and career_span >= 20:
            adaptive_count = 11
        elif len(notable_years) >= 7 and career_span >= 14:
            adaptive_count = 10
        elif len(notable_years) >= 5:
            adaptive_count = len(notable_years)
        else:
            adaptive_count = max(6, len(notable_years) or 7)

    recommended_years = generate_career_years(
        birth_year=birth_year,
        death_year=death_year,
        notable_years=notable_years,
        year_counts=dict(year_counter),
        count=adaptive_count,
    )

    portrait_url = (
        (summary_data.get("originalimage") or {}).get("source")
        or (summary_data.get("thumbnail") or {}).get("source")
        or chosen_cand.get("orig_url")
        or chosen_cand.get("thumb_url")
        or ""
    )
    thumb_url = (
        (summary_data.get("thumbnail") or {}).get("source")
        or chosen_cand.get("thumb_url")
        or portrait_url
    )

    candidate_image_urls: List[str] = [u for u in [thumb_url, portrait_url] if u]
    for c_item in candidates_list[:2]:
        if c_item.get("name", "").lower() == canonical_name.lower() and c_item.get("thumb_url"):
            if c_item["thumb_url"] not in candidate_image_urls:
                candidate_image_urls.append(c_item["thumb_url"])

    try:
        commons_url = "https://commons.wikimedia.org/w/api.php"
        rc = _wiki_get(
            commons_url,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f'filetype:bitmap "{canonical_name}"',
                "gsrnamespace": 6,
                "gsrlimit": 5,
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 600,
                "format": "json",
            },
            timeout=8,
        )
        if rc and rc.ok:
            c_pages = rc.json().get("query", {}).get("pages", {})
            for _, cp in c_pages.items():
                iinfo = (cp.get("imageinfo") or [{}])[0]
                u = iinfo.get("thumburl") or iinfo.get("url")
                if u and u not in candidate_image_urls:
                    candidate_image_urls.append(u)
    except Exception:
        pass

    target_img_dir = save_dir or cache_dir
    os.makedirs(target_img_dir, exist_ok=True)
    reference_images: List[str] = []
    for idx, url in enumerate(dict.fromkeys(candidate_image_urls)):
        if len(reference_images) >= 4:
            break
        try:
            resp = _wiki_get(url, timeout=10)
            if resp and resp.ok and len(resp.content) > 1500:
                ext = ".png" if ".png" in url.lower() else ".jpg"
                out_p = os.path.join(target_img_dir, f"ref_portrait_{idx}{ext}")
                with open(out_p, "wb") as f:
                    f.write(resp.content)
                reference_images.append(out_p)
        except Exception:
            continue

    # Clean internal temporary keys from candidates_list before returning/saving
    clean_cands = []
    for c in candidates_list:
        clean_cands.append(
            {
                "score": c["score"],
                "title": c["title"],
                "wiki_title": c["wiki_title"],
                "name": c["name"],
                "summary": c["summary"],
                "description": c["description"],
                "birth_year": c["birth_year"],
                "death_year": c["death_year"],
                "thumb_url": c["thumb_url"],
            }
        )

    youtube_forbidden_terms: List[str] = []
    if canonical_name.lower() == "prince" and any(k in combined_bio.lower() for k in ["musician", "singer", "songwriter"]):
        youtube_forbidden_terms = [
            "prince charles",
            "prince of wales",
            "prince william",
            "prince harry",
            "prince philip",
            "prince andrew",
            "prince edward",
            "princess diana",
            "happy prince",
            "royal family",
        ]

    auto_corrected_from = clean_name if canonical_name.lower() != clean_name.lower() and not exact_wiki_title else None

    result_payload = {
        "name": canonical_name,
        "wiki_title": raw_wiki_title,
        "input_query": clean_name,
        "auto_corrected_from": auto_corrected_from,
        "description": description,
        "extract": extract,
        "birth_year": birth_year,
        "death_year": death_year,
        "age_at_death": death_year - birth_year,
        "portrait_url": thumb_url or portrait_url,
        "reference_images": reference_images,
        "recommended_years": recommended_years,
        "notable_years": notable_years,
        "year_counts": dict(year_counter),
        "candidates": clean_cands,
        "youtube_forbidden_terms": youtube_forbidden_terms,
    }

    # Save valid lookup to persistent disk cache so subsequent runs never hit Wikipedia again
    if reference_images and (description or extract):
        try:
            os.makedirs(cache_dir, exist_ok=True)
            cached_refs = []
            for idx, r_img in enumerate(reference_images):
                if os.path.exists(r_img):
                    ext = os.path.splitext(r_img)[1] or ".jpg"
                    c_img = os.path.join(cache_dir, f"ref_portrait_{idx}{ext}")
                    if os.path.abspath(r_img) != os.path.abspath(c_img):
                        shutil.copy2(r_img, c_img)
                    cached_refs.append(c_img)
            cache_copy = dict(result_payload)
            cache_copy["reference_images"] = cached_refs
            with open(cache_json_p, "w", encoding="utf-8") as cf:
                json.dump(cache_copy, cf, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return result_payload


def generate_career_years(
    birth_year: int,
    death_year: int,
    notable_years: Optional[List[int]] = None,
    year_counts: Optional[Dict[int, int]] = None,
    count: int = 10,
) -> List[int]:
    """
    Generate `count` well-distributed years across the celebrity's career,
    snapping to the most frequently mentioned career/filmography years in Wikipedia
    so each chosen year corresponds to a major career milestone with abundant video footage.
    """
    start_year = max(birth_year + 21, 1960)
    if start_year >= death_year - 3:
        start_year = max(birth_year + 18, death_year - count)

    if death_year - start_year + 1 <= count:
        return list(range(max(birth_year + 18, death_year - count + 1), death_year + 1))

    counts = year_counts or {}
    notable_set = set(notable_years or [])

    # If celebrity has very few actual notable years in Wikipedia (<= count and >= 4), use those real notable years directly!
    if 4 <= len(notable_set) <= count:
        real_yrs = sorted(y for y in notable_set if birth_year + 18 <= y <= death_year)
        if death_year not in real_yrs:
            real_yrs.append(death_year)
        return sorted(set(real_yrs))

    step = (death_year - start_year) / float(max(1, count - 1))
    chosen: List[int] = []

    for i in range(count - 1):
        ideal = int(round(start_year + i * step))
        min_cand = max(start_year, (chosen[-1] + 2) if chosen else start_year)
        max_cand = min(death_year - 2, int(round(start_year + (i + 0.65) * step)))
        if max_cand < min_cand:
            max_cand = min(death_year - 1, min_cand + 2)

        best_y = ideal
        best_weight = -1.0
        for cand in range(min_cand, max_cand + 1):
            if cand in chosen:
                continue
            freq = counts.get(cand, 1 if cand in notable_set else 0)
            dist_pen = abs(cand - ideal) * 0.35
            weight = freq * 2.0 - dist_pen
            if weight > best_weight:
                best_weight = weight
                best_y = cand

        if best_y in chosen:
            best_y = max(min_cand, ideal)
        while best_y in chosen and best_y < death_year - 1:
            best_y += 1
        if best_y <= death_year and best_y not in chosen:
            chosen.append(best_y)

    if death_year not in chosen:
        chosen.append(death_year)

    return sorted(y for y in set(chosen) if birth_year + 18 <= y <= death_year)
