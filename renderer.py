import os
import shutil
import subprocess
from typing import List, Dict, Any, Optional
from PIL import Image, ImageDraw, ImageFont

from youtube_pipeline import FFMPEG_BIN, FFPROBE_BIN, get_video_duration


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
) -> bool:
    """
    Standardize a clip to 1080x1920 (9:16), 30fps H.264/AAC:
    - News clip (`keep_audio=True`): keeps original anchor audio.
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

    if use_source_audio:
        if year_label:
            overlay_png = output_clip.replace(".mp4", "_overlay.png")
            create_year_overlay_png(str(year_label), overlay_png, canvas_w=cw, canvas_h=ch, layout=layout)
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
        if year_label:
            overlay_png = output_clip.replace(".mp4", "_overlay.png")
            create_year_overlay_png(str(year_label), overlay_png, canvas_w=cw, canvas_h=ch, layout=layout)
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
    news_clip_info: Dict[str, Any],
    year_clips_info: List[Dict[str, Any]],
    output_path: str,
    work_dir: str,
    layout: str = "center_band",
) -> Dict[str, Any]:
    """
    Assemble the complete ~1-minute TikTok 9:16 (1080x1920, 30fps) video:
    1. News Intro clip (8-15s) WITH original anchor speech audio (no year label).
    2. Year clips (3-4s each) COMPLETELY MUTED, WITH bold yellow Year text overlay (#FFE600).
    3. No background music mixed (users add music directly on TikTok when posting).
    """
    render_dir = os.path.join(work_dir, "render_temp")
    os.makedirs(render_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    normalized_files: List[str] = []

    # 1. Normalize News Intro (keeps original audio, no year label)
    news_src = news_clip_info["clip_path"]
    news_dur = float(news_clip_info.get("duration_sec") or get_video_duration(news_src) or 11.0)
    norm_news = os.path.join(render_dir, "seg_000_news.mp4")
    if not normalize_segment_video(
        news_src,
        norm_news,
        duration_sec=news_dur,
        keep_audio=True,
        year_label=None,
        layout=layout,
    ):
        raise RuntimeError("Lỗi khi chuẩn hóa đoạn video Bản tin mở đầu.")
    actual_news_dur = get_video_duration(norm_news) or news_dur
    normalized_files.append(norm_news)

    # 2. Normalize each Year clip (muted original audio + yellow Year overlay)
    total_years_dur = 0.0
    for idx, yitem in enumerate(year_clips_info, start=1):
        yr = yitem["year"]
        y_src = yitem["clip_path"]
        y_dur = float(yitem.get("duration_sec") or get_video_duration(y_src) or 3.5)
        norm_y = os.path.join(render_dir, f"seg_{idx:03d}_{yr}.mp4")
        if not normalize_segment_video(
            y_src,
            norm_y,
            duration_sec=y_dur,
            keep_audio=False,
            year_label=str(yr),
            layout=layout,
        ):
            raise RuntimeError(f"Lỗi khi chuẩn hóa đoạn video năm {yr}.")
        total_years_dur += get_video_duration(norm_y) or y_dur
        normalized_files.append(norm_y)

    # 3. Concatenate all normalized segments directly into output_path (no BGM mixing)
    concat_list_path = os.path.join(render_dir, "concat_list.txt")
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in normalized_files:
            safe_p = p.replace("\\", "/")
            f.write(f"file '{safe_p}'\n")

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
        output_path,
    ]
    res_c = subprocess.run(cmd_concat, capture_output=True, text=True, timeout=90)
    if res_c.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(f"Lỗi khi ghép các đoạn video: {res_c.stderr[-300:]}")

    total_duration = get_video_duration(output_path) or (actual_news_dur + total_years_dur)

    return {
        "output_path": output_path,
        "total_duration": round(total_duration, 2),
        "news_duration": round(actual_news_dur, 2),
        "years_duration": round(total_years_dur, 2),
        "file_size_mb": round(os.path.getsize(output_path) / (1024 * 1024), 2),
    }
