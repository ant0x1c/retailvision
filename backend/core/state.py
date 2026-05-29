"""
Общее состояние всех камер для трансляции по WebSocket.
"""
import asyncio
import threading
from typing import Any

from backend.api import routes as api_routes
from backend.api.routes import connections

_main_loop: asyncio.AbstractEventLoop | None = None
_camera_states: dict[int, dict[str, Any]] = {}
_state_lock = threading.Lock()


def set_main_loop(loop):
    global _main_loop
    _main_loop = loop


async def broadcast(data: dict):
    dead = []
    for ws in list(connections):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in connections:
            connections.remove(ws)


def broadcast_from_thread(data: dict):
    if _main_loop is None or not _main_loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(broadcast(data), _main_loop)


def get_camera_states():
    with _state_lock:
        return dict(_camera_states)


def update_camera_state(cam_id, state: dict):
    with _state_lock:
        _camera_states[cam_id] = state


def clear_camera_states():
    with _state_lock:
        _camera_states.clear()


def reset_camera_tracks():
    with _state_lock:
        for st in _camera_states.values():
            st["points"] = []
            st["stats"] = {"active_tracks": 0}


def build_aggregate_payload(heatmap_grid=None, world_points=None, visitor_count=0):
    """
    world_points — дедуплицированные точки людей в мире (по одной на
    человека независимо от того, сколько камер его видит). active_tracks
    равен len(world_points).
    """
    with _state_lock:
        cam_items = sorted(_camera_states.items(), key=lambda x: x[0])
        cameras_payload = []
        raw_points = []

        for cam_id, st in cam_items:
            points = st.get("points", [])
            stats = st.get("stats", {"active_tracks": 0})
            raw_points.extend(points)
            cameras_payload.append({
                "id": cam_id,
                "label": st.get("label", f"Камера {cam_id + 1}"),
                "address": st.get("source", ""),
                "frame": st.get("frame", ""),
                "points": list(points),
                "stats": dict(stats),
            })

    world_points = world_points or []
    agg_stats = {
        "visitor_count": visitor_count,
        "active_tracks": len(world_points),
    }

    api_routes.LATEST_POINTS = raw_points
    api_routes.LATEST_WORLD_POINTS = world_points
    api_routes.LATEST_HEATMAP = heatmap_grid or []
    api_routes.LATEST_STATS = agg_stats

    return {
        "cameras": cameras_payload,
        "points": raw_points,
        "world_points": world_points,
        "heatmap": heatmap_grid or [],
        "stats": agg_stats,
    }


def broadcast_current_state(heatmap_grid=None, world_points=None, visitor_count=0):
    broadcast_from_thread(build_aggregate_payload(heatmap_grid, world_points, visitor_count))
