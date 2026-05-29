"""
WorldTracker — минималистичный трекер на полу с дедупликацией наблюдений.

Опирается на:
  1. Межкамерную схожеть (ByteTrack уже различает людей внутри камеры).
  2. Подтверждение от нескольких камер (> 1 камеры видит человека -> он реален).
  3. "Кладбище" для коротких потерь ("возрождение" при возвращении в ту же зону).
  4. Объединение треков.
  5. EMA сглаживание + ожидание подтверждения (для устойчивого отображения на карте).
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict, deque


ASSOCIATION_RADIUS = 1.5
TTL_SECONDS = 3.0
RECENT_CAMS_WINDOW = 25
PROMOTE_CAMS_WINDOW = 5
GRACE_SECONDS = 0.5
GHOST_MIN_FRAMES = 5
MATURE_FRAMES = 50
DISPLAY_CAM_WINDOW = 10
POSITION_SMOOTHING_ALPHA = 0.4
GRAVEYARD_TTL = 10.0
CENTROID_OUTLIER_M = 0.5
TRACK_MERGE_DISTANCE = 0.6
TRACK_MERGE_JACCARD = 0.5
PREEMPT_DISTANCE = 0.4
PREEMPT_REPORT_DISTANCE = 1.0


def _robust_centroid(obs_list: list[dict]) -> tuple[float, float]:
    if len(obs_list) == 1:
        return obs_list[0]["x"], obs_list[0]["y"]

    xs = [o["x"] for o in obs_list]
    ys = [o["y"] for o in obs_list]
    mx = statistics.median(xs)
    my = statistics.median(ys)

    kept = [
        o for o, x, y in zip(obs_list, xs, ys)
        if math.hypot(x - mx, y - my) <= CENTROID_OUTLIER_M
    ]
    if not kept:
        return mx, my

    total_w = 0.0
    wx_sum = 0.0
    wy_sum = 0.0
    for o in kept:
        bbox = o.get("bbox", [0, 0, 1, 1])
        w = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        total_w += w
        wx_sum += o["x"] * w
        wy_sum += o["y"] * w
    return wx_sum / total_w, wy_sum / total_w


class WorldTracker:
    def __init__(self, association_radius: float = ASSOCIATION_RADIUS,
                 ttl_seconds: float = TTL_SECONDS,
                 graveyard_ttl: float = GRAVEYARD_TTL):
        self.association_radius = float(association_radius)
        self.ttl_seconds = float(ttl_seconds)
        self.graveyard_ttl = float(graveyard_ttl)
        self._tracks: dict[int, dict] = {}
        self._graveyard: dict[int, dict] = {}
        self._next_id = 1
        self._total_unique = 0
        self.last_events: list[dict] = []
        self.last_diag: dict = {}
        self.diag_enabled: bool = False

    @property
    def total_unique(self) -> int:
        return self._total_unique

    def reset(self) -> None:
        self._tracks.clear()
        self._graveyard.clear()
        self._next_id = 1
        self._total_unique = 0
        self.last_events = []
        self.last_diag = {}

    def _snapshot_tracks(self, ts: float) -> list[dict]:
        """Снимок всех живых треков для лога. Включает recent_cams в
        развёрнутом виде, чтобы потом можно было понять, почему фильтр принял такое решение."""
        snap = []
        for tid, t in self._tracks.items():
            recent = [sorted(c for c in cs if c is not None)
                      for cs in t.get("recent_cams", ())]
            snap.append({
                "id": tid,
                "x": round(t["x"], 3),
                "y": round(t["y"], 3),
                "age_since_seen": round(ts - t["last_seen"], 3),
                "confirmed": t.get("confirmed", False),
                "frames_seen": t.get("frames_seen", 0),
                "fresh_after_confirm": t.get("fresh_after_confirm", 0),
                "ever_displayed": t.get("ever_displayed", False),
                "recent_cams": recent,
            })
        return snap

    def _snapshot_graveyard(self, ts: float) -> list[dict]:
        snap = []
        for gid, g in self._graveyard.items():
            snap.append({
                "id": gid,
                "x": round(g["x"], 3),
                "y": round(g["y"], 3),
                "age": round(ts - g["died_at"], 3),
                "ever_displayed": g.get("ever_displayed", False),
            })
        return snap

    def _try_confirm(self, tid: int, t: dict) -> bool:
        if t.get("confirmed"):
            return False
        recent_short = list(t.get("recent_cams", ()))[-PROMOTE_CAMS_WINDOW:]
        for cs in recent_short:
            if len(cs) >= 2:
                t["confirmed"] = True

                self.last_events.append({
                    "type": "promote", "id": tid,
                    "cams": sorted(c for c in cs if c is not None),
                    "x": round(t["x"], 3), "y": round(t["y"], 3),
                })
                return True
        return False

    def _merge_close_tracks(self, ts: float, diag: dict | None = None,
                            verbose: bool = False) -> int:
        candidates = [
            tid for tid, t in self._tracks.items()
            if t["last_seen"] == ts and t.get("confirmed")
        ]
        if len(candidates) < 2:
            return 0
        candidates.sort(key=lambda tid: -self._tracks[tid].get("frames_seen", 0))

        survivors: list[int] = []
        merged_count = 0
        for tid in candidates:
            if tid not in self._tracks:
                continue
            t_new = self._tracks[tid]
            merged_into_existing = False
            for tid_sv in survivors:
                t_sv = self._tracks.get(tid_sv)
                if t_sv is None:
                    continue
                d = math.hypot(t_new["x"] - t_sv["x"], t_new["y"] - t_sv["y"])
                if d > TRACK_MERGE_DISTANCE:
                    continue

                age_new = t_new.get("frames_seen", 0)
                age_sv = t_sv.get("frames_seen", 0)
                if age_new >= MATURE_FRAMES and age_sv >= MATURE_FRAMES:
                    if verbose and diag is not None:
                        diag["merges"].append({
                            "skipped": True,
                            "reason": "both_mature",
                            "tid_a": tid, "tid_b": tid_sv,
                            "d": round(d, 3),
                            "age_a": age_new, "age_b": age_sv,
                        })
                    continue

                r_new: set = set()
                for cs in t_new.get("recent_cams", ()):
                    r_new |= cs
                r_new.discard(None)
                r_sv: set = set()
                for cs in t_sv.get("recent_cams", ()):
                    r_sv |= cs
                r_sv.discard(None)
                jaccard = None
                if r_new and r_sv:
                    union = len(r_new | r_sv)
                    jaccard = len(r_new & r_sv) / union if union else 0.0
                    if jaccard >= TRACK_MERGE_JACCARD:
                        if verbose and diag is not None:
                            diag["merges"].append({
                                "skipped": True,
                                "reason": "jaccard_high",
                                "tid_a": tid, "tid_b": tid_sv,
                                "d": round(d, 3),
                                "jaccard": round(jaccard, 3),
                                "cams_a": sorted(r_new),
                                "cams_b": sorted(r_sv),
                            })
                        continue

                for cs in t_new.get("recent_cams", ()):
                    t_sv["recent_cams"].append(cs)

                if t_new.get("ever_displayed"):
                    t_sv["ever_displayed"] = True

                lost_displayed = t_new.get("ever_displayed", False)
                del self._tracks[tid]
                if lost_displayed:
                    self._total_unique = max(0, self._total_unique - 1)
                if verbose and diag is not None:
                    diag["merges"].append({
                        "skipped": False,
                        "lost_id": tid, "into_id": tid_sv,
                        "d": round(d, 3),
                        "jaccard": round(jaccard, 3) if jaccard is not None else None,
                        "lost_displayed": lost_displayed,
                        "lost_frames": age_new,
                        "survivor_frames": age_sv,
                        "cams_lost": sorted(r_new),
                        "cams_sv": sorted(r_sv),
                    })
                self.last_events.append({
                    "type": "merge",
                    "lost_id": tid, "into_id": tid_sv,
                    "d": round(d, 3),
                })
                merged_count += 1
                merged_into_existing = True
                break
            if not merged_into_existing:
                survivors.append(tid)
        return merged_count

    def update(self, observations: list[dict], ts: float) -> list[dict]:
        self.last_events = []
        verbose = self.diag_enabled
        diag: dict = {
            "ts": round(ts, 3),
            "obs_count": len(observations),
            "tracks_before": self._snapshot_tracks(ts) if verbose else None,
            "graveyard_before": self._snapshot_graveyard(ts) if verbose else None,
            "expired_to_graveyard": [],
            "graveyard_expired": [],
            "pairs": [],
            "assignments": [],
            "track_updates": [],
            "clusters": [],
            "cluster_decisions": [],
            "merges": [],
            "display_decisions": [],
        }

        alive: dict[int, dict] = {}
        for tid, t in self._tracks.items():
            if ts - t["last_seen"] < self.ttl_seconds:
                alive[tid] = t
            elif t.get("confirmed"):
                self._graveyard[tid] = {
                    "x": t["x"], "y": t["y"],
                    "died_at": t["last_seen"],
                    "ever_displayed": t.get("ever_displayed", False),
                }
                if verbose:
                    diag["expired_to_graveyard"].append({
                        "id": tid,
                        "x": round(t["x"], 3), "y": round(t["y"], 3),
                        "frames_seen": t.get("frames_seen", 0),
                        "ever_displayed": t.get("ever_displayed", False),
                    })
                self.last_events.append({
                    "type": "to_graveyard", "id": tid,
                    "x": round(t["x"], 3), "y": round(t["y"], 3),
                })
            elif verbose:
                diag["expired_to_graveyard"].append({
                    "id": tid,
                    "x": round(t["x"], 3), "y": round(t["y"], 3),
                    "frames_seen": t.get("frames_seen", 0),
                    "ever_displayed": False,
                    "tentative_drop": True,
                })
        self._tracks = alive
        expired_graves = [
            tid for tid, g in self._graveyard.items()
            if ts - g["died_at"] >= self.graveyard_ttl
        ]
        for tid in expired_graves:
            self.last_events.append({"type": "graveyard_expire", "id": tid})
            if verbose:
                diag["graveyard_expired"].append(tid)
            del self._graveyard[tid]

        pairs: list[tuple[float, int, int]] = []
        for tid, t in self._tracks.items():
            tx, ty = t["x"], t["y"]
            for oi, o in enumerate(observations):
                d = math.hypot(o["x"] - tx, o["y"] - ty)
                if d < self.association_radius:
                    pairs.append((d, tid, oi))

        pairs.sort(key=lambda p: (
            0 if self._tracks[p[1]].get("confirmed") else 1,
            p[0],
        ))
        if verbose:
            diag["pairs"] = [
                {"d": round(d, 3), "tid": tid, "oi": oi,
                 "obs_cam": observations[oi].get("camera_id"),
                 "obs_lid": observations[oi].get("track_id")}
                for d, tid, oi in pairs
            ]

        used_obs: set[int] = set()
        track_assignments: dict[int, dict[int, int]] = defaultdict(dict)
        for cost, tid, oi in pairs:
            if oi in used_obs:
                continue
            cam = observations[oi].get("camera_id")
            if cam in track_assignments[tid]:
                continue
            track_assignments[tid][cam] = oi
            used_obs.add(oi)
        if verbose:
            diag["assignments"] = [
                {"tid": tid,
                 "cams": {str(cam): oi for cam, oi in cam_to_oi.items()}}
                for tid, cam_to_oi in track_assignments.items()
            ]

        for tid, cam_to_oi in track_assignments.items():
            ois = list(cam_to_oi.values())
            obs_subset = [observations[i] for i in ois]
            t = self._tracks[tid]
            prev_x, prev_y = t["x"], t["y"]
            new_x, new_y = _robust_centroid(obs_subset)
            t["x"] = POSITION_SMOOTHING_ALPHA * new_x + (1 - POSITION_SMOOTHING_ALPHA) * t["x"]
            t["y"] = POSITION_SMOOTHING_ALPHA * new_y + (1 - POSITION_SMOOTHING_ALPHA) * t["y"]
            t["last_seen"] = ts
            t["sources"] = [observations[i] for i in ois]
            t["frames_seen"] = t.get("frames_seen", 0) + 1
            cams_now = {observations[i].get("camera_id") for i in ois}
            if "recent_cams" not in t:
                t["recent_cams"] = deque(maxlen=RECENT_CAMS_WINDOW)
            t["recent_cams"].append(cams_now)
            was_confirmed = t.get("confirmed", False)
            promoted_now = self._try_confirm(tid, t)

            if was_confirmed:
                t["fresh_after_confirm"] = t.get("fresh_after_confirm", 0) + 1
            if verbose:
                diag["track_updates"].append({
                    "tid": tid,
                    "prev": [round(prev_x, 3), round(prev_y, 3)],
                    "centroid_raw": [round(new_x, 3), round(new_y, 3)],
                    "after_ema": [round(t["x"], 3), round(t["y"], 3)],
                    "cams_now": sorted(cams_now),
                    "n_obs": len(obs_subset),
                    "was_confirmed": was_confirmed,
                    "promoted_now": promoted_now,
                    "frames_seen": t["frames_seen"],
                    "fresh_after_confirm": t.get("fresh_after_confirm", 0),
                })

        remaining = [i for i in range(len(observations)) if i not in used_obs]
        clusters: list[list[int]] = []
        for oi in remaining:
            o = observations[oi]
            placed = False
            for cl in clusters:
                cluster_cams = {observations[j].get("camera_id") for j in cl}
                if o.get("camera_id") in cluster_cams:
                    continue
                cl_obs = [observations[j] for j in cl]
                cx, cy = _robust_centroid(cl_obs)
                d = math.hypot(o["x"] - cx, o["y"] - cy)
                if d < self.association_radius:
                    cl.append(oi)
                    placed = True
                    break
            if not placed:
                clusters.append([oi])

        if verbose:
            for cidx, cl in enumerate(clusters):
                cl_obs = [observations[i] for i in cl]
                cx0, cy0 = _robust_centroid(cl_obs)
                diag["clusters"].append({
                    "cidx": cidx,
                    "obs_indices": list(cl),
                    "centroid": [round(cx0, 3), round(cy0, 3)],
                    "cams": sorted({observations[i].get("camera_id") for i in cl}),
                    "members": [
                        {"cam": observations[i].get("camera_id"),
                         "lid": observations[i].get("track_id"),
                         "x": round(observations[i]["x"], 3),
                         "y": round(observations[i]["y"], 3)}
                        for i in cl
                    ],
                })

        for cidx, cl in enumerate(clusters):
            cl_obs = [observations[i] for i in cl]
            cx, cy = _robust_centroid(cl_obs)
            cluster_cams = {observations[i].get("camera_id") for i in cl}
            recent_cams_init = deque([cluster_cams], maxlen=RECENT_CAMS_WINDOW)

            best_grave_tid = None
            best_grave_d = self.association_radius
            grave_candidates = [] if verbose else None
            for gtid, g in self._graveyard.items():
                d = math.hypot(cx - g["x"], cy - g["y"])
                if verbose:
                    grave_candidates.append({"gid": gtid, "d": round(d, 3),
                                              "x": round(g["x"], 3), "y": round(g["y"], 3),
                                              "age": round(ts - g["died_at"], 2)})
                if d < best_grave_d:
                    best_grave_d = d
                    best_grave_tid = gtid

            if best_grave_tid is not None:
                grave = self._graveyard.pop(best_grave_tid)
                self._tracks[best_grave_tid] = {
                    "x": cx, "y": cy,
                    "last_seen": ts,
                    "sources": cl_obs,
                    "frames_seen": 25,
                    "fresh_after_confirm": GHOST_MIN_FRAMES,  # уже зрелый
                    "confirmed": True,
                    "recent_cams": recent_cams_init,
                    "ever_displayed": grave.get("ever_displayed", True),
                }
                if verbose:
                    diag["cluster_decisions"].append({
                        "cidx": cidx,
                        "decision": "resurrect",
                        "from_grave_id": best_grave_tid,
                        "grave_d": round(best_grave_d, 3),
                        "grave_age_s": round(ts - grave["died_at"], 2),
                        "grave_candidates": grave_candidates,
                        "centroid": [round(cx, 3), round(cy, 3)],
                        "cluster_cams": sorted(cluster_cams),
                        "inherited_ever_displayed": grave.get("ever_displayed", True),
                    })
                self.last_events.append({
                    "type": "resurrect", "id": best_grave_tid,
                    "grave_age_s": round(ts - grave["died_at"], 2),
                    "x": round(cx, 3), "y": round(cy, 3),
                })
                continue

            preempt_tid = None
            preempt_d = PREEMPT_DISTANCE
            near_misses: list[dict] = []
            for tid_live, t_live in self._tracks.items():
                if not t_live.get("confirmed") or t_live.get("last_seen") != ts:
                    continue
                d_pre = math.hypot(cx - t_live["x"], cy - t_live["y"])
                if d_pre <= PREEMPT_REPORT_DISTANCE:
                    near_misses.append({
                        "tid": tid_live,
                        "d": round(d_pre, 3),
                        "passed": d_pre <= PREEMPT_DISTANCE,
                    })
                if d_pre < preempt_d:
                    preempt_d = d_pre
                    preempt_tid = tid_live
            if preempt_tid is not None:
                if verbose:
                    diag["cluster_decisions"].append({
                        "cidx": cidx,
                        "decision": "preempt",
                        "into_id": preempt_tid,
                        "preempt_d": round(preempt_d, 3),
                        "near_misses": near_misses,
                        "grave_candidates": grave_candidates,
                        "centroid": [round(cx, 3), round(cy, 3)],
                        "cluster_cams": sorted(cluster_cams),
                    })
                self.last_events.append({
                    "type": "phantom_suppressed",
                    "into_id": preempt_tid,
                    "d": round(preempt_d, 3),
                    "cluster_cams": sorted(cluster_cams),
                    "x": round(cx, 3), "y": round(cy, 3),
                })
                continue
            if near_misses:
                self.last_events.append({
                    "type": "preempt_near_miss",
                    "cluster_cams": sorted(cluster_cams),
                    "x": round(cx, 3), "y": round(cy, 3),
                    "candidates": near_misses[:5],
                })

            new_id = self._next_id
            self._next_id += 1
            confirmed_now = len(cluster_cams) >= 2
            self._tracks[new_id] = {
                "x": cx, "y": cy,
                "last_seen": ts,
                "sources": cl_obs,
                "frames_seen": 1,
                "fresh_after_confirm": 0,
                "confirmed": confirmed_now,
                "recent_cams": recent_cams_init,
                "ever_displayed": False,
            }
            if verbose:
                diag["cluster_decisions"].append({
                    "cidx": cidx,
                    "decision": "new_track",
                    "new_id": new_id,
                    "confirmed_now": confirmed_now,
                    "near_misses": near_misses,
                    "grave_candidates": grave_candidates,
                    "centroid": [round(cx, 3), round(cy, 3)],
                    "cluster_cams": sorted(cluster_cams),
                })
            if confirmed_now:
                self.last_events.append({
                    "type": "promote", "id": new_id,
                    "cams": sorted(cluster_cams),
                    "x": round(cx, 3), "y": round(cy, 3),
                })

        self._merge_close_tracks(ts, diag, verbose=verbose)

        out = []
        for tid, t in self._tracks.items():
            if not t.get("confirmed"):
                if verbose:
                    diag["display_decisions"].append({
                        "tid": tid, "result": "skip_tentative",
                        "frames_seen": t.get("frames_seen", 0),
                    })
                continue
            is_fresh = (t["last_seen"] == ts)
            if not is_fresh:
                age = ts - t["last_seen"]
                if age > GRACE_SECONDS:
                    if verbose:
                        diag["display_decisions"].append({
                            "tid": tid, "result": "skip_grace_expired",
                            "age_since_seen": round(age, 3),
                        })
                    continue

                if t.get("fresh_after_confirm", 0) < GHOST_MIN_FRAMES:
                    if verbose:
                        diag["display_decisions"].append({
                            "tid": tid, "result": "skip_too_young_for_ghost",
                            "fresh_after_confirm": t.get("fresh_after_confirm", 0),
                        })
                    continue

            recent_window = list(t.get("recent_cams", ()))[-DISPLAY_CAM_WINDOW:]
            has_multi_cam = any(len(cs) >= 2 for cs in recent_window)
            if not has_multi_cam:
                if verbose:
                    diag["display_decisions"].append({
                        "tid": tid, "result": "skip_no_multi_cam_in_window",
                        "recent_window": [sorted(c for c in cs if c is not None)
                                           for cs in recent_window],
                    })
                continue

            if not t.get("ever_displayed", False):
                t["ever_displayed"] = True
                self._total_unique += 1
                self.last_events.append({
                    "type": "first_display", "id": tid,
                    "x": round(t["x"], 3), "y": round(t["y"], 3),
                })
            recent: set = set()
            for cs in t.get("recent_cams", ()):
                recent |= cs
            recent.discard(None)
            current_cams = sorted({s.get("camera_id") for s in t.get("sources", ())
                                    if s.get("camera_id") is not None}) if is_fresh else []
            if verbose:
                diag["display_decisions"].append({
                    "tid": tid, "result": "displayed",
                    "current_cams": current_cams,
                    "source_count": max(len(current_cams), len(recent)),
                })
            out.append({
                "id": tid,
                "x": t["x"],
                "y": t["y"],
                "camera_ids": current_cams,
                "source_count": max(len(current_cams), len(recent)),
                "is_ghost": not is_fresh,
            })
        if verbose:
            diag["tracks_after"] = self._snapshot_tracks(ts)
            diag["graveyard_after"] = self._snapshot_graveyard(ts)
        diag["output_count"] = len(out)
        diag["total_unique"] = self._total_unique
        self.last_diag = diag
        return out
