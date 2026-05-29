"""
Модуль хранения данных в SQLite.

Таблицы:
  scenes      — конфигурация сцены (размеры зала)
  cameras     — параметры камер
  snapshots   — агрегированные аналитические снимки во времени
"""

import json
import time
import sqlite3
import os
import threading

DB_PATH = os.environ.get("DATABASE_URL", "analytics.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_writer_conn: sqlite3.Connection | None = None
_writer_lock = threading.Lock()


def _get_writer_conn() -> sqlite3.Connection:
    global _writer_conn
    if _writer_conn is None:
        _writer_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _writer_conn.execute("PRAGMA journal_mode=WAL")
        _writer_conn.execute("PRAGMA synchronous=NORMAL")
        _writer_conn.execute("PRAGMA cache_size=-64000")
    return _writer_conn


def init_db():
    conn = get_connection()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.commit()
    conn.close()

    conn = get_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS scenes (
            id INTEGER PRIMARY KEY,
            width REAL NOT NULL DEFAULT 20,
            height REAL NOT NULL DEFAULT 20,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scene_id INTEGER NOT NULL DEFAULT 1,
            x REAL, y REAL, height REAL,
            yaw REAL, pitch REAL, fov REAL,
            img_width INTEGER, img_height INTEGER,
            label TEXT,
            address TEXT DEFAULT '',
            FOREIGN KEY(scene_id) REFERENCES scenes(id)
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            visitor_count INTEGER NOT NULL DEFAULT 0,
            active_tracks INTEGER NOT NULL DEFAULT 0,
            heatmap TEXT
        );

        CREATE TABLE IF NOT EXISTS camera_homographies (
            cam_id INTEGER PRIMARY KEY,
            matrix TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            ts    REAL    NOT NULL,
            tid   INTEGER NOT NULL,
            x     REAL    NOT NULL,
            y     REAL    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions(ts);
    """)

    cur.execute(
        "INSERT OR IGNORE INTO scenes(id, width, height, updated_at) VALUES(1, 20, 20, ?)",
        (time.time(),)
    )

    existing = [row[1] for row in cur.execute("PRAGMA table_info(cameras)").fetchall()]
    if "address" not in existing:
        cur.execute("ALTER TABLE cameras ADD COLUMN address TEXT DEFAULT ''")

    conn.commit()
    conn.close()


# ---- Scene ----

def save_scene(width: float, height: float):
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO scenes(id, width, height, updated_at) VALUES(1, ?, ?, ?)",
        (width, height, time.time())
    )
    conn.commit()
    conn.close()


def load_scene():
    conn = get_connection()
    row = conn.execute("SELECT * FROM scenes WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {"id": 1, "width": 20, "height": 20}


# ---- Cameras ----

def save_cameras(cameras: list):
    conn = get_connection()
    conn.execute("DELETE FROM cameras WHERE scene_id=1")
    for cam in cameras:
        conn.execute("""
            INSERT INTO cameras(scene_id, x, y, height, yaw, pitch, fov, img_width, img_height, label, address)
            VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cam.get("x", 0), cam.get("y", 0), cam.get("height", 3.0),
            cam.get("yaw", 0), cam.get("pitch", -150), cam.get("fov", 90),
            cam.get("img_width", 1280), cam.get("img_height", 720),
            cam.get("label", ""), cam.get("address", "")
        ))
    conn.commit()
    conn.close()


def load_cameras():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM cameras WHERE scene_id=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- Гомография камеры ----

def save_camera_homography(cam_id: int, matrix_3x3: list):
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO camera_homographies(cam_id, matrix, updated_at) VALUES(?, ?, ?)",
        (cam_id, json.dumps(matrix_3x3), time.time())
    )
    conn.commit()
    conn.close()


def load_camera_homographies() -> dict[int, list]:
    """
    Возвращает {cam_id: [[3x3 matrix]]}. Пустой dict если нет калибровок.
    """
    conn = get_connection()
    rows = conn.execute("SELECT cam_id, matrix FROM camera_homographies").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[int(r["cam_id"])] = json.loads(r["matrix"])
        except (ValueError, json.JSONDecodeError):
            continue
    return result


def delete_camera_homography(cam_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM camera_homographies WHERE cam_id=?", (cam_id,))
    conn.commit()
    conn.close()


# ---- Snapshots ----

def save_snapshot(visitor_count: int, active_tracks: int, heatmap: list):
    conn = get_connection()
    conn.execute(
        "INSERT INTO snapshots(ts, visitor_count, active_tracks, heatmap) VALUES(?, ?, ?, ?)",
        (time.time(), visitor_count, active_tracks, json.dumps(heatmap))
    )
    conn.commit()
    conn.close()


def load_snapshots(limit=30):
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            DATE(ts, 'unixepoch', 'localtime') as day,
            MAX(visitor_count)                 as visitor_count,
            MAX(active_tracks)                 as active_tracks
        FROM snapshots
        GROUP BY day
        ORDER BY day DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [{"ts": r["day"], "visitor_count": r["visitor_count"],
             "active_tracks": r["active_tracks"]} for r in rows]


def load_today_visitor_count() -> int:
    """
    Возвращает максимальный visitor_count за сегодня.
    Используется при старте сервера чтобы восстановить счётчик WorldTracker'а
    и не начинать с нуля если сервер перезапускался в течение дня.
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT MAX(visitor_count) as cnt FROM snapshots
        WHERE DATE(ts, 'unixepoch', 'localtime') = DATE('now', 'localtime')
    """).fetchone()
    conn.close()
    return int(row["cnt"]) if row and row["cnt"] is not None else 0


def count_unique_visitors(from_ts: float, to_ts: float) -> int:
    """
    Число уникальных посетителей за период — разница между максимальным
    и минимальным значением visitor_count в снапшотах за этот период.
    Отражает сколько новых людей появилось за окно, а не накопленный итог.
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT MAX(visitor_count) as mx, MIN(visitor_count) as mn
        FROM snapshots
        WHERE ts >= ? AND ts <= ?
    """, (from_ts, to_ts)).fetchone()
    conn.close()
    if row and row["mx"] is not None:
        return max(0, int(row["mx"]) - int(row["mn"]))
    return 0


def count_peak_active(from_ts: float, to_ts: float) -> int:
    """Пиковое число людей одновременно в зале за период."""
    conn = get_connection()
    row = conn.execute("""
        SELECT MAX(active_tracks) as pk FROM snapshots
        WHERE ts >= ? AND ts <= ?
    """, (from_ts, to_ts)).fetchone()
    conn.close()
    return int(row["pk"]) if row and row["pk"] is not None else 0


# ---- Positions ----

POSITIONS_TTL_DAYS = 7
POSITIONS_CLEANUP_EVERY = 500
_positions_call_count = 0


def save_positions(points: list[dict], ts: float) -> None:
    global _positions_call_count
    if not points:
        return
    with _writer_lock:
        conn = _get_writer_conn()
        conn.executemany(
            "INSERT INTO positions(ts, tid, x, y) VALUES(?, ?, ?, ?)",
            [(ts, p["id"], p["x"], p["y"]) for p in points],
        )
        _positions_call_count += 1
        if _positions_call_count % POSITIONS_CLEANUP_EVERY == 0:
            cutoff = ts - POSITIONS_TTL_DAYS * 86400
            conn.execute("DELETE FROM positions WHERE ts < ?", (cutoff,))
        conn.commit()


def load_snapshots_raw(from_ts: float, to_ts: float, limit: int = 50000) -> list[dict]:
    """Минутные снапшоты за период без агрегации, нужны для CSV-экспорта."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT ts, visitor_count, active_tracks FROM snapshots
        WHERE ts >= ? AND ts <= ?
        ORDER BY ts ASC
        LIMIT ?
    """, (from_ts, to_ts, limit)).fetchall()
    conn.close()
    return [{"ts": r["ts"], "visitor_count": r["visitor_count"],
             "active_tracks": r["active_tracks"]} for r in rows]


def load_positions(from_ts: float, to_ts: float):
    import numpy as np
    conn = get_connection()
    cur = conn.execute(
        "SELECT x, y FROM positions WHERE ts >= ? AND ts <= ? LIMIT 500000",
        (from_ts, to_ts),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return None
    return np.array(rows, dtype=np.float32)