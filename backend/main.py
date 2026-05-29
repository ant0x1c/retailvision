import asyncio
import os
import threading

import cv2
cv2.ocl.setUseOpenCL(False)
cv2.setNumThreads(2)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api import routes as api_routes
from backend.api.routes import CAMERAS, SCENE, router
from backend.core.batch import batch_loop, reset_global_state
from backend.core.state import (
    set_main_loop, broadcast_current_state,
    clear_camera_states, reset_camera_tracks,
)
from backend.db import storage
from backend.video_analytics.pipeline import preload_detector

app = FastAPI(title="Retail Analytics API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(router)


@app.get("/")
def root():
    try:
        if storage.load_cameras():
            return RedirectResponse(url="/video.html")
    except Exception:
        pass
    return RedirectResponse(url="/editor.html")


_frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if not os.path.isdir(_frontend_dir):
    _frontend_dir = "frontend"

try:
    app.mount("/", StaticFiles(directory=_frontend_dir), name="frontend")
except Exception as e:
    print(f"[warn] StaticFiles: {e}")

_batch_stop = threading.Event()
_batch_thread: threading.Thread | None = None


def stop_all_video_loops():
    _batch_stop.set()
    if _batch_thread is not None and _batch_thread.is_alive():
        _batch_thread.join(timeout=5.0)
    clear_camera_states()
    broadcast_current_state()


def restart_all_video_loops():
    stop_all_video_loops()
    _batch_stop.clear()

    cam_cfgs = []
    for idx, cam in enumerate(CAMERAS):
        src = (cam.get("address") or "").strip()
        if not src:
            continue
        cam_cfgs.append((idx, cam, src))

    if not cam_cfgs:
        fallback_cam = {
            "x": 10.0, "y": 10.0, "height": 3.0,
            "yaw": 0.0, "pitch": -150.0, "fov": 90.0,
            "img_width": 1280, "img_height": 720,
            "label": "Fallback",
        }
        cam_cfgs.append((0, fallback_cam, "video.mp4"))

    global _batch_thread
    _batch_thread = threading.Thread(target=batch_loop, args=(cam_cfgs, _batch_stop), daemon=True)
    _batch_thread.start()
    print(f"[batch] Starting: {len(cam_cfgs)} cameras")


def restart_video_loop(source: str = "video.mp4"):
    source = source or "video.mp4"
    if CAMERAS:
        CAMERAS[0]["address"] = source
        restart_all_video_loops()
    else:
        stop_all_video_loops()
        fallback_cam = {
            "x": 10.0, "y": 10.0, "height": 3.0,
            "yaw": 0.0, "pitch": -150.0, "fov": 90.0,
            "img_width": 1280, "img_height": 720,
            "label": "Fallback",
        }
        cam_cfgs = [(0, fallback_cam, source)]
        _batch_stop.clear()
        global _batch_thread
        _batch_thread = threading.Thread(target=batch_loop, args=(cam_cfgs, _batch_stop), daemon=True)
        _batch_thread.start()


def reset_all_stats():
    reset_global_state()
    reset_camera_tracks()
    api_routes.LATEST_POINTS = []
    api_routes.LATEST_HEATMAP = []
    api_routes.LATEST_STATS = {"visitor_count": 0, "active_tracks": 0}
    broadcast_current_state()


@app.on_event("startup")
async def start_video():
    set_main_loop(asyncio.get_running_loop())

    storage.init_db()
    scene = storage.load_scene()
    if scene:
        SCENE["width"] = float(scene.get("width", 20))
        SCENE["height"] = float(scene.get("height", 20))
        api_routes.SCENE["width"] = SCENE["width"]
        api_routes.SCENE["height"] = SCENE["height"]
    saved = storage.load_cameras()
    if saved:
        CAMERAS.clear()
        CAMERAS.extend(saved)
        print(f"[startup] Loaded {len(saved)} cameras")

    preload_detector()
    restart_all_video_loops()
    print("[startup] Ready")
