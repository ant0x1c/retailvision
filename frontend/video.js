/* video.js — страница с видеопотоками */

const API = "http://127.0.0.1:8000";
const overlayLbl = document.getElementById("video-overlay-label");
const videoGrid = document.getElementById("video-grid");

// ─── Список камер из БД ─────────────────────────────────────────────────────
let cameras = [];
const cameraStatus = new Map(); // cam.id → true/false (живая/нет)

function renderCameraList() {
  const list = document.getElementById("camera-list");
  if (!cameras.length) {
    list.innerHTML = '<div class="hint">Камеры не найдены</div>';
    return;
  }
  list.innerHTML = "";
  cameras.forEach(cam => {
    const alive = cameraStatus.get(cam.id) ?? null;
    const dotColor = alive === null ? "#6b7080" : alive ? "#00e5a0" : "#ff4d4d";
    const el = document.createElement("div");
    el.id = `cam-item-${cam.id}`;
    el.className = "camera-item";
    el.style.cssText = "display:flex;align-items:center;gap:8px;padding:5px 0;";
    el.innerHTML = `
      <span style="width:8px;height:8px;border-radius:50%;background:${dotColor};flex-shrink:0;transition:background .3s"></span>
      <span class="camera-item-label" style="flex:1">${cam.label}</span>
      <span class="camera-item-addr" style="font-size:10px;color:#6b7080">${cam.address || "—"}</span>
    `;
    list.appendChild(el);
  });
}

function updateCameraStatuses(camerasPayload) {
  if (!Array.isArray(camerasPayload)) return;
  camerasPayload.forEach(cam => {
    const alive = !!(cam.frame && cam.frame.length > 100);
    cameraStatus.set(cam.id, alive);
    const item = document.getElementById(`cam-item-${cam.id}`);
    if (item) {
      const dot = item.querySelector("span");
      dot.style.background = alive ? "#00e5a0" : "#ff4d4d";
    }
  });
}

async function loadCamerasFromDB() {
  try {
    const res = await fetch(`${API}/cameras/list`);
    const data = await res.json();
    cameras = data.cameras || [];
    renderCameraList();
  } catch (e) {
    document.getElementById("camera-list").innerHTML =
      '<div class="hint" style="color:var(--warn)">Ошибка загрузки</div>';
  }
}

// Автозагрузка камер при старте
loadCamerasFromDB();

// ─── Сетка камер ────────────────────────────────────────────────────────────
let _camCards = [];

function renderMultiCamera(camerasPayload) {
  if (!Array.isArray(camerasPayload) || camerasPayload.length === 0) {
    overlayLbl.textContent = "Нет активных потоков";
    overlayLbl.style.display = "block";
    videoGrid.innerHTML = "";
    _camCards = [];
    return;
  }
  overlayLbl.style.display = "none";

  if (_camCards.length !== camerasPayload.length) {
    videoGrid.innerHTML = "";
    _camCards = [];
    camerasPayload.forEach(cam => {
      const card = document.createElement("div");
      card.className = "cam-card";
      card.style.cssText = "border:1px solid var(--border);border-radius:8px;background:var(--bg2);overflow:hidden;min-height:220px;display:flex;flex-direction:column";

      const head = document.createElement("div");
      head.style.cssText = "display:flex;justify-content:space-between;align-items:center;padding:6px 10px;font-family:var(--font);font-size:11px;border-bottom:1px solid var(--border)";
      card.appendChild(head);

      const img = document.createElement("img");
      img.style.cssText = "width:100%;height:100%;object-fit:contain;background:#05070c";
      img.alt = cam.label || `Камера ${cam.id}`;
      card.appendChild(img);

      videoGrid.appendChild(card);
      _camCards.push({ card, head, img });
    });
  }

  camerasPayload.forEach((cam, i) => {
    const { head, img } = _camCards[i];
    const inFrame = cam.stats?.active_tracks ?? 0;
    head.innerHTML = `<span>${cam.label || `Камера ${cam.id}`}</span><span>${inFrame} в кадре</span>`;
    if (cam.frame && cam.frame.length > 100)
      img.src = "data:image/jpeg;base64," + cam.frame;
  });
}

// ─── WebSocket ──────────────────────────────────────────────────────────────
const wsDot   = document.getElementById("ws-dot");
const wsLabel = document.getElementById("ws-label");
let wsRetry   = 0;
let lastFrameTs = Date.now();

function connectWS() {
  wsDot.className = "status-dot connecting";
  wsLabel.textContent = "Подключение...";

  const ws = new WebSocket("ws://127.0.0.1:8000/ws");

  ws.onopen = () => {
    wsDot.className = "status-dot connected";
    wsLabel.textContent = "Подключено";
    wsRetry = 0;
  };

  ws.onmessage = e => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (Array.isArray(data.cameras)) {
      const now = Date.now();
      const dt  = now - lastFrameTs;
      lastFrameTs = now;
      renderMultiCamera(data.cameras);
      updateCameraStatuses(data.cameras);

      const fps = Math.round(1000 / Math.max(1, dt));
      document.getElementById("v-fps").textContent     = fps + " кадр/с";
      document.getElementById("v-latency").textContent = dt + " мс";
    }

    const stats = data.stats || {};
    document.getElementById("v-active").textContent = stats.active_tracks ?? "—";
    document.getElementById("v-total").textContent  = stats.visitor_count  ?? "—";
  };

  ws.onclose = () => {
    wsDot.className = "status-dot error";
    wsLabel.textContent = "Нет соединения";
    overlayLbl.textContent = "Соединение потеряно. Переподключение...";
    overlayLbl.style.display = "block";
    setTimeout(connectWS, Math.min(5000, 1000 * ++wsRetry));
  };
}

connectWS();