"""
Pinhole-модель камеры и обратная проекция пикселя на пол (z = 0)
в мировых метрических координатах.

Координатная конвенция (в мире):
  x — вправо
  y — вглубь помещения
  z — вверх (= height камеры в editor.html)

Камера в локальной системе смотрит вдоль +Z (после применения R) с учётом
yaw (поворот вокруг мировой Z) и pitch (наклон вокруг X). Editor.html
делает преобразование `pitch_backend = -(90 + pitch_ui)`: pitch_ui=60 (камера
тилтнута вниз на 60°) → pitch_backend=-150, что и используется здесь.
"""
import math

import numpy as np


class Camera:
    def __init__(self, position, yaw=0.0, pitch=-150.0,
                 fov_h=90.0, width=1280, height=720):
        self.position = np.array(position, dtype=float)
        self.yaw = yaw
        self.pitch = pitch
        self.fov_h = fov_h
        self.set_image_size(width, height)
        self.rotation = self._build_rotation(yaw, pitch)

    def set_image_size(self, width: int, height: int):
        """
        Пересчитывает intrinsics под актуальное разрешение кадра.
        Вызывается из batch.py после открытия cv2.VideoCapture, потому что
        editor отправляет лишь декларативное `img_width/img_height` — оно
        может не совпадать с реальным разрешением источника.
        """
        self.width = int(max(1, width))
        self.height = int(max(1, height))
        # Горизонтальный угол → fx. fy=fx (квадратные пиксели).
        self.fx = (self.width / 2) / math.tan(math.radians(self.fov_h / 2))
        self.fy = self.fx
        self.cx = self.width / 2
        self.cy = self.height / 2

    def _build_rotation(self, yaw_deg, pitch_deg):
        yaw = math.radians(yaw_deg)
        pitch = math.radians(pitch_deg)
        Rz = np.array([
            [math.cos(yaw), -math.sin(yaw), 0],
            [math.sin(yaw),  math.cos(yaw), 0],
            [0,              0,             1],
        ])
        Rx = np.array([
            [1, 0,                0               ],
            [0, math.cos(pitch), -math.sin(pitch) ],
            [0, math.sin(pitch),  math.cos(pitch) ],
        ])
        return Rz @ Rx


class SpatialMapper:
    """
    Нижний центр bbox на камере преобразует в мировую точку на полу (z = 0).
    """

    def __init__(self, camera: Camera):
        self.camera = camera

    def pixel_to_world(self, u, v):
        cam = self.camera
        x = (u - cam.cx) / cam.fx
        y = (v - cam.cy) / cam.fy
        ray_camera = np.array([x, y, 1.0])
        ray_world = cam.rotation @ ray_camera
        C = cam.position
        if abs(ray_world[2]) < 1e-6:
            return None
        t = -C[2] / ray_world[2]
        if t < 0:
            return None
        P = C + t * ray_world
        return float(P[0]), float(P[1])


def create_mapper_from_config(cam_config: dict) -> SpatialMapper:
    cam = Camera(
        position=(
            cam_config.get("x", 0),
            cam_config.get("y", 0),
            cam_config.get("height", 3.0),
        ),
        yaw=cam_config.get("yaw", 0.0),
        pitch=cam_config.get("pitch", -150.0),
        fov_h=cam_config.get("fov", 90.0),
        width=cam_config.get("img_width", 1280),
        height=cam_config.get("img_height", 720),
    )
    return SpatialMapper(cam)
