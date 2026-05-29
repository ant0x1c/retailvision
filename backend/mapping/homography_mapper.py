"""
HomographyMapper — обратная проекция пикселя на пол через готовую матрицу 3×3.

В отличие от pinhole-модели, не делает предположений о внутренних/внешних
параметрах камеры — просто применяет аффинно-проективное преобразование,
которое можно посчитать из 4+ соответствий pixel↔world (cv2.findHomography).

Матрица ожидается уже в метрических единицах: H @ [u, v, 1]^T в гомогенных
координатах → [X, Y, W] → (X/W, Y/W) и есть мировые метры. Если исходная
калибровка была в пикселях top-view, перед сохранением её надо отнормировать
(см. scripts/load_epfl_calibration.py).
"""
from __future__ import annotations

import numpy as np


class HomographyMapper:
    def __init__(self, matrix: list | np.ndarray):
        m = np.asarray(matrix, dtype=float)
        if m.shape != (3, 3):
            raise ValueError(f"Homography must be 3x3, got {m.shape}")
        self.H = m

    def pixel_to_world(self, u: float, v: float):
        p = np.array([u, v, 1.0])
        w = self.H @ p
        if abs(w[2]) < 1e-9:
            return None
        return float(w[0] / w[2]), float(w[1] / w[2])
