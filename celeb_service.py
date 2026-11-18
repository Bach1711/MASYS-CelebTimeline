import os
import re
import datetime
from collections import Counter
import requests
from typing import Dict, Any, List, Optional

HEADERS = {
    "User-Agent": "CelebTimelineStudio/1.0 (https://localhost; contact@localhost)"
}


def _parse_birth_death_years(text: str, current_year: int) -> tuple:
    birth_year = None
    death_year = None
    span_match = re.search(r"\b(19\d{2}|20[01]\d)\s*[–—-]\s*(19\d{2}|20\d{2})\b", text or "")
    if span_match:
        birth_year = int(span_match.group(1))
        death_year = int(span_match.group(2))
    else:
        b_match = re.search(
            r"(?:born|b\.|\()\s*[^);\n]{0,40}?\b(19\d{2}|200\d)\b",
            text or "",
            flags=re.IGNORECASE,
        )
        if not b_match:
            b_match = re.search(r"\b(19\d{2})\b", (text or "")[:300])
        if b_match:
            birth_year = int(b_match.group(1))

        d_match = re.search(
            r"(?:died|d\.|passed away|–|—)[^);\n]{0,45}?\b(19\d{2}|20\d{2})\b",
            (text or "")[:800],
            flags=re.IGNORECASE,
        )
        if d_match:
            death_year = int(d_match.group(1))
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

    # Strictly reject disambiguation pages, lists, death-of articles, bands/companies/restaurants/films/albums
    if t_low.startswith("list of ") or t_low.startswith("death of ") or "in popular culture" in t_low:
        return -500.0

    hard_reject_phrases = [
        "may refer to",
        "disambiguation",
        "restaurant chain",
        "fast food",
        "multinational",
        "corporation",
        "company",
        "son of a ruler",
        "title of nobility",
        "monarch",
        "studio album",
        "discography",
        "filmography",
        "television series",
        "video game",
        "fictional character",
    ]
    if any(p in combined for p in hard_reject_phrases):
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
        score += 75.0
    elif q_low in clean_title:
        score += 20.0

    return score


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
    """
    clean_name = name.strip()
    current_year = datetime.datetime.now().year
    search_url = "https://en.wikipedia.org/w/api.php"

    wiki_title = exact_wiki_title or clean_name
    summary_data = {}
    full_extract = ""
    candidates_list: List[Dict[str, Any]] = []

    try:
        candidate_titles: List[str] = []
        if exact_wiki_title:
            candidate_titles.append(exact_wiki_title)

        # 1. Search Wikipedia with typo suggestion enabled
        r = requests.get(
            search_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": clean_name,
                "srinfo": "suggestion",
                "format": "json",
                "srlimit": 8,
            },
            headers=HEADERS,
            timeout=8,
        )
        suggestion = None
        if r.ok:
            q_data = r.json().get("query", {})
            suggestion = (q_data.get("searchinfo") or {}).get("suggestion")
            for h in q_data.get("search", []):
                if h.get("title") and h["title"] not in candidate_titles:
                    candidate_titles.append(h["title"])

        # If Wikipedia suggested a typo fix (e.g. "Michale Jackson" -> "michael jackson"), append suggested hits
        has_exact_hit = any(re.sub(r"\s*\([^)]*\)\s*$", "", t.lower()).strip() == clean_name.lower() for t in candidate_titles)
        if suggestion:
            r_sug = requests.get(
                search_url,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": suggestion,
                    "format": "json",
                    "srlimit": 5,
                },
                headers=HEADERS,
                timeout=6,
            )
            if r_sug.ok:
                for h in r_sug.json().get("query", {}).get("search", []):
                    if h.get("title") and h["title"] not in candidate_titles:
                        if has_exact_hit:
                            candidate_titles.append(h["title"])
                        else:
                            candidate_titles.insert(0, h["title"])

        # For single-word queries (like "Prince", "Madonna", "Cher"), also probe musician/actor disambiguations
        if " " not in clean_name:
            for suffix in ["(musician)", "(singer)", "(actor)", "(actress)"]:
                probe_t = f"{clean_name.capitalize()} {suffix}"
                if probe_t not in candidate_titles:
                    candidate_titles.insert(0, probe_t)

        effective_query = clean_name if has_exact_hit else (suggestion or clean_name)

        # 2. Batch-fetch intro extracts, descriptions, and thumbnails for candidate titles in 1 fast request
        batch_titles = candidate_titles[:10]
        if batch_titles:
            r_batch = requests.get(
                search_url,
                params={
                    "action": "query",
                    "prop": "pageprops|pageimages|extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "piprop": "thumbnail",
                    "pithumbsize": 300,
                    "redirects": 1,
                    "titles": "|".join(batch_titles),
                    "format": "json",
                },
                headers=HEADERS,
                timeout=8,
            )
            if r_batch.ok:
                pages = r_batch.json().get("query", {}).get("pages", {})
                scored_cands = []
                for pid, pinfo in pages.items():
                    if int(pid) < 0 or "missing" in pinfo:
                        continue
                    pt = pinfo.get("title") or ""
                    pdesc = (pinfo.get("pageprops") or {}).get("wikibase-shortdesc") or ""
                    pext = pinfo.get("extract") or ""
                    pthumb = (pinfo.get("thumbnail") or {}).get("source") or ""
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
                                "summary": pdesc or (pext[:120] + "..." if pext else ""),
                                "description": pdesc or (pext[:120] + "..." if pext else ""),
                                "birth_year": c_by or 1955,
                                "death_year": c_dy or current_year,
                                "thumb_url": pthumb,
                            }
                        )
                scored_cands.sort(key=lambda x: x["score"], reverse=True)
                candidates_list = scored_cands[:6]
                if candidates_list and not exact_wiki_title:
                    wiki_title = candidates_list[0]["wiki_title"]

        # 3. Fetch REST summary for the chosen wiki_title
        rest_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(wiki_title)}"
        r_sum = requests.get(rest_url, headers=HEADERS, timeout=8)
        if r_sum.ok:
            summary_data = r_sum.json()

        # 4. Fetch full plain-text extract (including Career & Filmography sections)
        r_ext = requests.get(
            search_url,
            params={
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "titles": wiki_title,
                "format": "json",
            },
            headers=HEADERS,
            timeout=10,
        )
        if r_ext.ok:
            pages = r_ext.json().get("query", {}).get("pages", {})
            for _, pinfo in pages.items():
                full_extract = pinfo.get("extract", "")
                break
    except Exception:
        pass

    raw_wiki_title = summary_data.get("title") or wiki_title
    canonical_name = re.sub(r"\s*\([^)]*\)\s*$", "", raw_wiki_title).strip() or clean_name
    description = summary_data.get("description") or ""
    extract = summary_data.get("extract") or ""
    combined_bio = f"{description}\n{extract}\n{full_extract[:2000]}"

    # Parse birth year & death year
    birth_year, death_year = _parse_birth_death_years(combined_bio, current_year)

    if not birth_year or birth_year < 1900 or birth_year > current_year - 15:
        birth_year = 1955
    if not death_year or death_year < birth_year + 18 or death_year > current_year:
        death_year = current_year

    # Count frequency of career years mentioned in full biography & filmography
    year_counter: Counter = Counter()
    for ym in re.findall(r"\b(19\d{2}|20\d{2})\b", full_extract or extract):
        yr = int(ym)
        if birth_year + 19 <= yr <= death_year:
            year_counter[yr] += 1

    notable_years = sorted(year_counter.keys())

    # Dynamically decide how many distinct milestones fit this celebrity's career depth:
    # - Always prioritize finding enough DISTINCT years (10-12 distinct years -> ~54-62s) so no year needs to be repeated!
    # - Only when a celebrity genuinely has fewer career years (< 9) will the pipeline repeat years to reach ~1 minute.
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
        summary_data.get("originalimage", {}).get("source")
        or summary_data.get("thumbnail", {}).get("source")
        or ""
    )
    thumb_url = summary_data.get("thumbnail", {}).get("source") or portrait_url

    candidate_image_urls: List[str] = [u for u in [thumb_url, portrait_url] if u]

    try:
        commons_url = "https://commons.wikimedia.org/w/api.php"
        rc = requests.get(
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
            headers=HEADERS,
            timeout=6,
        )
        if rc.ok:
            c_pages = rc.json().get("query", {}).get("pages", {})
            for _, cp in c_pages.items():
                iinfo = (cp.get("imageinfo") or [{}])[0]
                u = iinfo.get("thumburl") or iinfo.get("url")
                if u and u not in candidate_image_urls:
                    candidate_image_urls.append(u)
    except Exception:
        pass

    reference_images: List[str] = []
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        for idx, url in enumerate(dict.fromkeys(candidate_image_urls)):
            if len(reference_images) >= 4:
                break
            try:
                resp = requests.get(url, headers=HEADERS, timeout=8)
                if resp.ok and len(resp.content) > 1500:
                    ext = ".png" if ".png" in url.lower() else ".jpg"
                    out_p = os.path.join(save_dir, f"ref_portrait_{idx}{ext}")
                    with open(out_p, "wb") as f:
                        f.write(resp.content)
                    reference_images.append(out_p)
            except Exception:
                continue

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

    return {
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
        "candidates": candidates_list,
        "youtube_forbidden_terms": youtube_forbidden_terms,
    }


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
        return list(range(max(1950, death_year - count + 1), death_year + 1))

    counts = year_counts or {}
    notable_set = set(notable_years or [])

    # If celebrity has very few actual notable years in Wikipedia (<= count and >= 4), use those real notable years directly!
    if 4 <= len(notable_set) <= count:
        real_yrs = sorted(notable_set)
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
        chosen.append(best_y)

    if death_year not in chosen:
        chosen.append(death_year)

    return sorted(set(chosen))
