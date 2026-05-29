"""
Фабрика mapper с приоритетом homography > pinhole.

Если для cam_id есть сохранённая гомография (в БД), используем её
как override параметрической модели — она точнее и не зависит от того,
насколько корректно пользователь расставил камеру в редакторе.
Иначе строим SpatialMapper из параметров камеры (yaw/pitch/fov/height).
"""
from __future__ import annotations

from backend.mapping.mapper import SpatialMapper, create_mapper_from_config
from backend.mapping.homography_mapper import HomographyMapper


def build_mapper(cam_cfg: dict, cam_id: int, homographies: dict[int, list]):
    """Возвращает объект с методом pixel_to_world(u, v) → (x, y) | None."""
    H = homographies.get(cam_id)
    if H is not None:
        try:
            return HomographyMapper(H)
        except Exception as e:
            print(f"[mapper] cam {cam_id}: homography load failed ({e}), fallback to pinhole")
    return create_mapper_from_config(cam_cfg) if cam_cfg else None


def mapper_kind(cam_cfg: dict, cam_id: int, homographies: dict[int, list]) -> str:
    return "homography" if cam_id in homographies else "pinhole"
