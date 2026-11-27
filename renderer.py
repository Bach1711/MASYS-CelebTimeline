import os
import re
import time
import ctypes
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont

from youtube_pipeline import FFMPEG_BIN, FFPROBE_BIN, get_video_duration


FONT_CANDIDATES = [
    r"C:\Windows\Fonts\ariblk.ttf",
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


def _load_bold_font(font_size: int) -> ImageFont.FreeTypeFont:
    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, font_size)
            except Exception:
                continue
    return ImageFont.load_default()


def create_news_title_overlay_png(
    celeb_name: str,
    years_text: str,
    output_png: str,
    canvas_w: int = 1080,
    canvas_h: int = 1920,
    layout: str = "center_band",
) -> str:
    """
    Render a transparent PNG for the Opening News Clip with a 2-line header in the top area
    matching `305 Fred Rogers.mp4`:
    - Line 1 (Celebrity Name, e.g. `Fred Rogers`): Bold Yellow (#FFE600) with crisp black border (~90pt)
    - Line 2 (Birth-Death Years, e.g. `(1928-2003)`): Bold White (#FFFFFF) with crisp black border (~66pt)
    """
    os.makedirs(os.path.dirname(output_png), exist_ok=True)
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    clean_name = re.sub(r"\s*\([^)]*\)\s*", " ", (celeb_name or "").strip()).strip() or (celeb_name or "").strip()
    clean_years = (years_text or "").strip()

    # Auto-fit Line 1 font size so long celebrity names never clip horizontally
    base_name_size = 90 if canvas_w == 1080 else 72
    max_text_w = canvas_w - 80
    name_size = base_name_size
    name_font = _load_bold_font(name_size)
    name_stroke = 8
    while name_size > 44:
        name_font = _load_bold_font(name_size)
        nb = draw.textbbox((0, 0), clean_name, font=name_font, stroke_width=name_stroke)
        if (nb[2] - nb[0]) <= max_text_w:
            break
        name_size -= 4

    nb = draw.textbbox((0, 0), clean_name, font=name_font, stroke_width=name_stroke)
    name_w = nb[2] - nb[0]
    name_x = (canvas_w - name_w) // 2 - nb[0]

    years_size = 66 if canvas_w == 1080 else 52
    years_stroke = 7
    years_font = _load_bold_font(years_size)
    yb = draw.textbbox((0, 0), clean_years, font=years_font, stroke_width=years_stroke) if clean_years else (0, 0, 0, 0)
    years_w = yb[2] - yb[0]
    years_x = (canvas_w - years_w) // 2 - yb[0]

    if layout == "center_band":
        # Center video band starts at y=520 (or ~514 in sample). Place Line 2 bottom at y=484, Line 1 20px above Line 2.
        band_top_y = (canvas_h - 880) // 2  # 520
        if clean_years:
            target_years_bottom = band_top_y - 34  # ~486
            years_y = target_years_bottom - yb[3]
            years_top = years_y + yb[1]
            target_name_bottom = years_top - 20  # ~394
            name_y = target_name_bottom - nb[3]
        else:
            target_name_bottom = band_top_y - 45
            name_y = target_name_bottom - nb[3]
            years_y = 0
    else:
        top_anchor = int(canvas_h * 0.14)
        name_y = top_anchor - nb[1]
        years_y = name_y + nb[3] + 20 - yb[1]

    # Draw Line 1: Celebrity Name in Yellow (#FFE600) with crisp black border + drop shadow
    if clean_name:
        draw.text(
            (name_x + 3, name_y + 4),
            clean_name,
            font=name_font,
            fill=(0, 0, 0, 210),
            stroke_width=name_stroke + 1,
            stroke_fill=(0, 0, 0, 210),
        )
        draw.text(
            (name_x, name_y),
            clean_name,
            font=name_font,
            fill=(255, 230, 0, 255),
            stroke_width=name_stroke,
            stroke_fill=(0, 0, 0, 255),
        )

    # Draw Line 2: (Birth-Death) in White (#FFFFFF) with crisp black border + drop shadow
    if clean_years:
        draw.text(
            (years_x + 3, years_y + 4),
            clean_years,
            font=years_font,
            fill=(0, 0, 0, 210),
            stroke_width=years_stroke + 1,
            stroke_fill=(0, 0, 0, 210),
        )
        draw.text(
            (years_x, years_y),
            clean_years,
            font=years_font,
            fill=(255, 255, 255, 255),
            stroke_width=years_stroke,
            stroke_fill=(0, 0, 0, 255),
        )

    img.save(output_png, "PNG")
    return output_png


def create_year_overlay_png(
    year_text: str,
    output_png: str,
    canvas_w: int = 1080,
    canvas_h: int = 1920,
    layout: str = "center_band",
) -> str:
    """
    Render a transparent PNG with the bold yellow Year label (#FFE600) and black stroke,
    matching the exact visual style of `369.Catherine O'Hara 1954-2026.mp4`.
    """
    os.makedirs(os.path.dirname(output_png), exist_ok=True)
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Pick boldest system font on Windows
    font_candidates = [
        r"C:\Windows\Fonts\ariblk.ttf",
        r"C:\Windows\Fonts\impact.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
    ]
    font_size = 76 if canvas_w == 1080 else 64
    font = None
    for fp in font_candidates:
        if os.path.exists(fp):
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), year_text, font=font, stroke_width=8)
    tw = bbox[2] - bbox[0]

    x = (canvas_w - tw) // 2
    if layout == "center_band":
        # Center video band is 1080x880 placed at y=520..1400; text sits near bottom of band (~y=1265)
        y = 1265
    elif layout == "vertical_full":
        y = int(canvas_h * 0.78)
    else:
        # 16:9 landscape (1920x1080)
        y = int(canvas_h * 0.82)

    # Subtle drop shadow
    draw.text(
        (x + 3, y + 4),
        year_text,
        font=font,
        fill=(0, 0, 0, 210),
        stroke_width=9,
        stroke_fill=(0, 0, 0, 210),
    )
    # Main yellow text (#FFE600) with crisp black border
    draw.text(
        (x, y),
        year_text,
        font=font,
        fill=(255, 230, 0, 255),
        stroke_width=8,
        stroke_fill=(0, 0, 0, 255),
    )

    img.save(output_png, "PNG")
    return output_png


def _has_audio_stream(video_path: str) -> bool:
    """Check if a video file contains an audio stream."""
    if not os.path.exists(video_path):
        return False
    try:
        cmd = [
            FFPROBE_BIN,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            video_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return bool(res.stdout.strip())
    except Exception:
        return False


def normalize_segment_video(
    input_clip: str,
    output_clip: str,
    duration_sec: float,
    keep_audio: bool,
    year_label: Optional[str] = None,
    layout: str = "center_band",
    custom_overlay_png: Optional[str] = None,
) -> bool:
    """
    Standardize a clip to 1080x1920 (9:16), 30fps H.264/AAC:
    - News clip (`keep_audio=True`): keeps original anchor audio and optional top celebrity title overlay.
    - Year clip (`keep_audio=False`): mutes original audio completely (replaces with silent audio stream)
      and draws bold yellow Year overlay (#FFE600) with black border.
    """
    os.makedirs(os.path.dirname(output_clip), exist_ok=True)
    if layout == "landscape_16_9":
        cw, ch = 1920, 1080
        scale_filter = (
            f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
            f"crop={cw}:{ch},setsar=1,fps=30,format=yuv420p"
        )
    elif layout == "vertical_full":
        cw, ch = 1080, 1920
        scale_filter = (
            f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
            f"crop={cw}:{ch},setsar=1,fps=30,format=yuv420p"
        )
    else:
        # Default `center_band` matching `369.Catherine O'Hara 1954-2026.mp4`:
        # 1080x1920 vertical canvas, inner video scaled/cropped to 1080x880 centered at y=520
        cw, ch = 1080, 1920
        band_w, band_h = 1080, 880
        scale_filter = (
            f"scale={band_w}:{band_h}:force_original_aspect_ratio=increase,"
            f"crop={band_w}:{band_h},"
            f"pad={cw}:{ch}:0:{(ch - band_h) // 2}:black,"
            f"setsar=1,fps=30,format=yuv420p"
        )

    use_source_audio = keep_audio and _has_audio_stream(input_clip)

    overlay_png: Optional[str] = None
    if custom_overlay_png and os.path.exists(custom_overlay_png):
        overlay_png = custom_overlay_png
    elif year_label:
        overlay_png = output_clip.replace(".mp4", "_overlay.png")
        create_year_overlay_png(str(year_label), overlay_png, canvas_w=cw, canvas_h=ch, layout=layout)

    if use_source_audio:
        if overlay_png:
            cmd = [
                FFMPEG_BIN,
                "-y",
                "-i",
                input_clip,
                "-i",
                overlay_png,
                "-t",
                f"{duration_sec:.2f}",
                "-filter_complex",
                f"[0:v]{scale_filter}[base];[base][1:v]overlay=0:0:format=auto,format=yuv420p[vout]",
                "-map",
                "[vout]",
                "-map",
                "0:a:0",
            ]
        else:
            cmd = [
                FFMPEG_BIN,
                "-y",
                "-i",
                input_clip,
                "-t",
                f"{duration_sec:.2f}",
                "-vf",
                scale_filter,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
            ]
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "2",
            output_clip,
        ]
    else:
        # Silent stereo audio track so all year segments are 100% muted while keeping continuous audio stream
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-i",
            input_clip,
        ]
        if overlay_png:
            cmd += [
                "-i",
                overlay_png,
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                f"{duration_sec:.2f}",
                "-filter_complex",
                f"[0:v]{scale_filter}[base];[base][1:v]overlay=0:0:format=auto,format=yuv420p[vout]",
                "-map",
                "[vout]",
                "-map",
                "2:a:0",
            ]
        else:
            cmd += [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                f"{duration_sec:.2f}",
                "-vf",
                scale_filter,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-shortest",
            output_clip,
        ]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    return res.returncode == 0 and os.path.exists(output_clip)


def render_final_timeline_video(
    news_clip_info: Optional[Dict[str, Any]],
    year_clips_info: List[Dict[str, Any]],
    output_path: str,
    work_dir: str,
    layout: str = "center_band",
    celeb_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Assemble the complete ~1-minute TikTok 9:16 (1080x1920, 30fps) video:
    1. Optional News Intro clip (8-15s) WITH original anchor speech audio and top 2-line celebrity title
       (Yellow Celebrity Name + White `(Birth-Death)` years, matching `305 Fred Rogers.mp4`).
       If `news_clip_info` is None or cleared, skips news intro and exports year clips only.
    2. Year clips (3-4s each) COMPLETELY MUTED, WITH bold yellow Year text overlay (#FFE600).
    3. No background music mixed (users add music directly on TikTok when posting).
    """
    render_dir = os.path.join(work_dir, "render_temp")
    os.makedirs(render_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    cw, ch = (1920, 1080) if layout == "landscape_16_9" else (1080, 1920)

    # Build list of segment normalization jobs to execute in parallel across 4 FFmpeg threads
    # Each job: (order_idx, is_news, label_desc, src_path, dst_path, dur_sec, keep_audio, year_label, custom_overlay_png)
    norm_jobs: List[Tuple[int, bool, str, str, str, float, bool, Optional[str], Optional[str]]] = []

    if (
        news_clip_info
        and news_clip_info.get("status") != "cleared"
        and news_clip_info.get("clip_path")
        and os.path.exists(news_clip_info["clip_path"])
    ):
        news_src = news_clip_info["clip_path"]
        news_dur = float(news_clip_info.get("duration_sec") or get_video_duration(news_src) or 11.0)
        norm_news = os.path.join(render_dir, "seg_000_news.mp4")

        news_overlay_png: Optional[str] = None
        if celeb_info and celeb_info.get("name"):
            c_name = str(celeb_info.get("name") or "").strip()
            by = str(celeb_info.get("birth_year") or "").strip()
            dy = str(celeb_info.get("death_year") or "").strip()
            if by and dy:
                y_str = f"({by}-{dy})"
            elif by:
                y_str = f"({by})"
            else:
                y_str = ""
            news_overlay_png = os.path.join(render_dir, "seg_000_news_title_overlay.png")
            create_news_title_overlay_png(
                celeb_name=c_name,
                years_text=y_str,
                output_png=news_overlay_png,
                canvas_w=cw,
                canvas_h=ch,
                layout=layout,
            )

        norm_jobs.append((0, True, "Bản tin mở đầu", news_src, norm_news, news_dur, True, None, news_overlay_png))

    for idx, yitem in enumerate(year_clips_info, start=1):
        yr = yitem["year"]
        y_src = yitem["clip_path"]
        y_dur = float(yitem.get("duration_sec") or get_video_duration(y_src) or 3.5)
        norm_y = os.path.join(render_dir, f"seg_{idx:03d}_{yr}.mp4")
        norm_jobs.append((idx, False, f"năm {yr}", y_src, norm_y, y_dur, False, str(yr), None))

    def _run_norm_job(job: Tuple[int, bool, str, str, str, float, bool, Optional[str], Optional[str]]) -> Tuple[int, bool, str, float]:
        order_idx, is_news, label_desc, src_p, dst_p, d_sec, k_audio, y_lbl, ov_png = job
        ok = normalize_segment_video(
            src_p,
            dst_p,
            duration_sec=d_sec,
            keep_audio=k_audio,
            year_label=y_lbl,
            layout=layout,
            custom_overlay_png=ov_png,
        )
        if not ok:
            raise RuntimeError(f"Lỗi khi chuẩn hóa đoạn video {label_desc}.")
        real_dur = get_video_duration(dst_p) or d_sec
        return order_idx, is_news, dst_p, real_dur

    job_results: List[Tuple[int, bool, str, float]] = []
    ffmpeg_workers = min(4, max(1, len(norm_jobs)))
    with ThreadPoolExecutor(max_workers=ffmpeg_workers) as pool:
        futs = [pool.submit(_run_norm_job, jb) for jb in norm_jobs]
        for fut in as_completed(futs):
            job_results.append(fut.result())

    # Sort strictly by chronological order_idx (0 = News Intro, 1..N = Year clips)
    job_results.sort(key=lambda x: x[0])

    normalized_files: List[str] = []
    actual_news_dur = 0.0
    total_years_dur = 0.0
    for _, is_news, dst_p, real_dur in job_results:
        normalized_files.append(dst_p)
        if is_news:
            actual_news_dur += real_dur
        else:
            total_years_dur += real_dur

    # 3. Concatenate all normalized segments into a temporary MP4 file first,
    #    then delete the old file and atomically replace it + reset Windows CreationTime & LastWriteTime
    #    so Windows File Explorer always displays the brand-new export timestamp!
    concat_list_path = os.path.join(render_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in normalized_files:
            safe_p = p.replace("\\", "/")
            f.write(f"file '{safe_p}'\n")

    tmp_render_mp4 = os.path.join(render_dir, f"final_out_{int(time.time() * 1000)}.mp4")
    cmd_concat = [
        FFMPEG_BIN,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_list_path,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        tmp_render_mp4,
    ]
    res_c = subprocess.run(cmd_concat, capture_output=True, text=True, timeout=90)
    if res_c.returncode != 0 or not os.path.exists(tmp_render_mp4):
        raise RuntimeError(f"Lỗi khi ghép các đoạn video: {res_c.stderr[-300:]}")

    final_output_path = output_path
    try:
        if os.path.exists(final_output_path):
            os.remove(final_output_path)
        shutil.move(tmp_render_mp4, final_output_path)
    except Exception:
        try:
            shutil.copyfile(tmp_render_mp4, final_output_path)
            os.remove(tmp_render_mp4)
        except Exception:
            # If the existing file is locked by an external media player, save as a fresh timestamped copy instead of failing
            base_no_ext, ext_s = os.path.splitext(output_path)
            final_output_path = f"{base_no_ext}_{int(time.time()) % 10000}{ext_s}"
            shutil.move(tmp_render_mp4, final_output_path)

    _refresh_windows_file_timestamps(final_output_path)

    total_duration = get_video_duration(final_output_path) or (actual_news_dur + total_years_dur)

    return {
        "output_path": final_output_path,
        "total_duration": round(total_duration, 2),
        "news_duration": round(actual_news_dur, 2),
        "years_duration": round(total_years_dur, 2),
        "file_size_mb": round(os.path.getsize(final_output_path) / (1024 * 1024), 2),
    }


def _refresh_windows_file_timestamps(filepath: str):
    """
    Explicitly update both CreationTime ('Date created') and LastWriteTime ('Date modified')
    on Windows (bypassing NTFS File System Tunneling) so File Explorer immediately reflects
    the exact current time when a video is re-exported/overwritten.
    """
    now = time.time()
    try:
        os.utime(filepath, (now, now))
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

        # Convert Unix epoch seconds to Windows FILETIME (100-nanosecond intervals since Jan 1, 1601)
        ft_val = int((now + 11644473600.0) * 10000000)
        ft = FILETIME(ft_val & 0xFFFFFFFF, (ft_val >> 32) & 0xFFFFFFFF)
        FILE_WRITE_ATTRIBUTES = 0x0100
        OPEN_EXISTING = 3
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        handle = ctypes.windll.kernel32.CreateFileW(
            ctypes.c_wchar_p(os.path.abspath(filepath)),
            FILE_WRITE_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle and handle != -1 and handle != 0xFFFFFFFFFFFFFFFF:
            ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(ft), ctypes.byref(ft), ctypes.byref(ft))
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
