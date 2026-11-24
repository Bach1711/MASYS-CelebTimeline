import os
import sys
import re
import time
import json
import uuid
import ctypes
import shutil
import string
import zipfile
import threading
import subprocess
from typing import Dict, Any, List, Optional, Set
import requests
from fastapi import FastAPI, HTTPException, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from face_engine import FaceEngine
from celeb_service import lookup_celebrity
from youtube_pipeline import (
    find_and_prepare_news_clip,
    find_and_prepare_year_clip,
    discover_celebrity_youtube_pool,
    create_second_scene_for_year,
    count_distinct_same_year_videos,
    cut_video_clip,
    extract_thumbnail,
    get_video_duration,
    download_youtube_section,
    download_full_youtube_video,
    request_pipeline_cancel,
    clear_pipeline_cancel,
    is_pipeline_cancelled,
)
from renderer import render_final_timeline_video

BASE_DIR = (
    os.path.dirname(os.path.abspath(sys.executable))
    if getattr(sys, "frozen", False)
    else os.path.dirname(os.path.abspath(__file__))
)
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_OUTPUT_DIR = r"D:\TikTok_Videos\Output" if os.path.exists("D:\\") else os.path.join(BASE_DIR, "output")
FEEDBACK_FILE = os.path.join(BASE_DIR, "feedback.json")
VERSION_FILE = os.path.join(BASE_DIR, "version.json")
GITHUB_REPO = "Bach1711/MASYS-CelebTimeline"
GITHUB_BRANCH = "main"

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
BATCHES: Dict[str, Dict[str, Any]] = {}
PROJECTS_LOCK = threading.Lock()
BATCHES_FILE = os.path.join(WORKSPACE_DIR, "_batches_state.json")
UI_STATE_FILE = os.path.join(WORKSPACE_DIR, "_ui_state.json")


def _project_dir(project_id: str) -> str:
    p = os.path.join(WORKSPACE_DIR, project_id)
    os.makedirs(p, exist_ok=True)
    return p


def _ensure_project_loaded(project_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not project_id:
        return None
    with PROJECTS_LOCK:
        if project_id in PROJECTS:
            return PROJECTS[project_id]
    json_p = os.path.join(WORKSPACE_DIR, project_id, "project.json")
    if os.path.exists(json_p):
        try:
            with open(json_p, "r", encoding="utf-8") as f:
                proj = json.load(f)
            with PROJECTS_LOCK:
                PROJECTS[project_id] = proj
            return proj
        except Exception:
            return None
    return None


def _save_batches_state():
    with PROJECTS_LOCK:
        copy_data = json.loads(json.dumps(BATCHES, default=str))
    try:
        with open(BATCHES_FILE, "w", encoding="utf-8") as f:
            json.dump(copy_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_batches_state():
    if not os.path.exists(BATCHES_FILE):
        return
    try:
        with open(BATCHES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with PROJECTS_LOCK:
                for k, v in data.items():
                    if k not in BATCHES:
                        BATCHES[k] = v
    except Exception:
        pass


_load_batches_state()


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
    exact_wiki_title: Optional[str] = None
    row_index: Optional[int] = None
    create_project: bool = True
    output_folder: Optional[str] = None


class LookupBatchItem(BaseModel):
    row_index: int = 0
    name: str
    exact_wiki_title: Optional[str] = None
    existing_project_id: Optional[str] = None
    years: Optional[List[int]] = None


class LookupBatchRequest(BaseModel):
    items: List[LookupBatchItem]
    output_folder: Optional[str] = None
    batch_id: Optional[str] = None


class StartPipelineRequest(BaseModel):
    name: str
    exact_wiki_title: Optional[str] = None
    output_folder: Optional[str] = None
    birth_year: Optional[int] = None
    death_year: Optional[int] = None
    years: Optional[List[int]] = None
    clip_count: Optional[int] = None
    news_duration: float = 11.5
    year_clip_duration: float = 4.2
    custom_news_url: Optional[str] = None
    auto_render: bool = False


class StartBatchItem(BaseModel):
    row_index: Optional[int] = None
    name: str
    wiki_title: Optional[str] = None
    exact_wiki_title: Optional[str] = None
    years: Optional[List[int]] = None
    project_id: Optional[str] = None


class StartBatchRequest(BaseModel):
    names: Optional[List[str]] = None
    items: Optional[List[StartBatchItem]] = None
    batch_id: Optional[str] = None
    output_folder: Optional[str] = None
    news_duration: float = 11.5
    year_clip_duration: float = 4.2
    auto_render: bool = False


class SwapNewsRequest(BaseModel):
    action: Optional[str] = None  # "clear" | None
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
    new_year_label: Optional[int] = None


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


@app.get("/api/ui-state")
def api_get_ui_state():
    if os.path.exists(UI_STATE_FILE):
        try:
            with open(UI_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"has_state": False}


@app.post("/api/ui-state")
def api_save_ui_state(payload: Dict[str, Any]):
    try:
        payload["has_state"] = True
        payload["updated_at"] = time.time()
        with open(UI_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/lookup")
def api_lookup(req: LookupRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Vui lòng nhập tên người nổi tiếng.")
    output_folder = (req.output_folder or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR

    if req.create_project:
        p_req = StartPipelineRequest(
            name=req.name.strip(),
            exact_wiki_title=req.exact_wiki_title,
            output_folder=output_folder,
            clip_count=req.clip_count,
            auto_render=False,
        )
        pid = _create_project_record(p_req, max_keep=25, initial_status="scanned")
        with PROJECTS_LOCK:
            p = PROJECTS[pid]
            info = json.loads(json.dumps(p["celeb_info"], default=str))
            info["project_id"] = pid
            info["recommended_years"] = p.get("years") or info.get("recommended_years") or []
        if info.get("reference_images"):
            info["local_portrait_url"] = _to_web_url(info["reference_images"][0])
            info["portrait_url"] = info["local_portrait_url"] or info.get("portrait_url")
        return info

    key_str = (req.exact_wiki_title or req.name).strip().lower()
    temp_id = "lookup_" + re.sub(r"[^a-zA-Z0-9_-]", "_", key_str)[:30]
    save_dir = os.path.join(WORKSPACE_DIR, "_cache_portraits", temp_id)
    info = lookup_celebrity(
        req.name,
        save_dir=save_dir,
        target_clip_count=req.clip_count,
        exact_wiki_title=req.exact_wiki_title,
    )
    if info.get("reference_images"):
        info["local_portrait_url"] = _to_web_url(info["reference_images"][0])
    return info


@app.post("/api/lookup-batch")
def api_lookup_batch(req: LookupBatchRequest):
    """
    Scan a list of celebrity rows entered in the UI Table, disambiguate each celebrity via Wikipedia,
    and pre-create (or reuse) a preview project + batch tabs so the user can review/approve each celebrity
    and switch tabs freely before clicking "Tạo Video".
    """
    valid_items = [it for it in (req.items or []) if it.name and it.name.strip()]
    if not valid_items:
        raise HTTPException(status_code=400, detail="Vui lòng nhập ít nhất 1 tên nhân vật vào bảng.")

    output_folder = (req.output_folder or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR
    batch_id = req.batch_id or f"batch_{int(time.time())}_{uuid.uuid4().hex[:4]}"
    max_keep = max(25, len(valid_items) + 8)

    scanned_results = []
    batch_items = []
    first_proj_serialized = None

    for idx, it in enumerate(valid_items):
        pid = None
        existing_p = _ensure_project_loaded(it.existing_project_id)
        if existing_p:
            ex_ci = existing_p.get("celeb_info") or {}
            ex_wiki = (ex_ci.get("wiki_title") or "").lower()
            ex_query = (ex_ci.get("input_query") or "").lower()
            req_target = (it.exact_wiki_title or it.name).strip().lower()
            if req_target and req_target in (ex_wiki, ex_query, (ex_ci.get("name") or "").lower()):
                pid = it.existing_project_id
                if it.years:
                    with PROJECTS_LOCK:
                        PROJECTS[pid]["years"] = sorted(it.years)
                    _save_project_state(pid)

        if not pid:
            p_req = StartPipelineRequest(
                name=it.name.strip(),
                exact_wiki_title=it.exact_wiki_title,
                years=it.years,
                output_folder=output_folder,
                auto_render=False,
            )
            pid = _create_project_record(p_req, max_keep=max_keep, initial_status="scanned")

        with PROJECTS_LOCK:
            p = PROJECTS[pid]
            ci = p["celeb_info"]
            p_status = p.get("status", "scanned")
            p_pct = p.get("progress_pct", 0)
            p_msg = p.get("progress_message", "Đã quét thông tin — Sẵn sàng tạo video")
            p_fv = p.get("final_video") or {}
            if idx == 0:
                first_proj_serialized = _serialize_project(p)

        local_portrait = _to_web_url(ci["reference_images"][0]) if ci.get("reference_images") else (ci.get("portrait_url") or "")
        scanned_row = {
            "row_index": it.row_index,
            "input_name": it.name.strip(),
            "project_id": pid,
            "name": ci.get("name") or it.name.strip(),
            "wiki_title": ci.get("wiki_title") or ci.get("name") or it.name.strip(),
            "auto_corrected_from": ci.get("auto_corrected_from"),
            "description": ci.get("description") or "",
            "birth_year": ci.get("birth_year"),
            "death_year": ci.get("death_year"),
            "portrait_url": local_portrait,
            "local_portrait_url": local_portrait,
            "reference_images": ci.get("reference_images") or [],
            "recommended_years": p.get("years") or ci.get("recommended_years") or [],
            "candidates": ci.get("candidates") or [],
        }
        scanned_results.append(scanned_row)

        batch_items.append(
            {
                "index": idx,
                "row_index": it.row_index,
                "name": scanned_row["name"],
                "display_name": scanned_row["name"],
                "wiki_title": scanned_row["wiki_title"],
                "project_id": pid,
                "status": p_status,
                "progress_pct": p_pct,
                "progress_message": p_msg,
                "total_duration": float(p_fv.get("total_duration") or 0.0),
                "output_path": p_fv.get("output_path") or "",
                "error": p.get("error"),
            }
        )

    batch_state = {
        "batch_id": batch_id,
        "created_at": time.time(),
        "status": "scanned",
        "output_folder": output_folder,
        "news_duration": 11.5,
        "year_clip_duration": 4.2,
        "total_count": len(batch_items),
        "completed_count": 0,
        "ready_to_export_count": 0,
        "current_index": 0,
        "items": batch_items,
    }

    with PROJECTS_LOCK:
        BATCHES[batch_id] = batch_state
    _save_batches_state()

    return {
        "batch": _sync_batch_items(batch_id) or batch_state,
        "items": scanned_results,
        "scanned_rows": scanned_results,
        "first_project": first_proj_serialized,
    }


def _cleanup_old_projects(max_keep: int = 5, exclude_project_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Automatically clean up old draft project directories in `workspace/` (keeping the `max_keep`
    most recent projects) so the hard drive never fills up over hundreds of video runs.
    """
    removed_count = 0
    freed_bytes = 0
    if not os.path.exists(WORKSPACE_DIR):
        return {"removed_projects": 0, "freed_mb": 0.0}

    proj_entries = []
    for d in os.listdir(WORKSPACE_DIR):
        if not d.startswith("proj_"):
            continue
        full_d = os.path.join(WORKSPACE_DIR, d)
        if not os.path.isdir(full_d):
            continue
        jp = os.path.join(full_d, "project.json")
        mtime = os.path.getmtime(jp) if os.path.exists(jp) else os.path.getmtime(full_d)
        proj_entries.append((mtime, d, full_d))

    proj_entries.sort(reverse=True)
    for _, pid, full_d in proj_entries[max_keep:]:
        if pid == exclude_project_id:
            continue
        with PROJECTS_LOCK:
            if PROJECTS.get(pid, {}).get("status") == "running":
                continue
            PROJECTS.pop(pid, None)
        try:
            for root, _, files in os.walk(full_d):
                for fn in files:
                    try:
                        freed_bytes += os.path.getsize(os.path.join(root, fn))
                    except Exception:
                        pass
            shutil.rmtree(full_d, ignore_errors=True)
            removed_count += 1
        except Exception:
            continue

    return {
        "removed_projects": removed_count,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
    }


@app.post("/api/workspace/cleanup")
def api_cleanup_workspace():
    return _cleanup_old_projects(max_keep=5)


def _create_project_record(req: StartPipelineRequest, max_keep: int = 8, initial_status: str = "running") -> str:
    project_id = f"proj_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"
    _cleanup_old_projects(max_keep=max_keep, exclude_project_id=project_id)
    pdir = _project_dir(project_id)

    celeb_info = lookup_celebrity(
        req.name,
        save_dir=os.path.join(pdir, "refs"),
        target_clip_count=len(req.years) if req.years else req.clip_count,
        exact_wiki_title=req.exact_wiki_title,
    )
    if req.birth_year:
        celeb_info["birth_year"] = req.birth_year
    if req.death_year:
        celeb_info["death_year"] = req.death_year

    if req.years:
        years = sorted(req.years)
    else:
        years = celeb_info["recommended_years"]

    output_folder = (req.output_folder or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR

    if len(years) <= 7:
        base_dur, min_dur, max_dur = 4.9, 4.2, 5.6
    elif len(years) <= 10:
        base_dur, min_dur, max_dur = 4.7, 4.1, 5.4
    else:
        base_dur, min_dur, max_dur = 4.4, 3.8, 5.2

    is_scanned_only = (initial_status == "scanned")
    proj_state = {
        "project_id": project_id,
        "created_at": time.time(),
        "status": initial_status,
        "progress_pct": 0 if is_scanned_only else 5,
        "progress_message": (
            f"Đã quét nhân vật {celeb_info['name']} ({celeb_info['birth_year']}–{celeb_info['death_year']}) — Chờ bấm Tạo Video..."
            if is_scanned_only
            else f"Đang khởi tạo {len(celeb_info.get('reference_images', []))} ảnh khuôn mặt mẫu AI cho {celeb_info['name']} ({celeb_info['birth_year']}–{celeb_info['death_year']})..."
        ),
        "celeb_info": celeb_info,
        "output_folder": output_folder,
        "years": years,
        "news_duration": req.news_duration,
        "year_clip_duration": base_dur,
        "min_clip_duration": min_dur,
        "max_clip_duration": max_dur,
        "custom_news_url": req.custom_news_url,
        "auto_render": req.auto_render,
        "news_clip": {
            "status": "pending" if is_scanned_only else "processing",
            "title": "Chờ tạo bản tin mở đầu..." if is_scanned_only else "Đang tìm & cắt bản tin mở đầu...",
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
    return project_id


@app.post("/api/projects/start")
def api_start_project(req: StartPipelineRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Vui lòng nhập tên người nổi tiếng.")

    clear_pipeline_cancel()
    project_id = _create_project_record(req, max_keep=8)
    t = threading.Thread(
        target=_run_full_pipeline_worker,
        args=(project_id, bool(req.auto_render)),
        daemon=True,
    )
    t.start()

    with PROJECTS_LOCK:
        proj_state = PROJECTS[project_id]
    return _serialize_project(proj_state)


@app.post("/api/stop-all")
def api_stop_all_pipelines():
    """
    Immediately stop all running video discovery, downloading, and cutting tasks across single/batch runs.
    """
    request_pipeline_cancel()
    stopped_pids = []
    with PROJECTS_LOCK:
        for pid, p in PROJECTS.items():
            if p.get("status") in ("running", "queued"):
                p["status"] = "stopped"
                p["progress_message"] = "⏹️ Đã dừng chạy theo yêu cầu của bạn (Các đoạn đã cắt xong vẫn được giữ nguyên)"
                if p.get("news_clip") and p["news_clip"].get("status") == "processing":
                    p["news_clip"]["status"] = "pending"
                    p["news_clip"]["title"] = "⏹️ Đã dừng tìm bản tin"
                for yc in p.get("year_clips", []):
                    if yc.get("status") == "processing":
                        yc["status"] = "pending"
                        yc["title"] = f"⏹️ Đã dừng ở năm {yc.get('year')}"
                stopped_pids.append(pid)

        for bid, b in BATCHES.items():
            if b.get("status") == "running":
                b["status"] = "stopped"
                for it in b.get("items", []):
                    if it.get("status") in ("running", "queued"):
                        it["status"] = "stopped"
                        it["progress_message"] = "⏹️ Đã dừng chạy"

    for pid in stopped_pids:
        _save_project_state(pid)
    _save_batches_state()

    # Clean up dangling partial downloads (.part, .ytdl, *_temp*, *_tmp*) left behind when stopped mid-download
    try:
        for root_d, _, files_d in os.walk(WORKSPACE_DIR):
            for fn in files_d:
                low_fn = fn.lower()
                if low_fn.endswith((".part", ".ytdl")) or "_temp" in low_fn or "_tmp" in low_fn:
                    try:
                        os.remove(os.path.join(root_d, fn))
                    except Exception:
                        pass
    except Exception:
        pass

    return {"ok": True, "stopped_projects": len(stopped_pids)}


@app.post("/api/batch/start")
def api_start_batch(req: StartBatchRequest):
    output_folder = (req.output_folder or DEFAULT_OUTPUT_DIR).strip() or DEFAULT_OUTPUT_DIR

    entry_list: List[StartBatchItem] = []
    if req.items:
        for it in req.items:
            if it.name and it.name.strip():
                entry_list.append(it)
    elif req.names:
        seen_lower = set()
        for idx_nm, nm in enumerate(req.names):
            if nm and nm.strip() and nm.strip().lower() not in seen_lower:
                seen_lower.add(nm.strip().lower())
                entry_list.append(StartBatchItem(row_index=idx_nm, name=nm.strip()))

    if not entry_list:
        raise HTTPException(status_code=400, detail="Vui lòng nhập ít nhất 1 tên nhân vật.")

    batch_id = req.batch_id or f"batch_{int(time.time())}_{uuid.uuid4().hex[:4]}"
    max_keep = max(25, len(entry_list) + 8)

    # Check if some items are already cut (ready_for_preview/completed) while others are new/scanned
    already_cut_flags = []
    for entry in entry_list:
        p_loaded = _ensure_project_loaded(entry.project_id)
        is_cut = bool(
            p_loaded
            and p_loaded.get("status") in ("ready_for_preview", "completed")
            and any(yc.get("status") == "ready" for yc in p_loaded.get("year_clips", []))
        )
        already_cut_flags.append(is_cut)
    # Reuse already-cut projects if at least one row is NOT cut yet (e.g. user replaced Row #8 and clicked Tạo Video)
    preserve_already_cut = any(already_cut_flags) and not all(already_cut_flags)

    batch_items = []
    first_proj = None
    first_running_assigned = False

    # Pre-create (or reuse scanned/cut) project records for ALL items upfront so every tab is immediately clickable without any popup!
    for idx, entry in enumerate(entry_list):
        pid = entry.project_id
        wiki_t = entry.exact_wiki_title or entry.wiki_title
        r_idx = entry.row_index if entry.row_index is not None else idx
        p_existing = _ensure_project_loaded(pid)

        if p_existing and preserve_already_cut and already_cut_flags[idx]:
            with PROJECTS_LOCK:
                p_obj = PROJECTS[pid]
                disp_name = p_obj.get("celeb_info", {}).get("name") or entry.name.strip()
                fv = p_obj.get("final_video") or {}
                item_status = "completed" if fv.get("output_path") else "ready_for_preview"
                if idx == 0:
                    first_proj = _serialize_project(p_obj)
            batch_items.append(
                {
                    "index": idx,
                    "row_index": r_idx,
                    "name": entry.name.strip(),
                    "display_name": disp_name,
                    "wiki_title": wiki_t or disp_name,
                    "project_id": pid,
                    "status": item_status,
                    "progress_pct": 100,
                    "progress_message": "Đã cắt xong trước đó",
                    "total_duration": float(fv.get("total_duration") or 0.0),
                    "output_path": fv.get("output_path") or "",
                    "error": None,
                    "skip_worker": True,
                }
            )
            continue

        is_first_to_run = not first_running_assigned
        first_running_assigned = True

        if p_existing:
            with PROJECTS_LOCK:
                p = PROJECTS[pid]
                p["output_folder"] = output_folder
                p["status"] = "running" if is_first_to_run else "queued"
                p["progress_pct"] = 5 if is_first_to_run else 0
                p["progress_message"] = "Đang bắt đầu tìm & cắt video..." if is_first_to_run else "Đang chờ tới lượt xử lý..."
                if entry.years:
                    p["years"] = sorted(entry.years)
            _save_project_state(pid)
        else:
            p_req = StartPipelineRequest(
                name=entry.name.strip(),
                exact_wiki_title=wiki_t,
                years=entry.years,
                output_folder=output_folder,
                news_duration=req.news_duration,
                year_clip_duration=req.year_clip_duration,
                auto_render=False,
            )
            pid = _create_project_record(
                p_req,
                max_keep=max_keep,
                initial_status="running" if is_first_to_run else "queued",
            )

        with PROJECTS_LOCK:
            p_obj = PROJECTS[pid]
            disp_name = p_obj.get("celeb_info", {}).get("name") or entry.name.strip()
            if idx == 0:
                first_proj = _serialize_project(p_obj)

        batch_items.append(
            {
                "index": idx,
                "row_index": r_idx,
                "name": entry.name.strip(),
                "display_name": disp_name,
                "wiki_title": wiki_t or disp_name,
                "project_id": pid,
                "status": "running" if is_first_to_run else "queued",
                "progress_pct": 5 if is_first_to_run else 0,
                "progress_message": "Đang bắt đầu tìm & cắt video..." if is_first_to_run else "Đang chờ tới lượt xử lý...",
                "total_duration": 0.0,
                "output_path": "",
                "error": None,
                "skip_worker": False,
            }
        )

    batch_state = {
        "batch_id": batch_id,
        "created_at": time.time(),
        "status": "running",
        "output_folder": output_folder,
        "news_duration": req.news_duration,
        "year_clip_duration": req.year_clip_duration,
        "total_count": len(batch_items),
        "completed_count": sum(1 for x in batch_items if x.get("skip_worker")),
        "ready_to_export_count": sum(1 for x in batch_items if x.get("skip_worker")),
        "current_index": 0,
        "items": batch_items,
    }

    with PROJECTS_LOCK:
        BATCHES[batch_id] = batch_state
    _save_batches_state()

    clear_pipeline_cancel()
    t = threading.Thread(target=_run_batch_worker, args=(batch_id, max_keep), daemon=True)
    t.start()

    return {
        "batch": _sync_batch_items(batch_id) or batch_state,
        "first_project": first_proj,
    }


def _sync_batch_items(batch_id: str) -> Optional[Dict[str, Any]]:
    _load_batches_state()
    with PROJECTS_LOCK:
        b = BATCHES.get(batch_id)
        if not b:
            return None
        pids_to_check = [it.get("project_id") for it in b.get("items", []) if it.get("project_id")]

    for pid in pids_to_check:
        _ensure_project_loaded(pid)

    with PROJECTS_LOCK:
        b = BATCHES.get(batch_id)
        if not b:
            return None
        for item in b.get("items", []):
            pid = item.get("project_id")
            if pid and pid in PROJECTS:
                p = PROJECTS[pid]
                item["progress_pct"] = p.get("progress_pct", item.get("progress_pct", 0))
                item["progress_message"] = p.get("progress_message", item.get("progress_message", ""))
                if p.get("celeb_info") and p["celeb_info"].get("name"):
                    item["display_name"] = p["celeb_info"]["name"]

                # Compute total cut duration for preview
                nc_dur = float(p.get("news_clip", {}).get("duration_sec") or 0.0) if p.get("news_clip", {}).get("status") != "cleared" else 0.0
                yc_dur = sum(float(yc.get("duration_sec") or 0.0) for yc in p.get("year_clips", []) if yc.get("status") == "ready")
                cut_dur = round(nc_dur + yc_dur, 1)

                if p.get("final_video") and p["final_video"].get("output_path"):
                    item["total_duration"] = p["final_video"].get("total_duration", cut_dur)
                    item["output_path"] = p["final_video"].get("output_path", "")
                    if item["status"] != "running":
                        item["status"] = "completed"
                elif p.get("status") == "ready_for_preview":
                    item["total_duration"] = cut_dur
                    if item["status"] not in ("completed", "exported"):
                        item["status"] = "ready_for_preview"
        b["total_count"] = len(b.get("items", []))
        b["completed_count"] = sum(
            1 for x in b.get("items", []) if x.get("status") in ("ready_for_preview", "exported", "completed")
        )
        b["exported_count"] = sum(
            1 for x in b.get("items", []) if x.get("status") in ("exported", "completed") or x.get("output_path")
        )
        out_copy = json.loads(json.dumps(b, default=str))
    _save_batches_state()
    return out_copy


def _run_batch_worker(batch_id: str, max_keep: int = 25):
    with PROJECTS_LOCK:
        b = BATCHES.get(batch_id)
        if not b:
            return
        pids_ordered = [(it.get("project_id"), bool(it.get("skip_worker"))) for it in b["items"]]

    for pid, skip_flag in pids_ordered:
        if is_pipeline_cancelled():
            with PROJECTS_LOCK:
                if batch_id in BATCHES:
                    BATCHES[batch_id]["status"] = "stopped"
            _save_batches_state()
            return
        if not pid or skip_flag:
            continue
        with PROJECTS_LOCK:
            b = BATCHES.get(batch_id)
            if not b or b.get("status") == "stopped":
                return
            # Find current index of pid (since user may have removed a row dynamically)
            matching_indices = [i for i, it in enumerate(b["items"]) if it.get("project_id") == pid]
            if not matching_indices:
                continue
            idx = matching_indices[0]
            b["current_index"] = idx
            item = b["items"][idx]
            item["status"] = "running"
            if pid in PROJECTS:
                PROJECTS[pid]["status"] = "running"
        _save_batches_state()

        try:
            # Rule 4: Do NOT auto-render MP4 during batch run; stop at ready_for_preview so user can inspect & click Export / Export All!
            _run_full_pipeline_worker(pid, auto_render=False)
            if is_pipeline_cancelled():
                with PROJECTS_LOCK:
                    if batch_id in BATCHES:
                        BATCHES[batch_id]["status"] = "stopped"
                _save_batches_state()
                return
            with PROJECTS_LOCK:
                b = BATCHES.get(batch_id)
                p = PROJECTS.get(pid, {})
                ready_yc = [yc for yc in p.get("year_clips", []) if yc.get("status") == "ready"]
                if b:
                    for it in b["items"]:
                        if it.get("project_id") == pid:
                            if p.get("status") == "stopped":
                                it["status"] = "stopped"
                            elif ready_yc:
                                it["status"] = "ready_for_preview"
                                it["progress_pct"] = 100
                            else:
                                it["status"] = "error"
                                it["error"] = p.get("error") or "Không tìm đủ tư liệu video"
        except Exception as e:
            with PROJECTS_LOCK:
                b = BATCHES.get(batch_id)
                if b:
                    for it in b["items"]:
                        if it.get("project_id") == pid:
                            it["status"] = "stopped" if "PIPELINE_CANCELLED" in str(e) else "error"
                            it["error"] = None if "PIPELINE_CANCELLED" in str(e) else str(e)
        _save_batches_state()

    with PROJECTS_LOCK:
        if batch_id in BATCHES and BATCHES[batch_id].get("status") != "stopped":
            BATCHES[batch_id]["status"] = "ready_for_preview"
    _save_batches_state()


class RemoveBatchRowRequest(BaseModel):
    row_index: Optional[int] = None
    project_id: Optional[str] = None


@app.post("/api/batch/{batch_id}/remove-row")
def api_remove_batch_row(batch_id: str, req: RemoveBatchRowRequest):
    """
    Remove a deleted table row / celebrity from an active or scanned batch so its Tab disappears immediately.
    """
    _load_batches_state()
    with PROJECTS_LOCK:
        b = BATCHES.get(batch_id)
        if not b:
            return {"ok": True, "batch": None}
        old_items = b.get("items", [])
        new_items = []
        for it in old_items:
            if req.project_id and it.get("project_id") == req.project_id:
                continue
            if req.row_index is not None and it.get("row_index") == req.row_index:
                continue
            new_items.append(it)
        for new_idx, it in enumerate(new_items):
            it["index"] = new_idx
        b["items"] = new_items
        b["total_count"] = len(new_items)
        if b.get("current_index", 0) >= len(new_items):
            b["current_index"] = max(0, len(new_items) - 1)
    return {"ok": True, "batch": _sync_batch_items(batch_id)}


@app.post("/api/batch/{batch_id}/render-all")
def api_render_batch_all(batch_id: str, req: RenderRequest):
    """
    Export MP4 for ALL ready celebrities in the batch at once ("💾 Xuất Tất Cả MP4").
    """
    _load_batches_state()
    with PROJECTS_LOCK:
        b = BATCHES.get(batch_id)
        if not b:
            raise HTTPException(status_code=404, detail="Không tìm thấy lô chạy hàng loạt.")
        items = list(b.get("items", []))
        default_out = b.get("output_folder") or DEFAULT_OUTPUT_DIR

    target_folder = (req.output_folder or default_out).strip() or DEFAULT_OUTPUT_DIR
    exported_files = []
    errors = []

    for item in items:
        pid = item.get("project_id")
        if not pid:
            continue
        try:
            proj_ser = _render_project_internal(
                project_id=pid,
                output_folder=target_folder,
                layout=req.layout,
                allow_missing_news=True,
            )
            fv = proj_ser.get("final_video") or {}
            if fv.get("output_path"):
                exported_files.append(
                    {
                        "project_id": pid,
                        "name": item.get("display_name") or item.get("name"),
                        "output_path": fv["output_path"],
                        "total_duration": fv.get("total_duration", 0.0),
                        "file_size_mb": fv.get("file_size_mb", 0.0),
                    }
                )
        except Exception as e:
            errors.append(f"{item.get('name')}: {e}")

    with PROJECTS_LOCK:
        if batch_id in BATCHES:
            BATCHES[batch_id]["status"] = "completed"
    _save_batches_state()

    return {
        "ok": True,
        "batch": _sync_batch_items(batch_id),
        "exported_count": len(exported_files),
        "rendered": exported_files,
        "exported_files": exported_files,
        "errors": errors,
        "output_folder": target_folder,
    }


@app.get("/api/batch/{batch_id}")
def api_get_batch(batch_id: str):
    state = _sync_batch_items(batch_id)
    if not state:
        raise HTTPException(status_code=404, detail="Không tìm thấy lô chạy hàng loạt.")
    return state


@app.get("/api/batch-latest")
def api_get_latest_batch():
    _load_batches_state()
    with PROJECTS_LOCK:
        if not BATCHES:
            return {"batch_id": None}
        latest_bid = max(BATCHES.keys(), key=lambda k: BATCHES[k].get("created_at", 0))
    return _sync_batch_items(latest_bid) or {"batch_id": None}


def _compute_year_window(years: List[int], idx: int) -> tuple:
    yr = years[idx]
    prev_yr = years[idx - 1] if idx > 0 else yr - 3
    next_yr = years[idx + 1] if idx < len(years) - 1 else yr + 1
    min_yr = max(yr - 2, (prev_yr + yr) // 2 + 1) if idx > 0 and prev_yr < yr else yr - 1
    max_yr = min(yr + 2, (yr + next_yr) // 2) if idx < len(years) - 1 and next_yr > yr else yr
    min_yr = min(min_yr, yr)
    max_yr = max(max_yr, yr)
    return (min_yr, max_yr)


def _run_full_pipeline_worker(project_id: str, auto_render: bool = False):
    if is_pipeline_cancelled():
        with PROJECTS_LOCK:
            if project_id in PROJECTS:
                PROJECTS[project_id]["status"] = "stopped"
        _save_project_state(project_id)
        return

    pdir = _project_dir(project_id)
    with PROJECTS_LOCK:
        proj = PROJECTS[project_id]
        celeb_name = proj["celeb_info"]["name"]
        birth_year = int(proj["celeb_info"].get("birth_year") or 1950)
        death_year = int(proj["celeb_info"].get("death_year") or 2026)
        ref_imgs = proj["celeb_info"].get("reference_images", [])
        extra_forbidden = proj["celeb_info"].get("youtube_forbidden_terms", [])
        years = list(proj["years"])
        news_dur = float(proj["news_duration"])
        year_dur = float(proj["year_clip_duration"])
        min_dur = float(proj.get("min_clip_duration", 3.6))
        max_dur = float(proj.get("max_clip_duration", 5.4))
        custom_news_url = proj.get("custom_news_url")

    ref_embeddings = face_engine.extract_reference_embeddings(ref_imgs)
    used_video_ids: Set[str] = set()
    used_years: Set[int] = set()
    tried_years: Set[int] = set()

    # Step 1: Find & cut News Intro clip (8-15s with original anchor audio)
    actual_news_dur = news_dur
    try:
        if is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")
        with PROJECTS_LOCK:
            PROJECTS[project_id]["progress_pct"] = 8
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
        if "PIPELINE_CANCELLED" in str(e) or is_pipeline_cancelled():
            with PROJECTS_LOCK:
                PROJECTS[project_id]["status"] = "stopped"
            _save_project_state(project_id)
            return
        with PROJECTS_LOCK:
            PROJECTS[project_id]["news_clip"] = {
                "status": "error",
                "error": str(e),
                "title": "Không tự động lấy được bản tin (Bấm Chỉnh giây / Dán link để thử nguồn khác)",
                "duration_sec": news_dur,
            }
        _save_project_state(project_id)

    # Step 1.5: YouTube-First Year Discovery — Scan real YouTube videos of the celebrity to discover
    # which years actually have footage (and assign true upload_year for undated clips of minor celebrities)!
    dated_pool: Dict[int, List[Dict[str, Any]]] = {}
    try:
        if is_pipeline_cancelled():
            raise RuntimeError("PIPELINE_CANCELLED")
        with PROJECTS_LOCK:
            PROJECTS[project_id]["progress_pct"] = 14
            PROJECTS[project_id]["progress_message"] = (
                f"Đang quét kho video thực tế của {celeb_name} trên YouTube để lấy các mốc năm có tư liệu thật..."
            )
        _save_project_state(project_id)

        discovered_years, dated_pool = discover_celebrity_youtube_pool(
            celeb_name=celeb_name,
            birth_year=birth_year,
            death_year=death_year,
            requested_years=years,
            extra_forbidden_terms=extra_forbidden,
        )
        if discovered_years and len(discovered_years) >= 3:
            years = list(discovered_years)
            if len(years) <= 7:
                year_dur = max(year_dur, 4.8)
                max_dur = max(max_dur, 5.6)
            with PROJECTS_LOCK:
                PROJECTS[project_id]["years"] = list(years)
                PROJECTS[project_id]["year_clips"] = [
                    {
                        "slot_index": idx,
                        "year": y,
                        "requested_year": y,
                        "scene_index": 1,
                        "status": "pending",
                        "title": f"Đang chờ xử lý năm {y}...",
                        "duration_sec": year_dur,
                    }
                    for idx, y in enumerate(years)
                ]
            _save_project_state(project_id)
    except Exception as e:
        if "PIPELINE_CANCELLED" in str(e) or is_pipeline_cancelled():
            with PROJECTS_LOCK:
                PROJECTS[project_id]["status"] = "stopped"
            _save_project_state(project_id)
            return

    tried_years.update(years)

    # Step 2: Process each distinct Year clip sequentially with strict chronological window & adaptive duration
    total_years = max(1, len(years))
    year_Counts_seen: Dict[int, int] = {}

    for i, yr in enumerate(years):
        if is_pipeline_cancelled():
            with PROJECTS_LOCK:
                PROJECTS[project_id]["status"] = "stopped"
            _save_project_state(project_id)
            return

        pct = 18 + int((i / total_years) * 65)
        win = _compute_year_window(years, i)
        sc_idx = year_Counts_seen.get(yr, 0) + 1
        year_Counts_seen[yr] = sc_idx

        with PROJECTS_LOCK:
            PROJECTS[project_id]["progress_pct"] = pct
            PROJECTS[project_id]["progress_message"] = (
                f"Đang tìm & cắt video năm {yr} ({i + 1}/{total_years}) — xác thực khuôn mặt AI {celeb_name}..."
            )
            if i < len(PROJECTS[project_id]["year_clips"]):
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
                extra_forbidden_terms=extra_forbidden,
                pre_discovered_candidates=dated_pool.get(yr),
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
            if "PIPELINE_CANCELLED" in str(e) or is_pipeline_cancelled():
                with PROJECTS_LOCK:
                    PROJECTS[project_id]["status"] = "stopped"
                _save_project_state(project_id)
                return
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

    if is_pipeline_cancelled():
        with PROJECTS_LOCK:
            PROJECTS[project_id]["status"] = "stopped"
        _save_project_state(project_id)
        return

    # Step 2.5: Before EVER repeating a year, try finding additional DISTINCT years from dated_pool and notable_years!
    with PROJECTS_LOCK:
        current_clips = list(PROJECTS[project_id].get("year_clips", []))
        celeb_info_snapshot = dict(PROJECTS[project_id].get("celeb_info") or {})

    ready_clips = [c for c in current_clips if c.get("status") == "ready"]
    current_total_dur = actual_news_dur + sum(float(c.get("duration_sec") or 0) for c in ready_clips)

    if len(ready_clips) < 10 and current_total_dur < 52.0:
        pool_unused_years = [
            int(py)
            for py in sorted(dated_pool.keys())
            if int(py) not in tried_years and int(py) not in used_years
        ]
        notable_yrs = celeb_info_snapshot.get("notable_years") or []
        yr_counts = celeb_info_snapshot.get("year_counts") or {}
        wiki_reserve = [
            int(ny)
            for ny in notable_yrs
            if int(ny) not in tried_years
            and int(ny) not in used_years
            and int(ny) not in pool_unused_years
            and all(abs(int(ny) - uy) >= 2 for uy in used_years)
        ]
        wiki_reserve.sort(key=lambda y: int(yr_counts.get(str(y), yr_counts.get(y, 1))), reverse=True)
        reserve_candidates = pool_unused_years + wiki_reserve

        for extra_yr in reserve_candidates[:5]:
            if is_pipeline_cancelled():
                break
            if len(ready_clips) >= 10 or current_total_dur >= 52.0:
                break
            tried_years.add(extra_yr)
            with PROJECTS_LOCK:
                PROJECTS[project_id]["progress_pct"] = 85
                PROJECTS[project_id]["progress_message"] = (
                    f"Đang lấy thêm mốc năm có video thật ({extra_yr}) để đủ thời lượng ~1 phút..."
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
                    extra_forbidden_terms=extra_forbidden,
                    pre_discovered_candidates=dated_pool.get(extra_yr),
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
            except Exception as e:
                if "PIPELINE_CANCELLED" in str(e) or is_pipeline_cancelled():
                    break
                continue

    if is_pipeline_cancelled():
        with PROJECTS_LOCK:
            PROJECTS[project_id]["status"] = "stopped"
        _save_project_state(project_id)
        return

    # Step 3: Repeat a year ("1 năm 2 cảnh liên tiếp") ONLY IF the app could NOT find enough distinct years
    # to reach a ~1-minute video (e.g. minor celebrity with only 4–7 real years on YouTube).
    has_enough_distinct_years = (
        len(ready_clips) >= 10
        or (len(ready_clips) >= 9 and current_total_dur >= 48.0)
        or current_total_dur >= 52.0
    )

    if ready_clips and not has_enough_distinct_years:
        with PROJECTS_LOCK:
            PROJECTS[project_id]["progress_pct"] = 92
            PROJECTS[project_id]["progress_message"] = (
                "Nhân vật có ít mốc năm riêng biệt trên YouTube — đang lấy thêm Cảnh 2 từ video khác cho các năm nhiều tư liệu..."
            )
        _save_project_state(project_id)

        eligible_for_double = ready_clips[1:] if len(ready_clips) > 2 else list(ready_clips)
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
            if is_pipeline_cancelled() or current_total_dur >= 55.0:
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
        # Remove any failed error slots whenever we have at least 2 ready clips so the storyboard NEVER has dead grey error boxes!
        all_yc = PROJECTS[project_id].get("year_clips", [])
        ready_only = [yc for yc in all_yc if yc.get("status") == "ready"]
        if len(ready_only) >= 2:
            ready_only.sort(key=lambda x: (int(x["year"]), int(x.get("scene_index", 1))))
            PROJECTS[project_id]["year_clips"] = ready_only
            PROJECTS[project_id]["years"] = [int(x["year"]) for x in ready_only]

        ready_cnt = (1 if PROJECTS[project_id].get("news_clip", {}).get("status") == "ready" else 0) + len(
            [yc for yc in PROJECTS[project_id].get("year_clips", []) if yc.get("status") == "ready"]
        )
        if is_pipeline_cancelled():
            PROJECTS[project_id]["status"] = "stopped"
            PROJECTS[project_id]["progress_message"] = f"⏹️ Đã dừng chạy (Đã cắt xong {ready_cnt} đoạn)"
        elif auto_render:
            PROJECTS[project_id]["progress_pct"] = 95
            PROJECTS[project_id]["progress_message"] = f"Đang tự động xuất file MP4 cho {celeb_name}..."
        else:
            PROJECTS[project_id]["status"] = "ready_for_preview"
            PROJECTS[project_id]["progress_pct"] = 100
            PROJECTS[project_id]["progress_message"] = f"Đã cắt xong {ready_cnt} đoạn"
    _save_project_state(project_id)

    if auto_render:
        try:
            _render_project_internal(project_id, allow_missing_news=True)
            with PROJECTS_LOCK:
                PROJECTS[project_id]["progress_pct"] = 100
                PROJECTS[project_id]["progress_message"] = f"Đã tự động xuất MP4 hoàn tất ({ready_cnt} đoạn)"
            _save_project_state(project_id)
        except Exception as e:
            with PROJECTS_LOCK:
                PROJECTS[project_id]["status"] = "ready_for_preview"
                PROJECTS[project_id]["progress_pct"] = 100
                PROJECTS[project_id]["error"] = str(e)
                PROJECTS[project_id]["progress_message"] = f"Đã cắt {ready_cnt} đoạn (Lỗi xuất MP4 tự động: {e})"
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
    proj = _ensure_project_loaded(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")
    if not any(p.get("status") == "running" for p in PROJECTS.values()):
        clear_pipeline_cancel()

    pdir = _project_dir(project_id)
    celeb_name = proj["celeb_info"]["name"]
    current_news = proj.get("news_clip") or {}

    if req.action == "clear":
        with PROJECTS_LOCK:
            PROJECTS[project_id]["news_clip"] = {
                "status": "cleared",
                "title": "Đã xóa bản tin mở đầu (Bấm Chỉnh giây để dán link mới, hoặc Xuất MP4 chỉ gồm các cảnh năm)",
                "duration_sec": 0.0,
                "start_sec": 0.0,
                "candidates": current_news.get("candidates", []),
            }
        _save_project_state(project_id)
        return _serialize_project(PROJECTS[project_id])

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


def _build_project_ref_embeddings(proj: Dict[str, Any], exclude_slot_idx: Optional[int] = None) -> List[Any]:
    """
    Build enriched SFace reference embeddings using BOTH Wikipedia portraits
    AND verified high-confidence thumbnails (`sim >= 0.34`, `match_ratio >= 0.85`) from
    other year clips of the same celebrity (covering young, mid-career, and recent ages).
    """
    ref_paths = list(proj.get("celeb_info", {}).get("reference_images", []) or [])
    verified_thumbs = []
    for idx, yc in enumerate(proj.get("year_clips") or []):
        if exclude_slot_idx is not None and idx == exclude_slot_idx:
            continue
        if (
            yc.get("status") == "ready"
            and float(yc.get("face_similarity") or 0.0) >= 0.34
            and float(yc.get("match_ratio") or 0.0) >= 0.85
            and yc.get("thumb_path")
            and os.path.exists(yc["thumb_path"])
        ):
            verified_thumbs.append((int(yc.get("year") or 9999), -float(yc.get("face_similarity") or 0.0), yc["thumb_path"]))
    verified_thumbs.sort()
    for _, _, tp in verified_thumbs[:6]:
        if tp not in ref_paths:
            ref_paths.append(tp)
    return face_engine.extract_reference_embeddings(ref_paths)


@app.post("/api/projects/{project_id}/swap-year")
def api_swap_year(project_id: str, req: SwapYearClipRequest):
    proj = _ensure_project_loaded(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")
    if not any(p.get("status") == "running" for p in PROJECTS.values()):
        clear_pipeline_cancel()

    pdir = _project_dir(project_id)
    celeb_name = proj["celeb_info"]["name"]

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

    ref_embeddings = _build_project_ref_embeddings(proj, exclude_slot_idx=target_idx)

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
        if len(year_clips) <= 2:
            raise HTTPException(status_code=400, detail="Cần giữ lại tối thiểu 2 cảnh năm.")
        with PROJECTS_LOCK:
            PROJECTS[project_id]["year_clips"].pop(target_idx)
            PROJECTS[project_id]["years"] = [int(x["year"]) for x in PROJECTS[project_id]["year_clips"]]
        _save_project_state(project_id)
        return _serialize_project(PROJECTS[project_id])

    clip_dur = req.duration_sec or float(current_yc.get("duration_sec") or proj.get("year_clip_duration") or 4.2)
    req_yr_dir = int(current_yc.get("requested_year") or current_yc["year"])
    year_dir = os.path.join(pdir, f"year_{req_yr_dir}")
    os.makedirs(year_dir, exist_ok=True)

    # Action 1: "Đổi cảnh" — pick ANOTHER face moment of the celebrity inside the SAME YouTube video.
    if req.action == "next_segment":
        new_yc = _swap_to_next_face_segment(current_yc, year_dir, clip_dur, ref_embeddings, celeb_name)
        with PROJECTS_LOCK:
            PROJECTS[project_id]["year_clips"][target_idx] = new_yc
        _save_project_state(project_id)
        return _serialize_project(PROJECTS[project_id])

    # Action 2: "Chỉnh giây" — manual start_sec / duration trim (current URL or a custom URL)
    target_url = (req.custom_url or "").strip() or current_yc.get("url")
    if req.action == "custom_trim" and req.start_sec is not None and target_url:
        vid_match = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", target_url)
        vid = vid_match.group(1) if vid_match else current_yc.get("video_id", "vid")
        ts = int(time.time() * 1000) % 10000000
        out_clip = os.path.join(year_dir, f"clip_{req.year}_{vid}_manual_{ts}.mp4")
        thumb_path = os.path.join(year_dir, f"thumb_{req.year}_{vid}_manual_{ts}.jpg")
        start_abs = max(0.0, float(req.start_sec))

        cut_ok = False
        # Fast path: trim locally if the requested range lies inside the already-downloaded raw window of the same video
        raw_path = current_yc.get("raw_path")
        raw_offset = float(current_yc.get("raw_offset") or 0.0)
        if (
            not req.custom_url
            and vid == current_yc.get("video_id")
            and raw_path
            and os.path.exists(raw_path)
        ):
            raw_len = get_video_duration(raw_path)
            rel = start_abs - raw_offset
            if rel >= 0 and rel + clip_dur <= raw_len + 0.2:
                cut_ok = cut_video_clip(raw_path, out_clip, start_sec=rel, duration_sec=clip_dur, keep_audio=False)

        if not cut_ok:
            dl_path = os.path.join(year_dir, f"raw_{vid}_manual_{int(start_abs * 10)}_{int(clip_dur * 10)}_{ts}.mp4")
            try:
                ok_dl = download_youtube_section(target_url, dl_path, start_sec=start_abs, duration_sec=clip_dur + 1.5, with_audio=False)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Không tải được đoạn video từ YouTube: {e}")
            if not ok_dl:
                raise HTTPException(status_code=500, detail="Không tải được đoạn video từ YouTube (thử lại hoặc đổi giây bắt đầu).")
            cut_ok = cut_video_clip(dl_path, out_clip, start_sec=0.0, duration_sec=clip_dur, keep_audio=False)
            if not cut_ok:
                raise HTTPException(status_code=500, detail="Không cắt được đoạn video mới.")

        extract_thumbnail(out_clip, thumb_path, time_sec=clip_dur * 0.4)
        new_yc = dict(current_yc)
        new_yc["video_id"] = vid
        new_yc["url"] = target_url
        if req.custom_url:
            new_yc["title"] = f"Custom Video ({vid})"
            new_yc["raw_path"] = None
            new_yc["segments"] = []
        if req.new_year_label is not None and 1930 <= int(req.new_year_label) <= 2035:
            new_yc["year"] = int(req.new_year_label)
        new_yc["start_sec"] = round(start_abs, 2)
        new_yc["duration_sec"] = round(get_video_duration(out_clip) or clip_dur, 2)
        new_yc["clip_path"] = out_clip
        new_yc["thumb_path"] = thumb_path
        new_yc["status"] = "ready"
        with PROJECTS_LOCK:
            PROJECTS[project_id]["year_clips"][target_idx] = new_yc
            PROJECTS[project_id]["years"] = [int(x["year"]) for x in PROJECTS[project_id]["year_clips"]]
        _save_project_state(project_id)
        return _serialize_project(PROJECTS[project_id])

    # Action 3: "Đổi video" — swap to next YouTube video candidate or custom URL (AI auto-locate face)
    next_cand_idx = 0 if req.custom_url else int(current_yc.get("candidate_index", 0)) + 1
    years_list = [int(x.get("requested_year") or x["year"]) for x in year_clips]
    win = _compute_year_window(years_list, target_idx)
    req_yr = int(req.new_year_label or current_yc.get("requested_year") or req.year)
    other_used_ids = {
        yc.get("video_id")
        for idx, yc in enumerate(year_clips)
        if idx != target_idx and yc.get("video_id")
    }
    if not req.custom_url and current_yc.get("video_id"):
        other_used_ids.add(current_yc["video_id"])
    other_used_years = {
        int(yc["year"])
        for idx, yc in enumerate(year_clips)
        if idx != target_idx and yc.get("year")
    }
    sc_idx_val = int(current_yc.get("scene_index", 1))

    try:
        new_yc = find_and_prepare_year_clip(
            celeb_name=celeb_name,
            year=req_yr,
            project_dir=pdir,
            face_engine=face_engine,
            ref_embeddings=ref_embeddings,
            clip_duration=clip_dur,
            candidate_index=0 if not req.custom_url else 0,
            segment_index=0,
            custom_url=req.custom_url,
            year_window=win,
            used_video_ids=other_used_ids,
            forbidden_years=other_used_years if sc_idx_val == 1 else None,
            extra_forbidden_terms=proj["celeb_info"].get("youtube_forbidden_terms", []),
            scene_index=sc_idx_val,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không tìm được video khác cho năm {req_yr}: {e}")
    new_yc["candidate_index"] = next_cand_idx
    if req.new_year_label is not None and 1930 <= int(req.new_year_label) <= 2035:
        new_yc["year"] = int(req.new_year_label)
    new_yc["status"] = "ready"
    with PROJECTS_LOCK:
        PROJECTS[project_id]["year_clips"][target_idx] = new_yc
        PROJECTS[project_id]["years"] = [int(x["year"]) for x in PROJECTS[project_id]["year_clips"]]
    _save_project_state(project_id)
    return _serialize_project(PROJECTS[project_id])


def _swap_to_next_face_segment(
    current_yc: Dict[str, Any],
    year_dir: str,
    clip_dur: float,
    ref_embeddings: List[Any],
    celeb_name: str,
) -> Dict[str, Any]:
    """
    "Đổi cảnh": Scan inside the SAME YouTube video using enriched face embeddings of the target celebrity
    and pick the strongest unshown segment where the celebrity's own face matches.
    1. If not yet full-scanned (e.g. initial clip came from a 50s partial window with a wrong actor or low similarity),
       first check if a high-confidence (`sim >= 0.36`, `match >= 0.78`) unshown segment already exists in the
       current window; otherwise download & deep-scan the ENTIRE video once (`raw_full_{vid}.mp4`) so we find the
       actual scene of the target celebrity anywhere in the video!
    2. On subsequent "Đổi cảnh" clicks, instantly pick the next highest-similarity distinct scene from the full scan.
    """
    vid = current_yc.get("video_id") or "vid"
    url = current_yc.get("url")
    shown_abs = [float(x) for x in (current_yc.get("shown_abs_starts") or [])]
    cur_abs = float(current_yc.get("start_sec") or 0.0)
    if all(abs(cur_abs - s) > 0.5 for s in shown_abs):
        shown_abs.append(cur_abs)

    def _is_new_moment(abs_start: float) -> bool:
        return all(abs(abs_start - s) >= 4.0 for s in shown_abs)

    def _rank(seg: Dict[str, Any]):
        sim_v = float(seg.get("similarity", 0.0))
        match_v = float(seg.get("match_ratio", 0.0))
        is_clean_live = bool(seg.get("is_live_motion", True)) and bool(seg.get("is_clean_no_text", True))
        return (
            is_clean_live and sim_v >= 0.34 and match_v >= 0.78,
            is_clean_live and sim_v >= 0.34 and match_v >= 0.65,
            sim_v >= 0.32 and match_v >= 0.60,
            is_clean_live and sim_v >= 0.27,
            round(sim_v, 3),
            float(seg.get("score", 0.0)),
        )

    def _cut_from(
        raw_path: str,
        raw_offset: float,
        seg: Dict[str, Any],
        segs: List[Dict[str, Any]],
        seg_idx: int,
        tried_windows: List[float],
        is_full_scanned: bool,
    ):
        rel_start = float(seg["start_sec"])
        seg_dur = float(seg.get("duration_sec") or clip_dur)
        ts = int(time.time() * 1000) % 10000000
        out_clip = os.path.join(year_dir, f"clip_{current_yc.get('year')}_{vid}_seg_{ts}.mp4")
        thumb_path = os.path.join(year_dir, f"thumb_{current_yc.get('year')}_{vid}_seg_{ts}.jpg")
        if not cut_video_clip(raw_path, out_clip, start_sec=rel_start, duration_sec=seg_dur, keep_audio=False):
            return None
        thumb_off = float(seg.get("best_thumb_offset_sec") or seg_dur * 0.4)
        extract_thumbnail(out_clip, thumb_path, time_sec=thumb_off)
        abs_start = raw_offset + rel_start
        new_yc = dict(current_yc)
        new_yc.update(
            {
                "raw_path": raw_path,
                "raw_offset": round(raw_offset, 2),
                "full_scanned": is_full_scanned,
                "segments": segs,
                "segment_index": seg_idx,
                "rel_start_sec": round(rel_start, 2),
                "start_sec": round(abs_start, 2),
                "duration_sec": round(get_video_duration(out_clip) or seg_dur, 2),
                "face_score": seg.get("score", 0.5),
                "face_similarity": seg.get("similarity", 0.5),
                "face_ratio": seg.get("face_ratio", 1.0),
                "match_ratio": seg.get("match_ratio", 0.5),
                "clip_path": out_clip,
                "thumb_path": thumb_path,
                "status": "ready",
                "shown_abs_starts": shown_abs + [round(abs_start, 2)],
                "tried_windows": tried_windows,
            }
        )
        return new_yc

    tried_windows = [float(x) for x in (current_yc.get("tried_windows") or [])]
    raw_path = current_yc.get("raw_path")
    raw_offset = float(current_yc.get("raw_offset") or 0.0)
    already_full = bool(current_yc.get("full_scanned"))
    if raw_offset not in tried_windows:
        tried_windows.append(raw_offset)

    # Step 1: If already full-scanned, or if the current window has a HIGH-CONFIDENCE unshown match of the target celebrity, use it!
    if raw_path and os.path.exists(raw_path):
        segs = list(current_yc.get("segments") or [])
        if not segs:
            segs = face_engine.find_best_segments(
                raw_path,
                clip_duration=clip_dur,
                min_duration=3.2,
                max_duration=5.5,
                ref_embeddings=ref_embeddings,
                sample_fps=1.2 if already_full else 2.0,
                top_k=14 if already_full else 8,
                skip_start_sec=0.3,
            ) or []
        cands = [(i, s) for i, s in enumerate(segs) if _is_new_moment(raw_offset + float(s["start_sec"]))]
        cands.sort(key=lambda x: _rank(x[1]), reverse=True)
        for i, s in cands:
            sim_s = float(s.get("similarity", 0.0))
            match_s = float(s.get("match_ratio", 0.0))
            # If we haven't scanned the whole video yet, ONLY accept cached partial-window segments if they are a CLEAR celebrity match!
            if not already_full and (sim_s < 0.36 or match_s < 0.78):
                break
            if sim_s < 0.25 and match_s < 0.50:
                continue
            res = _cut_from(raw_path, raw_offset, s, segs, i, tried_windows, is_full_scanned=already_full)
            if res:
                return res

    if not url:
        raise HTTPException(status_code=400, detail="Đoạn này chưa có video gốc để đổi cảnh — hãy bấm 🔄 Đổi video.")

    vid_dur = 0.0
    for c in (current_yc.get("candidates") or []):
        if c.get("video_id") == vid:
            vid_dur = float(c.get("duration") or 0.0)
            break

    # Step 2: Deep-scan the ENTIRE YouTube video once (`raw_full_{vid}.mp4`) when vid_dur <= 900s (15 min)
    full_raw = os.path.join(year_dir, f"raw_full_{vid}.mp4")
    if (not already_full) and (vid_dur <= 900.0):
        ok_full = False
        try:
            ok_full = download_full_youtube_video(url, full_raw, max_height=480)
        except Exception:
            ok_full = False

        if ok_full and os.path.exists(full_raw):
            full_segs = face_engine.find_best_segments(
                full_raw,
                clip_duration=clip_dur,
                min_duration=3.2,
                max_duration=5.4,
                ref_embeddings=ref_embeddings,
                sample_fps=1.0,
                top_k=16,
                skip_start_sec=0.5,
            ) or []
            cands = [(i, sg) for i, sg in enumerate(full_segs) if _is_new_moment(float(sg["start_sec"]))]
            cands.sort(key=lambda x: _rank(x[1]), reverse=True)
            for i, sg in cands:
                if float(sg.get("similarity", 0.0)) < 0.25:
                    continue
                res = _cut_from(full_raw, 0.0, sg, full_segs, i, tried_windows, is_full_scanned=True)
                if res:
                    return res

    # Step 3: Fallback multi-window scan across very long videos (> 15 min)
    if vid_dur <= 0:
        vid_dur = max(raw_offset + 120.0, 240.0)
    window_len = 50.0
    window_starts = []
    s = 0.0
    while s < max(1.0, vid_dur - 8.0) and len(window_starts) < 12:
        window_starts.append(round(s, 1))
        s += 45.0
    window_starts = [w for w in window_starts if all(abs(w - t) >= 20.0 for t in tried_windows)]
    window_starts.sort(key=lambda w: (w < raw_offset, abs(w - raw_offset)))

    for w_start in window_starts[:4]:
        tried_windows.append(w_start)
        new_raw = os.path.join(year_dir, f"raw_{vid}_swap_{int(w_start)}.mp4")
        try:
            ok = download_youtube_section(url, new_raw, start_sec=w_start, duration_sec=min(window_len, max(10.0, vid_dur - w_start)), with_audio=False)
        except Exception:
            ok = False
        if not ok:
            continue
        segs = face_engine.find_best_segments(
            new_raw, clip_duration=clip_dur, min_duration=3.2, max_duration=5.5,
            ref_embeddings=ref_embeddings, sample_fps=1.5, top_k=8, skip_start_sec=0.3,
        ) or []
        cands = [(i, sg) for i, sg in enumerate(segs) if _is_new_moment(w_start + float(sg["start_sec"]))]
        cands.sort(key=lambda x: _rank(x[1]), reverse=True)
        for i, sg in cands:
            if float(sg.get("similarity", 0.0)) < 0.25:
                continue
            res = _cut_from(new_raw, w_start, sg, segs, i, tried_windows, is_full_scanned=False)
            if res:
                return res

    raise HTTPException(
        status_code=400,
        detail=f"Video này không còn cảnh nào khác có mặt {celeb_name} giống ảnh mẫu — hãy bấm 🔄 Đổi video hoặc ✂️ Chỉnh giây.",
    )


def _render_project_internal(
    project_id: str,
    output_folder: Optional[str] = None,
    layout: str = "center_band",
    allow_missing_news: bool = False,
) -> Dict[str, Any]:
    proj = _ensure_project_loaded(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Không tìm thấy dự án.")

    news_clip = proj.get("news_clip")
    is_news_cleared = bool(news_clip and news_clip.get("status") == "cleared")
    has_valid_news = bool(
        news_clip
        and news_clip.get("status") == "ready"
        and news_clip.get("clip_path")
        and os.path.exists(news_clip["clip_path"])
    )

    if not has_valid_news and not is_news_cleared and not allow_missing_news:
        raise HTTPException(status_code=400, detail="Đoạn video bản tin mở đầu chưa sẵn sàng (Hoặc bấm ✕ để xóa bản tin nếu muốn xuất riêng các năm).")

    effective_news = news_clip if has_valid_news else None

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

    target_folder = (output_folder or proj.get("output_folder") or DEFAULT_OUTPUT_DIR).strip()
    try:
        os.makedirs(target_folder, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Không thể tạo thư mục lưu video '{target_folder}': {e}")

    output_path = os.path.normpath(os.path.join(target_folder, out_filename))

    res = render_final_timeline_video(
        news_clip_info=effective_news,
        year_clips_info=ready_year_clips,
        output_path=output_path,
        work_dir=pdir,
        layout=layout,
        celeb_info=celeb,
    )
    res["output_folder"] = target_folder

    with PROJECTS_LOCK:
        PROJECTS[project_id]["output_folder"] = target_folder
        PROJECTS[project_id]["final_video"] = res
        PROJECTS[project_id]["status"] = "completed"
    _save_project_state(project_id)
    return _serialize_project(PROJECTS[project_id])


@app.post("/api/projects/{project_id}/render")
def api_render_project(project_id: str, req: RenderRequest):
    return _render_project_internal(
        project_id=project_id,
        output_folder=req.output_folder,
        layout=req.layout,
        allow_missing_news=False,
    )


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


# ==============================================================================
# AUTO-UPDATE SYSTEM (1-Click Check Update & Apply Update across PCs)
# ==============================================================================

class ApplyUpdateRequest(BaseModel):
    auto_restart: bool = True


class PublishUpdateRequest(BaseModel):
    message: Optional[str] = None
    version: Optional[str] = None


def _normalize_ver_label(raw_ver: Optional[str], default: str = "ver 0.1") -> str:
    s = (raw_ver or "").strip()
    if not s:
        return default
    m = re.search(r"(\d+)\.(\d+)", s)
    if m:
        return f"ver {int(m.group(1))}.{int(m.group(2))}"
    return s if s.lower().startswith("ver") else f"ver {s}"


def _bump_ver_label(cur_ver: Optional[str]) -> str:
    """Increment version in 0.1 steps: ver 0.1 -> ver 0.2 ... ver 0.9 -> ver 1.0 ... ver 3.4 -> ver 3.5."""
    m = re.search(r"(\d+)\.(\d+)", cur_ver or "0.1")
    if not m:
        return "ver 0.1"
    major, minor = int(m.group(1)), int(m.group(2))
    total_tenths = major * 10 + minor + 1
    return f"ver {total_tenths // 10}.{total_tenths % 10}"


def _has_git_repo() -> bool:
    return os.path.isdir(os.path.join(BASE_DIR, ".git")) and (shutil.which("git") is not None)


def _run_git(args: List[str], timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _get_local_version_info() -> Dict[str, Any]:
    """
    Read current local version (`ver X.Y`, commit SHA, message, date) from `version.json` and Git if available,
    so both Git clones and ZIP copies display the exact `ver X.Y` label.
    """
    info: Dict[str, Any] = {
        "version": "ver 0.1",
        "sha": "",
        "short_sha": "unknown",
        "message": "Bản cài đặt cục bộ",
        "date": "",
        "author": "",
        "has_git": False,
        "has_local_changes": False,
        "unpushed_count": 0,
        "repo": GITHUB_REPO,
        "branch": GITHUB_BRANCH,
    }

    if os.path.exists(VERSION_FILE):
        try:
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                info.update({k: v for k, v in saved.items() if v})
        except Exception:
            pass
    info["version"] = _normalize_ver_label(info.get("version"), default="ver 0.1")

    if _has_git_repo():
        info["has_git"] = True
        try:
            r_sha = _run_git(["rev-parse", "HEAD"], timeout=5)
            if r_sha.returncode == 0 and r_sha.stdout.strip():
                sha = r_sha.stdout.strip()
                info["sha"] = sha
                info["short_sha"] = sha[:7]

            r_log = _run_git(["log", "-1", "--format=%s||%ci||%an"], timeout=5)
            if r_log.returncode == 0 and "||" in r_log.stdout:
                parts = r_log.stdout.strip().split("||")
                info["message"] = parts[0].strip()
                info["date"] = parts[1].strip() if len(parts) > 1 else ""
                info["author"] = parts[2].strip() if len(parts) > 2 else ""

            r_st = _run_git(["status", "--porcelain"], timeout=5)
            if r_st.returncode == 0:
                dirty_lines = [
                    ln for ln in r_st.stdout.splitlines()
                    if ln.strip() and not ln.strip().endswith("version.json") and not ln.strip().endswith("feedback.json")
                ]
                info["has_local_changes"] = len(dirty_lines) > 0

            r_unpushed = _run_git(["rev-list", "--count", f"origin/{GITHUB_BRANCH}..HEAD"], timeout=5)
            if r_unpushed.returncode == 0 and r_unpushed.stdout.strip().isdigit():
                info["unpushed_count"] = int(r_unpushed.stdout.strip())

            # Keep version.json synced with current HEAD while preserving human-readable `ver X.Y`
            if info["sha"]:
                try:
                    with open(VERSION_FILE, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "version": info["version"],
                                "sha": info["sha"],
                                "short_sha": info["short_sha"],
                                "message": info["message"],
                                "date": info["date"],
                                "author": info["author"],
                                "repo": GITHUB_REPO,
                                "branch": GITHUB_BRANCH,
                            },
                            f,
                            ensure_ascii=False,
                            indent=2,
                        )
                except Exception:
                    pass
        except Exception:
            pass

    return info


def _fetch_remote_github_info(local_sha: str = "") -> Dict[str, Any]:
    """
    Query GitHub REST API (and/or `git fetch origin main`) for the latest commit on `main`.
    Works on ANY PC even if Git is not installed!
    """
    remote_info: Dict[str, Any] = {
        "version": "",
        "sha": "",
        "short_sha": "",
        "message": "",
        "date": "",
        "author": "",
        "commits": [],
    }

    if _has_git_repo():
        try:
            _run_git(["fetch", "origin", GITHUB_BRANCH, "--quiet"], timeout=15)
            r_v = _run_git(["show", f"origin/{GITHUB_BRANCH}:version.json"], timeout=5)
            if r_v.returncode == 0 and r_v.stdout.strip():
                rv_obj = json.loads(r_v.stdout)
                if rv_obj.get("version"):
                    remote_info["version"] = _normalize_ver_label(rv_obj["version"])
        except Exception:
            pass

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MASYS-CelebTimeline-Updater/1.0",
    }
    try:
        if not remote_info["version"]:
            try:
                rv_res = requests.get(
                    f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/version.json?t={int(time.time())}",
                    timeout=8,
                )
                if rv_res.ok:
                    rv_data = rv_res.json()
                    if rv_data.get("version"):
                        remote_info["version"] = _normalize_ver_label(rv_data["version"])
            except Exception:
                pass
        url = f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
        r = requests.get(url, headers=headers, timeout=10)
        if r.ok:
            data = r.json()
            sha = data.get("sha") or ""
            commit_obj = data.get("commit") or {}
            author_obj = commit_obj.get("author") or {}
            remote_info["sha"] = sha
            remote_info["short_sha"] = sha[:7] if sha else ""
            remote_info["message"] = (commit_obj.get("message") or "").splitlines()[0]
            remote_info["date"] = author_obj.get("date") or ""
            remote_info["author"] = author_obj.get("name") or ""

        if remote_info["sha"] and local_sha and remote_info["sha"] != local_sha:
            comp_url = f"https://api.github.com/repos/{GITHUB_REPO}/compare/{local_sha}...{remote_info['sha']}"
            rc = requests.get(comp_url, headers=headers, timeout=8)
            if rc.ok:
                cdata = rc.json()
                raw_commits = cdata.get("commits") or []
                for c in reversed(raw_commits[-10:]):
                    c_sha = c.get("sha") or ""
                    c_msg = ((c.get("commit") or {}).get("message") or "").splitlines()[0]
                    c_date = (((c.get("commit") or {}).get("author") or {}).get("date") or "")
                    remote_info["commits"].append(
                        {"sha": c_sha[:7], "message": c_msg, "date": c_date}
                    )
    except Exception:
        pass

    # Fallback to git origin/main if GitHub API was unreachable or rate-limited
    if not remote_info["sha"] and _has_git_repo():
        try:
            r_sha = _run_git(["rev-parse", f"origin/{GITHUB_BRANCH}"], timeout=5)
            if r_sha.returncode == 0 and r_sha.stdout.strip():
                sha = r_sha.stdout.strip()
                remote_info["sha"] = sha
                remote_info["short_sha"] = sha[:7]
            r_log = _run_git(["log", "-1", f"origin/{GITHUB_BRANCH}", "--format=%s||%ci||%an"], timeout=5)
            if r_log.returncode == 0 and "||" in r_log.stdout:
                parts = r_log.stdout.strip().split("||")
                remote_info["message"] = parts[0].strip()
                remote_info["date"] = parts[1].strip() if len(parts) > 1 else ""
                remote_info["author"] = parts[2].strip() if len(parts) > 2 else ""
            if local_sha and remote_info["sha"] and local_sha != remote_info["sha"]:
                r_list = _run_git(["log", f"{local_sha}..origin/{GITHUB_BRANCH}", "--oneline", "-n", "10"], timeout=5)
                if r_list.returncode == 0:
                    for ln in r_list.stdout.splitlines():
                        if ln.strip():
                            sp = ln.strip().split(" ", 1)
                            remote_info["commits"].append(
                                {"sha": sp[0], "message": sp[1] if len(sp) > 1 else "", "date": ""}
                            )
        except Exception:
            pass

    if not remote_info["commits"] and remote_info["sha"]:
        remote_info["commits"] = [
            {
                "sha": remote_info["short_sha"],
                "message": remote_info["message"],
                "date": remote_info["date"],
            }
        ]

    return remote_info


@app.get("/api/update/version")
def api_update_version():
    return _get_local_version_info()


@app.get("/api/update/check")
def api_update_check():
    local_info = _get_local_version_info()
    local_sha = local_info.get("sha") or ""
    remote_info = _fetch_remote_github_info(local_sha=local_sha)
    remote_sha = remote_info.get("sha") or ""

    if not remote_sha:
        raise HTTPException(
            status_code=503,
            detail="Không thể kết nối tới máy chủ GitHub để kiểm tra cập nhật. Vui lòng kiểm tra kết nối mạng.",
        )

    is_ahead_of_remote = False
    if _has_git_repo() and local_sha and remote_sha and local_sha != remote_sha:
        try:
            r_anc = _run_git(["merge-base", "--is-ancestor", remote_sha, local_sha], timeout=5)
            if r_anc.returncode == 0:
                is_ahead_of_remote = True
        except Exception:
            pass

    has_update = bool(remote_sha and remote_sha != local_sha and not is_ahead_of_remote)
    update_method = "git" if _has_git_repo() else "github_zip"

    return {
        "ok": True,
        "has_update": has_update,
        "is_ahead_of_remote": is_ahead_of_remote,
        "update_method": update_method,
        "local": local_info,
        "remote": remote_info,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _schedule_server_restart():
    """
    Restart the server process cleanly on Windows after returning the HTTP response:
    - Works in BOTH raw Python mode and frozen PyInstaller `.exe` mode.
    """
    def _restart_worker():
        time.sleep(0.6)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            if getattr(sys, "frozen", False):
                exe_path = os.path.abspath(sys.executable)
                cmd_str = f'timeout /t 2 /nobreak >nul & start "" "{exe_path}" --no-browser'
                subprocess.Popen(
                    ["cmd", "/c", cmd_str],
                    cwd=BASE_DIR,
                    creationflags=creation_flags,
                )
            else:
                app_script = os.path.abspath(__file__)
                helper_code = (
                    "import time, subprocess; "
                    "time.sleep(1.2); "
                    f"subprocess.Popen([r'''{sys.executable}''', r'''{app_script}'''], cwd=r'''{BASE_DIR}''')"
                )
                subprocess.Popen(
                    [sys.executable, "-c", helper_code],
                    cwd=BASE_DIR,
                    creationflags=creation_flags,
                )
        except Exception:
            pass
        os._exit(0)

    t = threading.Thread(target=_restart_worker, daemon=True)
    t.start()


@app.post("/api/update/apply")
def api_update_apply(req: ApplyUpdateRequest):
    """
    1-Click Update:
    1. If `.git` and `git` exist, fetches `origin/main` and resets tracked files cleanly (`git reset --hard origin/main`).
    2. Otherwise (or if git fails), downloads the latest ZIP archive directly from GitHub and extracts over `BASE_DIR`
       while preserving `workspace/` and `feedback.json`.
    3. Installs any new dependencies in `requirements.txt`.
    4. Writes updated `version.json` and automatically restarts the server.
    """
    local_info = _get_local_version_info()
    remote_info = _fetch_remote_github_info(local_sha=local_info.get("sha") or "")
    remote_sha = remote_info.get("sha") or ""

    updated_via = ""
    error_details = []

    # Engine 1: Update via Git if available
    if _has_git_repo():
        try:
            r_fetch = _run_git(["fetch", "origin", GITHUB_BRANCH], timeout=35)
            if r_fetch.returncode == 0:
                r_reset = _run_git(["reset", "--hard", f"origin/{GITHUB_BRANCH}"], timeout=20)
                if r_reset.returncode == 0:
                    updated_via = "git"
                else:
                    error_details.append(f"git reset error: {r_reset.stderr}")
            else:
                error_details.append(f"git fetch error: {r_fetch.stderr}")
        except Exception as e:
            error_details.append(f"git exception: {e}")

    # Engine 2: Fallback to direct GitHub ZIP download (works even on PCs without Git installed!)
    if not updated_via:
        tmp_dir = os.path.join(WORKSPACE_DIR, "_update_tmp")
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            os.makedirs(tmp_dir, exist_ok=True)

            zip_path = os.path.join(tmp_dir, "update_main.zip")
            zip_urls = [
                f"https://codeload.github.com/{GITHUB_REPO}/zip/refs/heads/{GITHUB_BRANCH}",
                f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{GITHUB_BRANCH}.zip",
            ]
            downloaded = False
            for zurl in zip_urls:
                try:
                    resp = requests.get(zurl, timeout=45, stream=True)
                    if resp.ok:
                        with open(zip_path, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                        if os.path.exists(zip_path) and os.path.getsize(zip_path) > 5000:
                            downloaded = True
                            break
                except Exception as ex:
                    error_details.append(str(ex))

            if not downloaded:
                raise RuntimeError("Không thể tải gói cập nhật từ GitHub.")

            extract_root = os.path.join(tmp_dir, "extracted")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_root)

            entries = os.listdir(extract_root)
            src_root = os.path.join(extract_root, entries[0]) if len(entries) == 1 else extract_root

            preserve_names = {
                "workspace",
                "output",
                ".git",
                "__pycache__",
                "feedback.json",
                "cookies.txt",
                "bin",
                "_internal",
                "MASYS-CelebTimeline.exe",
                "MASYS-CelebTimeline_EXE",
            }
            for item_name in os.listdir(src_root):
                if item_name in preserve_names:
                    continue
                s_path = os.path.join(src_root, item_name)
                d_path = os.path.join(BASE_DIR, item_name)
                if os.path.isdir(s_path):
                    shutil.copytree(s_path, d_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(s_path, d_path)

            updated_via = "github_zip"
        except Exception as e:
            error_details.append(f"zip update error: {e}")
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    if not updated_via:
        raise HTTPException(
            status_code=500,
            detail=f"Cập nhật thất bại: {'; '.join(error_details)}",
        )

    # Install any updated Python dependencies & keep yt-dlp fresh when running via Python (never spawn pip inside frozen EXE)
    if not getattr(sys, "frozen", False):
        req_file = os.path.join(BASE_DIR, "requirements.txt")
        if os.path.exists(req_file):
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"],
                    cwd=BASE_DIR,
                    capture_output=True,
                    timeout=45,
                )
            except Exception:
                pass
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "--quiet"],
                cwd=BASE_DIR,
                capture_output=True,
                timeout=45,
            )
        except Exception:
            pass

    # Save updated version.json
    new_version_data = {
        "version": remote_info.get("version") or local_info.get("version") or "ver 0.1",
        "sha": remote_sha or local_info.get("sha", ""),
        "short_sha": (remote_sha[:7] if remote_sha else local_info.get("short_sha", "")),
        "message": remote_info.get("message") or local_info.get("message", ""),
        "date": remote_info.get("date") or time.strftime("%Y-%m-%d %H:%M:%S"),
        "author": remote_info.get("author") or local_info.get("author", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": GITHUB_REPO,
        "branch": GITHUB_BRANCH,
    }
    try:
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            json.dump(new_version_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if req.auto_restart:
        _schedule_server_restart()

    return {
        "ok": True,
        "updated_via": updated_via,
        "new_version": new_version_data,
        "restarting": req.auto_restart,
    }


@app.post("/api/update/publish")
def api_update_publish(req: PublishUpdateRequest):
    """
    Developer helper: Commit any local changes & push `HEAD` to `origin/main` on GitHub
    so that client PCs immediately see and receive the new update when clicking "Check Update".
    """
    if not _has_git_repo():
        raise HTTPException(status_code=400, detail="Máy này không có Git repository (.git) để đẩy code.")

    commit_msg = (req.message or "").strip() or f"Update MASYS CelebTimeline ({time.strftime('%Y-%m-%d %H:%M')})"
    cur_local = _get_local_version_info()
    target_ver = _normalize_ver_label(req.version) if req.version else _bump_ver_label(cur_local.get("version"))

    try:
        # Update version.json with target_ver first
        try:
            cur_v_data = {}
            if os.path.exists(VERSION_FILE):
                with open(VERSION_FILE, "r", encoding="utf-8") as f:
                    cur_v_data = json.load(f)
            cur_v_data["version"] = target_ver
            with open(VERSION_FILE, "w", encoding="utf-8") as f:
                json.dump(cur_v_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        _run_git(["add", "-A"], timeout=15)
        r_st = _run_git(["status", "--porcelain"], timeout=10)
        if r_st.returncode == 0 and r_st.stdout.strip():
            r_cm = _run_git(["commit", "-m", commit_msg], timeout=20)
            if r_cm.returncode != 0 and "nothing to commit" not in (r_cm.stdout + r_cm.stderr).lower():
                raise RuntimeError(r_cm.stderr or r_cm.stdout)

        # Refresh version.json with the new commit SHA while preserving target_ver
        new_local = _get_local_version_info()
        if new_local.get("sha"):
            _run_git(["add", "version.json"], timeout=10)
            _run_git(["commit", "--amend", "--no-edit"], timeout=15)
            new_local = _get_local_version_info()

        r_push = _run_git(["push", "origin", GITHUB_BRANCH], timeout=45)
        if r_push.returncode != 0:
            raise RuntimeError(r_push.stderr or r_push.stdout)

        return {
            "ok": True,
            "published_version": new_local,
            "message": f"Đã đẩy bản cập nhật {new_local.get('version')} (#{new_local['short_sha']}) lên GitHub ({GITHUB_REPO}) thành công! Các máy khác đã có thể bấm Cập nhật ngay.",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không thể đẩy bản cập nhật lên GitHub: {e}")


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
