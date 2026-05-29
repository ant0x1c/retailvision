/* analytics.js — план помещения + heatmap с сервера + точки людей */

const API = "http://127.0.0.1:8000";
const WS_URL = "ws://127.0.0.1:8000/ws";

class AnalyticsPage {
  constructor() {
    this.el = {
      heatmapCanvas: document.getElementById("heatmap-canvas"),
      heatmapWrap: document.getElementById("heatmap-wrap"),
      historyCanvas: document.getElementById("history-canvas"),
      historyHint: document.getElementById("history-hint"),
      zonesList: document.getElementById("zones-list"),
      wsDot: document.getElementById("ws-dot"),
      wsLabel: document.getElementById("ws-label"),
      total: document.getElementById("a-total"),
      active: document.getElementById("a-active"),
      footerCoords: document.getElementById("footer-coords"),
      btnHmReset: document.getElementById("btn-hm-reset"),
      btnHmExport: document.getElementById("btn-hm-export"),
      totalLabel: document.querySelector("#a-total + .stat-label"),
      activeLabel: document.querySelector("#a-active + .stat-label"),
    };
    this.historyCtx = this.el.historyCanvas.getContext("2d");

    this.sceneW = 20;
    this.sceneH = 20;
    this.wsRetry = 0;

    this.personMarkers = new Map();
    this.sceneCameraObjects = new Map();
    this.heatmapMode = "live";
    this.lastHeatmap = null;

    this.setup3D();
  }

  setup3D() {
    this.renderer = new THREE.WebGLRenderer({ canvas: this.el.heatmapCanvas, antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);

    this.scene3d = new THREE.Scene();
    this.scene3d.background = new THREE.Color(0x0d0f14);
    this.scene3d.fog = new THREE.FogExp2(0x0d0f14, 0.015);
    this.scene3d.add(new THREE.AmbientLight(0xffffff, 0.6));
    this.scene3d.add(new THREE.HemisphereLight(0x7aa2ff, 0x1a1f2b, 0.5));
    const sun = new THREE.DirectionalLight(0xffffff, 0.7);
    sun.position.set(12, 20, 10);
    this.scene3d.add(sun);
    this.scene3d.add(new THREE.GridHelper(120, 120, 0x2f364a, 0x1f2536));

    this.cam3d = new THREE.PerspectiveCamera(50, 1, 0.1, 500);
    this.cam3d.position.set(10, 16, 14);
    this.cam3d.lookAt(10, 0, 10);

    this.floorMesh = null;
    this.heatmapPivot = null;
    this.roomGroup = new THREE.Group();
    this.markersGroup = new THREE.Group();
    this.scene3d.add(this.roomGroup);
    this.scene3d.add(this.markersGroup);

    this.hmTextureCanvas = document.createElement("canvas");
    this.hmTextureCtx = this.hmTextureCanvas.getContext("2d");
    this.hmTexture = null;

    this.orbit = this.buildOrbitControls();
    window.addEventListener("resize", () => this.resize3D());
    this.resize3D();
  }

  buildOrbitControls() {
    let rmb = false;
    let lx = 0, ly = 0;
    let theta = Math.PI / 4;
    let phi = Math.PI / 3.2;
    let radius = 24;
    const target = new THREE.Vector3(10, 0, 10);

    const update = () => {
      this.cam3d.position.set(
        target.x + radius * Math.sin(phi) * Math.sin(theta),
        target.y + radius * Math.cos(phi),
        target.z + radius * Math.sin(phi) * Math.cos(theta)
      );
      this.cam3d.lookAt(target);
    };
    update();

    this.el.heatmapCanvas.addEventListener("contextmenu", e => e.preventDefault());
    this.el.heatmapCanvas.addEventListener("mousedown", e => {
      if (e.button === 2) rmb = true;
      lx = e.clientX; ly = e.clientY;
    });
    window.addEventListener("mouseup", e => { if (e.button === 2) rmb = false; });
    window.addEventListener("mousemove", e => {
      if (!rmb) return;
      const dx = e.clientX - lx;
      const dy = e.clientY - ly;
      lx = e.clientX; ly = e.clientY;
      theta -= dx * 0.005;
      phi = Math.max(0.05, Math.min(Math.PI / 2.05, phi + dy * 0.005));
      update();
    });
    this.el.heatmapCanvas.addEventListener("wheel", e => {
      radius = Math.max(6, Math.min(120, radius + e.deltaY * 0.04));
      update();
    }, { passive: true });

    return {
      reset: (w, h) => {
        theta = Math.PI / 4;
        phi = Math.PI / 3.2;
        radius = Math.max(w, h) * 1.2;
        target.set(w / 2, 0, h / 2);
        update();
      },
    };
  }

  resize3D() {
    const w = this.el.heatmapWrap.clientWidth || 600;
    const h = this.el.heatmapWrap.clientHeight || 420;
    this.renderer.setSize(w, h, false);
    this.cam3d.aspect = w / h;
    this.cam3d.updateProjectionMatrix();
  }

  createFloor() {
    if (this.floorMesh) this.scene3d.remove(this.floorMesh);
    if (this.heatmapPivot) this.scene3d.remove(this.heatmapPivot);

    this.floorMesh = new THREE.Mesh(
      new THREE.PlaneGeometry(this.sceneW, this.sceneH),
      new THREE.MeshStandardMaterial({ color: 0x1a2233, roughness: 0.9, metalness: 0.1, side: THREE.DoubleSide })
    );
    this.floorMesh.rotation.x = -Math.PI / 2;
    this.floorMesh.position.set(this.sceneW / 2, 0, this.sceneH / 2);
    this.scene3d.add(this.floorMesh);

    this.hmTextureCanvas.width = 200;
    this.hmTextureCanvas.height = 200;
    this.hmTexture = new THREE.CanvasTexture(this.hmTextureCanvas);
    this.hmTexture.needsUpdate = true;

    this.heatmapPivot = new THREE.Group();
    this.heatmapPivot.position.set(this.sceneW / 2, 0.02, this.sceneH / 2);
    this.scene3d.add(this.heatmapPivot);

    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(this.sceneW, this.sceneH),
      new THREE.MeshBasicMaterial({
        map: this.hmTexture,
        transparent: true,
        opacity: 0.9,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        side: THREE.DoubleSide,
      })
    );
    mesh.rotation.x = -Math.PI / 2;
    this.heatmapPivot.add(mesh);
    this.orbit.reset(this.sceneW, this.sceneH);
  }

  clearRoom() {
    this.scene3d.remove(this.roomGroup);
    this.roomGroup = new THREE.Group();
    this.scene3d.add(this.roomGroup);
  }

  addBox(obj, color) {
    const w = obj.w || obj.length || 1;
    const d = obj.d || 0.2;
    const h = obj.h || 1;
    const mesh = new THREE.Mesh(
      new THREE.BoxGeometry(w, h, d),
      new THREE.MeshStandardMaterial({ color, roughness: 0.75 })
    );
    mesh.position.set(obj.x || 0, h / 2, obj.z || 0);
    mesh.rotation.y = THREE.MathUtils.degToRad(obj.rot || 0);
    this.roomGroup.add(mesh);
  }

  buildCameraMarker(camData) {
    const h = camData.height || 3;
    const group = new THREE.Group();
    const body = new THREE.Mesh(
      new THREE.BoxGeometry(0.3, 0.18, 0.45),
      new THREE.MeshStandardMaterial({ color: 0x0055cc })
    );
    body.position.y = h;
    group.add(body);
    const pole = new THREE.Mesh(
      new THREE.CylinderGeometry(0.03, 0.03, h, 6),
      new THREE.MeshStandardMaterial({ color: 0x334455 })
    );
    pole.position.y = h / 2;
    group.add(pole);
    const fov = camData.fov || 90;
    const radius = h * Math.tan(THREE.MathUtils.degToRad(fov / 2)) * 0.9;
    const arrow = new THREE.Mesh(
      new THREE.ConeGeometry(0.08, 0.5, 6),
      new THREE.MeshBasicMaterial({ color: 0x0099ff })
    );
    arrow.rotation.x = Math.PI / 2;
    arrow.position.set(0, h, 0.55);
    group.add(arrow);
    group.position.set(camData.x || 0, 0, camData.z || 0);
    group.rotation.y = THREE.MathUtils.degToRad(-(camData.yaw || 0));
    return { group };
  }

  syncSceneCameras(cameras) {
    const keys = new Set(cameras.map((_, i) => String(i)));
    this.sceneCameraObjects.forEach((obj, key) => {
      if (!keys.has(key)) {
        this.scene3d.remove(obj.group);
        this.sceneCameraObjects.delete(key);
      }
    });
    cameras.forEach((camData, i) => {
      const key = String(i);
      if (this.sceneCameraObjects.has(key)) return;
      const marker = this.buildCameraMarker(camData);
      this.scene3d.add(marker.group);
      this.sceneCameraObjects.set(key, marker);
    });
  }

  loadRoomFromEditor() {
    this.clearRoom();
    let sceneData = [];
    try { sceneData = JSON.parse(localStorage.getItem("rv_scene") || "[]"); } catch (_) {}
    sceneData.forEach(obj => {
      if (obj.type === "wall") this.addBox(obj, 0x3a4055);
      if (obj.type === "shelf") this.addBox(obj, 0x1a3a6e);
      if (obj.type === "counter") this.addBox(obj, 0x5a2a1a);
    });
    this.syncSceneCameras(sceneData.filter(obj => obj.type === "camera"));
  }

  // ─── Точки людей ──────────────────────────────────────────────────────────
  updatePersonMarkers(worldPoints) {
    if (!Array.isArray(worldPoints)) return;
    const seen = new Set();
    worldPoints.forEach(p => {
      if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return;
      const key = String(p.id);
      seen.add(key);
      let marker = this.personMarkers.get(key);
      if (!marker) {
        const geo = new THREE.CylinderGeometry(0.1, 0.1, 0.02, 20);
        const mat = new THREE.MeshBasicMaterial({ color: 0xffd166 });
        marker = new THREE.Mesh(geo, mat);

        const ringGeo = new THREE.RingGeometry(0.11, 0.13, 20);
        const ringMat = new THREE.MeshBasicMaterial({
          color: 0xffd166, transparent: true, opacity: 0.45, side: THREE.DoubleSide,
        });
        const ring = new THREE.Mesh(ringGeo, ringMat);
        ring.rotation.x = -Math.PI / 2;
        ring.position.y = 0.01;
        marker.add(ring);
        marker._ring = ring;
        marker._ringMat = ringMat;

        this.markersGroup.add(marker);
        this.personMarkers.set(key, marker);
      }

      marker.position.set(p.x, 0.02, p.y);

      const hot = (p.source_count || 1) > 1;
      const col = hot ? 0x00e5a0 : 0xffd166;
      marker.material.color.setHex(col);
      if (marker._ringMat) marker._ringMat.color.setHex(col);
    });
    this.personMarkers.forEach((marker, key) => {
      if (seen.has(key)) return;
      this.markersGroup.remove(marker);
      marker.geometry.dispose();
      marker.material.dispose();
      this.personMarkers.delete(key);
    });

    this.el.footerCoords.textContent = worldPoints.length
      ? `Людей в зале: ${worldPoints.length}`
      : "Нет live-координат";
  }

  // ─── Heatmap ──────────────────────────────────────────────────────────────
  drawHeatmap(grid) {
    if (!Array.isArray(grid) || !grid.length || !grid[0]?.length) {
      const W = this.hmTextureCanvas.width;
      const H = this.hmTextureCanvas.height;
      this.hmTextureCtx.clearRect(0, 0, W, H);
      this.hmTexture.needsUpdate = true;
      this.updateZonesList(null);
      return;
    }
    this.lastHeatmap = grid;
    const rows = grid.length;
    const cols = grid[0].length;
    const W = this.hmTextureCanvas.width;
    const H = this.hmTextureCanvas.height;

    this.hmTextureCtx.clearRect(0, 0, W, H);
    this.hmTextureCtx.fillStyle = "rgba(20,24,34,0.2)";
    this.hmTextureCtx.fillRect(0, 0, W, H);

    const cw = W / cols;
    const ch = H / rows;

    let maxVal = 0;
    for (let r = 0; r < rows; r++)
      for (let c = 0; c < cols; c++)
        maxVal = Math.max(maxVal, grid[r][c] || 0);
    console.log("drawHeatmap maxVal:", maxVal, "rows:", rows, "cols:", cols);

    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const v = Math.min(1, Math.max(0, grid[r][c] || 0));
        if (v < 0.01) continue;
        this.hmTextureCtx.fillStyle = `hsla(${(1 - v) * 240},100%,50%,${(v * 0.85).toFixed(3)})`;
        this.hmTextureCtx.fillRect(c * cw, r * ch, cw + 1, ch + 1);
      }
    }
    this.hmTexture.needsUpdate = true;
    this.updateZonesList(grid);
  }

  updateZonesList(grid) {
    if (!grid?.length || !grid[0]?.length) {
      this.el.zonesList.innerHTML = '<div class="hint">Данных пока нет</div>';
      return;
    }
    const rows = grid.length;
    const cols = grid[0].length;
    const hot = [];
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const v = grid[r][c] || 0;
        if (v > 0.05) hot.push({ r, c, v });
      }
    }
    hot.sort((a, b) => b.v - a.v);
    const top = hot.slice(0, 6);
    if (!top.length) {
      this.el.zonesList.innerHTML = '<div class="hint">Данных пока нет</div>';
      return;
    }
    this.el.zonesList.innerHTML = "";
    top.forEach((z, i) => {
      const wx = ((z.c + 0.5) / cols) * this.sceneW;
      const wy = ((z.r + 0.5) / rows) * this.sceneH;
      const row = document.createElement("div");
      row.className = "track-item";
      row.textContent = `${i + 1}. (${wx.toFixed(1)}, ${wy.toFixed(1)}) м · ${(z.v * 100).toFixed(0)}%`;
      this.el.zonesList.appendChild(row);
    });
  }

  // ─── История ──────────────────────────────────────────────────────────────
  drawHistory(data) {
    const W = this.el.historyCanvas.width;
    const H = this.el.historyCanvas.height;
    this.historyCtx.clearRect(0, 0, W, H);
    this.historyCtx.fillStyle = "#13161e";
    this.historyCtx.fillRect(0, 0, W, H);

    if (!data || data.length < 2) {
      this.historyCtx.fillStyle = "#6b7080";
      this.historyCtx.font = "10px monospace";
      this.historyCtx.textAlign = "center";
      this.historyCtx.fillText("Недостаточно данных", W / 2, H / 2);
      return;
    }

    const sorted = [...data].reverse();
    const maxV = Math.max(...sorted.map(d => d.visitor_count), 1);
    const pL = 28, pR = 6, pT = 8, pB = 18;
    const cW = W - pL - pR;
    const cH = H - pT - pB;

    this.historyCtx.strokeStyle = "#252936";
    this.historyCtx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
      const y = pT + cH - (i / 4) * cH;
      this.historyCtx.beginPath();
      this.historyCtx.moveTo(pL, y);
      this.historyCtx.lineTo(pL + cW, y);
      this.historyCtx.stroke();
      this.historyCtx.fillStyle = "#6b7080";
      this.historyCtx.font = "8px monospace";
      this.historyCtx.textAlign = "right";
      this.historyCtx.fillText(Math.round((i / 4) * maxV), pL - 3, y + 3);
    }

    this.historyCtx.beginPath();
    this.historyCtx.strokeStyle = "#00e5a0";
    this.historyCtx.lineWidth = 1.5;
    sorted.forEach((d, i) => {
      const px = pL + (i / (sorted.length - 1)) * cW;
      const py = pT + cH - (d.visitor_count / maxV) * cH;
      i === 0 ? this.historyCtx.moveTo(px, py) : this.historyCtx.lineTo(px, py);
    });
    this.historyCtx.stroke();
    this.historyCtx.lineTo(pL + cW, pT + cH);
    this.historyCtx.lineTo(pL, pT + cH);
    this.historyCtx.closePath();
    this.historyCtx.fillStyle = "rgba(0,229,160,0.08)";
    this.historyCtx.fill();

    this.historyCtx.fillStyle = "#6b7080";
    this.historyCtx.font = "8px monospace";
    this.historyCtx.textAlign = "left";
    this.historyCtx.fillText(sorted[0].ts, pL, H - 3);
    this.historyCtx.textAlign = "right";
    this.historyCtx.fillText(sorted[sorted.length - 1].ts, pL + cW, H - 3);
    this.el.historyHint.textContent = `${sorted.length} дн. · макс ${maxV} чел.`;
  }

  async loadHistory() {
    try {
      const res = await fetch(`${API}/history?limit=60`);
      const data = await res.json();
      this.drawHistory(data.history || []);
    } catch (_) {
      this.el.historyHint.textContent = "Нет данных";
    }
  }

  // ─── WebSocket / actions ──────────────────────────────────────────────────
  connectWS() {
    this.el.wsDot.className = "status-dot connecting";
    this.el.wsLabel.textContent = "Подключение...";
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      this.el.wsDot.className = "status-dot connected";
      this.el.wsLabel.textContent = "Подключено";
      this.wsRetry = 0;
    };

    ws.onmessage = e => {
      let data;
      try { data = JSON.parse(e.data); } catch { return; }

      if (Array.isArray(data.world_points)) this.updatePersonMarkers(data.world_points);
      if (Array.isArray(data.heatmap) && this.heatmapMode === "live") this.drawHeatmap(data.heatmap);
      if (data.stats) {
        this._lastLiveTotal = data.stats.visitor_count ?? "—";
        this._lastLiveActive = data.stats.active_tracks ?? "—";
        if (this.heatmapMode === "live") {
          this.el.total.textContent = this._lastLiveTotal;
          this.el.active.textContent = this._lastLiveActive;
        }
      }
    };

    ws.onclose = () => {
      this.el.wsDot.className = "status-dot error";
      this.el.wsLabel.textContent = "Нет соединения";
      setTimeout(() => this.connectWS(), Math.min(5000, 1000 * ++this.wsRetry));
    };
  }

  bindActionButtons() {
    this.el.btnHmReset.addEventListener("click", () => {
      fetch(`${API}/reset`, { method: "POST" }).catch(console.error);
      this.drawHeatmap(null);
    });

    this.el.btnHmExport.addEventListener("click", async () => {
      if (!this._historyFrom || !this._historyTo) return;
      const res = await fetch(`${API}/history/export?from=${this._historyFrom}&to=${this._historyTo}`);
      const data = await res.json();
      const rows = (data.snapshots || []).map(h => {
        const dt = new Date(h.ts * 1000).toISOString();
        return `${dt},${h.visitor_count},${h.active_tracks}`;
      });
      const csv = ["timestamp,visitor_count,peak_active", ...rows].join("\n");
      const blob = new Blob([csv], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "analytics.csv";
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  async loadSceneConfig() {
    try {
      const res = await fetch(`${API}/scene`);
      const scene = await res.json();
      this.sceneW = Number(scene.width) || 20;
      this.sceneH = Number(scene.height) || 20;
    } catch (_) {
      this.sceneW = 20;
      this.sceneH = 20;
    }
  }

  // ─── Историческая тепловая карта ─────────────────────────────────────────
  setHeatmapMode(mode) {
    this.heatmapMode = mode;
    const bar = document.getElementById("hm-time-bar");
    if (bar) bar.dataset.mode = mode;

    if (this.markersGroup) {
      this.markersGroup.visible = (mode === "live");
    }

    this.el.btnHmExport.style.display = (mode === "live") ? "none" : "";
    this.el.btnHmReset.style.display = (mode === "live") ? "" : "none";

    if (mode === "live") {
      this.el.totalLabel.textContent = "Посетителей";
      this.el.activeLabel.textContent = "Сейчас";
    } else {
      this.el.totalLabel.textContent = "За период";
      this.el.activeLabel.textContent = "Пик";
    }
  }

  async loadHistoricalHeatmap(fromTs, toTs) {
    const bar = document.getElementById("hm-time-bar");
    if (bar) bar.dataset.loading = "true";
    this._historyFrom = fromTs;
    this._historyTo = toTs;
    try {
      const res = await fetch(`${API}/heatmap/history?from=${fromTs}&to=${toTs}`);
      if (!res.ok) throw new Error(res.status);
      const data = await res.json();
      console.log("heatmap type:", typeof data.heatmap, Array.isArray(data.heatmap), data.heatmap?.length);
      console.log("first row:", data.heatmap?.[0]);
      this.drawHeatmap(data.heatmap || null);

      if (data.visitor_count !== undefined) {
          this.el.total.textContent = data.visitor_count;
      }
      if (data.peak_active !== undefined) {
          this.el.active.textContent = data.peak_active;
      }
    } catch (_) {
      this.drawHeatmap(null);
    } finally {
      if (bar) bar.dataset.loading = "false";
    }
  }

  buildTimeWindowBar() {
    const wrap = document.getElementById("heatmap-wrap");
    if (!wrap) return;

    const bar = document.createElement("div");
    bar.id = "hm-time-bar";
    bar.dataset.mode = "live";
    bar.innerHTML = `
      <div id="hm-mode-pills">
        <button class="hm-pill active" data-mode="live">● Реалтайм</button>
        <button class="hm-pill" data-mode="1h">1 ч</button>
        <button class="hm-pill" data-mode="4h">4 ч</button>
        <button class="hm-pill" data-mode="today">Сегодня</button>
        <button class="hm-pill" data-mode="custom">Диапазон</button>
      </div>
      <div id="hm-custom-range" style="display:none">
        <label>С <input type="datetime-local" id="hm-from"/></label>
        <label>По <input type="datetime-local" id="hm-to"/></label>
        <button id="hm-apply" class="btn-secondary">Применить</button>
      </div>
      <div id="hm-period-label" class="hint"></div>
    `;

    wrap.parentNode.insertBefore(bar, wrap);
    this._injectTimeBarStyles();

    const now = new Date();
    const pad = n => String(n).padStart(2, "0");
    const localISO = d =>
      `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    bar.querySelector("#hm-to").value = localISO(now);
    const h1ago = new Date(now - 3600000);
    bar.querySelector("#hm-from").value = localISO(h1ago);

    bar.querySelectorAll(".hm-pill").forEach(btn => {
      btn.addEventListener("click", () => {
        bar.querySelectorAll(".hm-pill").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        const mode = btn.dataset.mode;
        const rangeDiv = bar.querySelector("#hm-custom-range");
        const label = bar.querySelector("#hm-period-label");

        if (mode === "live") {
          rangeDiv.style.display = "none";
          label.textContent = "";
          this.setHeatmapMode("live");
          this.el.total.textContent = this._lastLiveTotal ?? "—";
          this.el.active.textContent = this._lastLiveActive ?? "—";
          this.drawHeatmap(this.lastHeatmap);
          return;
        }

        this.setHeatmapMode("history");

        if (mode === "custom") {
          rangeDiv.style.display = "flex";
          return;
        }

        rangeDiv.style.display = "none";
        const toTs = Math.floor(Date.now() / 1000);
        const offsets = { "1h": 3600, "4h": 14400, "today": this._secondsSinceMidnight() };
        const fromTs = toTs - offsets[mode];

        const fmtTime = ts => {
          const d = new Date(ts * 1000);
          return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
        };
        label.textContent = `${fmtTime(fromTs)} — ${fmtTime(toTs)}`;
        this.loadHistoricalHeatmap(fromTs, toTs);
      });
    });

    bar.querySelector("#hm-apply").addEventListener("click", () => {
      const fromVal = bar.querySelector("#hm-from").value;
      const toVal = bar.querySelector("#hm-to").value;
      if (!fromVal || !toVal) return;
      const fromTs = Math.floor(new Date(fromVal).getTime() / 1000);
      const toTs = Math.floor(new Date(toVal).getTime() / 1000);
      if (fromTs >= toTs) return;
      const label = bar.querySelector("#hm-period-label");
      label.textContent = `${fromVal.replace("T", " ")} — ${toVal.replace("T", " ")}`;
      this.loadHistoricalHeatmap(fromTs, toTs);
    });
  }

  _secondsSinceMidnight() {
    const now = new Date();
    return now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
  }

  _injectTimeBarStyles() {
    if (document.getElementById("hm-time-bar-styles")) return;
    const s = document.createElement("style");
    s.id = "hm-time-bar-styles";
    s.textContent = `
      #hm-time-bar {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 8px 16px 6px;
        border-bottom: 1px solid #1e2535;
        background: #0d0f14;
      }
      #hm-mode-pills {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
      }
      .hm-pill {
        font-family: var(--font, 'JetBrains Mono', monospace);
        font-size: 11px;
        letter-spacing: .04em;
        padding: 4px 12px;
        border-radius: 20px;
        border: 1px solid #2a3048;
        background: transparent;
        color: #6b7080;
        cursor: pointer;
        transition: all .15s;
      }
      .hm-pill:hover { border-color: #00e5a0; color: #00e5a0; }
      .hm-pill.active {
        background: #00e5a015;
        border-color: #00e5a0;
        color: #00e5a0;
      }
      #hm-custom-range {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
      }
      #hm-custom-range label {
        font-family: var(--font, 'JetBrains Mono', monospace);
        font-size: 10px;
        color: #6b7080;
        display: flex;
        align-items: center;
        gap: 5px;
      }
      #hm-custom-range input[type="datetime-local"] {
        background: #13161e;
        border: 1px solid #2a3048;
        border-radius: 4px;
        color: #c8cdd8;
        font-family: var(--font, 'JetBrains Mono', monospace);
        font-size: 10px;
        padding: 3px 7px;
        outline: none;
        color-scheme: dark;
      }
      #hm-custom-range input:focus { border-color: #00e5a0; }
      #hm-time-bar[data-loading="true"]::after {
        content: 'Загрузка...';
        font-family: var(--font, 'JetBrains Mono', monospace);
        font-size: 10px;
        color: #00e5a0;
        animation: pulse-text 1s ease-in-out infinite alternate;
      }
      #hm-period-label { min-height: 14px; }
      @keyframes pulse-text { from { opacity: .4 } to { opacity: 1 } }
    `;
    document.head.appendChild(s);
  }

  animate() {
    requestAnimationFrame(() => this.animate());
    const t = performance.now() / 1000;
    this.personMarkers.forEach(marker => {
      if (!marker._ring || !marker._ringMat) return;
      const pulse = 0.5 + 0.5 * Math.sin(t * 2.8);
      marker._ringMat.opacity = 0.15 + 0.35 * pulse;
      const s = 1 + 0.18 * pulse;
      marker._ring.scale.set(s, s, s);
    });
    this.renderer.render(this.scene3d, this.cam3d);
  }

  async init() {
    await this.loadSceneConfig();
    this.bindActionButtons();
    this.el.btnHmExport.style.display = "none";
    this.el.btnHmReset.style.display = "";
    this.createFloor();
    this.loadRoomFromEditor();
    this.buildTimeWindowBar();
    this.drawHeatmap(null);
    this.loadHistory();
    setInterval(() => this.loadHistory(), 30000);
    this.connectWS();
    this.animate();
  }
}

new AnalyticsPage().init();