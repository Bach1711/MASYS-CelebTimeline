import os
import cv2
import numpy as np
from typing import List, Dict, Any, Optional

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
YUNET_PATH = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_PATH = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")


def read_image_unicode(path: str) -> Optional[np.ndarray]:
    """Read image safely on Windows even with Vietnamese/Unicode characters in path."""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def write_image_unicode(path: str, img: np.ndarray) -> bool:
    """Write image safely on Windows with Unicode paths."""
    try:
        ext = os.path.splitext(path)[1] or ".jpg"
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception:
        return False


class FaceEngine:
    def __init__(self):
        self.detector = None
        self.recognizer = None
        self._init_models()

    def _init_models(self):
        if os.path.exists(YUNET_PATH):
            self.detector = cv2.FaceDetectorYN.create(
                YUNET_PATH,
                "",
                (320, 320),
                score_threshold=0.6,
                nms_threshold=0.3,
                top_k=5000,
            )
        if os.path.exists(SFACE_PATH):
            self.recognizer = cv2.FaceRecognizerSF.create(SFACE_PATH, "")

    def detect_faces(self, img: np.ndarray) -> List[np.ndarray]:
        """Return list of raw YuNet face rows sorted by area descending."""
        if img is None or self.detector is None:
            return []
        h, w = img.shape[:2]
        if h == 0 or w == 0:
            return []
        max_dim = 640
        scale = 1.0
        if max(h, w) > max_dim:
            scale = max_dim / float(max(h, w))
            resized = cv2.resize(img, (int(w * scale), int(h * scale)))
        else:
            resized = img

        rh, rw = resized.shape[:2]
        self.detector.setInputSize((rw, rh))
        _, faces = self.detector.detect(resized)
        if faces is None or len(faces) == 0:
            return []

        results = []
        for f in faces:
            f_scaled = f.copy()
            f_scaled[:14] = f_scaled[:14] / scale
            results.append(f_scaled)

        results.sort(key=lambda x: float(x[2] * x[3]), reverse=True)
        return results

    def extract_embedding(self, img: np.ndarray, face_row: np.ndarray) -> Optional[np.ndarray]:
        """Extract 128-D SFace embedding for a detected face."""
        if self.recognizer is None or img is None or face_row is None:
            return None
        try:
            aligned = self.recognizer.alignCrop(img, face_row)
            feat = self.recognizer.feature(aligned)
            return feat
        except Exception:
            return None

    def extract_reference_embeddings(self, image_paths: List[str]) -> List[np.ndarray]:
        """Extract reference face embeddings from one or more reference portrait photos."""
        embeddings = []
        for path in image_paths:
            if not os.path.exists(path):
                continue
            img = read_image_unicode(path)
            if img is None:
                continue
            faces = self.detect_faces(img)
            if not faces:
                continue
            emb = self.extract_embedding(img, faces[0])
            if emb is not None:
                embeddings.append(emb)
        return embeddings

    def compare_embeddings(self, feat: np.ndarray, ref_embeddings: List[np.ndarray]) -> float:
        """Return maximum cosine similarity (0.0 to 1.0) against reference embeddings."""
        if self.recognizer is None or feat is None or not ref_embeddings:
            return 0.5
        best_sim = -1.0
        for ref in ref_embeddings:
            try:
                sim = float(
                    self.recognizer.match(
                        feat, ref, cv2.FaceRecognizerSF_FR_COSINE
                    )
                )
                if sim > best_sim:
                    best_sim = sim
            except Exception:
                continue
        return max(0.0, min(1.0, best_sim))

    def _compute_text_overlay_score(self, frame: np.ndarray) -> float:
        """
        Detect burned-in text, subtitles, audio waveforms, and lower-third news banners (chyrons)
        using morphological TopHat (bright text on dark bg) and BlackHat (dark text on white banner)
        in the middle and lower bands of the frame.
        Returns a score in [0.0, 1.0] where <= 0.14 is clean video and >= 0.20 has visible text/banner overlays.
        """
        try:
            small = cv2.resize(frame, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            roi = gray[68:172, 12:308]
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
            tophat = cv2.morphologyEx(roi, cv2.MORPH_TOPHAT, kernel)
            blackhat = cv2.morphologyEx(roi, cv2.MORPH_BLACKHAT, kernel)
            char_strokes = ((tophat > 75) | (blackhat > 75)).astype(np.float32)
            row_cov = np.mean(char_strokes, axis=1)
            return round(float(np.mean(row_cov > 0.085)), 3)
        except Exception:
            return 0.0

    def score_frame(
        self,
        frame: np.ndarray,
        ref_embeddings: List[np.ndarray],
        require_identity: bool = True,
    ) -> Dict[str, Any]:
        """
        Evaluate a video frame for:
        1. Presence & identity match of the main celebrity face (`SFace >= 0.26`).
        2. Absence of burned-in text, subtitles, or lower-third banners (`text_score`).
        3. Aligned face crop & scene patch for live-motion verification across frames.
        """
        if frame is None:
            return {
                "score": 0.0,
                "matched": False,
                "similarity": 0.0,
                "face_count": 0,
                "text_score": 0.0,
                "aligned_gray": None,
                "frame_gray": None,
            }

        h, w = frame.shape[:2]
        frame_area = float(max(1, h * w))
        text_score = self._compute_text_overlay_score(frame)
        small_frame_gray = cv2.cvtColor(cv2.resize(frame, (96, 54)), cv2.COLOR_BGR2GRAY).astype(np.float32)

        faces = self.detect_faces(frame)
        if not faces:
            return {
                "score": 0.0,
                "matched": False,
                "similarity": 0.0,
                "face_count": 0,
                "text_score": text_score,
                "aligned_gray": None,
                "frame_gray": small_frame_gray,
            }

        best_face_score = 0.0
        best_sim = 0.0
        matched = False
        best_bbox = None
        best_emb = None
        best_aligned_gray = None

        for face in faces[:4]:
            x, y, fw, fh = float(face[0]), float(face[1]), float(face[2]), float(face[3])
            conf = float(face[14])
            area_ratio = (fw * fh) / frame_area

            # Ignore tiny background faces (< 1.5% of frame area)
            if area_ratio < 0.015:
                continue

            cx = (x + fw * 0.5) / w
            cy = (y + fh * 0.5) / h
            center_dist = ((cx - 0.5) ** 2 + (cy - 0.45) ** 2) ** 0.5
            center_weight = max(0.45, 1.0 - center_dist)

            size_score = min(1.0, area_ratio / 0.075)

            sim = 0.5
            emb = None
            aligned_gray = None
            if self.recognizer is not None:
                try:
                    aligned_bgr = self.recognizer.alignCrop(frame, face)
                    aligned_gray = cv2.cvtColor(cv2.resize(aligned_bgr, (64, 64)), cv2.COLOR_BGR2GRAY).astype(np.float32)
                    if require_identity and ref_embeddings:
                        emb = self.recognizer.feature(aligned_bgr)
                        if emb is not None:
                            sim = self.compare_embeddings(emb, ref_embeddings)
                except Exception:
                    pass

            if require_identity and ref_embeddings:
                # >= 0.26 is a verified identity match; strictly reject other people (< 0.26)
                if sim >= 0.26:
                    is_celeb = True
                    sim_mult = min(1.0, 0.45 + (sim / 0.55) * 0.55)
                else:
                    is_celeb = False
                    sim_mult = 0.01
            else:
                is_celeb = True
                sim_mult = 0.8

            crowd_penalty = 1.0 if len(faces) <= 2 else max(0.50, 1.0 - 0.14 * (len(faces) - 2))
            if text_score >= 0.22:
                text_penalty = 0.08
            elif text_score >= 0.14:
                text_penalty = 0.35
            else:
                text_penalty = 1.0

            total_score = conf * size_score * center_weight * sim_mult * crowd_penalty * text_penalty
            if total_score > best_face_score:
                best_face_score = total_score
                best_sim = sim
                matched = is_celeb
                best_bbox = [int(x), int(y), int(fw), int(fh)]
                best_emb = emb
                best_aligned_gray = aligned_gray

        return {
            "score": round(float(best_face_score), 4),
            "matched": matched,
            "similarity": round(float(best_sim), 4),
            "face_count": len(faces),
            "text_score": text_score,
            "bbox": best_bbox,
            "embedding": best_emb,
            "aligned_gray": best_aligned_gray,
            "frame_gray": small_frame_gray,
        }

    def find_best_segments(
        self,
        video_path: str,
        clip_duration: float = 4.0,
        min_duration: float = 3.5,
        max_duration: float = 5.4,
        ref_embeddings: Optional[List[np.ndarray]] = None,
        sample_fps: float = 2.0,
        top_k: int = 6,
        skip_start_sec: float = 1.0,
    ) -> List[Dict[str, Any]]:
        """
        Scan a video and find the top_k non-overlapping segments that satisfy all 3 rules:
        1. Continuous presence of the target celebrity's face (`match_ratio >= 0.78`, including start/mid/end frames).
        2. Real live-action motion (`is_live_motion=True`: celebrity's aligned face is actively moving/talking/interacting,
           strictly rejecting static photos, slideshows, and audio-waveform clips).
        3. Clean video without heavy burned-in text, subtitles, or TV lower-third banners (`is_clean_no_text=True`).
        """
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return []

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        total_duration = total_frames / fps if total_frames > 0 else 0.0

        min_dur = max(2.8, min(min_duration, clip_duration))
        max_dur = max(clip_duration, max_duration)

        if total_duration <= min_dur + 0.5:
            cap.release()
            return []

        step_frames = max(1, int(round(fps / sample_fps)))
        start_frame = int(min(skip_start_sec, max(0.0, total_duration - min_dur - 0.5)) * fps)

        timeline: List[Dict[str, Any]] = []
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        frame_idx = start_frame

        while True:
            ret, frame = cap.read()
            if not ret:
                break
            t_sec = frame_idx / fps
            info = self.score_frame(
                frame,
                ref_embeddings=ref_embeddings or [],
                require_identity=bool(ref_embeddings),
            )
            info["time"] = round(t_sec, 2)
            timeline.append(info)

            frame_idx += step_frames
            if frame_idx >= total_frames:
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)

        cap.release()

        if not timeline:
            return []

        min_samples = max(3, int(round(min_dur * sample_fps)))
        max_samples = max(min_samples + 1, int(round(max_dur * sample_fps)))
        candidates = []

        for i in range(len(timeline)):
            base_slice = timeline[i : i + min_samples]
            if len(base_slice) < min_samples:
                continue

            # Adaptively extend the window up to max_samples ONLY while the celebrity's face stays continuously matched & clean!
            w_slice = list(base_slice)
            for j in range(i + min_samples, min(len(timeline), i + max_samples)):
                nxt = timeline[j]
                if nxt["matched"] and nxt["score"] >= 0.08 and nxt.get("text_score", 0.0) <= 0.16:
                    w_slice.append(nxt)
                else:
                    break

            start_t = w_slice[0]["time"]
            adaptive_dur = round(min(max_dur, max(min_dur, (w_slice[-1]["time"] - start_t) + (1.0 / sample_fps))), 2)
            end_t = round(start_t + adaptive_dur, 2)
            if end_t > total_duration + 0.3 and i > 0:
                break

            scores = [x["score"] for x in w_slice]
            matched_sims = [x["similarity"] for x in w_slice if x["matched"]]
            all_sims = [x["similarity"] for x in w_slice]
            matched_flags = [1.0 if x["matched"] else 0.0 for x in w_slice]
            has_face_flags = [1.0 if x["face_count"] > 0 else 0.0 for x in w_slice]
            text_scores = [float(x.get("text_score", 0.0)) for x in w_slice]

            avg_score = float(np.mean(scores))
            avg_sim = float(np.mean(matched_sims)) if matched_sims else float(np.mean(all_sims))
            match_ratio = float(np.mean(matched_flags))
            face_ratio = float(np.mean(has_face_flags))
            avg_text_score = float(np.mean(text_scores))

            # Check continuous celebrity presence across start, middle, and end of the clip (prevents cutaway to audience/other people!)
            mid_idx = len(w_slice) // 2
            start_has_celeb = any(x["matched"] for x in w_slice[:2])
            mid_has_celeb = bool(w_slice[mid_idx]["matched"]) or (len(w_slice) >= 3 and bool(w_slice[mid_idx - 1]["matched"]))
            end_has_celeb = any(x["matched"] for x in w_slice[-2:])
            has_continuous_celeb = (match_ratio >= 0.78 and start_has_celeb and mid_has_celeb and end_has_celeb)

            # Compute live facial & scene motion across consecutive frames (rejects static photos / audio-waveform slideshows!)
            aligned_patches = [x["aligned_gray"] for x in w_slice if x.get("aligned_gray") is not None]
            face_diffs = [
                float(np.mean(np.abs(aligned_patches[k] - aligned_patches[k - 1])))
                for k in range(1, len(aligned_patches))
            ]
            median_face_motion = float(np.median(face_diffs)) if face_diffs else 0.0

            frame_patches = [x["frame_gray"] for x in w_slice if x.get("frame_gray") is not None]
            frame_diffs = [
                float(np.mean(np.abs(frame_patches[k] - frame_patches[k - 1])[:36, :]))
                for k in range(1, len(frame_patches))
            ]
            median_frame_motion = float(np.median(frame_diffs)) if frame_diffs else 0.0

            # Live motion requires continuous movement of the face (not frozen photo) AND scene
            is_live_motion = (median_face_motion >= 7.8 and median_frame_motion >= 1.8)
            is_clean_no_text = (avg_text_score <= 0.16)

            # Find best timestamp offset inside this segment where the celebrity's face is clearest (for thumbnail!)
            best_frame_item = max(w_slice, key=lambda x: (x["matched"], x["score"], x["similarity"]))
            best_thumb_offset = max(0.3, min(adaptive_dur - 0.3, round(best_frame_item["time"] - start_t, 2)))

            continuity_bonus = 1.4 if has_continuous_celeb else (0.6 if match_ratio >= 0.60 else 0.05)
            motion_bonus = 1.35 if is_live_motion else 0.04
            text_bonus = 1.25 if is_clean_no_text else (0.35 if avg_text_score <= 0.24 else 0.05)
            length_bonus = 1.0 + 0.06 * max(0.0, adaptive_dur - min_dur)

            combined = avg_score * continuity_bonus * motion_bonus * text_bonus * length_bonus
            candidates.append(
                {
                    "start_sec": round(start_t, 2),
                    "end_sec": round(min(total_duration, end_t), 2),
                    "duration_sec": round(min(total_duration - start_t, adaptive_dur), 2),
                    "best_thumb_offset_sec": best_thumb_offset,
                    "score": round(combined, 4),
                    "similarity": round(avg_sim, 3),
                    "face_ratio": round(face_ratio, 2),
                    "match_ratio": round(match_ratio, 2),
                    "face_motion": round(median_face_motion, 2),
                    "text_score": round(avg_text_score, 3),
                    "is_live_motion": is_live_motion,
                    "is_clean_no_text": is_clean_no_text,
                    "has_continuous_celeb": has_continuous_celeb,
                }
            )

        candidates.sort(
            key=lambda x: (
                x["has_continuous_celeb"] and x["is_live_motion"] and x["is_clean_no_text"],
                x["has_continuous_celeb"] and x["is_live_motion"],
                x["is_live_motion"] and x["match_ratio"] >= 0.65,
                x["score"],
                x["similarity"],
            ),
            reverse=True,
        )

        selected: List[Dict[str, Any]] = []
        for cand in candidates:
            if any(abs(cand["start_sec"] - s["start_sec"]) < 3.8 for s in selected):
                continue
            selected.append(cand)
            if len(selected) >= top_k:
                break

        if not selected and candidates:
            selected.append(candidates[0])

        return selected
