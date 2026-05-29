"""
AppearanceModel — лёгкий визуальный отпечаток bbox-а через HSV-гистограмму.

Без визуального сигнала WorldTracker не может различить «один человек,
видимый двумя камерами» от «двое разных людей рядом». OSNet/ReID для
этого избыточны; задача требует только дополнительного фактора в моменте
ассоциации, а не глобальной повторной идентификации поверх времени.

Реализация:
  Считаем 2D гистограмму (Hue * Saturation) отдельно для верхней и нижней
  половин bbox-а. Это даёт устойчивость к ракурсам (верх — одежда корпуса,
  низ — штаны или обувь) и не требует GPU. Яркость (Value) игнорируем,
  чтобы быть устойчивыми к локальным изменениям освещения.

Гистограмма нормализована (L1=1), сравнивается косинусом — итог в [0, 1].
"""
from __future__ import annotations

import cv2
import numpy as np

_HUE_BINS = 16   # 0..180 в OpenCV HSV
_SAT_BINS = 8    # 0..256
_HALF_DIM = _HUE_BINS * _SAT_BINS  # 128
DESCRIPTOR_DIM = 2 * _HALF_DIM      # 256

_MIN_BBOX_W = 6
_MIN_BBOX_H = 12


def _hist_2d(region_hsv) -> np.ndarray:
    h = cv2.calcHist([region_hsv], [0, 1], None,
                     [_HUE_BINS, _SAT_BINS],
                     [0, 180, 0, 256]).flatten()
    s = float(h.sum())
    return (h / s).astype(np.float32) if s > 0 else h.astype(np.float32)


def extract(frame, bbox) -> np.ndarray | None:
    """Извлекает дескриптор из bbox. Возвращает None если кадр слишком мелкий."""
    if frame is None:
        return None
    x1, y1, x2, y2 = [int(v) for v in bbox]
    H, W = frame.shape[:2]
    x1 = max(0, min(W, x1)); x2 = max(0, min(W, x2))
    y1 = max(0, min(H, y1)); y2 = max(0, min(H, y2))
    if x2 - x1 < _MIN_BBOX_W or y2 - y1 < _MIN_BBOX_H:
        return None
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mid = hsv.shape[0] // 2
    upper = hsv[:mid] if mid > 0 else hsv
    lower = hsv[mid:] if mid > 0 else hsv
    return np.concatenate([_hist_2d(upper), _hist_2d(lower)])


def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Косинусная похожесть в [0, 1]"""
    if a is None or b is None:
        return 0.5
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.5
    sim = float(np.dot(a, b) / (na * nb))
    return max(0.0, min(1.0, sim))


def ema_update(old: np.ndarray | None, new: np.ndarray | None, alpha: float = 0.3) -> np.ndarray | None:
    """EMA-сглаживание дескриптора трека."""
    if old is None:
        return new
    if new is None:
        return old
    return ((1 - alpha) * old + alpha * new).astype(np.float32)
