from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from typing import List, Optional
import time

from backend.db import storage

router = APIRouter()

connections: list[WebSocket] = []

CAMERAS: list[dict] = []
SCENE: dict = {"width": 20, "height": 20}
LATEST_POINTS: list[dict] = []
LATEST_WORLD_POINTS: list[dict] = []
LATEST_HEATMAP: list[list] = []
LATEST_STATS: dict = {"visitor_count": 0, "active_tracks": 0}


class SceneConfig(BaseModel):
    width: float = 20
    height: float = 20


class CameraConfig(BaseModel):
    x: float = 0
    y: float = 0
    height: float = 3.0
    yaw: float = 0.0
    pitch: float = -150.0
    fov: float = 90.0
    img_width: int = 1280
    img_height: int = 720
    label: Optional[str] = ""
    address: Optional[str] = ""


@router.get("/scene")
def get_scene():
    return SCENE


@router.post("/scene")
def set_scene(config: SceneConfig):
    global SCENE
    SCENE = config.dict()
    storage.save_scene(config.width, config.height)
    return {"status": "ok", "scene": SCENE}


@router.get("/cameras")
def get_cameras():
    return {"cameras": CAMERAS}


@router.get("/cameras/list")
def list_cameras_for_video():
    return {
        "cameras": [
            {
                "id": i,
                "label": c.get("label") or f"Камера {i + 1}",
                "address": c.get("address", ""),
            }
            for i, c in enumerate(CAMERAS)
        ]
    }


@router.post("/cameras")
def set_cameras(cameras: List[CameraConfig]):
    CAMERAS.clear()
    CAMERAS.extend([c.dict() for c in cameras])
    storage.save_cameras(CAMERAS)
    print(f"[routes] Сохранено {len(CAMERAS)} камер")
    try:
        from backend.main import restart_all_video_loops
        restart_all_video_loops()
    except Exception as e:
        print(f"[routes] restart_all_video_loops warn: {e}")
    return {"status": "ok", "count": len(CAMERAS)}


@router.get("/points")
def get_points():
    return {"points": LATEST_POINTS}


@router.get("/world_points")
def get_world_points():
    return {"world_points": LATEST_WORLD_POINTS}


@router.get("/heatmap")
def get_heatmap():
    return {"heatmap": LATEST_HEATMAP}


def _build_heatmap_grid(points, scene, cols=40, rows=40):
    import numpy as np
    from scipy.ndimage import gaussian_filter

    sw = float(scene.get("width", 20))
    sh = float(scene.get("height", 20))

    if points is None or len(points) == 0:
        return [[0.0] * cols for _ in range(rows)]

    xs = np.clip(points[:, 0], 0, sw)
    ys = np.clip(points[:, 1], 0, sh)

    grid, _, _ = np.histogram2d(ys, xs,
                                 bins=[rows, cols],
                                 range=[[0, sh], [0, sw]])
    grid = gaussian_filter(grid.astype(np.float32), sigma=1.5)
    max_val = grid.max()
    if max_val > 0:
        grid /= max_val

    return grid.tolist()


@router.get("/heatmap/history")
async def get_heatmap_history(
    from_ts: float = Query(alias="from"),
    to_ts: float = Query(alias="to"),
):
    import asyncio
    MAX_RANGE = 7 * 86400
    if to_ts <= from_ts:
        return {"heatmap": [], "error": "to_ts must be greater than from_ts"}
    if to_ts - from_ts > MAX_RANGE:
        from_ts = to_ts - MAX_RANGE

    loop = asyncio.get_event_loop()
    points = await loop.run_in_executor(
        None, storage.load_positions, from_ts, to_ts
    )
    if points is None:
        return {"heatmap": [], "point_count": 0}

    grid = await loop.run_in_executor(
        None, _build_heatmap_grid, points, SCENE
    )
    return {
        "heatmap": grid,
        "point_count": len(points),
        "visitor_count": storage.count_unique_visitors(from_ts, to_ts),
        "peak_active": storage.count_peak_active(from_ts, to_ts),
        "from_ts": from_ts,
        "to_ts": to_ts,
    }


@router.get("/stats")
def get_stats():
    return LATEST_STATS


@router.get("/history")
def get_history(limit: int = 50):
    return {"history": storage.load_snapshots(limit)}


@router.get("/history/export")
def get_history_export(
    from_ts: float = Query(alias="from", default=None),
    to_ts: float = Query(alias="to", default=None),
):
    """Минутные снапшоты за период для CSV-экспорта."""
    now = time.time()
    if to_ts is None:
        to_ts = now
    if from_ts is None:
        from_ts = now - 7 * 86400  # последние 7 дней по умолчанию
    rows = storage.load_snapshots_raw(from_ts, to_ts)
    return {"snapshots": rows, "count": len(rows)}


@router.post("/reset")
def reset_stats():
    LATEST_STATS["visitor_count"] = 0
    LATEST_STATS["active_tracks"] = 0
    LATEST_POINTS.clear()
    LATEST_WORLD_POINTS.clear()
    LATEST_HEATMAP.clear()
    try:
        from backend.main import reset_all_stats
        reset_all_stats()
    except Exception:
        pass
    return {"status": "reset"}


@router.post("/set_source")
def set_source(body: dict):
    from backend.main import restart_video_loop
    src = body.get("source", "video.mp4")
    restart_video_loop(src)
    return {"status": "ok", "source": src}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    print(f"[ws] подключён, всего: {len(connections)}")
    try:
        while True:
            await websocket.receive()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in connections:
            connections.remove(websocket)
        print(f"[ws] отключён, осталось: {len(connections)}")