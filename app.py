import os
import re
import time
import json
import uuid
import ctypes
import string
import threading
from typing import Dict, Any, List, Optional, Set
from fastapi import FastAPI, HTTPException, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from face_engine import FaceEngine
from celeb_service import lookup_celebrity
from youtube_pipeline import (
    find_and_prepare_news_clip,
    find_and_prepare_year_clip,
    create_second_scene_for_year,
    count_distinct_same_year_videos,
    cut_video_clip,
    extract_thumbnail,
    get_video_duration,
    download_youtube_section,
)
from renderer import render_final_timeline_video

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_OUTPUT_DIR = r"D:\TikTok_Videos\Output"
FEEDBACK_FILE = os.path.join(BASE_DIR, "feedback.json")

os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
try:
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)
except Exception:
    DEFAULT_OUTPUT_DIR = os.path.join(BASE_DIR, "output")
    os.makedirs(DEFAULT_OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="MASYS CelebTimeline")
face_engine = FaceEngine()

PROJECTS: Dict[str, Dict[str, Any]] = {}
PROJECTS_LOCK = threading.Lock()


def _project_dir(project_id: str) -> str:
    p = os.path.join(WORKSPACE_DIR, project_id)
    os.makedirs(p, exist_ok=True)
    return p


def _save_project_state(project_id: str):
    with PROJECTS_LOCK:
        proj = PROJECTS.get(project_id)
        if not proj:
            return
        state_copy = json.loads(json.dumps(proj, default=str))
    pdir = _project_dir(project_id)
    with open(os.path.join(pdir, "project.json"), "w", encoding="utf-8") as f:
        json.dump(state_copy, f, ensure_ascii=False, indent=2)


def _to_web_url(abs_path: Optional[str]) -> str:
    if not abs_path or not os.path.exists(abs_path):
        return ""
    try:
        rel = os.path.relpath(abs_path, BASE_DIR).replace("\\", "/")
        if rel.startswith(".."):
            return ""
        mtime = int(os.path.getmtime(abs_path))
        return f"/files/{rel}?v={mtime}"
    except Exception:
        return ""


def _serialize_project(proj: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(proj, default=str))
    project_id = out.get("project_id", "")
    if out.get("news_clip"):
        nc = out["news_clip"]
        nc["clip_url"] = _to_web_url(nc.get("clip_path"))
        nc["thumb_url"] = _to_web_url(nc.get("thumb_path"))
    for idx, yc in enumerate(out.get("year_clips", [])):
        yc["slot_index"] = idx
        yc["clip_url"] = _to_web_url(yc.get("clip_path"))
        yc["thumb_url"] = _to_web_url(yc.get("thumb_path"))
    if out.get("final_video") and out["final_video"].get("output_path"):
        out_p = out["final_video"]["output_path"]
        if os.path.exists(out_p):
            mtime = int(os.path.getmtime(out_p))
            out["final_video"]["video_url"] = f"/api/projects/{project_id}/final-video?v={mtime}"
    if out.get("celeb_info") and out["celeb_info"].get("reference_images"):
        imgs = out["celeb_info"]["reference_images"]
        if imgs:
            out["celeb_info"]["local_portrait_url"] = _to_web_url(imgs[0])
    return out


class LookupRequest(BaseModel):
    name: str
    clip_count: Optional[int] = None


class StartPipelineRequest(BaseModel):
    name: str
    output_folder: Optional[str] = None
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    years: Optional[List[int]] = None
    clip_count: Optional[int] = None
    news_duration: float = 11.5
    year_clip_duration: float = 4.2
    custom_news_url: Optional[str] = None


class SwapNewsRequest(BaseModel):
    candidate_index: Optional[int] = None
    custom_url: Optional[str] = None
    start_sec: Optional[float] = None
    duration_sec: Optional[float] = None


class SwapYearClipRequest(BaseModel):
    year: int
    slot_index: Optional[int] = None
    action: str = "next_segment"  # "next_segment" | "next_video" | "custom_trim" | "custom_url" | "add_scene" | "remove_scene"
    start_sec: Optional[float] = None
    duration_sec: Optional[float] = None
    custom_url: Optional[str] = None


class RenderRequest(BaseModel):
    output_folder: Optional[str] = None
    layout: str = "center_band"


class MkdirRequest(BaseModel):
    parent_path: str
    folder_name: str


@app.get("/api/default-output-folder")
def api_default_output_folder():
    return {"output_folder": DEFAULT_OUTPUT_DIR}


@app.get("/api/fs/list-dirs")
def api_list_dirs(path: Optional[str] = None):
    """
    Pure in-process directory browser for Windows so choosing an Output Folder
    is instantaneous (< 10ms) and NEVER spawns a subprocess or gets blocked by 360 Security.
    """
    drives = []
    for letter in string.ascii_uppercase:
        drv = f"{letter}:\\"
        if os.path.exists(drv):
            drives.append(drv)

    user_home = os.path.expanduser("~")
    quick_paths = [
        {"label": "🎯 Mặc định (D:\\TikTok_Videos\\Output)", "path": DEFAULT_OUTPUT_DIR},
        {"label": "📥 Downloads", "path": os.path.join(user_home, "Downloads")},
        {"label": "🖥️ Desktop", "path": os.path.join(user_home, "Desktop")},
        {"label": "📄 Documents", "path": os.path.join(user_home, "Documents")},
        {"label": "📁 Thư mục Dự án", "path": os.path.join(BASE_DIR, "output")},
    ]
    quick_paths = [q for q in quick_paths if os.path.exists(q["path"]) or q["path"] == DEFAULT_OUTPUT_DIR]

    curr = (path or DEFAULT_OUTPUT_DIR).strip()
    if not os.path.exists(curr):
        try:
            os.makedirs(curr, exist_ok=True)
        except Exception:
            curr = BASE_DIR
    curr = os.path.normpath(os.path.abspath(curr))
    parent = os.path.dirname(curr)
    if parent == curr:
        parent = ""

    subdirs = []
    try:
        with os.scandir(curr) as it:
            for entry in it:
                try:
                    if entry.is_dir() and not entry.name.startswith(".") and not entry.name.startswith("$"):
                        subdirs.append({"name": entry.name, "path": os.path.normpath(entry.path)})
                except Exception:
                    continue
        subdirs.sort(key=lambda x: x["name"].lower())
    except Exception:
        pass

    return {
        "current_path": curr,
        "parent_path": parent,
        "drives": drives,
        "quick_paths": quick_paths,
        "subdirs": subdirs,
    }


@app.post("/api/fs/mkdir")
def api_mkdir(req: MkdirRequest):
    safe_name = re.sub(r'[<>:"/\\|?*]', "", (req.folder_name or "").strip())
    if not safe_name:
        raise HTTPException(status_code=400, detail="Tên thư mục không hợp lệ.")
    target = os.path.normpath(os.path.join(req.parent_path, safe_name))
    try:
        os.makedirs(target, exist_ok=True)
        return {"ok": True, "path": target}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể tạo thư mục: {e}")


@app.get("/api/projects-latest")
def api_get_latest_project():
    with PROJECTS_LOCK:
        if PROJECTS:
            latest_id = max(PROJECTS.keys(), key=lambda k: PROJECTS[k].get("created_at", 0))
            return _serialize_project(PROJECTS[latest_id])
    if os.path.exists(WORKSPACE_DIR):
        candidates = []
        for d in os.listdir(WORKSPACE_DIR):
            jp = os.path.join(WORKSPACE_DIR, d, "project.json")
            if os.path.exists(jp):
                candidates.append((os.path.getmtime(jp), d, jp))
        if candidates:
            candidates.sort(reverse=True)
            _, pid, jp = candidates[0]
            try:
                with open(jp, "r", encoding="utf-8") as f:
                    proj = json.load(f)
                with PROJECTS_LOCK:
                    PROJECTS[pid] = proj
                return _serialize_project(proj)
            except Exception:
                pass
    return {"project_id": None}


@app.post("/api/lookup")
def api_lookup(req: LookupRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Vui lòng nhập tên người nổi tiếng.")
    temp_id = "lookup_" + re.sub(r"[^a-zA-Z0-9_-]", "_", req.name.strip().lower())[:30]
    save_dir = os.path.join(WORKSPACE_DIR, "_cache_portraits", temp_id)
    info = lookup_celebrity(req.name, save_dir=save_dir, target_clip_count=req.clip_count)
    if info.get("reference_images"):
        info["local_portrait_url"] = _to_web_url(info["reference_images"][0])
    return info


@app.post("/api/projects/start")
def api_start_project(req: StartPipelineRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Vui lòng nhập tên người nổi tiếng.")

    project_id = f"proj_{int(time.time())}_{uuid.uuid4().hex[:4]}"
    pdir = _project_dir(project_id)

    celeb_info = lookup_celebrity(
        req.name,
        save_dir=os.path.join(pdir, "refs"),
        target_clip_count=len(req.years) if req.years else req.clip_count,
    )
    if req.birth_year:
        celeb_info["birth_year"] = req.birth_year
    if req.death_year:
        celeb_info["death_year"] = req.death_year

    # Allow user-specified years (including duplicate years if user explicitly typed e.g. 1990, 1990 for 2 scenes)
    if req.years:
        years = sorted(req.years)
    else:
        years = celeb_info["recommended_years"]

    output_folder = (req.output_folder or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR

    # Adaptive target duration per clip based on how many distinct years we start with:
    # Fewer years -> longer clip duration (4.6s-5.6s); 10-12 distinct years -> 4.4s-5.2s so total is ~56-62s without repeating years!
    if len(years) <= 7:
        base_dur, min_dur, max_dur = 4.9, 4.2, 5.6
    elif len(years) <= 10:
        base_dur, min_dur, max_dur = 4.7, 4.1, 5.4
    else:
        base_dur, min_dur, max_dur = 4.4, 3.8, 5.2

    proj_state = {
        "project_id": project_id,
        "created_at": time.time(),
        "status": "running",
        "progress_pct": 5,
        "progress_message": f"Đang khởi tạo {len(celeb_info.get('reference_images', []))} ảnh khuôn mặt mẫu AI cho {celeb_info['name']} ({celeb_info['birth_year']}–{celeb_info['death_year']})...",
        "celeb_info": celeb_info,
        "output_folder": output_folder,
        "years": years,
        "news_duration": req.news_duration,
        "year_clip_duration": base_dur,
        "min_clip_duration": min_dur,
        "max_clip_duration": max_dur,
        "custom_news_url": req.custom_news_url,
        "news_clip": {
            "status": "processing",
            "title": "Đang tìm & cắt bản tin mở đầu...",
            "duration_sec": req.news_duration,
        },
        "year_clips": [
            {
                "slot_index": idx,
                "year": y,
                "requested_year": y,
                "scene_index": 1,
                "status": "pending",
                "title": f"Đang chờ xử lý năm {y}...",
                "duration_sec": base_dur,
            }
            for idx, y in enumerate(years)
        ],
        "final_video": None,
        "error": None,
    }

    with PROJECTS_LOCK:
        PROJECTS[project_id] = proj_state
    _save_project_state(project_id)

    t = threading.Thread(target=_run_full_pipeline_worker, args=(project_id,), daemon=True)
    t.start()

    return _serialize_project(proj_state)


def _compute_year_window(years: List[int], idx: int) -> tuple:
    yr = years[idx]
    prev_yr = years[idx - 1] if idx > 0 else yr - 3
    next_yr = years[idx + 1] if idx < len(years) - 1 else yr + 1
    min_yr = max(yr - 2, (prev_yr + yr) // 2 + 1) if idx > 0 and prev_yr < yr else yr - 1
    max_yr = min(yr + 2, (yr + next_yr) // 2) if idx < len(years) - 1 and next_yr > yr else yr
    min_yr = min(min_yr, yr)
    max_yr = max(max_yr, yr)
    return (min_yr, max_yr)


def _run_full_pipeline_worker(project_id: str):
    pdir = _project_dir(project_id)
    with PROJECTS_LOCK:
        proj = PROJECTS[project_id]
        celeb_name = proj["celeb_info"]["name"]
        ref_imgs = proj["celeb_info"].get("reference_images", [])
        years = list(proj["years"])
        news_dur = float(proj["news_duration"])
        year_dur = float(proj["year_clip_duration"])
        min_dur = float(proj.get("min_clip_duration", 3.6))
        max_dur = float(proj.get("max_clip_duration", 5.4))
        custom_news_url = proj.get("custom_news_url")

    ref_embeddings = face_engine.extract_reference_embeddings(ref_imgs)
    used_video_ids: Set[str] = set()
    used_years: Set[int] = set()
    tried_years: Set[int] = set(years)

    # Step 1: Find & cut News Intro clip (8-15s with original anchor audio)
    actual_news_dur = news_dur
    try:
        with PROJECTS_LOCK:
            PROJECTS[project_id]["progress_pct"] = 10
            PROJECTS[project_id]["progress_message"] = (
                f"Đang tìm bản tin thông báo về {celeb_name} & dò phụ đề cắt câu dẫn (8–15s)..."
            )
        news_clip = find_and_prepare_news_clip(
            celeb_name=celeb_name,
            project_dir=pdir,
            target_duration=news_dur,
            custom_url=custom_news_url,
            candidate_index=0,
        )
        news_clip["status"] = "ready"
        actual_news_dur = float(news_clip.get("duration_sec") or news_dur)
        if news_clip.get("video_id"):
            used_video_ids.add(news_clip["video_id"])
        with PROJECTS_LOCK:
            PROJECTS[project_id]["news_clip"] = news_clip
        _save_project_state(project_id)
    except Exception as e:
        with PROJECTS_LOCK:
            PROJECTS[project_id]["news_clip"] = {
                "status": "error",
                "error": str(e),
                "title": "Không tự động lấy được bản tin (Bấm Chỉnh giây / Dán link để thử nguồn khác)",
                "duration_sec": news_dur,
            }
        _save_project_state(project_id)

    # Step 2: Process each distinct Year clip sequentially with strict chronological window & adaptive duration
    total_years = max(1, len(years))
    year_Counts_seen: Dict[int, int] = {}

    for i, yr in enumerate(years):
        pct = 18 + int((i / total_years) * 65)
        win = _compute_year_window(years, i)
        sc_idx = year_Counts_seen.get(yr, 0) + 1
        year_Counts_seen[yr] = sc_idx

        with PROJECTS_LOCK:
            PROJECTS[project_id]["progress_pct"] = pct
            PROJECTS[project_id]["progress_message"] = (
                f"Đang tìm video đúng năm {yr} ({i + 1}/{total_years}) & xác thực khuôn mặt AI {celeb_name}..."
            )
            PROJECTS[project_id]["year_clips"][i]["status"] = "processing"
        _save_project_state(project_id)

        try:
            y_clip = find_and_prepare_year_clip(
                celeb_name=celeb_name,
                year=yr,
                project_dir=pdir,
                face_engine=face_engine,
                ref_embeddings=ref_embeddings,
                clip_duration=year_dur,
                min_clip_duration=min_dur,
                max_clip_duration=max_dur,
                candidate_index=sc_idx - 1,
                segment_index=0,
                year_window=win,
                used_video_ids=used_video_ids,
                forbidden_years=used_years if sc_idx == 1 else None,
                scene_index=sc_idx,
            )
            y_clip["status"] = "ready"
            if y_clip.get("video_id"):
                used_video_ids.add(y_clip["video_id"])
            used_years.add(int(y_clip["year"]))

            if float(y_clip.get("face_similarity", 0)) >= 0.30 and y_clip.get("thumb_path") and len(ref_embeddings) < 8:
                extra_embs = face_engine.extract_reference_embeddings([y_clip["thumb_path"]])
                if extra_embs:
                    ref_embeddings.extend(extra_embs)

            with PROJECTS_LOCK:
                PROJECTS[project_id]["year_clips"][i] = y_clip
                PROJECTS[project_id]["years"][i] = int(y_clip["year"])
        except Exception as e:
            with PROJECTS_LOCK:
                PROJECTS[project_id]["year_clips"][i] = {
                    "year": yr,
                    "requested_year": yr,
                    "scene_index": sc_idx,
                    "status": "error",
                    "error": str(e),
                    "title": f"Không tìm được clip năm {yr}",
                    "duration_sec": year_dur,
                }
        _save_project_state(project_id)

    # Step 2.5: Before EVER repeating a year, try finding additional DISTINCT years from the celebrity's career!
    # Rule: "Khi đủ video của các năm thì không cần lấy video lặp lại; chỉ khi không tìm thấy đủ năm để làm thành video tầm 1 phút thì mới lặp lại năm."
    with PROJECTS_LOCK:
        current_clips = list(PROJECTS[project_id].get("year_clips", []))
        celeb_info_snapshot = dict(PROJECTS[project_id].get("celeb_info") or {})

    ready_clips = [c for c in current_clips if c.get("status") == "ready"]
    current_total_dur = actual_news_dur + sum(float(c.get("duration_sec") or 0) for c in ready_clips)

    if len(ready_clips) < 10 and current_total_dur < 52.0:
        notable_yrs = celeb_info_snapshot.get("notable_years") or []
        yr_counts = celeb_info_snapshot.get("year_counts") or {}
        reserve_candidates = [
            int(ny)
            for ny in notable_yrs
            if int(ny) not in tried_years
            and int(ny) not in used_years
            and all(abs(int(ny) - uy) >= 2 for uy in used_years)
        ]
        reserve_candidates.sort(key=lambda y: int(yr_counts.get(str(y), yr_counts.get(y, 1))), reverse=True)

        for extra_yr in reserve_candidates[:4]:
            if len(ready_clips) >= 10 or current_total_dur >= 52.0:
                break
            tried_years.add(extra_yr)
            with PROJECTS_LOCK:
                PROJECTS[project_id]["progress_pct"] = 85
                PROJECTS[project_id]["progress_message"] = (
                    f"Đang tìm thêm mốc năm khác biệt ({extra_yr}) để đủ thời lượng ~1 phút không cần lặp năm..."
                )
            _save_project_state(project_id)
            try:
                extra_clip = find_and_prepare_year_clip(
                    celeb_name=celeb_name,
                    year=extra_yr,
                    project_dir=pdir,
                    face_engine=face_engine,
                    ref_embeddings=ref_embeddings,
                    clip_duration=year_dur,
                    min_clip_duration=min_dur,
                    max_clip_duration=max_dur,
                    candidate_index=0,
                    segment_index=0,
                    year_window=(extra_yr - 1, extra_yr + 1),
                    used_video_ids=used_video_ids,
                    forbidden_years=used_years,
                    scene_index=1,
                )
                extra_clip["status"] = "ready"
                if extra_clip.get("video_id"):
                    used_video_ids.add(extra_clip["video_id"])
                used_years.add(int(extra_clip["year"]))
                ready_clips.append(extra_clip)
                ready_clips.sort(key=lambda c: int(c["year"]))
                current_total_dur = actual_news_dur + sum(float(c.get("duration_sec") or 0) for c in ready_clips)
                with PROJECTS_LOCK:
                    PROJECTS[project_id]["year_clips"] = list(ready_clips)
                    PROJECTS[project_id]["years"] = [int(x["year"]) for x in ready_clips]
                _save_project_state(project_id)
            except Exception:
                continue

    # Step 3: Repeat a year ("1 năm 2 cảnh liên tiếp") ONLY IF the app could NOT find enough distinct years
    # to reach a ~1-minute video (i.e. fewer than 9 distinct years OR total duration is still under 49s).
    # When a year IS repeated, strictly prioritize years that have >= 2 completely different videos in that same year!
    has_enough_distinct_years = (
        len(ready_clips) >= 10
        or (len(ready_clips) >= 9 and current_total_dur >= 48.0)
        or current_total_dur >= 52.0
    )

    if ready_clips and not has_enough_distinct_years:
        with PROJECTS_LOCK:
            PROJECTS[project_id]["progress_pct"] = 92
            PROJECTS[project_id]["progress_message"] = (
                "Không tìm thấy đủ mốc năm riêng biệt cho ~1 phút — đang lấy 2 cảnh từ 2 video khác nhau cho các năm nhiều tư liệu..."
            )
        _save_project_state(project_id)

        # NEVER double the very first year clip (`ready_clips[0]`) so TikTok viewers immediately see the years progress!
        eligible_for_double = ready_clips[1:] if len(ready_clips) > 1 else []
        # Strictly prioritize years that have at least 2 completely different videos in that exact same year!
        ranked_for_double = sorted(
            eligible_for_double,
            key=lambda c: (
                count_distinct_same_year_videos(c, celeb_name) >= 1,
                count_distinct_same_year_videos(c, celeb_name),
                len(c.get("segments") or []) >= 2,
                float(c.get("face_similarity") or 0),
            ),
            reverse=True,
        )

        doubled_years: Set[int] = set()
        new_timeline_clips: List[Dict[str, Any]] = []

        extra_scenes_by_year: Dict[int, Dict[str, Any]] = {}
        for rc in ranked_for_double:
            if current_total_dur >= 55.0:
                break
            yr_val = int(rc["year"])
            if yr_val in doubled_years:
                continue
            sc2 = create_second_scene_for_year(
                existing_yc=rc,
                celeb_name=celeb_name,
                project_dir=pdir,
                face_engine=face_engine,
                ref_embeddings=ref_embeddings,
                used_video_ids=used_video_ids,
                min_clip_duration=min_dur,
                max_clip_duration=max_dur,
            )
            if sc2 is not None:
                doubled_years.add(yr_val)
                extra_scenes_by_year[yr_val] = sc2
                if sc2.get("video_id"):
                    used_video_ids.add(sc2["video_id"])
                current_total_dur += float(sc2.get("duration_sec") or 4.0)

        if extra_scenes_by_year:
            for c in ready_clips:
                new_timeline_clips.append(c)
                yr_val = int(c["year"])
                if yr_val in extra_scenes_by_year:
                    new_timeline_clips.append(extra_scenes_by_year.pop(yr_val))
            with PROJECTS_LOCK:
                PROJECTS[project_id]["year_clips"] = new_timeline_clips
                PROJECTS[project_id]["years"] = [int(x["year"]) for x in new_timeline_clips]

    with PROJECTS_LOCK:
        # Remove any failed error slots if we already have >= 8 ready clips so the storyboard is clean
        all_yc = PROJECTS[project_id].get("year_clips", [])
        ready_only = [yc for yc in all_yc if yc.get("status") == "ready"]
        if len(ready_only) >= 7:
            PROJECTS[project_id]["year_clips"] = ready_only
            PROJECTS[project_id]["years"] = [int(x["year"]) for x in ready_only]

        ready_cnt = (1 if PROJECTS[project_id].get("news_clip", {}).get("status") == "ready" else 0) + len(
            [yc for yc in PROJECTS[project_id].get("year_clips", []) if yc.get("status") == "ready"]
        )
        PROJECTS[project_id]["status"] = "ready_for_preview"
        PROJECTS[project_id]["progress_pct"] = 100
        PROJECTS[project_id]["progress_message"] = f"Đã cắt xong {ready_cnt} đoạn"
    _save_project_state(project_id)


@app.get("/api/projects/{project_id}")
def api_get_project(project_id: str):
    with PROJECTS_LOCK:
        proj = PROJECTS.get(project_id)
    if not proj:
        json_p = os.path.join(_project_dir(project_id), "project.json")
        if os.path.exists(json_p):
            with open(json_p, "r", encoding="utf-8") as f:
                proj = json.load(f)
            with PROJECTS_LOCK:
                PROJECTS[project_id] = proj
        else:
            raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")
    return _serialize_project(proj)


@app.post("/api/projects/{project_id}/swap-news")
def api_swap_news(project_id: str, req: SwapNewsRequest):
    with PROJECTS_LOCK:
        proj = PROJECTS.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")

    pdir = _project_dir(project_id)
    celeb_name = proj["celeb_info"]["name"]
    current_news = proj.get("news_clip") or {}

    if req.start_sec is not None and not req.custom_url and req.candidate_index is None:
        dur = req.duration_sec or float(current_news.get("duration_sec") or proj.get("news_duration") or 11.5)
        url = current_news.get("url")
        vid = current_news.get("video_id", "news")
        if not url:
            raise HTTPException(status_code=400, detail="Chưa có video bản tin gốc để chỉnh giây.")
        news_dir = os.path.join(pdir, "news")
        raw_path = os.path.join(news_dir, f"raw_{vid}_manual_{int(req.start_sec)}.mp4")
        if not download_youtube_section(url, raw_path, start_sec=req.start_sec, duration_sec=dur + 2.0, with_audio=True):
            raise HTTPException(status_code=500, detail="Không tải được phân đoạn mới từ video bản tin.")
        out_clip = os.path.join(news_dir, f"news_clip_{vid}_manual_{int(time.time())}.mp4")
        thumb_path = os.path.join(news_dir, f"news_thumb_{vid}_manual_{int(time.time())}.jpg")
        cut_video_clip(raw_path, out_clip, start_sec=0.0, duration_sec=dur, keep_audio=True)
        extract_thumbnail(out_clip, thumb_path, time_sec=min(2.0, dur * 0.3))
        current_news["start_sec"] = round(req.start_sec, 2)
        current_news["duration_sec"] = round(get_video_duration(out_clip) or dur, 2)
        current_news["clip_path"] = out_clip
        current_news["thumb_path"] = thumb_path
        current_news["status"] = "ready"
        with PROJECTS_LOCK:
            PROJECTS[project_id]["news_clip"] = current_news
        _save_project_state(project_id)
        return _serialize_project(PROJECTS[project_id])

    next_idx = (
        req.candidate_index
        if req.candidate_index is not None
        else int(current_news.get("candidate_index", 0)) + 1
    )
    dur = req.duration_sec or float(proj.get("news_duration") or 11.5)
    new_news = find_and_prepare_news_clip(
        celeb_name=celeb_name,
        project_dir=pdir,
        target_duration=dur,
        custom_url=req.custom_url,
        candidate_index=next_idx,
    )
    new_news["status"] = "ready"
    with PROJECTS_LOCK:
        PROJECTS[project_id]["news_clip"] = new_news
    _save_project_state(project_id)
    return _serialize_project(PROJECTS[project_id])


@app.post("/api/projects/{project_id}/swap-year")
def api_swap_year(project_id: str, req: SwapYearClipRequest):
    with PROJECTS_LOCK:
        proj = PROJECTS.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")

    pdir = _project_dir(project_id)
    celeb_name = proj["celeb_info"]["name"]
    ref_imgs = proj["celeb_info"].get("reference_images", [])
    ref_embeddings = face_engine.extract_reference_embeddings(ref_imgs)

    year_clips = proj.get("year_clips", [])
    target_idx = None
    current_yc = None

    if req.slot_index is not None and 0 <= req.slot_index < len(year_clips):
        target_idx = req.slot_index
        current_yc = year_clips[target_idx]
    else:
        for idx, yc in enumerate(year_clips):
            if int(yc["year"]) == int(req.year) or int(yc.get("requested_year", yc["year"])) == int(req.year):
                target_idx = idx
                current_yc = yc
                break

    if target_idx is None or current_yc is None:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy mốc năm {req.year}.")

    # Action: Add a 2nd consecutive scene for this year ("1 năm 2 cảnh liên tiếp")
    if req.action == "add_scene":
        if target_idx == 0:
            raise HTTPException(
                status_code=400,
                detail="Cảnh năm đầu tiên luôn phải là 1 năm - 1 cảnh duy nhất để giữ chân người xem TikTok!",
            )
        other_used_ids = {yc.get("video_id") for yc in year_clips if yc.get("video_id")}
        sc2 = create_second_scene_for_year(
            existing_yc=current_yc,
            celeb_name=celeb_name,
            project_dir=pdir,
            face_engine=face_engine,
            ref_embeddings=ref_embeddings,
            used_video_ids=other_used_ids,
        )
        if not sc2:
            raise HTTPException(status_code=400, detail=f"Chưa tìm thêm được cảnh thứ 2 khác biệt cho năm {current_yc['year']}.")
        with PROJECTS_LOCK:
            PROJECTS[project_id]["year_clips"].insert(target_idx + 1, sc2)
            PROJECTS[project_id]["years"] = [int(x["year"]) for x in PROJECTS[project_id]["year_clips"]]
        _save_project_state(project_id)
        return _serialize_project(PROJECTS[project_id])

    # Action: Remove a scene
    if req.action == "remove_scene":
        if len(year_clips) <= 3:
            raise HTTPException(status_code=400, detail="Cần giữ lại tối thiểu 3 cảnh năm.")
        with PROJECTS_LOCK:
            PROJECTS[project_id]["year_clips"].pop(target_idx)
            PROJECTS[project_id]["years"] = [int(x["year"]) for x in PROJECTS[project_id]["year_clips"]]
        _save_project_state(project_id)
        return _serialize_project(PROJECTS[project_id])

    clip_dur = req.duration_sec or float(current_yc.get("duration_sec") or proj.get("year_clip_duration") or 4.2)

    # Action 1: Next face segment within the ALREADY downloaded video
    if req.action == "next_segment" and current_yc.get("raw_path") and os.path.exists(current_yc["raw_path"]):
        segments = current_yc.get("segments") or []
        if len(segments) > 1:
            next_seg_idx = (int(current_yc.get("segment_index", 0)) + 1) % len(segments)
            chosen = segments[next_seg_idx]
            rel_start = float(chosen["start_sec"])
            seg_dur = float(chosen.get("duration_sec") or clip_dur)
            vid = current_yc.get("video_id", "vid")
            req_yr = int(current_yc.get("requested_year") or current_yc["year"])
            year_dir = os.path.join(pdir, f"year_{req_yr}")
            out_clip = os.path.join(year_dir, f"clip_{req.year}_{vid}_s{next_seg_idx}_{int(time.time())}.mp4")
            thumb_path = os.path.join(year_dir, f"thumb_{req.year}_{vid}_s{next_seg_idx}_{int(time.time())}.jpg")
            if cut_video_clip(current_yc["raw_path"], out_clip, start_sec=rel_start, duration_sec=seg_dur, keep_audio=False):
                extract_thumbnail(out_clip, thumb_path, time_sec=seg_dur * 0.4)
                current_yc["segment_index"] = next_seg_idx
                current_yc["rel_start_sec"] = round(rel_start, 2)
                current_yc["start_sec"] = round(float(current_yc.get("raw_offset", 0.0)) + rel_start, 2)
                current_yc["duration_sec"] = round(get_video_duration(out_clip) or seg_dur, 2)
                current_yc["face_score"] = chosen.get("score", 0.5)
                current_yc["face_similarity"] = chosen.get("similarity", 0.5)
                current_yc["face_ratio"] = chosen.get("face_ratio", 1.0)
                current_yc["match_ratio"] = chosen.get("match_ratio", 0.5)
                current_yc["clip_path"] = out_clip
                current_yc["thumb_path"] = thumb_path
                current_yc["status"] = "ready"
                with PROJECTS_LOCK:
                    PROJECTS[project_id]["year_clips"][target_idx] = current_yc
                _save_project_state(project_id)
                return _serialize_project(PROJECTS[project_id])

    # Action 2: Manual start_sec trim
    if req.action == "custom_trim" and req.start_sec is not None and current_yc.get("url"):
        req_yr = int(current_yc.get("requested_year") or current_yc["year"])
        year_dir = os.path.join(pdir, f"year_{req_yr}")
        vid = current_yc.get("video_id", "vid")
        raw_path = os.path.join(year_dir, f"raw_{vid}_manual_{int(req.start_sec)}.mp4")
        if download_youtube_section(current_yc["url"], raw_path, start_sec=req.start_sec, duration_sec=clip_dur + 1.5, with_audio=False):
            out_clip = os.path.join(year_dir, f"clip_{req.year}_{vid}_manual_{int(time.time())}.mp4")
            thumb_path = os.path.join(year_dir, f"thumb_{req.year}_{vid}_manual_{int(time.time())}.jpg")
            cut_video_clip(raw_path, out_clip, start_sec=0.0, duration_sec=clip_dur, keep_audio=False)
            extract_thumbnail(out_clip, thumb_path, time_sec=clip_dur * 0.4)
            current_yc["start_sec"] = round(req.start_sec, 2)
            current_yc["duration_sec"] = round(get_video_duration(out_clip) or clip_dur, 2)
            current_yc["clip_path"] = out_clip
            current_yc["thumb_path"] = thumb_path
            current_yc["status"] = "ready"
            with PROJECTS_LOCK:
                PROJECTS[project_id]["year_clips"][target_idx] = current_yc
            _save_project_state(project_id)
            return _serialize_project(PROJECTS[project_id])

    # Action 3: Swap to next YouTube video candidate or custom URL
    next_cand_idx = 0 if req.custom_url else int(current_yc.get("candidate_index", 0)) + 1
    years_list = [int(x.get("requested_year") or x["year"]) for x in year_clips]
    win = _compute_year_window(years_list, target_idx)
    req_yr = int(current_yc.get("requested_year") or req.year)
    other_used_ids = {
        yc.get("video_id")
        for idx, yc in enumerate(year_clips)
        if idx != target_idx and yc.get("video_id")
    }
    other_used_years = {
        int(yc["year"])
        for idx, yc in enumerate(year_clips)
        if idx != target_idx and yc.get("year")
    }
    sc_idx_val = int(current_yc.get("scene_index", 1))

    new_yc = find_and_prepare_year_clip(
        celeb_name=celeb_name,
        year=req_yr,
        project_dir=pdir,
        face_engine=face_engine,
        ref_embeddings=ref_embeddings,
        clip_duration=clip_dur,
        candidate_index=next_cand_idx,
        segment_index=0,
        custom_url=req.custom_url,
        year_window=win,
        used_video_ids=other_used_ids,
        forbidden_years=other_used_years if sc_idx_val == 1 else None,
        scene_index=sc_idx_val,
    )
    new_yc["status"] = "ready"
    with PROJECTS_LOCK:
        PROJECTS[project_id]["year_clips"][target_idx] = new_yc
        PROJECTS[project_id]["years"][target_idx] = int(new_yc["year"])
    _save_project_state(project_id)
    return _serialize_project(PROJECTS[project_id])


@app.post("/api/projects/{project_id}/render")
def api_render_project(project_id: str, req: RenderRequest):
    with PROJECTS_LOCK:
        proj = PROJECTS.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")

    news_clip = proj.get("news_clip")
    if not news_clip or not news_clip.get("clip_path") or not os.path.exists(news_clip["clip_path"]):
        raise HTTPException(status_code=400, detail="Đoạn video bản tin mở đầu chưa sẵn sàng.")

    ready_year_clips = [
        yc
        for yc in proj.get("year_clips", [])
        if yc.get("status") == "ready" and yc.get("clip_path") and os.path.exists(yc["clip_path"])
    ]
    if not ready_year_clips:
        raise HTTPException(status_code=400, detail="Chưa có đoạn video theo năm nào sẵn sàng để ghép.")

    pdir = _project_dir(project_id)
    celeb = proj["celeb_info"]
    safe_celeb = re.sub(r'[<>:"/\\|?*]', "", celeb["name"]).strip()
    by = celeb.get("birth_year", "")
    dy = celeb.get("death_year", "")
    out_filename = f"{safe_celeb} {by}-{dy}.mp4".strip()

    target_folder = (req.output_folder or proj.get("output_folder") or DEFAULT_OUTPUT_DIR).strip()
    try:
        os.makedirs(target_folder, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể tạo thư mục lưu video '{target_folder}': {e}")

    output_path = os.path.normpath(os.path.join(target_folder, out_filename))

    res = render_final_timeline_video(
        news_clip_info=news_clip,
        year_clips_info=ready_year_clips,
        output_path=output_path,
        work_dir=pdir,
        layout=req.layout,
    )
    res["output_folder"] = target_folder

    with PROJECTS_LOCK:
        PROJECTS[project_id]["output_folder"] = target_folder
        PROJECTS[project_id]["final_video"] = res
        PROJECTS[project_id]["status"] = "completed"
    _save_project_state(project_id)
    return _serialize_project(PROJECTS[project_id])


@app.get("/api/projects/{project_id}/final-video")
def api_get_final_video(project_id: str):
    with PROJECTS_LOCK:
        proj = PROJECTS.get(project_id)
    if not proj or not proj.get("final_video") or not proj["final_video"].get("output_path"):
        raise HTTPException(status_code=404, detail="Chưa có file video xuất ra.")
    out_p = proj["final_video"]["output_path"]
    if not os.path.exists(out_p):
        raise HTTPException(status_code=404, detail="File video không tồn tại trên ổ đĩa.")
    return FileResponse(out_p, media_type="video/mp4", filename=os.path.basename(out_p))


def _open_folder_in_process_win32(target_path: str) -> bool:
    """
    Open Windows File Explorer and highlight the file (or open the directory)
    purely in-process using Win32 Shell COM (`shell32.dll` / `os.startfile`)
    WITHOUT spawning `explorer.exe` via `CreateProcess` (so 360 Total Security never blocks it!).
    """
    norm_target = os.path.normpath(os.path.abspath(target_path))
    if os.path.isfile(norm_target):
        folder_dir = os.path.dirname(norm_target)
        try:
            ole32 = ctypes.windll.ole32
            shell32 = ctypes.windll.shell32
            ole32.CoInitialize(None)
            shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
            shell32.ILCreateFromPathW.restype = ctypes.c_void_p
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_ulong,
            ]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            shell32.ILFree.argtypes = [ctypes.c_void_p]

            pidl = shell32.ILCreateFromPathW(norm_target)
            if pidl:
                hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
                shell32.ILFree(pidl)
                if hr == 0:
                    return True
        except Exception:
            pass
        os.startfile(folder_dir)
        return True
    else:
        os.makedirs(norm_target, exist_ok=True)
        os.startfile(norm_target)
        return True


@app.post("/api/open-folder")
def api_open_folder(path: str = Form(...)):
    target = path.strip() if path and os.path.exists(path.strip()) else DEFAULT_OUTPUT_DIR
    try:
        _open_folder_in_process_win32(target)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/feedback")
def api_get_feedback():
    if os.path.exists(FEEDBACK_FILE):
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"general_feedback": "", "tickets": {}, "updated_at": None}


@app.post("/api/feedback")
def api_save_feedback(payload: Dict[str, Any]):
    payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"ok": True, "path": FEEDBACK_FILE, "updated_at": payload["updated_at"]}


app.mount("/files", StaticFiles(directory=BASE_DIR), name="files")


@app.get("/tracker.html")
def serve_tracker():
    tracker_path = os.path.join(BASE_DIR, "tracker.html")
    if os.path.exists(tracker_path):
        return FileResponse(tracker_path)
    raise HTTPException(status_code=404, detail="Chưa tìm thấy tracker.html")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
