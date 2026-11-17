import os
import re
import time
import shutil
import datetime
import subprocess
from typing import List, Dict, Any, Optional, Set, Tuple
import yt_dlp
import numpy as np

from face_engine import FaceEngine

FFMPEG_BIN = shutil.which("ffmpeg") or r"C:\Users\My-PC\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
FFPROBE_BIN = shutil.which("ffprobe") or r"C:\Users\My-PC\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"


def get_video_duration(path: str) -> float:
    """Return duration of local video file in seconds."""
    if not os.path.exists(path):
        return 0.0
    try:
        cmd = [
            FFPROBE_BIN,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(res.stdout.strip())
    except Exception:
        return 0.0


def cut_video_clip(
    input_path: str,
    output_path: str,
    start_sec: float,
    duration_sec: float,
    keep_audio: bool = True,
) -> bool:
    """Cut a clean H.264 MP4 clip for browser preview and final composition."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-ss",
        f"{max(0.0, start_sec):.2f}",
        "-i",
        input_path,
        "-t",
        f"{max(1.0, duration_sec):.2f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
    ]
    if keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100"]
    else:
        cmd += ["-an"]
    cmd.append(output_path)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000
    except Exception:
        return False


def extract_thumbnail(video_path: str, thumb_path: str, time_sec: float = 1.5) -> bool:
    """Extract a JPEG thumbnail from a video clip."""
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-ss",
        f"{max(0.0, time_sec):.2f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        thumb_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return res.returncode == 0 and os.path.exists(thumb_path)
    except Exception:
        return False


def search_youtube(query: str, max_results: int = 8) -> List[Dict[str, Any]]:
    """Search YouTube via yt-dlp flat extraction for fast candidate discovery."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    results = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
            entries = info.get("entries", []) if info else []
            for e in entries:
                if not e or not e.get("id"):
                    continue
                vid = e["id"]
                dur = float(e.get("duration") or 0)
                if dur > 0 and (dur < 8 or dur > 1200):
                    continue
                thumbs = e.get("thumbnails") or []
                thumb_url = thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                results.append(
                    {
                        "video_id": vid,
                        "title": e.get("title") or "",
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration": dur,
                        "channel": e.get("uploader") or e.get("channel") or "",
                        "view_count": e.get("view_count") or 0,
                        "thumbnail": thumb_url,
                    }
                )
    except Exception:
        pass
    return results


def fetch_video_transcript(video_id: str) -> List[Dict[str, Any]]:
    """
    Fetch timed English transcript for a YouTube video using youtube_transcript_api.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        if hasattr(YouTubeTranscriptApi, "fetch"):
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB", "en-CA"])
            items = []
            for snippet in fetched:
                if isinstance(snippet, dict):
                    items.append(snippet)
                else:
                    items.append(
                        {
                            "text": getattr(snippet, "text", ""),
                            "start": float(getattr(snippet, "start", 0.0)),
                            "duration": float(getattr(snippet, "duration", 2.0)),
                        }
                    )
            return items
        else:
            return YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-US", "en-GB"])
    except Exception:
        return []


def locate_news_hook_in_transcript(
    transcript: List[Dict[str, Any]],
    celeb_name: str,
    target_duration: float = 11.5,
    min_duration: float = 8.5,
    max_duration: float = 15.0,
) -> Optional[Dict[str, Any]]:
    """
    Find the best 8-15s anchor intro sentence announcing the celebrity's death.
    """
    if not transcript:
        return None

    name_parts = [p.lower() for p in re.split(r"\s+", celeb_name.strip()) if len(p) >= 2]
    last_name = name_parts[-1] if name_parts else celeb_name.lower()
    full_lower = celeb_name.lower()

    death_keywords = [
        "died",
        "dead",
        "dies",
        "passed away",
        "passing",
        "breaking news",
        "sad news",
        "sad breaking",
        "losing",
        "lost",
        "years old",
        "age of",
        "at age",
        "tribute",
        "remembering",
        "actress",
        "actor",
        "legend",
        "star",
    ]

    best_idx = None
    best_score = -1.0

    for i, item in enumerate(transcript):
        window_items = transcript[i : i + 3]
        window_text = " ".join(x.get("text", "") for x in window_items).lower()
        single_text = item.get("text", "").lower()
        t_start = float(item.get("start", 0.0))

        score = 0.0
        if full_lower in window_text:
            score += 6.0
        elif last_name in window_text:
            score += 3.5

        if score == 0.0 and t_start > 25.0:
            continue

        if last_name in single_text:
            score += 2.0

        for kw in death_keywords:
            if kw in window_text:
                score += 1.8

        if t_start <= 20.0 and score > 0:
            score += 2.5

        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx is None or best_score <= 0:
        return None

    start_idx = best_idx
    if best_idx > 0:
        prev_text = transcript[best_idx - 1].get("text", "").lower()
        prev_start = float(transcript[best_idx - 1].get("start", 0.0))
        curr_start = float(transcript[best_idx].get("start", 0.0))
        if curr_start - prev_start <= 4.5 and any(
            k in prev_text for k in ["breaking", "sad", "news", "report", "tonight", "afternoon", "morning", "learning"]
        ):
            start_idx = best_idx - 1

    raw_start = max(0.0, float(transcript[start_idx].get("start", 0.0)) - 0.25)

    end_sec = raw_start + target_duration
    collected_text = []
    for j in range(start_idx, min(len(transcript), start_idx + 12)):
        seg = transcript[j]
        s_start = float(seg.get("start", 0.0))
        s_dur = min(5.0, float(seg.get("duration", 2.5)))
        s_end = s_start + s_dur
        collected_text.append(seg.get("text", ""))
        elapsed = s_end - raw_start
        if elapsed >= min_duration:
            if elapsed <= max_duration:
                end_sec = s_end + 0.2
            else:
                end_sec = raw_start + target_duration
            if elapsed >= target_duration - 1.5:
                break

    final_dur = max(min_duration, min(max_duration, end_sec - raw_start))
    return {
        "start_sec": round(raw_start, 2),
        "duration_sec": round(final_dur, 2),
        "end_sec": round(raw_start + final_dur, 2),
        "transcript_excerpt": " ".join(collected_text).strip(),
        "match_score": round(best_score, 2),
    }


def download_youtube_section(
    video_id_or_url: str,
    output_path: str,
    start_sec: float = 0.0,
    duration_sec: float = 45.0,
    with_audio: bool = True,
) -> bool:
    """
    Download a specific time section of a YouTube video quickly.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    url = (
        video_id_or_url
        if video_id_or_url.startswith("http")
        else f"https://www.youtube.com/watch?v={video_id_or_url}"
    )

    if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
        return True

    end_sec = start_sec + duration_sec
    fmt = (
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best"
        if with_audio
        else "bestvideo[height<=720][ext=mp4]/best[height<=720][ext=mp4]/best[height<=720]"
    )

    temp_base = output_path.replace(".mp4", "_temp")
    ydl_opts = {
        "format": fmt,
        "outtmpl": temp_base + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "ffmpeg_location": os.path.dirname(FFMPEG_BIN),
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
        "force_keyframes_at_cuts": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        for ext in [".mp4", ".mkv", ".webm"]:
            cand = temp_base + ext
            if os.path.exists(cand) and os.path.getsize(cand) > 5000:
                if ext == ".mp4":
                    shutil.move(cand, output_path)
                else:
                    cut_video_clip(cand, output_path, 0, duration_sec, keep_audio=with_audio)
                    os.remove(cand)
                return True
    except Exception:
        pass

    # Fallback: download without range if video is under 6 minutes, then trim with ffmpeg
    try:
        ydl_opts_full = {
            "format": fmt,
            "outtmpl": temp_base + "_full.%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
            "ffmpeg_location": os.path.dirname(FFMPEG_BIN),
        }
        with yt_dlp.YoutubeDL(ydl_opts_full) as ydl:
            ydl.download([url])
        for ext in [".mp4", ".mkv", ".webm"]:
            cand = temp_base + "_full" + ext
            if os.path.exists(cand) and os.path.getsize(cand) > 5000:
                ok = cut_video_clip(cand, output_path, start_sec, duration_sec, keep_audio=with_audio)
                try:
                    os.remove(cand)
                except Exception:
                    pass
                return ok
    except Exception:
        pass

    return False


def find_and_prepare_news_clip(
    celeb_name: str,
    project_dir: str,
    target_duration: float = 11.5,
    custom_url: Optional[str] = None,
    candidate_index: int = 0,
) -> Dict[str, Any]:
    """
    Search YouTube for celebrity death news, locate the exact 8-15s anchor intro hook,
    and cut the ready-to-preview news clip with audio.
    """
    news_dir = os.path.join(project_dir, "news")
    os.makedirs(news_dir, exist_ok=True)

    candidates: List[Dict[str, Any]] = []
    if custom_url:
        vid_match = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", custom_url)
        vid = vid_match.group(1) if vid_match else "custom"
        candidates.append(
            {
                "video_id": vid,
                "title": f"Custom News Source ({vid})",
                "url": custom_url,
                "duration": 120.0,
                "channel": "Custom URL",
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            }
        )
    else:
        queries = [
            f"{celeb_name} dead breaking news",
            f"{celeb_name} dies news report",
            f"Celebrity deaths news {celeb_name}",
        ]
        seen_ids = set()
        for q in queries:
            for item in search_youtube(q, max_results=6):
                vid = item["video_id"]
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)
                title_l = item["title"].lower()
                name_parts = [p.lower() for p in celeb_name.split() if len(p) > 2]
                rel = 0
                if any(p in title_l for p in name_parts):
                    rel += 5
                if any(k in title_l for k in ["dead", "dies", "died", "passed away", "breaking", "news", "death", "tribute"]):
                    rel += 4
                item["relevance"] = rel
                candidates.append(item)
            if len(candidates) >= 8:
                break

        candidates.sort(key=lambda x: x.get("relevance", 0), reverse=True)

    if not candidates:
        raise RuntimeError(f"Không tìm thấy video bản tin cho '{celeb_name}'.")

    ordered = candidates[candidate_index % len(candidates) :] + candidates[: candidate_index % len(candidates)]

    for cand in ordered[:4]:
        vid = cand["video_id"]
        transcript = fetch_video_transcript(vid) if vid != "custom" else []
        hook = locate_news_hook_in_transcript(
            transcript,
            celeb_name=celeb_name,
            target_duration=target_duration,
            min_duration=8.5,
            max_duration=15.0,
        )

        if hook:
            start_sec = hook["start_sec"]
            clip_dur = hook["duration_sec"]
            excerpt = hook["transcript_excerpt"]
        else:
            start_sec = 0.0
            clip_dur = target_duration
            excerpt = "(Tự động lấy câu dẫn mở đầu của bản tin)"

        raw_path = os.path.join(news_dir, f"raw_{vid}_{int(start_sec)}.mp4")
        dl_start = max(0.0, start_sec - 2.0)
        dl_dur = clip_dur + 8.0

        if download_youtube_section(cand["url"], raw_path, start_sec=dl_start, duration_sec=dl_dur, with_audio=True):
            rel_start = start_sec - dl_start
            out_clip = os.path.join(news_dir, f"news_clip_{vid}_{int(start_sec)}.mp4")
            thumb_path = os.path.join(news_dir, f"news_thumb_{vid}_{int(start_sec)}.jpg")
            if cut_video_clip(raw_path, out_clip, start_sec=rel_start, duration_sec=clip_dur, keep_audio=True):
                extract_thumbnail(out_clip, thumb_path, time_sec=min(2.0, clip_dur * 0.3))
                actual_dur = get_video_duration(out_clip) or clip_dur
                return {
                    "video_id": vid,
                    "title": cand["title"],
                    "url": cand["url"],
                    "channel": cand.get("channel", ""),
                    "start_sec": round(start_sec, 2),
                    "duration_sec": round(actual_dur, 2),
                    "transcript_excerpt": excerpt,
                    "clip_path": out_clip,
                    "raw_path": raw_path,
                    "raw_offset": round(dl_start, 2),
                    "thumb_path": thumb_path,
                    "candidate_index": candidates.index(cand),
                    "candidates": candidates[:8],
                }

    raise RuntimeError("Không thể tải đoạn video bản tin từ YouTube.")


def _normalize_text(s: str) -> str:
    return re.sub(r"[’‘`]", "'", (s or "").lower())


def _extract_years_from_title(title: str) -> List[int]:
    """Extract explicit 4-digit years (and 2-digit dates like 1/14/90) from a video title."""
    years = [int(y) for y in re.findall(r"\b(19[4-9]\d|20[0-2]\d)\b", title)]
    for m in re.findall(r"\b\d{1,2}/\d{1,2}/([6-9]\d)\b", title):
        years.append(1900 + int(m))
    return sorted(set(years))


def _score_and_validate_year_candidate(
    item: Dict[str, Any],
    celeb_name: str,
    target_year: int,
    year_window: Tuple[int, int],
    current_year: int,
) -> Optional[Dict[str, Any]]:
    """
    Validate and score a YouTube candidate for a specific career milestone year.
    Returns None if the candidate should be strictly rejected.
    """
    title_l = _normalize_text(item.get("title", ""))
    channel_l = _normalize_text(item.get("channel", ""))
    combined_l = f"{title_l} {channel_l}"

    name_clean = _normalize_text(celeb_name)
    name_parts = [p for p in re.split(r"\s+", name_clean) if len(p) >= 2]
    last_name = name_parts[-1] if name_parts else name_clean
    last_name_no_apos = last_name.replace("'", "")

    # 1. Must mention the celebrity's last name
    if last_name not in combined_l and last_name_no_apos not in combined_l.replace("'", ""):
        return None

    # 2. Strictly reject multi-year compilation ranges like "1954-2026", "1983-2024", "2008-2025"
    if re.search(r"\b(19\d{2}|20\d{2})\s*[-–—~to]+\s*(19\d{2}|20\d{2})\b", title_l):
        return None

    # 3. Strictly reject death reports, other-celebrity tributes, and multi-actor movie trailers
    always_forbidden = [
        "evolution",
        "transformation",
        "then and now",
        "through the years",
        "from 21 to",
        "dies",
        "died",
        "dead",
        "passed away",
        "passing",
        "breaking news",
        "sad news",
        "funeral",
        "in memoriam",
        "memoriam",
        "honoring",
        "honors",
        "pays tribute",
        "pay tribute",
        "tribute to",
        "remembering",
        "reacts to",
        "accepts late",
        "late costar",
        "official trailer",
        "trailer #",
        "teaser trailer",
    ]
    if any(k in title_l for k in always_forbidden):
        return None

    if target_year < current_year - 1:
        historical_forbidden = [
            "last interview",
            "before her death",
            "before his death",
            "before death",
            "final interview",
            "last appeared",
            "last public",
        ]
        if any(k in title_l for k in historical_forbidden):
            return None

    # 4. Validate explicit years in the video title
    min_yr, max_yr = year_window
    title_years = _extract_years_from_title(title_l)

    score = 0
    matched_year = None
    has_explicit_year = False

    if title_years:
        valid_tys = [ty for ty in title_years if min_yr <= ty <= max_yr]
        if not valid_tys:
            return None
        has_explicit_year = True
        matched_year = min(valid_tys, key=lambda ty: abs(ty - target_year))
        diff = abs(matched_year - target_year)
        if diff == 0:
            score += 25
        elif diff == 1:
            score += 18
        else:
            score += 14
    else:
        if target_year < current_year - 2:
            score -= 12
        else:
            score += 4

    if name_clean in title_l or name_clean.replace("'", "") in title_l.replace("'", ""):
        score += 6
    if any(k in title_l for k in ["interview", "talk", "speech", "acceptance", "monologue", "commercial", "late night", "tonight show", "letterman", "sctv", "scene", "clip"]):
        score += 4

    dur = float(item.get("duration") or 120.0)
    if 20 <= dur <= 480:
        score += 3
    elif dur > 720:
        score -= 4

    cand = dict(item)
    cand["relevance"] = score
    cand["has_explicit_year"] = has_explicit_year
    cand["matched_year"] = matched_year or target_year
    return cand


def find_and_prepare_year_clip(
    celeb_name: str,
    year: int,
    project_dir: str,
    face_engine: FaceEngine,
    ref_embeddings: List[np.ndarray],
    clip_duration: float = 4.2,
    min_clip_duration: float = 3.5,
    max_clip_duration: float = 5.4,
    candidate_index: int = 0,
    segment_index: int = 0,
    custom_url: Optional[str] = None,
    year_window: Optional[Tuple[int, int]] = None,
    used_video_ids: Optional[Set[str]] = None,
    scene_index: int = 1,
) -> Dict[str, Any]:
    """
    Search YouTube for `<Celeb Name> <Year>`, strictly validate that candidate videos
    actually belong to that year/era, download active sections, and verify via FaceEngine
    that the main celebrity is clearly on screen with adaptive shot duration (3.5s - 5.4s).
    """
    current_year = datetime.datetime.now().year
    win = year_window or (year - 2, min(current_year, year + 2))
    used_ids = used_video_ids or set()

    year_dir = os.path.join(project_dir, f"year_{year}")
    os.makedirs(year_dir, exist_ok=True)

    candidates: List[Dict[str, Any]] = []
    if custom_url:
        vid_match = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", custom_url)
        vid = vid_match.group(1) if vid_match else f"custom_{year}"
        candidates.append(
            {
                "video_id": vid,
                "title": f"Custom Video ({year})",
                "url": custom_url,
                "duration": 90.0,
                "channel": "Custom URL",
                "relevance": 100,
                "has_explicit_year": True,
                "matched_year": year,
            }
        )
    else:
        primary_queries = [
            f"{celeb_name} {year}",
            f"{celeb_name} {year} interview",
        ]
        if year >= current_year:
            primary_queries.append(f"{celeb_name} {year - 1} interview")
            primary_queries.append(f"{celeb_name} {year - 1} speech")

        seen: Set[str] = set()

        def _collect_from_queries(q_list: List[str]):
            for q in q_list:
                for item in search_youtube(q, max_results=7):
                    vid = item["video_id"]
                    if vid in seen or vid in used_ids:
                        continue
                    seen.add(vid)
                    scored = _score_and_validate_year_candidate(
                        item,
                        celeb_name=celeb_name,
                        target_year=year,
                        year_window=win,
                        current_year=current_year,
                    )
                    if scored is not None:
                        candidates.append(scored)

        _collect_from_queries(primary_queries)

        explicit_count = sum(1 for c in candidates if c.get("has_explicit_year"))
        if explicit_count < 2:
            adj_queries = []
            for delta in [-1, 1, -2, 2]:
                adj_y = year + delta
                if win[0] <= adj_y <= win[1]:
                    adj_queries.append(f"{celeb_name} {adj_y}")
                    adj_queries.append(f"{celeb_name} {adj_y} interview")
            _collect_from_queries(adj_queries[:4])

        explicit_cands = [c for c in candidates if c.get("has_explicit_year")]
        if explicit_cands and year < current_year - 1:
            candidates = explicit_cands

        candidates.sort(key=lambda x: (x.get("has_explicit_year", False), x.get("relevance", 0)), reverse=True)

    if not candidates:
        raise RuntimeError(f"Không tìm thấy video phù hợp cho năm {year}.")

    idx = candidate_index % len(candidates)
    ordered = candidates[idx:] + candidates[:idx]

    best_fallback: Optional[Dict[str, Any]] = None
    best_fallback_score = -1.0

    for cand in ordered[:4]:
        vid = cand["video_id"]
        vid_dur = float(cand.get("duration") or 90.0)

        if vid_dur <= 45.0:
            windows = [(0.0, vid_dur)]
        elif vid_dur <= 120.0:
            windows = [(6.0, min(50.0, vid_dur - 6.0))]
        else:
            w1_start = round(min(40.0, max(12.0, vid_dur * 0.20)), 1)
            w2_start = round(min(vid_dur - 45.0, w1_start + 50.0), 1)
            windows = [(w1_start, 50.0)]
            if w2_start > w1_start + 20.0:
                windows.append((w2_start, 50.0))

        for w_idx, (dl_start, dl_dur) in enumerate(windows):
            raw_path = os.path.join(year_dir, f"raw_{vid}_w{w_idx}.mp4")
            if not download_youtube_section(cand["url"], raw_path, start_sec=dl_start, duration_sec=dl_dur, with_audio=False):
                continue

            segments = face_engine.find_best_segments(
                raw_path,
                clip_duration=clip_duration,
                min_duration=min_clip_duration,
                max_duration=max_clip_duration,
                ref_embeddings=ref_embeddings,
                sample_fps=2.0,
                top_k=6,
                skip_start_sec=0.5,
            )
            if not segments:
                continue

            seg_idx = segment_index % len(segments)
            chosen_seg = segments[seg_idx]
            rel_start = float(chosen_seg["start_sec"])
            seg_dur = float(chosen_seg.get("duration_sec") or clip_duration)
            sim = float(chosen_seg.get("similarity", 0.0))
            m_ratio = float(chosen_seg.get("match_ratio", 0.0))
            f_ratio = float(chosen_seg.get("face_ratio", 0.0))

            out_clip = os.path.join(year_dir, f"clip_{year}_{vid}_w{w_idx}_s{seg_idx}_sc{scene_index}.mp4")
            thumb_path = os.path.join(year_dir, f"thumb_{year}_{vid}_w{w_idx}_s{seg_idx}_sc{scene_index}.jpg")

            if not cut_video_clip(raw_path, out_clip, start_sec=rel_start, duration_sec=seg_dur, keep_audio=False):
                continue

            extract_thumbnail(out_clip, thumb_path, time_sec=seg_dur * 0.4)
            actual_dur = get_video_duration(out_clip) or seg_dur
            display_year = int(cand.get("matched_year") or year)

            result_dict = {
                "clip_id": f"{display_year}_sc{scene_index}_{int(time.time()*100)%10000}",
                "year": display_year,
                "requested_year": year,
                "scene_index": scene_index,
                "video_id": vid,
                "title": cand["title"],
                "url": cand["url"],
                "channel": cand.get("channel", ""),
                "start_sec": round(dl_start + rel_start, 2),
                "rel_start_sec": round(rel_start, 2),
                "raw_offset": round(dl_start, 2),
                "duration_sec": round(actual_dur, 2),
                "face_score": chosen_seg.get("score", 0.0),
                "face_similarity": sim,
                "face_ratio": f_ratio,
                "match_ratio": m_ratio,
                "clip_path": out_clip,
                "raw_path": raw_path,
                "thumb_path": thumb_path,
                "candidate_index": candidates.index(cand),
                "segment_index": seg_idx,
                "segments": segments,
                "candidates": candidates[:8],
            }

            is_verified_celeb = (sim >= 0.25 and m_ratio >= 0.50 and f_ratio >= 0.70)
            if is_verified_celeb:
                return result_dict

            fb_quality = sim * 2.0 + m_ratio + f_ratio * 0.5
            if fb_quality > best_fallback_score:
                best_fallback_score = fb_quality
                best_fallback = result_dict

    if best_fallback is not None:
        return best_fallback

    raise RuntimeError(f"Không thể tải và cắt video có mặt nhân vật cho năm {year}.")


def create_second_scene_for_year(
    existing_yc: Dict[str, Any],
    celeb_name: str,
    project_dir: str,
    face_engine: FaceEngine,
    ref_embeddings: List[np.ndarray],
    used_video_ids: Optional[Set[str]] = None,
    min_clip_duration: float = 3.8,
    max_clip_duration: float = 5.4,
) -> Optional[Dict[str, Any]]:
    """
    Create a 2nd consecutive scene (`Cảnh 2`) for the same year when a celebrity has fewer years
    or when we want to enrich a prominent year with 2 consecutive scenes:
    1. First tries a different verified face segment (`segment_index + 1`) from the same year's raw video
       or a 2nd distinct YouTube video from that same year (`candidate_index + 1`).
    """
    yr = int(existing_yc["year"])
    req_yr = int(existing_yc.get("requested_year") or yr)
    year_dir = os.path.join(project_dir, f"year_{req_yr}")
    os.makedirs(year_dir, exist_ok=True)

    # Option A: Try picking a 2nd distinct YouTube video of the same year first if available
    cands = existing_yc.get("candidates") or []
    explicit_cands = [c for c in cands if c.get("has_explicit_year")]
    if len(explicit_cands) >= 2:
        try:
            second_vid_yc = find_and_prepare_year_clip(
                celeb_name=celeb_name,
                year=yr,
                project_dir=project_dir,
                face_engine=face_engine,
                ref_embeddings=ref_embeddings,
                clip_duration=4.4,
                min_clip_duration=min_clip_duration,
                max_clip_duration=max_clip_duration,
                candidate_index=int(existing_yc.get("candidate_index", 0)) + 1,
                segment_index=0,
                year_window=(yr - 1, yr + 1),
                used_video_ids=used_video_ids,
                scene_index=2,
            )
            if float(second_vid_yc.get("face_similarity", 0)) >= 0.25:
                second_vid_yc["year"] = yr
                second_vid_yc["status"] = "ready"
                return second_vid_yc
        except Exception:
            pass

    # Option B: Cut the 2nd non-overlapping verified face segment from the existing raw video
    raw_path = existing_yc.get("raw_path")
    segments = existing_yc.get("segments") or []
    curr_seg_idx = int(existing_yc.get("segment_index", 0))
    if raw_path and os.path.exists(raw_path) and len(segments) >= 2:
        for cand_seg_idx, seg in enumerate(segments):
            if cand_seg_idx == curr_seg_idx:
                continue
            if abs(float(seg["start_sec"]) - float(existing_yc.get("rel_start_sec", 0))) < 3.8:
                continue
            if float(seg.get("similarity", 0)) < 0.24:
                continue

            rel_start = float(seg["start_sec"])
            seg_dur = max(min_clip_duration, min(max_clip_duration, float(seg.get("duration_sec") or 4.2)))
            vid = existing_yc.get("video_id", "vid")
            ts = int(time.time() * 100) % 100000
            out_clip = os.path.join(year_dir, f"clip_{yr}_{vid}_sc2_{ts}.mp4")
            thumb_path = os.path.join(year_dir, f"thumb_{yr}_{vid}_sc2_{ts}.jpg")
            if cut_video_clip(raw_path, out_clip, start_sec=rel_start, duration_sec=seg_dur, keep_audio=False):
                extract_thumbnail(out_clip, thumb_path, time_sec=seg_dur * 0.4)
                actual_dur = get_video_duration(out_clip) or seg_dur
                sc2 = dict(existing_yc)
                sc2["clip_id"] = f"{yr}_sc2_{ts}"
                sc2["scene_index"] = 2
                sc2["segment_index"] = cand_seg_idx
                sc2["rel_start_sec"] = round(rel_start, 2)
                sc2["start_sec"] = round(float(existing_yc.get("raw_offset", 0.0)) + rel_start, 2)
                sc2["duration_sec"] = round(actual_dur, 2)
                sc2["face_score"] = seg.get("score", 0.5)
                sc2["face_similarity"] = seg.get("similarity", 0.35)
                sc2["face_ratio"] = seg.get("face_ratio", 1.0)
                sc2["match_ratio"] = seg.get("match_ratio", 0.8)
                sc2["clip_path"] = out_clip
                sc2["thumb_path"] = thumb_path
                sc2["status"] = "ready"
                return sc2

    return None
