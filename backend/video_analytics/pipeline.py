"""
YOLO детекция людей и ByteTrack для локальных треков на одной камере.
Параметры детектора и трекера подобраны под наше тестовое видео,
переопределяются через настройки модели.
"""
import numpy as np
import supervision as sv
from ultralytics import YOLO

YOLO_IMGSZ = 640
YOLO_CONF = 0.483
YOLO_NMS_IOU = 0.45

TRACK_ACTIVATION_THRESHOLD = 0.358
LOST_TRACK_BUFFER = 82
MINIMUM_MATCHING_THRESHOLD = 0.919
MINIMUM_CONSECUTIVE_FRAMES = 2
FRAME_RATE = 25

_shared_model = None
_detect_device = None
_detect_half = False


def _auto_device():
    global _detect_device, _detect_half
    if _detect_device is not None:
        return
    try:
        import torch
        if torch.cuda.is_available():
            _detect_device = 0
            _detect_half = True
            torch.backends.cudnn.benchmark = True
        else:
            _detect_device = "cpu"
            _detect_half = False
    except ImportError:
        _detect_device = "cpu"
        _detect_half = False
    print(f"[detector] device={_detect_device}, half={_detect_half}, imgsz={YOLO_IMGSZ}, nms_iou={YOLO_NMS_IOU}")


def preload_detector():
    _get_shared_model()
    print("[detector] Pre-loaded")


def _get_shared_model():
    global _shared_model
    if _shared_model is None:
        _auto_device()
        _shared_model = YOLO("yolo26n_people_tuned.pt")
        warmup_frames = [np.random.randint(0, 255, (288, 360, 3), dtype=np.uint8) for _ in range(4)]
        for _ in range(3):
            _shared_model(warmup_frames, verbose=False, imgsz=YOLO_IMGSZ,
                          device=_detect_device, half=_detect_half, classes=[0],
                          iou=YOLO_NMS_IOU)
        if _detect_device != "cpu":
            try:
                import torch
                torch.cuda.synchronize()
            except Exception:
                pass
        print("[detector] YOLO warmed up")
    return _shared_model


def batch_detect(frames, conf=None):
    model = _get_shared_model()
    results = model(frames, verbose=False, conf=conf or YOLO_CONF, imgsz=YOLO_IMGSZ,
                    device=_detect_device, half=_detect_half, classes=[0],
                    iou=YOLO_NMS_IOU, agnostic_nms=True)

    all_objects = []
    for res in results:
        objects = []
        if res.boxes is not None and len(res.boxes) > 0:
            xyxy = res.boxes.xyxy.cpu().numpy()
            conf_arr = res.boxes.conf.cpu().numpy()
            objects.append((xyxy, conf_arr))
        all_objects.append(objects)
    return all_objects


def make_tracker():
    return sv.ByteTrack(
        track_activation_threshold=TRACK_ACTIVATION_THRESHOLD,
        lost_track_buffer=LOST_TRACK_BUFFER,
        minimum_matching_threshold=MINIMUM_MATCHING_THRESHOLD,
        minimum_consecutive_frames=MINIMUM_CONSECUTIVE_FRAMES,
        frame_rate=FRAME_RATE,
    )


def track_objects(tracker, det_list, timestamp, cam_id_hint=0):
    objects = []
    if not det_list:
        return objects
    xyxy, conf_arr = det_list[0]
    sv_det = sv.Detections(
        xyxy=xyxy,
        confidence=conf_arr,
        class_id=np.zeros(len(xyxy), dtype=int),
    )
    tracks = tracker.update_with_detections(sv_det)

    if tracks.tracker_id is None or len(tracks.tracker_id) == 0:
        return objects

    for i in range(len(tracks.xyxy)):
        x1, y1, x2, y2 = tracks.xyxy[i]
        tid = int(tracks.tracker_id[i])
        cx = int((x1 + x2) / 2)
        cy = int(y2)
        conf = float(tracks.confidence[i]) if tracks.confidence is not None else 0.0
        objects.append({
            "id": tid,
            "x": cx,
            "y": cy,
            "bbox": [float(x1), float(y1), float(x2), float(y2)],
            "timestamp": timestamp,
            "confidence": conf,
        })
    return objects
