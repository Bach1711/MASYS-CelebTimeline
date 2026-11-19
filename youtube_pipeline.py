import os
import re
import glob
import time
import shutil
import datetime
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Set, Tuple
import yt_dlp
import numpy as np

from face_engine import FaceEngine

PIPELINE_CANCEL_EVENT = threading.Event()


def request_pipeline_cancel():
    """Immediately signal all pipeline threads to stop and kill any active ffmpeg/yt-dlp child processes."""
    PIPELINE_CANCEL_EVENT.set()
    try:
        subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe", "/T"], capture_output=True, timeout=3)
    except Exception:
        pass


def clear_pipeline_cancel():
    """Reset the cancellation flag before starting a new pipeline run."""
    PIPELINE_CANCEL_EVENT.clear()


def is_pipeline_cancelled() -> bool:
    return PIPELINE_CANCEL_EVENT.is_set()


def _resolve_ffmpeg_bin(exe_name: str) -> str:
    """
    Dynamically locate ffmpeg/ffprobe across any Windows PC:
    1. System PATH (`shutil.which`)
    2. Local project folder (`./bin/` or `./`)
    3. Current Windows user's WinGet Links (`%LOCALAPPDATA%\\Microsoft\\WinGet\\Links`)
    4. Current Windows user's WinGet Packages (`%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\*FFmpeg*`)
    5. Standard install paths (`C:\\ffmpeg\\bin`, `C:\\Program Files\\ffmpeg\\bin`)
    """
    found = shutil.which(exe_name)
    if found and os.path.exists(found):
        return found

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base_dir, "bin", f"{exe_name}.exe"),
        os.path.join(base_dir, f"{exe_name}.exe"),
        os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Links\{exe_name}.exe"),
        rf"C:\ffmpeg\bin\{exe_name}.exe",
        rf"C:\Program Files\ffmpeg\bin\{exe_name}.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c

    winget_pkg_pattern = os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\**\bin\{exe_name}.exe")
    matches = glob.glob(winget_pkg_pattern, recursive=True)
    if matches:
        return matches[0]

    return exe_name


FFMPEG_BIN = _resolve_ffmpeg_bin("ffmpeg")
FFPROBE_BIN = _resolve_ffmpeg_bin("ffprobe")


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
    if is_pipeline_cancelled():
        return []
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
                if is_pipeline_cancelled():
                    break
                if not e or not e.get("id"):
                    continue
                vid = e["id"]
                dur = float(e.get("duration") or 0)
                if dur > 0 and (dur < 8 or dur > 1200):
                    continue
                thumbs = e.get("thumbnails") or []
                thumb_url = thumbs[-1].get("url") if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                upload_yr = None
                if e.get("release_year"):
                    upload_yr = int(e["release_year"])
                elif e.get("upload_date") and len(str(e["upload_date"])) >= 4:
                    upload_yr = int(str(e["upload_date"])[:4])
                results.append(
                    {
                        "video_id": vid,
                        "title": e.get("title") or "",
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "duration": dur,
                        "channel": e.get("uploader") or e.get("channel") or "",
                        "view_count": e.get("view_count") or 0,
                        "thumbnail": thumb_url,
                        "upload_year": upload_yr,
                    }
                )
    except Exception:
        pass
    return results


def fetch_video_upload_year(video_id: str) -> Optional[int]:
    """Quickly fetch the actual YouTube upload year of a video when its title has no explicit year."""
    if is_pipeline_cancelled() or not video_id:
        return None
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "playlist_items": "1",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if not info:
                return None
            if info.get("release_year"):
                return int(info["release_year"])
            up_date = str(info.get("upload_date") or "")
            if len(up_date) >= 4 and up_date[:4].isdigit():
                return int(up_date[:4])
            ts = info.get("timestamp")
            if ts:
                return datetime.datetime.utcfromtimestamp(int(ts)).year
    except Exception:
        pass
    return None


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
    Aborts immediately if `is_pipeline_cancelled()` is True.
    """
    if is_pipeline_cancelled():
        raise RuntimeError("PIPELINE_CANCELLED")

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

    def _cancel_hook(d):
        if is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")

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
        "progress_hooks": [_cancel_hook],
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
    except Exception as e:
        if "PIPELINE_CANCELLED" in str(e) or is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")

    if is_pipeline_cancelled():
        raise RuntimeError("PIPELINE_CANCELLED")

    # Fallback: download without range if video is under 6 minutes, then trim with ffmpeg
    try:
        ydl_opts_full = {
            "format": fmt,
            "outtmpl": temp_base + "_full.%(ext)s",
            "quiet": True,
            "no_warnings": True,
            "merge_output_format": "mp4",
            "ffmpeg_location": os.path.dirname(FFMPEG_BIN),
            "progress_hooks": [_cancel_hook],
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
    except Exception as e:
        if "PIPELINE_CANCELLED" in str(e) or is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")

    return False


def download_full_youtube_video(
    video_id_or_url: str,
    output_path: str,
    max_height: int = 480,
) -> bool:
    """
    Download an entire YouTube video (at reasonable resolution up to 480p/720p) once
    so Deep Face Scan ("Đổi cảnh") can scan every frame of the whole video quickly.
    """
    if is_pipeline_cancelled():
        raise RuntimeError("PIPELINE_CANCELLED")
    if os.path.exists(output_path) and os.path.getsize(output_path) > 20000:
        return True

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    url = (
        video_id_or_url
        if video_id_or_url.startswith("http")
        else f"https://www.youtube.com/watch?v={video_id_or_url}"
    )

    def _cancel_hook(d):
        if is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")

    temp_base = output_path.replace(".mp4", "_tmp")
    ydl_opts = {
        "format": f"bestvideo[height<={max_height}][ext=mp4]/best[height<={max_height}][ext=mp4]/best[height<=720][ext=mp4]/best",
        "outtmpl": temp_base + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "ffmpeg_location": os.path.dirname(FFMPEG_BIN),
        "progress_hooks": [_cancel_hook],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        for ext in [".mp4", ".mkv", ".webm"]:
            cand = temp_base + ext
            if os.path.exists(cand) and os.path.getsize(cand) > 10000:
                if ext == ".mp4":
                    shutil.move(cand, output_path)
                    return True
                ok = cut_video_clip(cand, output_path, 0, get_video_duration(cand) or 600.0, keep_audio=False)
                try:
                    os.remove(cand)
                except Exception:
                    pass
                return ok
    except Exception as e:
        if "PIPELINE_CANCELLED" in str(e) or is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")
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


def _clean_person_name_for_search(celeb_name: str) -> str:
    """Strip Wikipedia subpage suffixes like 'on stage and screen' or '(musician)' for clean YouTube queries."""
    s = re.sub(r"\s*\([^)]*\)\s*", " ", celeb_name or "")
    s = re.sub(
        r"(?i)\s+(?:on stage and screen|filmography|discography|videography|career|in popular culture)\b.*$",
        "",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


def _extract_years_from_title(title: str) -> List[int]:
    """Extract explicit 4-digit years (and 2-digit dates like 1/14/90) from a video title."""
    years = [int(y) for y in re.findall(r"\b(19[4-9]\d|20[0-2]\d)\b", title)]
    for m in re.findall(r"\b\d{1,2}/\d{1,2}/([6-9]\d)\b", title):
        years.append(1900 + int(m))
    return sorted(set(years))


def _extract_decade_from_title(title: str) -> Optional[int]:
    """Extract explicit decade tags like '1970s', '1980s', '70s', '80s', ''90s' from a video title."""
    m4 = re.search(r"\b(19[5-9]0|20[0-2]0)s\b", title.lower())
    if m4:
        return int(m4.group(1))
    m2 = re.search(r"(?:\b|')([6-9]0)s\b", title.lower())
    if m2:
        return 1900 + int(m2.group(1))
    return None


def _score_and_validate_year_candidate(
    item: Dict[str, Any],
    celeb_name: str,
    target_year: int,
    year_window: Tuple[int, int],
    current_year: int,
    forbidden_years: Optional[Set[int]] = None,
    extra_forbidden_terms: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Validate and score a YouTube candidate for a specific career milestone year.
    Strictly enforces True Year: never stamps a historical year (e.g. 1964, 1969) onto an undated video.
    """
    title_l = _normalize_text(item.get("title", ""))
    channel_l = _normalize_text(item.get("channel", ""))
    combined_l = f"{title_l} {channel_l}"

    clean_celeb = _clean_person_name_for_search(celeb_name)
    name_clean = _normalize_text(clean_celeb)
    name_parts = [p for p in re.split(r"\s+", name_clean) if len(p) >= 2]
    first_name = name_parts[0] if name_parts else name_clean
    last_name = name_parts[-1] if name_parts else name_clean
    last_name_no_apos = last_name.replace("'", "")

    # 1. Must mention the celebrity's name in the video TITLE itself (not just channel name)
    title_no_apos = title_l.replace("'", "")
    if last_name not in title_l and last_name_no_apos not in title_no_apos:
        return None
    if len(name_parts) >= 2 and first_name not in title_l and first_name.replace("'", "") not in title_no_apos:
        return None

    # 2. Strictly reject multi-year compilation ranges like "1954-2026", "1983-2024", "2008-2025"
    if re.search(r"\b(19\d{2}|20\d{2})\s*[-–—~to]+\s*(19\d{2}|20\d{2})\b", title_l):
        return None

    # 3. Strictly reject death reports, static-photo/audio recordings, subtitle/lyric videos, commercials, and tabloid news
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
        "audio",
        "phone interview",
        "telephone",
        "voicemail",
        "subtitled",
        "subtitulado",
        "legendado",
        "with subtitles",
        "sub.",
        "lyrics",
        "lyric video",
        "slideshow",
        "rare photos",
        "photo",
        "pictures",
        "caught on fire",
        "pepsi commercial",
        "commercial",
        "hard copy",
        "good morning britain",
        "inside edition",
        "spills",
        "secrets",
    ]
    if any(k in title_l for k in always_forbidden):
        return None

    if extra_forbidden_terms and any(k.lower() in combined_l for k in extra_forbidden_terms):
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

    # 4. Validate True Year of the video (Title year -> Upload year for modern clips -> Decade tag)
    min_yr, max_yr = year_window
    forbid_yrs = forbidden_years or set()
    title_years = _extract_years_from_title(title_l)
    title_decade = _extract_decade_from_title(title_l)
    upload_yr = int(item["upload_year"]) if item.get("upload_year") else None

    score = 0
    matched_year = None
    has_explicit_year = False

    if title_years:
        valid_tys = [ty for ty in title_years if min_yr <= ty <= max_yr and ty not in forbid_yrs]
        if not valid_tys:
            return None
        has_explicit_year = True
        matched_year = min(valid_tys, key=lambda ty: abs(ty - target_year))
        diff = abs(matched_year - target_year)
        if diff == 0:
            score += 28
        elif diff == 1:
            score += 20
        else:
            score += 14
    elif upload_yr and min_yr <= upload_yr <= max_yr and upload_yr not in forbid_yrs and upload_yr >= 2008:
        has_explicit_year = True
        matched_year = upload_yr
        score += 18
    elif title_decade and (title_decade <= target_year <= title_decade + 9) and target_year not in forbid_yrs:
        has_explicit_year = True
        matched_year = target_year
        score += 12
    else:
        # CRITICAL RULE: Never stamp a historical year (e.g. 1964, 1969, 1985) onto a video that has NO year!
        if target_year < current_year - 2 or target_year in forbid_yrs:
            return None
        score += 4

    if name_clean in title_l or name_clean.replace("'", "") in title_l.replace("'", ""):
        score += 6
    if any(k in title_l for k in ["interview", "talk", "speech", "acceptance", "monologue", "late night", "tonight show", "letterman", "oprah", "arsenio", "live", "awards", "grammy", "oscar", "emmy", "sctv", "scene", "clip"]):
        score += 5

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


def discover_celebrity_youtube_pool(
    celeb_name: str,
    birth_year: int,
    death_year: int,
    requested_years: List[int],
    extra_forbidden_terms: Optional[List[str]] = None,
) -> Tuple[List[int], Dict[int, List[Dict[str, Any]]]]:
    """
    YouTube-First Year Discovery:
    1. Scans YouTube across the celebrity's career and decades to discover which years ACTUALLY have valid videos.
    2. For lesser-known celebrities with undated videos, fetches the true YouTube `upload_year` so modern clips
       are placed at their true upload year (e.g. 2016, 2020) and NEVER mislabeled as 1964 or 1969.
    3. Adapts the timeline years to match the real available video years on YouTube.
    """
    if is_pipeline_cancelled():
        raise RuntimeError("PIPELINE_CANCELLED")

    clean_celeb = _clean_person_name_for_search(celeb_name)
    current_year = datetime.datetime.now().year
    min_career_yr = max(1950, (birth_year or 1950) + 16)
    max_career_yr = min(current_year, death_year or current_year)

    queries = [
        f"{clean_celeb} interview",
        f"{clean_celeb} live talk show",
        f"{clean_celeb} clip scene",
    ]
    start_dec = (min_career_yr // 10) * 10
    end_dec = (max_career_yr // 10) * 10
    for dec in range(start_dec, end_dec + 1, 10):
        queries.append(f"{clean_celeb} {dec}s interview")
    # Also include a few of the requested years directly so specific milestone hits are included
    for ry in requested_years[::2]:
        queries.append(f"{clean_celeb} {ry}")

    raw_items: List[Dict[str, Any]] = []
    seen_vids: Set[str] = set()

    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {pool.submit(search_youtube, q, 10): q for q in queries[:12]}
        for fut in as_completed(future_map):
            if is_pipeline_cancelled():
                raise RuntimeError("PIPELINE_CANCELLED")
            try:
                items = fut.result() or []
                for it in items:
                    vid = it.get("video_id")
                    if vid and vid not in seen_vids:
                        seen_vids.add(vid)
                        raw_items.append(it)
            except Exception:
                continue

    dated_by_year: Dict[int, List[Dict[str, Any]]] = {}
    undated_items: List[Dict[str, Any]] = []

    for item in raw_items:
        title_l = _normalize_text(item.get("title", ""))
        tys = [y for y in _extract_years_from_title(title_l) if min_career_yr <= y <= max_career_yr]
        if tys:
            yr = tys[0]
            scored = _score_and_validate_year_candidate(
                item,
                celeb_name=clean_celeb,
                target_year=yr,
                year_window=(yr, yr),
                current_year=current_year,
                extra_forbidden_terms=extra_forbidden_terms,
            )
            if scored:
                dated_by_year.setdefault(yr, []).append(scored)
        else:
            dec = _extract_decade_from_title(title_l)
            if dec and min_career_yr <= dec + 5 <= max_career_yr:
                yr = min(max_career_yr, max(min_career_yr, dec + 5))
                scored = _score_and_validate_year_candidate(
                    item,
                    celeb_name=clean_celeb,
                    target_year=yr,
                    year_window=(dec, dec + 9),
                    current_year=current_year,
                    extra_forbidden_terms=extra_forbidden_terms,
                )
                if scored:
                    dated_by_year.setdefault(yr, []).append(scored)
            else:
                # Check if title passes basic celebrity name & forbidden word filter before probing upload_year
                probe = _score_and_validate_year_candidate(
                    item,
                    celeb_name=clean_celeb,
                    target_year=current_year,
                    year_window=(current_year - 2, current_year),
                    current_year=current_year,
                    extra_forbidden_terms=extra_forbidden_terms,
                )
                if probe:
                    undated_items.append(item)

    # If celebrity has fewer than 9 dated years on YouTube (e.g. minor/niche celebrity like John Leeson),
    # fetch true YouTube upload_year for top undated videos so they get their REAL year (e.g. 2016, 2019, 2022)!
    if len(dated_by_year) < 9 and undated_items:
        top_undated = undated_items[:10]
        with ThreadPoolExecutor(max_workers=5) as pool:
            fut_to_item = {pool.submit(fetch_video_upload_year, it["video_id"]): it for it in top_undated}
            for fut in as_completed(fut_to_item):
                if is_pipeline_cancelled():
                    raise RuntimeError("PIPELINE_CANCELLED")
                it = fut_to_item[fut]
                try:
                    up_yr = fut.result()
                except Exception:
                    up_yr = None
                if up_yr and min_career_yr <= up_yr <= max_career_yr:
                    it_copy = dict(it)
                    it_copy["upload_year"] = up_yr
                    scored = _score_and_validate_year_candidate(
                        it_copy,
                        celeb_name=clean_celeb,
                        target_year=up_yr,
                        year_window=(up_yr, up_yr),
                        current_year=current_year,
                        extra_forbidden_terms=extra_forbidden_terms,
                    )
                    if scored:
                        dated_by_year.setdefault(up_yr, []).append(scored)

    for yr in list(dated_by_year.keys()):
        dated_by_year[yr].sort(key=lambda x: x.get("relevance", 0), reverse=True)

    available_years = sorted(dated_by_year.keys())
    target_cnt = len(requested_years) if requested_years else 10

    if not available_years:
        return requested_years, dated_by_year

    # If celebrity has fewer or equal real years on YouTube than target_cnt, use ALL real available years!
    if len(available_years) <= target_cnt:
        return available_years, dated_by_year

    # Otherwise, snap requested_years to the closest real available years in dated_by_year
    chosen: List[int] = []
    for req_y in requested_years:
        rem = [ay for ay in available_years if ay not in chosen]
        if not rem:
            break
        closest = min(rem, key=lambda ay: abs(ay - req_y))
        chosen.append(closest)

    return sorted(set(chosen)), dated_by_year


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
    forbidden_years: Optional[Set[int]] = None,
    extra_forbidden_terms: Optional[List[str]] = None,
    pre_discovered_candidates: Optional[List[Dict[str, Any]]] = None,
    scene_index: int = 1,
) -> Dict[str, Any]:
    """
    Search YouTube for `<Celeb Name> <Year>`, strictly validate that candidate videos
    actually belong to that year/era, download active sections, and verify via FaceEngine
    that the main celebrity is clearly on screen with live motion and no text overlays.
    """
    if is_pipeline_cancelled():
        raise RuntimeError("PIPELINE_CANCELLED")

    clean_celeb = _clean_person_name_for_search(celeb_name)
    current_year = datetime.datetime.now().year
    win = year_window or (year - 2, min(current_year, year + 2))
    used_ids = used_video_ids or set()
    forbid_yrs = forbidden_years or set()

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
        seen: Set[str] = set()
        for pc in (pre_discovered_candidates or []):
            vid = pc.get("video_id")
            if vid and vid not in seen and vid not in used_ids:
                seen.add(vid)
                candidates.append(dict(pc))

        if len(candidates) < 3:
            primary_queries = [
                f"{clean_celeb} {year} interview",
                f"{clean_celeb} {year} live",
                f"{clean_celeb} {year}",
            ]
            if year >= current_year:
                primary_queries.append(f"{clean_celeb} {year - 1} interview")
                primary_queries.append(f"{clean_celeb} {year - 1} speech")

            def _collect_from_queries(q_list: List[str]):
                for q in q_list:
                    if is_pipeline_cancelled():
                        raise RuntimeError("PIPELINE_CANCELLED")
                    for item in search_youtube(q, max_results=7):
                        vid = item["video_id"]
                        if vid in seen or vid in used_ids:
                            continue
                        seen.add(vid)
                        scored = _score_and_validate_year_candidate(
                            item,
                            celeb_name=clean_celeb,
                            target_year=year,
                            year_window=win,
                            current_year=current_year,
                            forbidden_years=forbid_yrs,
                            extra_forbidden_terms=extra_forbidden_terms,
                        )
                        if scored is not None:
                            candidates.append(scored)

            _collect_from_queries(primary_queries)

            explicit_count = sum(1 for c in candidates if c.get("has_explicit_year"))
            if explicit_count < 2:
                adj_queries = []
                for delta in [-1, 1, -2, 2]:
                    adj_y = year + delta
                    if win[0] <= adj_y <= win[1] and adj_y not in forbid_yrs:
                        adj_queries.append(f"{clean_celeb} {adj_y} interview")
                        adj_queries.append(f"{clean_celeb} {adj_y}")
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

    for cand in ordered[:5]:
        if is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")
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
            if vid_dur >= 180.0:
                w3_start = round(min(vid_dur - 50.0, vid_dur * 0.62), 1)
                if all(abs(w3_start - w[0]) >= 30.0 for w in windows):
                    windows.append((w3_start, 50.0))

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
            is_live = bool(chosen_seg.get("is_live_motion", True))
            is_no_text = bool(chosen_seg.get("is_clean_no_text", True))
            has_cont_celeb = bool(chosen_seg.get("has_continuous_celeb", m_ratio >= 0.78))
            thumb_offset = float(chosen_seg.get("best_thumb_offset_sec") or (seg_dur * 0.4))

            out_clip = os.path.join(year_dir, f"clip_{year}_{vid}_w{w_idx}_s{seg_idx}_sc{scene_index}.mp4")
            thumb_path = os.path.join(year_dir, f"thumb_{year}_{vid}_w{w_idx}_s{seg_idx}_sc{scene_index}.jpg")

            if not cut_video_clip(raw_path, out_clip, start_sec=rel_start, duration_sec=seg_dur, keep_audio=False):
                continue

            extract_thumbnail(out_clip, thumb_path, time_sec=thumb_offset)
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
                "face_motion": chosen_seg.get("face_motion", 15.0),
                "text_score": chosen_seg.get("text_score", 0.0),
                "clip_path": out_clip,
                "raw_path": raw_path,
                "thumb_path": thumb_path,
                "candidate_index": candidates.index(cand),
                "segment_index": seg_idx,
                "segments": segments,
                "candidates": candidates[:8],
            }

            is_verified_celeb = (
                sim >= 0.34
                and m_ratio >= 0.78
                and f_ratio >= 0.80
                and has_cont_celeb
                and is_live
                and is_no_text
            )
            if is_verified_celeb:
                return result_dict

            fb_quality = (
                sim * 2.5
                + m_ratio * 2.0
                + (1.5 if is_live else -2.0)
                + (1.0 if is_no_text else -1.5)
                + (1.0 if has_cont_celeb else -1.0)
            )
            if fb_quality > best_fallback_score:
                best_fallback_score = fb_quality
                best_fallback = result_dict

    if best_fallback is not None:
        return best_fallback

    raise RuntimeError(f"Không thể tải và cắt video có mặt nhân vật cho năm {year}.")


def _are_clips_visually_distinct(thumb_path_1: str, thumb_path_2: str, max_hist_corr: float = 0.78) -> bool:
    """
    Compare two clip thumbnails using HSV color histogram correlation + structural diff
    to ensure 2 consecutive scenes of the same year do NOT look like the same static shot.
    """
    try:
        import cv2
        from face_engine import read_image_unicode

        img1 = read_image_unicode(thumb_path_1)
        img2 = read_image_unicode(thumb_path_2)
        if img1 is None or img2 is None:
            return True

        hsv1 = cv2.cvtColor(cv2.resize(img1, (160, 90)), cv2.COLOR_BGR2HSV)
        hsv2 = cv2.cvtColor(cv2.resize(img2, (160, 90)), cv2.COLOR_BGR2HSV)

        hist1 = cv2.calcHist([hsv1], [0, 1], None, [30, 32], [0, 180, 0, 256])
        hist2 = cv2.calcHist([hsv2], [0, 1], None, [30, 32], [0, 180, 0, 256])
        cv2.normalize(hist1, hist1, 0, 1, cv2.NORM_MINMAX)
        cv2.normalize(hist2, hist2, 0, 1, cv2.NORM_MINMAX)

        corr = float(cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL))
        return corr <= max_hist_corr
    except Exception:
        return True


def _extract_content_tokens(title: str, celeb_name: str, year: int) -> Set[str]:
    """
    Extract meaningful context tokens (movie/show/event/interviewer words) from a video title,
    excluding the celebrity's name, years, and generic filler words.
    """
    t = _normalize_text(title)
    name_parts = {p for p in re.split(r"[^a-z0-9]+", _normalize_text(celeb_name)) if len(p) >= 2}
    stopwords = {
        "interview", "clip", "video", "scene", "full", "rare", "hd", "4k", "official",
        "the", "with", "and", "for", "from", "in", "on", "at", "of", "to", "by", "part", "pt",
        "actress", "actor", "star", "show", "talk", "late", "night", "archive",
        str(year), str(year - 1), str(year + 1), str(year % 100),
    } | name_parts
    tokens = {w for w in re.split(r"[^a-z0-9]+", t) if len(w) >= 3 and w not in stopwords and not w.isdigit()}
    return tokens


def _title_content_similarity(title1: str, title2: str, celeb_name: str, year: int) -> float:
    """
    Return Jaccard similarity (0.0 = completely different video topic/event, 1.0 = identical topic)
    between two video titles of the same celebrity and year.
    """
    toks1 = _extract_content_tokens(title1, celeb_name, year)
    toks2 = _extract_content_tokens(title2, celeb_name, year)
    if not toks1 or not toks2:
        return 0.0
    inter = len(toks1 & toks2)
    union = len(toks1 | toks2)
    return float(inter) / float(max(1, union))


def _are_video_candidates_completely_different(
    cand1: Dict[str, Any],
    cand2: Dict[str, Any],
    celeb_name: str,
    year: int,
) -> bool:
    """
    Check if two YouTube video candidates in the same year are genuinely 2 completely different videos
    (different video_id AND not a re-upload of the same interview/movie clip).
    """
    if not cand1 or not cand2:
        return False
    if cand1.get("video_id") == cand2.get("video_id"):
        return False
    sim = _title_content_similarity(
        cand1.get("title", ""),
        cand2.get("title", ""),
        celeb_name=celeb_name,
        year=year,
    )
    return sim < 0.45


def count_distinct_same_year_videos(yc: Dict[str, Any], celeb_name: str) -> int:
    """
    Return the number of OTHER completely different videos in the exact same year `yc['year']`
    available in `yc['candidates']`. If >= 1, then this year has at least 2 completely different videos!
    """
    yr = int(yc.get("year") or 0)
    cands = yc.get("candidates") or []
    distinct_others = []
    for c in cands:
        if c.get("video_id") == yc.get("video_id"):
            continue
        if not c.get("has_explicit_year") or int(c.get("matched_year") or 0) != yr:
            continue
        if _are_video_candidates_completely_different(yc, c, celeb_name=celeb_name, year=yr):
            distinct_others.append(c)
    return len(distinct_others)


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
    Create a 2nd consecutive scene (`Cảnh 2`) for the same year (ONLY used when the project does not
    have enough distinct years to reach ~1 minute, or when requested by user, and never for the first year clip):
    1. Priority 1 (Strictly prioritized): If the year has >= 2 completely different YouTube videos in that
       exact same year (`matched_year == yr`), take Scene 2 from that 2nd completely different video!
    2. Priority 2 (Fallback): Pick a non-overlapping segment from the same video ONLY if it is at least 12s
       away AND visually distinct (different camera angle/background via histogram check).
    """
    yr = int(existing_yc["year"])
    req_yr = int(existing_yc.get("requested_year") or yr)
    year_dir = os.path.join(project_dir, f"year_{req_yr}")
    os.makedirs(year_dir, exist_ok=True)
    existing_thumb = existing_yc.get("thumb_path") or ""
    existing_vid = existing_yc.get("video_id", "")
    existing_channel = _normalize_text(existing_yc.get("channel", ""))
    forbid_ids = set(used_video_ids or set())
    if existing_vid:
        forbid_ids.add(existing_vid)

    # Option A: Gather all candidate videos belonging to the EXACT SAME YEAR `yr`
    cands = list(existing_yc.get("candidates") or [])
    same_year_other_cands = [
        c for c in cands
        if c.get("video_id") and c.get("video_id") not in forbid_ids
        and c.get("has_explicit_year") and int(c.get("matched_year") or 0) == yr
    ]

    # If fewer than 2 same-year distinct candidates were cached, do a quick extra search for this exact year
    if len(same_year_other_cands) < 2:
        current_year = datetime.datetime.now().year
        seen_ids = {c.get("video_id") for c in cands if c.get("video_id")} | forbid_ids
        extra_queries = [
            f"{celeb_name} {yr}",
            f"{celeb_name} {yr} movie scene",
            f"{celeb_name} {yr} tv show",
        ]
        for eq in extra_queries:
            for item in search_youtube(eq, max_results=6):
                vid = item.get("video_id")
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)
                scored = _score_and_validate_year_candidate(
                    item,
                    celeb_name=celeb_name,
                    target_year=yr,
                    year_window=(yr, yr),
                    current_year=current_year,
                )
                if scored and scored.get("has_explicit_year") and int(scored.get("matched_year") or 0) == yr:
                    same_year_other_cands.append(scored)
            if len(same_year_other_cands) >= 3:
                break

    # Sort same-year candidates so COMPLETELY DIFFERENT videos (different topic/movie/show & different channel) come FIRST!
    def _rank_second_video(c: Dict[str, Any]):
        is_comp_diff = _are_video_candidates_completely_different(existing_yc, c, celeb_name=celeb_name, year=yr)
        title_sim = _title_content_similarity(existing_yc.get("title", ""), c.get("title", ""), celeb_name=celeb_name, year=yr)
        diff_channel = bool(_normalize_text(c.get("channel", "")) != existing_channel)
        return (
            is_comp_diff,
            diff_channel,
            round(1.0 - title_sim, 3),
            float(c.get("relevance") or 0),
        )

    same_year_other_cands.sort(key=_rank_second_video, reverse=True)

    # Try downloading & cutting Scene 2 from the top distinct video(s) of that exact same year
    for cand in same_year_other_cands[:4]:
        vid = cand["video_id"]
        vid_dur = float(cand.get("duration") or 90.0)
        if vid_dur <= 45.0:
            windows = [(0.0, vid_dur)]
        elif vid_dur <= 120.0:
            windows = [(6.0, min(50.0, vid_dur - 6.0))]
        else:
            w1_start = round(min(40.0, max(12.0, vid_dur * 0.20)), 1)
            windows = [(w1_start, 50.0)]

        for w_idx, (dl_start, dl_dur) in enumerate(windows):
            raw_path = os.path.join(year_dir, f"raw_{vid}_w{w_idx}.mp4")
            if not download_youtube_section(cand["url"], raw_path, start_sec=dl_start, duration_sec=dl_dur, with_audio=False):
                continue

            segments = face_engine.find_best_segments(
                raw_path,
                clip_duration=4.4,
                min_duration=min_clip_duration,
                max_duration=max_clip_duration,
                ref_embeddings=ref_embeddings,
                sample_fps=2.0,
                top_k=5,
                skip_start_sec=0.5,
            )
            if not segments:
                continue

            for seg_idx, chosen_seg in enumerate(segments[:3]):
                sim = float(chosen_seg.get("similarity", 0.0))
                m_ratio = float(chosen_seg.get("match_ratio", 0.0))
                f_ratio = float(chosen_seg.get("face_ratio", 0.0))
                is_live = bool(chosen_seg.get("is_live_motion", True))
                is_no_text = bool(chosen_seg.get("is_clean_no_text", True))
                if sim < 0.27 or m_ratio < 0.75 or f_ratio < 0.78 or not is_live or not is_no_text:
                    continue

                rel_start = float(chosen_seg["start_sec"])
                seg_dur = float(chosen_seg.get("duration_sec") or 4.4)
                thumb_offset = float(chosen_seg.get("best_thumb_offset_sec") or (seg_dur * 0.4))
                ts = int(time.time() * 100) % 100000
                out_clip = os.path.join(year_dir, f"clip_{yr}_{vid}_w{w_idx}_s{seg_idx}_sc2_{ts}.mp4")
                thumb_path = os.path.join(year_dir, f"thumb_{yr}_{vid}_w{w_idx}_s{seg_idx}_sc2_{ts}.jpg")

                if not cut_video_clip(raw_path, out_clip, start_sec=rel_start, duration_sec=seg_dur, keep_audio=False):
                    continue

                extract_thumbnail(out_clip, thumb_path, time_sec=thumb_offset)
                if existing_thumb and os.path.exists(existing_thumb):
                    if not _are_clips_visually_distinct(existing_thumb, thumb_path):
                        try:
                            os.remove(out_clip)
                            os.remove(thumb_path)
                        except Exception:
                            pass
                        continue

                actual_dur = get_video_duration(out_clip) or seg_dur
                merged_cands = [cand] + [c for c in cands if c.get("video_id") != vid]
                return {
                    "clip_id": f"{yr}_sc2_{ts}",
                    "year": yr,
                    "requested_year": req_yr,
                    "scene_index": 2,
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
                    "candidate_index": 0,
                    "segment_index": seg_idx,
                    "segments": segments,
                    "candidates": merged_cands[:8],
                    "status": "ready",
                }

    # Option B: Fallback to cutting a 2nd segment from the existing raw video ONLY if >= 12s apart AND visually distinct shot
    raw_path = existing_yc.get("raw_path")
    segments = existing_yc.get("segments") or []
    curr_seg_idx = int(existing_yc.get("segment_index", 0))
    if raw_path and os.path.exists(raw_path) and len(segments) >= 2:
        for cand_seg_idx, seg in enumerate(segments):
            if cand_seg_idx == curr_seg_idx:
                continue
            # Must be at least 12.0 seconds away so it never overlaps or repeats the same moment
            if abs(float(seg["start_sec"]) - float(existing_yc.get("rel_start_sec", 0))) < 12.0:
                continue
            if (
                float(seg.get("similarity", 0)) < 0.27
                or float(seg.get("match_ratio", 0)) < 0.75
                or not bool(seg.get("is_live_motion", True))
                or not bool(seg.get("is_clean_no_text", True))
            ):
                continue

            rel_start = float(seg["start_sec"])
            seg_dur = max(min_clip_duration, min(max_clip_duration, float(seg.get("duration_sec") or 4.2)))
            vid = existing_yc.get("video_id", "vid")
            ts = int(time.time() * 100) % 100000
            out_clip = os.path.join(year_dir, f"clip_{yr}_{vid}_sc2_{ts}.mp4")
            thumb_path = os.path.join(year_dir, f"thumb_{yr}_{vid}_sc2_{ts}.jpg")
            if cut_video_clip(raw_path, out_clip, start_sec=rel_start, duration_sec=seg_dur, keep_audio=False):
                extract_thumbnail(out_clip, thumb_path, time_sec=seg_dur * 0.4)
                # Verify visual distinctness against Scene 1
                if existing_thumb and os.path.exists(existing_thumb):
                    if not _are_clips_visually_distinct(existing_thumb, thumb_path):
                        try:
                            os.remove(out_clip)
                            os.remove(thumb_path)
                        except Exception:
                            pass
                        continue

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
