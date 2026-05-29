"""
Batch-цикл обработки всех камер.

Pipeline:
  Видеопоток -> YOLO -> ByteTrack (локально на камеру) ->
  перевод в "мировые координаты" через pinhole-mapper ->
  дедупликация между камерами через WorldTracker ->
  серверный heatmap -> WS broadcast.
"""
import base64
import datetime
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import cv2

from backend.analytics.heatmap import Heatmap
from backend.api.routes import SCENE
from backend.core.render import annotate_frame, project_point
from backend.core.state import update_camera_state, broadcast_current_state
from backend.db import storage
from backend.mapping.factory import build_mapper, mapper_kind
from backend.video_analytics.pipeline import (
    batch_detect, make_tracker, track_objects,
)
from backend.video_analytics.world_tracker import WorldTracker

TARGET_FPS_FALLBACK = 30
TARGET_FPS_CAP = 30
JPEG_QUALITY = 50
WS_MAX_W = 480
WARMUP_ITERS = 3

WORLD_LOG_PATH = os.environ.get("WORLD_TRACKER_LOG", "world_tracker.jsonl")
WORLD_LOG_VERBOSE = os.environ.get("WORLD_TRACKER_VERBOSE", "0") in ("1", "true", "True")

_encode_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="enc")

_global_heatmap: Heatmap | None = None
_world_tracker: WorldTracker | None = None


def get_global_heatmap() -> Heatmap:
    global _global_heatmap
    sw = float(SCENE.get("width", 20))
    sh = float(SCENE.get("height", 20))
    if _global_heatmap is None:
        _global_heatmap = Heatmap(width=sw, height=sh)
    else:
        _global_heatmap.resize(sw, sh)
    return _global_heatmap


def get_world_tracker() -> WorldTracker:
    global _world_tracker
    if _world_tracker is None:
        _world_tracker = WorldTracker()
    return _world_tracker


def reset_global_state():
    if _global_heatmap is not None:
        _global_heatmap.reset()
    if _world_tracker is not None:
        _world_tracker.reset()


def _open_capture(source, cam_id):
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"[camera {cam_id}] ERROR opening {source}")
        return None, 0, 0, 0.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if w <= 0 or h <= 0:
        ret, frame = cap.read()
        if ret:
            h, w = frame.shape[:2]
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    return cap, w, h, fps


def _process_camera(cam_id, frame, det_list, tracker, mapper, sw, sh,
                     verbose: bool = False):
    timestamp = time.time()
    objects_px = track_objects(tracker, det_list, timestamp, cam_id)

    frame_h, frame_w = frame.shape[:2]

    if not verbose:
        out = []
        dropped: list[dict] = []
        for obj in objects_px:
            px = float(obj.get("x", 0))
            py = float(obj.get("y", 0))
            lid = int(obj["id"])
            conf = float(obj.get("confidence", 0.0))
            wxwy = project_point(mapper, px, py, frame_w, frame_h, sw, sh)
            if wxwy is None:
                dropped.append({"cam": cam_id, "lid": lid,
                                "conf": round(conf, 3),
                                "reason": "off_scene"})
                continue
            wx, wy = wxwy
            out.append({
                "id": f"{cam_id}:{lid}",
                "track_id": lid,
                "camera_id": cam_id,
                "bbox": [float(v) for v in obj["bbox"]],
                "confidence": conf,
                "x": wx,
                "y": wy,
            })
        return out, dropped, None

    raw_yolo_count = 0
    raw_yolo: list = []
    if det_list:
        xyxy, conf_arr = det_list[0]
        raw_yolo_count = len(xyxy)
        for i in range(len(xyxy)):
            x1, y1, x2, y2 = [float(v) for v in xyxy[i]]
            raw_yolo.append({
                "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                "conf": round(float(conf_arr[i]), 3),
                "foot_px": [round((x1 + x2) / 2, 1), round(y2, 1)],
            })

    out = []
    dropped = []
    bytetrack_log: list = []
    for obj in objects_px:
        px = float(obj.get("x", 0))
        py = float(obj.get("y", 0))
        lid = int(obj["id"])
        conf = float(obj.get("confidence", 0.0))
        bbox = [float(v) for v in obj["bbox"]]
        wxwy = project_point(mapper, px, py, frame_w, frame_h, sw, sh)
        if wxwy is None:
            dropped.append({"cam": cam_id, "lid": lid,
                            "conf": round(conf, 3),
                            "reason": "off_scene"})
            bytetrack_log.append({
                "lid": lid, "conf": round(conf, 3),
                "foot_px": [round(px, 1), round(py, 1)],
                "bbox": [round(v, 1) for v in bbox],
                "projected": None,
                "drop_reason": "off_scene",
            })
            continue
        wx, wy = wxwy
        out.append({
            "id": f"{cam_id}:{lid}",
            "track_id": lid,
            "camera_id": cam_id,
            "bbox": bbox,
            "confidence": conf,
            "x": wx,
            "y": wy,
        })
        bytetrack_log.append({
            "lid": lid, "conf": round(conf, 3),
            "foot_px": [round(px, 1), round(py, 1)],
            "bbox": [round(v, 1) for v in bbox],
            "projected": [round(wx, 3), round(wy, 3)],
            "drop_reason": None,
        })

    cam_diag = {
        "cam": cam_id,
        "frame_w": frame_w,
        "frame_h": frame_h,
        "raw_yolo_count": raw_yolo_count,
        "bytetrack_count": len(objects_px),
        "projected_count": len(out),
        "dropped_count": len(dropped),
        "raw_yolo": raw_yolo,
        "bytetrack": bytetrack_log,
    }
    return out, dropped, cam_diag


def _strip_internal(points):
    return list(points)


def _encode_and_publish(cam_id, frame, objects_world, cam_cfg, source):
    frame_h, frame_w = frame.shape[:2]
    if frame_w > WS_MAX_W:
        scale = WS_MAX_W / frame_w
        small = cv2.resize(frame, (WS_MAX_W, int(frame_h * scale)),
                           interpolation=cv2.INTER_AREA)
        scaled = [
            {**o, "bbox": [v * scale for v in o["bbox"]]}
            for o in objects_world
        ]
        ann = annotate_frame(small, scaled)
    else:
        ann = annotate_frame(frame, objects_world)

    _, buf = cv2.imencode(".jpg", ann, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    frame_b64 = base64.b64encode(buf.tobytes()).decode("ascii")

    cam_label = cam_cfg.get("label") or f"Камера {cam_id + 1}"
    update_camera_state(cam_id, {
        "id": cam_id,
        "label": cam_label,
        "source": source,
        "frame": frame_b64,
        "points": _strip_internal(objects_world),
        "stats": {"active_tracks": len(objects_world)},
    })


def batch_loop(cam_cfgs, stop):
    sw = float(SCENE.get("width", 20))
    sh = float(SCENE.get("height", 20))
    heatmap = get_global_heatmap()
    world_tracker = get_world_tracker()

    today_count = storage.load_today_visitor_count()
    if today_count > 0:
        world_tracker._total_unique = today_count
        print(f"[batch] restored today visitor count: {today_count}", flush=True)

    _current_day = datetime.date.today()

    world_tracker.diag_enabled = WORLD_LOG_VERBOSE

    n = len(cam_cfgs)
    homographies = storage.load_camera_homographies()
    print(
        f"[batch] init {n} cameras, scene={sw:g}x{sh:g} m, "
        f"homographies for cams={sorted(homographies.keys()) if homographies else '[]'}",
        flush=True,
    )
    caps = []
    trackers = []
    mappers = []
    fps_list: list[float] = []

    for idx, (cam_id, cam_cfg, source) in enumerate(cam_cfgs):
        cap, fw, fh, fps = _open_capture(source, cam_id)
        caps.append(cap)
        if fps and fps > 1.0:
            fps_list.append(fps)
        trackers.append(make_tracker())
        try:
            patched_cfg = dict(cam_cfg) if cam_cfg else {}
            if fw > 0 and fh > 0:
                patched_cfg["img_width"] = fw
                patched_cfg["img_height"] = fh
            mappers.append(build_mapper(patched_cfg, cam_id, homographies))
            kind = mapper_kind(patched_cfg, cam_id, homographies)

            def _g(k, d=None):
                v = patched_cfg.get(k)
                return d if v is None else v
            print(
                f"[camera {cam_id}] mapper={kind} src={source!r} resolution={fw}x{fh} "
                f"fps={fps:.1f} "
                f"pos=({_g('x', 0):.1f},{_g('y', 0):.1f},{_g('height', 0):.1f}) "
                f"yaw={_g('yaw', 0):.0f} pitch={_g('pitch', 0):.0f} fov={_g('fov', 0):.0f}",
                flush=True,
            )
        except Exception as e:
            print(f"[camera {cam_id}] mapper error: {e}", flush=True)
            mappers.append(None)

    effective_fps = min(fps_list) if fps_list else float(TARGET_FPS_FALLBACK)
    effective_fps = min(effective_fps, float(TARGET_FPS_CAP))
    frame_interval = 1.0 / max(1.0, effective_fps)
    print(f"[batch] target processing rate = {effective_fps:.1f} fps "
          f"(per-camera fps={[round(f, 1) for f in fps_list]})", flush=True)

    _iter = 0
    _stat_t0 = time.time()
    _stat_obs_total = 0
    _stat_world_total = 0
    _stat_frames = 0
    _max_active_in_interval = 0
    _last_snapshot_minute = -1

    log_file = None
    if WORLD_LOG_PATH:
        try:
            log_file = open(WORLD_LOG_PATH, "w", encoding="utf-8")
            cam_headers = []
            for i in range(n):
                cam_id_i, cam_cfg_i, src_i = cam_cfgs[i]
                cfg = dict(cam_cfg_i or {})
                fw_i = fh_i = 0
                if caps[i] is not None:
                    fw_i = int(caps[i].get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                    fh_i = int(caps[i].get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
                kind = mapper_kind(cfg, cam_id_i, homographies)
                hdr = {
                    "id": cam_id_i,
                    "src": src_i,
                    "label": cfg.get("label"),
                    "mapper": kind,
                    "frame_w": fw_i, "frame_h": fh_i,
                    "pinhole": {
                        "x": cfg.get("x"), "y": cfg.get("y"),
                        "height": cfg.get("height"),
                        "yaw": cfg.get("yaw"), "pitch": cfg.get("pitch"),
                        "fov": cfg.get("fov"),
                        "img_w_cfg": cfg.get("img_width"),
                        "img_h_cfg": cfg.get("img_height"),
                    },
                    "homography": homographies.get(cam_id_i),
                }
                cam_headers.append(hdr)
            log_file.write(json.dumps({
                "type": "session_start",
                "ts": time.time(),
                "verbose": WORLD_LOG_VERBOSE,
                "scene_w": sw, "scene_h": sh,
                "effective_fps": effective_fps,
                "frame_interval": frame_interval,
                "warmup_iters": WARMUP_ITERS,
                "tracker_params": {
                    "ASSOCIATION_RADIUS": 1.5,
                    "TTL_SECONDS": 3.0,
                    "RECENT_CAMS_WINDOW": 25,
                    "PROMOTE_CAMS_WINDOW": 5,
                    "GRACE_SECONDS": 0.5,
                    "GHOST_MIN_FRAMES": 5,
                    "MATURE_FRAMES": 50,
                    "DISPLAY_CAM_WINDOW": 10,
                    "POSITION_SMOOTHING_ALPHA": 0.4,
                    "GRAVEYARD_TTL": 10.0,
                    "CENTROID_OUTLIER_M": 0.5,
                    "TRACK_MERGE_DISTANCE": 0.6,
                    "TRACK_MERGE_JACCARD": 0.5,
                    "PREEMPT_DISTANCE": 0.4,
                    "PREEMPT_REPORT_DISTANCE": 1.0,
                },
                "cameras": cam_headers,
            }, ensure_ascii=False) + "\n")
            log_file.flush()
            print(f"[batch] tracker log -> {os.path.abspath(WORLD_LOG_PATH)}", flush=True)
        except OSError as e:
            print(f"[batch] couldn't open log {WORLD_LOG_PATH}: {e}", flush=True)
            log_file = None
    try:
        while not stop.is_set():
            t0 = time.time()
            frames = [None] * n
            valid_indices = []

            for idx in range(n):
                cap = caps[idx]
                if cap is None:
                    continue
                ret, frame = cap.read()
                if not ret:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()
                    if not ret:
                        continue
                    trackers[idx] = make_tracker()
                frames[idx] = frame
                valid_indices.append(idx)

            if not valid_indices:
                continue

            valid_frames = [frames[i] for i in valid_indices]
            all_dets = batch_detect(valid_frames)

            per_cam_results = []
            all_observations = []
            all_dropped = []
            cam_diags = [] if WORLD_LOG_VERBOSE else None
            for vi, cam_idx in enumerate(valid_indices):
                cam_id, cam_cfg, source = cam_cfgs[cam_idx]
                objs, dropped, cam_diag = _process_camera(
                    cam_id, frames[cam_idx], all_dets[vi],
                    trackers[cam_idx], mappers[cam_idx], sw, sh,
                    verbose=WORLD_LOG_VERBOSE,
                )
                per_cam_results.append((cam_idx, objs))
                all_observations.extend(objs)
                all_dropped.extend(dropped)
                if WORLD_LOG_VERBOSE:
                    cam_diags.append(cam_diag)

            ts = time.time()

            today = datetime.date.today()
            if today != _current_day:
                _current_day = today
                world_tracker._total_unique = 0
                print(f"[batch] новый день {today}, счётчик сброшен", flush=True)

            world_points = world_tracker.update(all_observations, ts)
            _max_active_in_interval = max(_max_active_in_interval, len(world_points))
            storage.save_positions(
                [{"id": p["id"], "x": p["x"], "y": p["y"]}
                 for p in world_points if not p.get("is_ghost")],
                ts,
            )

            if log_file is not None:
                try:
                    record = {
                        "ts": round(ts, 3),
                        "frame": _iter,
                        "world_points": [
                            {"id": p["id"],
                             "x": round(p["x"], 3), "y": round(p["y"], 3),
                             "n": p["source_count"],
                             "ghost": p.get("is_ghost", False)}
                            for p in world_points
                        ],
                        "events": world_tracker.last_events,
                        "stats": {
                            "unique_total": world_tracker.total_unique,
                            "tracks_alive": len(world_tracker._tracks),
                            "graveyard": len(world_tracker._graveyard),
                            "raw_obs": len(all_observations) + len(all_dropped),
                            "dropped_count": len(all_dropped),
                            "world_points_out": len(world_points),
                        },
                    }
                    if WORLD_LOG_VERBOSE:
                        age_lookup = {tid: t.get("frames_seen", 0)
                                      for tid, t in world_tracker._tracks.items()}
                        record["cameras"] = cam_diags
                        record["obs"] = [
                            {"cam": o["camera_id"], "lid": o["track_id"],
                             "x": round(o["x"], 3), "y": round(o["y"], 3),
                             "conf": round(o.get("confidence", 0.0), 2)}
                            for o in all_observations
                        ]
                        record["dropped"] = all_dropped
                        record["tracker"] = world_tracker.last_diag
                        record["world_points"] = [
                            {"id": p["id"],
                             "x": round(p["x"], 3), "y": round(p["y"], 3),
                             "cams": p["camera_ids"],
                             "n": p["source_count"],
                             "ghost": p.get("is_ghost", False),
                             "age": age_lookup.get(p["id"], 0)}
                            for p in world_points
                        ]
                    log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"[batch] log write err: {e}", flush=True)

            _stat_obs_total += len(all_observations)
            _stat_world_total += len(world_points)
            _stat_frames += 1
            if ts - _stat_t0 >= 2.0:
                avg_obs = _stat_obs_total / max(1, _stat_frames)
                avg_world = _stat_world_total / max(1, _stat_frames)
                print(
                    f"[batch] {_stat_frames}fr/{(ts - _stat_t0):.1f}s — "
                    f"obs/frame={avg_obs:.1f} world/frame={avg_world:.1f} "
                    f"unique_total={world_tracker.total_unique}",
                    flush=True,
                )
                _stat_t0 = ts
                _stat_obs_total = 0
                _stat_world_total = 0
                _stat_frames = 0

            heatmap.update([p for p in world_points if not p.get("is_ghost")])

            encode_futures = []
            for cam_idx, objects_world in per_cam_results:
                cam_id, cam_cfg, source = cam_cfgs[cam_idx]
                encode_futures.append(_encode_pool.submit(
                    _encode_and_publish,
                    cam_id, frames[cam_idx], objects_world, cam_cfg, source,
                ))
            for f in encode_futures:
                try:
                    f.result()
                except Exception as exc:
                    print(f"[encode] error: {exc}")

            _iter += 1
            if _iter <= WARMUP_ITERS:
                continue

            broadcast_current_state(
                heatmap_grid=heatmap.get_normalized(),
                world_points=world_points,
                visitor_count=world_tracker.total_unique,
            )

            current_minute = int(ts // 60)
            if current_minute != _last_snapshot_minute:
                _last_snapshot_minute = current_minute
                storage.save_snapshot(
                    world_tracker.total_unique,
                    _max_active_in_interval,
                    [],
                )
                _max_active_in_interval = 0

            elapsed = time.time() - t0
            remaining = frame_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
    except Exception as e:
        import traceback
        print(f"[batch] FATAL: {e}")
        traceback.print_exc()

    for cap in caps:
        if cap is not None:
            cap.release()
    if log_file is not None:
        try:
            log_file.flush()
            log_file.close()
        except Exception:
            pass
    print("[batch] Stopped")
