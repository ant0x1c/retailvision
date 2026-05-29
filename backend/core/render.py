import cv2


SCENE_MARGIN_M = 0.2


def annotate_frame(frame, objects):
    """
    Рисует bbox каждого детектированного человека.
    """
    out = frame.copy()
    color = (200, 80, 0)
    for obj in objects:
        x1, y1, x2, y2 = [int(v) for v in obj.get("bbox", [0, 0, 0, 0])]
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 1)
    return out


def project_point(mapper, px, py, frame_w, frame_h, scene_w, scene_h):
    """
    Проецирует точку (px, py) с кадра в мировые метрические координаты.

    Возвращает (wx, wy) если проекция попадает в сцену (с учётом "отсутпа"),
    либо None — если гомография уехала далеко за границы (типичный случай
    ложной детекции на краю кадра, артефакт искажения fish-eye камер и т.п.).

    Если mapper нет — считаем нормализованные доли кадра, всегда в пределах сцены.
    """
    if mapper is not None:
        wxwy = mapper.pixel_to_world(px, py)

        if wxwy is None:
            return None

        wx, wy = wxwy

        if (-SCENE_MARGIN_M <= wx <= scene_w + SCENE_MARGIN_M
                and -SCENE_MARGIN_M <= wy <= scene_h + SCENE_MARGIN_M):
            return min(max(wx, 0.0), scene_w), min(max(wy, 0.0), scene_h)

        return None

    wx = (px / max(1.0, frame_w)) * scene_w
    wy = (py / max(1.0, frame_h)) * scene_h
    return min(max(wx, 0.0), scene_w), min(max(wy, 0.0), scene_h)
