import os
import re
import datetime
from collections import Counter
import requests
from typing import Dict, Any, List, Optional

HEADERS = {
    "User-Agent": "CelebTimelineStudio/1.0 (https://localhost; contact@localhost)"
}


def lookup_celebrity(name: str, save_dir: Optional[str] = None, target_clip_count: Optional[int] = None) -> Dict[str, Any]:
    """
    Look up celebrity info from Wikipedia, extract birth/death years, career years weighted by
    actual prominence in their biography/filmography, and download multiple reference portrait images
    for robust AI face verification across decades.
    """
    clean_name = name.strip()
    current_year = datetime.datetime.now().year

    wiki_title = clean_name
    summary_data = {}
    full_extract = ""

    try:
        # 1. Search Wikipedia for exact page title
        search_url = "https://en.wikipedia.org/w/api.php"
        r = requests.get(
            search_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": clean_name,
                "format": "json",
                "srlimit": 3,
            },
            headers=HEADERS,
            timeout=8,
        )
        if r.ok:
            s_hits = r.json().get("query", {}).get("search", [])
            if s_hits:
                wiki_title = s_hits[0]["title"]

        # 2. Fetch REST summary
        rest_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(wiki_title)}"
        r_sum = requests.get(rest_url, headers=HEADERS, timeout=8)
        if r_sum.ok:
            summary_data = r_sum.json()

        # 3. Fetch full plain-text extract (including Career & Filmography sections)
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

    canonical_name = summary_data.get("title") or clean_name
    canonical_name = re.sub(r"\s*\([^)]*\)\s*$", "", canonical_name).strip() or clean_name
    description = summary_data.get("description") or ""
    extract = summary_data.get("extract") or ""
    combined_bio = f"{description}\n{extract}\n{full_extract[:2000]}"

    # Parse birth year & death year
    birth_year = None
    death_year = None

    span_match = re.search(r"\b(19\d{2}|20[01]\d)\s*[–—-]\s*(19\d{2}|20\d{2})\b", combined_bio)
    if span_match:
        birth_year = int(span_match.group(1))
        death_year = int(span_match.group(2))
    else:
        b_match = re.search(
            r"(?:born|b\.)[^);\n]{0,40}?\b(19\d{2}|200\d)\b",
            combined_bio,
            flags=re.IGNORECASE,
        )
        if not b_match:
            b_match = re.search(r"\b(19\d{2})\b", combined_bio[:300])
        if b_match:
            birth_year = int(b_match.group(1))

        d_match = re.search(
            r"(?:died|d\.|passed away)[^);\n]{0,45}?\b(19\d{2}|20\d{2})\b",
            combined_bio[:800],
            flags=re.IGNORECASE,
        )
        if d_match:
            death_year = int(d_match.group(1))

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

    return {
        "name": canonical_name,
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
