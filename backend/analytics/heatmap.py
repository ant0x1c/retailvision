import numpy as np


class Heatmap:
    """
    Глобальная тепловая карта помещения в метрических координатах.
    Собирает мировые точки людей с лёгким затуханием —
    свежая активность ярче, старая постепенно гаснет.
    """

    def __init__(self, width=20.0, height=20.0, resolution=0.05, decay=0.999):
        self.resolution = resolution
        self.decay = decay
        self._configure(width, height)

    def _configure(self, width, height):
        self.width = float(width)
        self.height = float(height)
        self.grid_w = max(1, int(round(self.width / self.resolution)))
        self.grid_h = max(1, int(round(self.height / self.resolution)))
        self.map = np.zeros((self.grid_h, self.grid_w), dtype=float)

    def resize(self, width, height):
        if abs(width - self.width) < 1e-6 and abs(height - self.height) < 1e-6:
            return
        self._configure(width, height)

    def update(self, points):
        self.map *= self.decay
        for p in points:
            x = p.get("x")
            y = p.get("y")
            if x is None or y is None:
                continue
            gx = int(x / self.resolution)
            gy = int(y / self.resolution)
            if 0 <= gx < self.grid_w and 0 <= gy < self.grid_h:
                self.map[gy, gx] += 1.0

    def get_normalized(self):
        m = float(self.map.max())
        if m <= 0:
            return self.map.tolist()
        return (self.map / m).tolist()

    def reset(self):
        self.map[:] = 0
