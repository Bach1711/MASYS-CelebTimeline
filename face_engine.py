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

    def score_frame(
        self,
        frame: np.ndarray,
        ref_embeddings: List[np.ndarray],
        require_identity: bool = True,
    ) -> Dict[str, Any]:
        """
        Evaluate a video frame for presence of the main celebrity face.
        Strongly penalizes frames where only an interviewer/other actor is present.
        """
        if frame is None:
            return {"score": 0.0, "matched": False, "similarity": 0.0, "face_count": 0}

        h, w = frame.shape[:2]
        frame_area = float(max(1, h * w))
        faces = self.detect_faces(frame)
        if not faces:
            return {"score": 0.0, "matched": False, "similarity": 0.0, "face_count": 0}

        best_face_score = 0.0
        best_sim = 0.0
        matched = False
        best_bbox = None
        best_emb = None

        for face in faces[:4]:
            x, y, fw, fh = float(face[0]), float(face[1]), float(face[2]), float(face[3])
            conf = float(face[14])
            area_ratio = (fw * fh) / frame_area

            # Ignore tiny background faces (< 1.4% of frame area)
            if area_ratio < 0.014:
                continue

            cx = (x + fw * 0.5) / w
            cy = (y + fh * 0.5) / h
            center_dist = ((cx - 0.5) ** 2 + (cy - 0.45) ** 2) ** 0.5
            center_weight = max(0.45, 1.0 - center_dist)

            size_score = min(1.0, area_ratio / 0.075)

            sim = 0.5
            emb = None
            if require_identity and ref_embeddings and self.recognizer is not None:
                emb = self.extract_embedding(frame, face)
                if emb is not None:
                    sim = self.compare_embeddings(emb, ref_embeddings)

            if require_identity and ref_embeddings:
                # >= 0.24 is a verified identity match across decades in SFace cosine similarity
                if sim >= 0.24:
                    is_celeb = True
                    sim_mult = min(1.0, 0.45 + (sim / 0.55) * 0.55)
                else:
                    is_celeb = False
                    # Strong penalty for non-matching faces so other people are never selected
                    sim_mult = 0.03
            else:
                is_celeb = True
                sim_mult = 0.8

            crowd_penalty = 1.0 if len(faces) <= 2 else max(0.55, 1.0 - 0.12 * (len(faces) - 2))

            total_score = conf * size_score * center_weight * sim_mult * crowd_penalty
            if total_score > best_face_score:
                best_face_score = total_score
                best_sim = sim
                matched = is_celeb
                best_bbox = [int(x), int(y), int(fw), int(fh)]
                best_emb = emb

        return {
            "score": round(float(best_face_score), 4),
            "matched": matched,
            "similarity": round(float(best_sim), 4),
            "face_count": len(faces),
            "bbox": best_bbox,
            "embedding": best_emb,
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
        Scan a video and find the top_k non-overlapping segments that have the clearest,
        most continuous view of the target celebrity's face.
        Supports adaptive segment duration (`min_duration` to `max_duration` seconds)
        so continuous close-up shots can naturally play for 4.0s-5.4s instead of a rigid 3.5s.
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
            dur_val = round(min(total_duration, clip_duration), 2)
            return [
                {
                    "start_sec": 0.0,
                    "end_sec": dur_val,
                    "duration_sec": dur_val,
                    "score": 0.1,
                    "similarity": 0.2,
                    "face_ratio": 0.5,
                    "match_ratio": 0.0,
                }
            ]

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

        min_samples = max(2, int(round(min_dur * sample_fps)))
        max_samples = max(min_samples + 1, int(round(max_dur * sample_fps)))
        candidates = []

        for i in range(len(timeline)):
            base_slice = timeline[i : i + min_samples]
            if len(base_slice) < max(2, min_samples - 1):
                continue

            # Adaptively extend the window up to max_samples while the celebrity's face stays on screen!
            w_slice = list(base_slice)
            for j in range(i + min_samples, min(len(timeline), i + max_samples)):
                nxt = timeline[j]
                if nxt["matched"] and nxt["score"] >= 0.08:
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

            avg_score = float(np.mean(scores))
            avg_sim = float(np.mean(matched_sims)) if matched_sims else float(np.mean(all_sims))
            match_ratio = float(np.mean(matched_flags))
            face_ratio = float(np.mean(has_face_flags))

            continuity_bonus = 1.3 if face_ratio >= 0.85 else (0.9 if face_ratio >= 0.6 else 0.25)
            if match_ratio >= 0.75:
                identity_bonus = 2.0
            elif match_ratio >= 0.50:
                identity_bonus = 1.25
            elif match_ratio >= 0.28:
                identity_bonus = 0.5
            else:
                identity_bonus = 0.05

            # Slight bonus for longer continuous shots
            length_bonus = 1.0 + 0.06 * max(0.0, adaptive_dur - min_dur)
            combined = avg_score * continuity_bonus * identity_bonus * length_bonus
            candidates.append(
                {
                    "start_sec": round(start_t, 2),
                    "end_sec": round(min(total_duration, end_t), 2),
                    "duration_sec": round(min(total_duration - start_t, adaptive_dur), 2),
                    "score": round(combined, 4),
                    "similarity": round(avg_sim, 3),
                    "face_ratio": round(face_ratio, 2),
                    "match_ratio": round(match_ratio, 2),
                }
            )

        candidates.sort(key=lambda x: (x["match_ratio"] >= 0.5, x["score"], x["similarity"]), reverse=True)

        selected: List[Dict[str, Any]] = []
        for cand in candidates:
            # Ensure non-overlapping segments (at least 3.5s apart) so Scene 1 and Scene 2 of the same video are distinct!
            if any(abs(cand["start_sec"] - s["start_sec"]) < 3.8 for s in selected):
                continue
            selected.append(cand)
            if len(selected) >= top_k:
                break

        if not selected and candidates:
            selected.append(candidates[0])

        return selected
