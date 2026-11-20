/* CUTLAB PRO — YouTube Shorts Studio Interactive NLE Frontend */

const $ = (id) => document.getElementById(id);

function esc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTime(sec) {
  const s = Math.max(0, Number(sec) || 0);
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return `${String(m).padStart(2, "0")}:${r.toFixed(2).padStart(5, "0")}`;
}

function showToast(msg, isErr = false) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("err", Boolean(isErr));
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const LOOK_CSS = {
  neutral: { b: 1, c: 1, s: 1, hue: 0, sepia: 0 },
  gaming_hdr: { b: 1.02, c: 1.08, s: 1.18, hue: 0, sepia: 0 },
  teal_orange: { b: 0.99, c: 1.1, s: 1.1, hue: -4, sepia: 0.12 },
  vice_neon: { b: 1.0, c: 1.14, s: 1.3, hue: 8, sepia: 0 },
  vintage_film: { b: 1.03, c: 0.96, s: 0.86, hue: -6, sepia: 0.24 },
  clean_daylight: { b: 1.05, c: 1.02, s: 1.06, hue: 2, sepia: 0 },
};

const state = {
  library: null,
  source: null, // { source_type, source_id, source_name, media_url, probe, waveform, peaks, silences }
  project: null,
  selectedSegIdx: 0,
  timelineTime: 0,
  playing: false,
  undoStack: [],
  redoStack: [],
  sfxAudioCache: {},
  lastPlayedSfx: new Set(),
  renderPollTimer: null,
};

function cloneProject() {
  return JSON.parse(JSON.stringify(state.project));
}

function pushUndo() {
  if (!state.project) return;
  state.undoStack.push(cloneProject());
  if (state.undoStack.length > 40) state.undoStack.shift();
  state.redoStack = [];
}

function undo() {
  if (!state.undoStack.length) return showToast("Nothing to undo");
  state.redoStack.push(cloneProject());
  state.project = state.undoStack.pop();
  state.selectedSegIdx = Math.min(state.selectedSegIdx, Math.max(0, state.project.segments.length - 1));
  syncInspectorFromProject();
  renderTimeline();
  showToast("Undid last edit");
}

function redo() {
  if (!state.redoStack.length) return showToast("Nothing to redo");
  state.undoStack.push(cloneProject());
  state.project = state.redoStack.pop();
  syncInspectorFromProject();
  renderTimeline();
  showToast("Redid edit");
}

/* ---------------- TIMELINE MATH ---------------- */
function getSegmentSpans() {
  if (!state.project || !state.project.segments.length) return { spans: [], total: 0 };
  let cursor = 0;
  const spans = state.project.segments.map((seg, idx) => {
    const sp = Math.max(0.5, Math.min(2.5, Number(seg.speed) || 1));
    const outDur = Math.max(0.12, (seg.end - seg.start) / sp);
    const item = { idx, seg, tlStart: cursor, tlEnd: cursor + outDur, outDur, speed: sp };
    cursor += outDur;
    return item;
  });
  return { spans, total: cursor };
}

function timelineToSource(tlTime) {
  const { spans, total } = getSegmentSpans();
  if (!spans.length) return null;
  const t = Math.max(0, Math.min(total - 0.001, tlTime));
  for (const sp of spans) {
    if (t >= sp.tlStart && t <= sp.tlEnd + 0.001) {
      const local = Math.max(0, t - sp.tlStart);
      return { span: sp, local, srcTime: Math.min(sp.seg.end, sp.seg.start + local * sp.speed) };
    }
  }
  const last = spans[spans.length - 1];
  return { span: last, local: last.outDur, srcTime: last.seg.end };
}

/* ---------------- LIBRARY, SYNC & MEDIA LOADING ---------------- */
let knownBotCount = null;
let knownExportCount = null;
let bgmAudioEl = null;
let bgmAuditioning = false;

function getBgmUrl(fileName) {
  if (!fileName || !state.library) return "";
  const hit = (state.library.bgm || []).find((b) => b.name === fileName);
  return hit ? hit.url : `/media/builtin-bgm/${encodeURIComponent(fileName)}`;
}

function stopBgmPlayback() {
  if (bgmAudioEl) {
    bgmAudioEl.pause();
    bgmAudioEl.currentTime = 0;
  }
  bgmAuditioning = false;
  const btn = $("btn-bgm-preview");
  if (btn) btn.innerHTML = "&#9654;";
}

function startBgmForPlayback() {
  if (!state.project || !state.project.audio.bgm_file) return;
  const url = getBgmUrl(state.project.audio.bgm_file);
  if (!url) return;
  if (!bgmAudioEl || bgmAudioEl.dataset.url !== url) {
    stopBgmPlayback();
    bgmAudioEl = new Audio(url);
    bgmAudioEl.dataset.url = url;
    bgmAudioEl.loop = true;
  }
  bgmAudioEl.volume = Math.max(0.02, Math.min(0.6, Number(state.project.audio.bgm_volume ?? 0.16)));
  bgmAudioEl.play().catch(() => {});
}

function updateSyncPill(sync) {
  if (!sync) return;
  const dot = $("sync-dot");
  const txt = $("sync-pill-text");
  dot.classList.toggle("online", Boolean(sync.bot_online));
  $("vs-live-cnt").textContent = String(sync.active_workers || 0);
  if (sync.auto_rendering_clip) {
    txt.textContent = `AUTO-EDITING: ${sync.auto_rendering_clip.slice(0, 18)}...`;
  } else if (sync.bot_online) {
    txt.textContent = `24/7 BOT LIVE (${sync.active_workers})`;
  } else {
    txt.textContent = "Bot Offline";
  }
  if (sync.sync_config) {
    $("chk-auto-sync").checked = Boolean(sync.sync_config.auto_edit_enabled);
    if ($("bot-sync-score") && document.activeElement !== $("bot-sync-score")) {
      $("bot-sync-score").value = sync.sync_config.min_viral_score ?? 70;
    }
    if ($("bot-sync-expire") && document.activeElement !== $("bot-sync-expire")) {
      $("bot-sync-expire").value = sync.sync_config.auto_delete_minutes ?? 10;
    }
    if ($("bot-sync-preset") && document.activeElement !== $("bot-sync-preset")) {
      $("bot-sync-preset").value = sync.sync_config.preset || "viral_gaming";
    }
  }
  if ($("bot-sync-summary")) {
    $("bot-sync-summary").textContent = `${sync.last_sync_event || "Live sync ready"} • Auto-rendered: ${sync.auto_rendered_count || 0}`;
  }
}

let activeView = "editor";
function switchWorkspaceView(view) {
  activeView = view;
  const isBot = view === "bot";
  document.querySelector(".studio-shell").classList.toggle("bot-mode", isBot);
  $("bot-workspace").classList.toggle("hidden", !isBot);
  $("view-switch").querySelectorAll("[data-view]").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === view);
  });
  if (isBot) {
    pausePlayback();
    loadBotView();
  } else {
    renderTimeline();
  }
}

async function loadBotView() {
  try {
    const st = await api("/api/bot/status");
    $("bot-subline").textContent = `Last poll: ${st.last_poll_at} • Active workers: ${st.active_workers} • Clips folder: ${st.clips_dir_abs}`;
    const roster = $("bot-roster-list");
    const list = st.streamers || [];
    roster.innerHTML = list
      .map((s) => {
        const w = s.worker;
        const badge = s.is_live && s.matches_target
          ? `<span class="badge" style="background:rgba(34,197,94,0.2);color:#86efac;">LIVE • ${Number(s.viewers || 0).toLocaleString()}</span>`
          : s.is_live
          ? `<span class="badge">LIVE (${esc(s.game || "Other")})</span>`
          : `<span class="badge">OFFLINE</span>`;
        const tele = w
          ? `<div class="mono" style="font-size:10.5px;color:var(--cyan);margin-top:3px;">${esc(w.state)} &bull; Chat ${w.chat_mps}/s (Z=${w.chat_z}) &bull; Audio Z=${w.audio_z} &bull; Viral=${w.last_viral_score} &bull; Clips=${w.clips_captured}</div>`
          : "";
        return `
          <div class="bot-row ${w ? "watching" : ""}">
            <div>
              <div><strong>${esc(s.name)}</strong> <span class="mono muted">[${esc(s.platform)}]</span> ${badge}</div>
              ${tele}
            </div>
            <div style="display:flex;gap:5px;flex-wrap:wrap;">
              ${w ? `<button class="btn btn-xs btn-primary" data-b-force="${esc(s.name)}">Force Clip</button>` : ""}
              <button class="btn btn-xs btn-ghost" data-b-any="${esc(s.name)}">${s.watch_any_game ? "Any Game" : "GTA 6 Only"}</button>
              <button class="btn btn-xs btn-danger" data-b-rem="${esc(s.name)}">&times;</button>
            </div>
          </div>`;
      })
      .join("");

    roster.querySelectorAll("[data-b-force]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const r = await api("/api/bot/force-clip", { method: "POST", body: JSON.stringify({ name: btn.dataset.bForce }) });
          showToast(r.message || `Clip queued for ${btn.dataset.bForce}`);
          setTimeout(() => { loadBotView(); loadLibrary(true); }, 3500);
        } catch (err) { showToast(err.message, true); }
      });
    });
    roster.querySelectorAll("[data-b-any]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api("/api/bot/streamers/toggle", { method: "POST", body: JSON.stringify({ name: btn.dataset.bAny, field: "watch_any_game" }) });
        showToast(`Toggled game filter for ${btn.dataset.bAny}`);
        loadBotView();
      });
    });
    roster.querySelectorAll("[data-b-rem]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await api("/api/bot/streamers/remove", { method: "POST", body: JSON.stringify({ name: btn.dataset.bRem }) });
        showToast(`Removed ${btn.dataset.bRem}`);
        loadBotView();
      });
    });

    const evs = st.events || [];
    $("bot-events-list").innerHTML = evs.length ? evs.map((e) => `<div>${esc(e)}</div>`).join("") : "<div>Waiting for 24/7 events...</div>";
  } catch (err) {
    $("bot-roster-list").innerHTML = `<div class="muted" style="padding:12px;">${esc(err.message)}</div>`;
  }
}

async function loadLibrary(silent = false) {
  const lib = await api("/api/library");
  state.library = lib;
  $("cnt-bot").textContent = String(lib.bot_clips.length);
  $("cnt-up").textContent = String(lib.uploads.length);
  $("cnt-exp").textContent = String(lib.exports.length);
  $("exports-dir-label").textContent = lib.exports_dir_abs || "exports/";
  updateSyncPill(lib.sync);

  if (silent) {
    if (knownBotCount !== null && lib.bot_clips.length > knownBotCount) {
      showToast(`Synced new viral clip from 24/7 Bot: ${lib.bot_clips[0].streamer}!`);
    }
    if (knownExportCount !== null && lib.exports.length > knownExportCount) {
      showToast(`New auto-edited 1080x1920 Short ready in Exports!`);
    }
  }
  knownBotCount = lib.bot_clips.length;
  knownExportCount = lib.exports.length;

  renderBotClips(lib.bot_clips);
  renderUploads(lib.uploads);
  renderExports(lib.exports);
  if (!silent) {
    renderLookGrid(lib.look_presets);
    renderSfxRack(lib.sfx);
  }
  renderBgmSelect(lib.bgm);

  // Support deep link ?clip=2026-11-18/... from the 24/7 Clipper Bot dashboard!
  if (!state.source && !state.activeSourceKey) {
    const params = new URLSearchParams(window.location.search);
    const deepClip = params.get("clip");
    if (deepClip) {
      loadSource("bot", deepClip, null, false, false);
    } else if (lib.bot_clips.length) {
      // Initial preview does NOT auto-keep
      loadSource("bot", lib.bot_clips[0].source_id, null, false, false);
    }
  }
}

function fmtCountdown(sec) {
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}m ${String(r).padStart(2, "0")}s`;
}

function isCardActive(srcType, srcId) {
  if (state.activeSourceKey) return state.activeSourceKey === `${srcType}:${srcId}`;
  return Boolean(state.source && state.source.source_type === srcType && state.source.source_id === srcId);
}

function updateActiveCardHighlights() {
  document.querySelectorAll(".media-card[data-src-id]").forEach((el) => {
    el.classList.toggle("active", isCardActive(el.dataset.srcType, el.dataset.srcId));
  });
}

function buildExpireRowHtml(c) {
  let statusHtml = "";
  if (c.kept) {
    statusHtml = `<span class="exp-pill kept mono" data-keep-id="${esc(c.source_id)}" data-keep-next="0" title="Click to unpin">&#9733; KEPT</span>`;
  } else if (c.expires_in_sec !== null && c.expires_in_sec !== undefined) {
    const urgent = c.expires_in_sec < 120;
    statusHtml = `<span class="exp-pill ${urgent ? "urgent" : ""} mono" data-exp-sec="${c.expires_in_sec}">&#9201; ${fmtCountdown(c.expires_in_sec)}</span>`;
  } else {
    statusHtml = `<span class="exp-pill mono">Saved</span>`;
  }
  return `
    ${statusHtml}
    <button type="button" class="keep-btn ${c.kept ? "kept" : ""}" data-keep-id="${esc(c.source_id)}" data-keep-next="${c.kept ? "0" : "1"}">${c.kept ? "Unkeep" : "Keep"}</button>`;
}

function renderBotClips(list) {
  const wrap = $("bot-clips-list");
  if (!list.length) {
    wrap.innerHTML = `<div class="muted" style="padding:12px;">No clips in <code>clips_to_edit/</code> right now — waiting for the next viral moment (or upload any video in Uploads).</div>`;
    return;
  }
  // If card list order hasn't changed, update in-place so background 7s polls never cancel an in-progress click!
  const existingCards = Array.from(wrap.querySelectorAll(".media-card[data-src-id]"));
  if (existingCards.length === list.length && existingCards.every((el, idx) => el.dataset.srcId === list[idx].source_id)) {
    existingCards.forEach((el, idx) => {
      const c = list[idx];
      el.classList.toggle("active", isCardActive("bot", c.source_id));
      const row = el.querySelector(".clip-expire-row");
      if (row) row.innerHTML = buildExpireRowHtml(c);
    });
    return;
  }

  wrap.innerHTML = list
    .map((c) => {
      const active = isCardActive("bot", c.source_id);
      return `
        <div class="media-card ${active ? "active" : ""}" data-src-type="bot" data-src-id="${esc(c.source_id)}">
          <img class="media-thumb" src="${esc(c.thumb_url)}" alt="" />
          <div class="media-info">
            <div class="media-title">${esc(c.streamer)} <span class="media-score">${c.viral_score}</span></div>
            <div class="media-sub mono">${c.duration_sec}s &bull; Peak ${c.peak_marker_sec}s</div>
            <div class="clip-expire-row">
              ${buildExpireRowHtml(c)}
            </div>
          </div>
        </div>`;
    })
    .join("");
}

function renderUploads(list) {
  const wrap = $("uploads-list");
  if (!list.length) {
    wrap.innerHTML = `<div class="muted" style="padding:8px;">Uploaded videos will appear here.</div>`;
    return;
  }
  wrap.innerHTML = list
    .map((u) => {
      const active = isCardActive("upload", u.source_id);
      return `
        <div class="media-card ${active ? "active" : ""}" data-src-type="upload" data-src-id="${esc(u.source_id)}">
          <div class="media-thumb" style="display:grid;place-items:center;color:var(--cyan);font-weight:800;">MP4</div>
          <div class="media-info">
            <div class="media-title">${esc(u.name)}</div>
            <div class="media-sub mono">${u.size_mb} MB</div>
          </div>
        </div>`;
    })
    .join("");
}

function renderExports(list) {
  const wrap = $("exports-list");
  if (!list.length) {
    wrap.innerHTML = `<div class="muted" style="padding:10px;">Your rendered 1080&times;1920 YouTube Shorts will appear here.</div>`;
    return;
  }
  wrap.innerHTML = list
    .map(
      (x) => `
      <div class="media-card export-card">
        <img class="media-thumb-vert" src="${esc(x.cover_url)}" alt="" />
        <div class="media-info">
          <div class="media-title">${esc(x.title)}</div>
          <div class="media-sub mono">${x.width}&times;${x.height} &bull; ${x.duration_sec}s &bull; ${x.size_mb} MB${x.bitrate_mbps ? ` (${x.bitrate_mbps} Mbps)` : ""}</div>
          <div class="export-actions">
            <button class="btn btn-xs btn-primary" data-preview-exp="${esc(x.id)}">Watch</button>
            <a class="btn btn-xs btn-ghost" href="${esc(x.mp4_url)}" download style="text-decoration:none;">MP4</a>
            <a class="btn btn-xs btn-ghost" href="${esc(x.cover_url)}" download style="text-decoration:none;">Cover</a>
            <a class="btn btn-xs btn-ghost" href="${esc(x.srt_url)}" download style="text-decoration:none;">SRT</a>
            <button class="btn btn-xs btn-danger" data-del-exp="${esc(x.mp4_name)}">&times;</button>
          </div>
        </div>
      </div>`
    )
    .join("");

  wrap.querySelectorAll("[data-preview-exp]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const item = list.find((e) => e.id === btn.dataset.previewExp);
      if (item) openExportModal(item);
    });
  });
  wrap.querySelectorAll("[data-del-exp]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/exports/delete", { method: "POST", body: JSON.stringify({ mp4_name: btn.dataset.delExp }) });
      showToast("Deleted exported Short");
      loadLibrary();
    });
  });
}

function openExportModal(item) {
  $("modal-title").textContent = `${item.title} (${item.width}x${item.height} • ${item.duration_sec}s)`;
  const titles = (item.seo && item.seo.titles ? item.seo.titles : []).map((t) => `<li>${esc(t)}</li>`).join("");
  $("modal-body").innerHTML = `
    <video src="${esc(item.mp4_url)}" poster="${esc(item.cover_url)}" controls autoplay></video>
    <div style="display:flex;flex-direction:column;gap:10px;">
      <div class="mono" style="font-size:11px;color:var(--cyan);">
        ${item.width}&times;${item.height} @ ${item.fps}fps &bull; ${item.size_mb} MB${item.bitrate_mbps ? ` (${item.bitrate_mbps} Mbps)` : ""} &bull; Look: ${esc(item.look)} &bull; ${item.loudnorm ? "-14 LUFS" : "Raw Audio"}
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        <a class="btn btn-primary btn-sm" href="${esc(item.mp4_url)}" download style="text-decoration:none;">Download ${item.width}x${item.height} MP4</a>
        <a class="btn btn-amber btn-sm" href="${esc(item.cover_url)}" download style="text-decoration:none;">Download Cover JPG</a>
        <a class="btn btn-ghost btn-sm" href="${esc(item.srt_url)}" download style="text-decoration:none;">Download .SRT</a>
      </div>
      ${titles ? `<div><strong style="font-size:11.5px;">Suggested YouTube Shorts Titles:</strong><ul style="margin:6px 0 0 16px;line-height:1.6;">${titles}</ul></div>` : ""}
      ${item.seo && item.seo.description ? `<pre style="white-space:pre-wrap;background:var(--bg-0);padding:8px;border-radius:6px;border:1px solid var(--border);font-size:11px;">${esc(item.seo.description)}</pre>` : ""}
    </div>`;
  $("export-modal").classList.remove("hidden");
}

function renderLookGrid(presets) {
  const wrap = $("look-grid");
  wrap.innerHTML = presets
    .map((p) => `<button class="look-btn" data-look="${esc(p.id)}">${esc(p.name)}</button>`)
    .join("");
  wrap.querySelectorAll("[data-look]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!state.project) return;
      pushUndo();
      state.project.color.preset = btn.dataset.look;
      syncInspectorFromProject();
    });
  });
}

function renderSfxRack(sfxList) {
  const wrap = $("sfx-rack");
  wrap.innerHTML = sfxList
    .map(
      (s) => `
      <div class="sfx-pad">
        <div class="sfx-pad-top">
          <span class="sfx-pad-name">${esc(s.name)}</span>
          <span class="sfx-pad-tag">${esc(s.duration_label)}</span>
        </div>
        <div class="sfx-pad-tag">${esc(s.tag)}</div>
        <div class="sfx-pad-btns">
          <button class="btn btn-xs btn-ghost" data-audition="${esc(s.id)}" data-url="${esc(s.url)}">&#9654; Hear</button>
          <button class="btn btn-xs btn-amber" data-drop-sfx="${esc(s.id)}">+ Drop</button>
        </div>
      </div>`
    )
    .join("");

  wrap.querySelectorAll("[data-audition]").forEach((btn) => {
    btn.addEventListener("click", () => playSfxAudio(btn.dataset.audition, btn.dataset.url, 0.7));
  });
  wrap.querySelectorAll("[data-drop-sfx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!state.project) return;
      pushUndo();
      state.project.sfx.push({
        id: `sfx_${Date.now()}`,
        sfx: btn.dataset.dropSfx,
        time: Number(state.timelineTime.toFixed(2)),
        volume: 0.5,
      });
      renderSfxTimelineList();
      renderTimeline();
      showToast(`Dropped ${btn.dataset.dropSfx.toUpperCase()} at ${fmtTime(state.timelineTime)}`);
    });
  });
}

function renderBgmSelect(bgmList) {
  const sel = $("in-bgm-select");
  const cur = state.project ? state.project.audio.bgm_file : "";
  sel.innerHTML =
    `<option value="">None (Pure Voice + SFX)</option>` +
    bgmList.map((b) => `<option value="${esc(b.name)}">${esc(b.label || b.name)}</option>`).join("");
  sel.value = cur || "";
}

function playSfxAudio(id, url, vol = 0.6) {
  try {
    const srcUrl = url || `/media/sfx/${id}.wav`;
    const audio = new Audio(srcUrl);
    audio.volume = Math.max(0, Math.min(1, vol));
    audio.play().catch(() => {});
  } catch {}
}

let activeLoadSeq = 0;

function buildMediaUrl(sourceType, sourceId) {
  const clean = String(sourceId || "").replace(/^\/+/, "");
  const encoded = clean.split("/").map(encodeURIComponent).join("/");
  return sourceType === "bot" ? `/media/bot/${encoded}` : `/media/uploads/${encoded}`;
}

function buildInstantPreviewProject(sourceType, sourceId) {
  const clipMeta = sourceType === "bot" && state.library?.bot_clips
    ? state.library.bot_clips.find((c) => c.source_id === sourceId)
    : null;
  const dur = Math.max(5, Number(clipMeta?.duration_sec || 25));
  const streamer = (clipMeta?.streamer || "clip").toLowerCase();
  const camPresets = {
    kaicenat:    { cam_x: 0.00, cam_y: 0.395, cam_w: 0.205, cam_h: 0.210, pan_x: 0.72 },
    ishowspeed:  { cam_x: 0.00, cam_y: 0.420, cam_w: 0.204, cam_h: 0.230, pan_x: 0.72 },
    jynxzi:      { cam_x: 0.00, cam_y: 0.430, cam_w: 0.242, cam_h: 0.320, pan_x: 0.72 },
    xqc:         { cam_x: 0.00, cam_y: 0.498, cam_w: 0.194, cam_h: 0.274, pan_x: 0.72 },
    summit1g:    { cam_x: 0.00, cam_y: 0.482, cam_w: 0.266, cam_h: 0.268, pan_x: 0.72 },
    caseoh_:     { cam_x: 0.00, cam_y: 0.125, cam_w: 0.210, cam_h: 0.280, pan_x: 0.72 },
    caseoh:      { cam_x: 0.00, cam_y: 0.125, cam_w: 0.210, cam_h: 0.280, pan_x: 0.72 },
    darkviperau: { cam_x: 0.812, cam_y: 0.172, cam_w: 0.174, cam_h: 0.196, pan_x: -0.68 },
  };
  const cam = camPresets[streamer] || { cam_x: 0.00, cam_y: 0.40, cam_w: 0.21, cam_h: 0.24, pan_x: 0.72 };
  const mode = streamer === "lirik" ? "blur_bg" : "streamer_top";
  return {
    title: `${streamer}_short`,
    vibe_label: "Loading audio & smart cuts...",
    framing: {
      mode,
      pan_x: mode === "streamer_top" ? cam.pan_x : 0.0,
      pan_y: 0.0,
      zoom: 1.0,
      split_ratio: 0.31,
      game_height: 0.42,
      top_pad: 0.05,
      cam_x: cam.cam_x,
      cam_y: cam.cam_y,
      cam_w: cam.cam_w,
      cam_h: cam.cam_h,
    },
    color: { preset: "gaming_hdr", exposure: 0.02, contrast: 1.06, saturation: 1.12, warmth: 0, sharpen: 0.45, vignette: false },
    audio: { highpass: true, denoise: true, presence_eq: true, compressor: true, loudnorm: true, master_volume: 1.0, bgm_file: "", bgm_volume: 0.16, bgm_ducking: true },
    hook: { enabled: false, text: "", start: 0, end: 2.8, accent: "#FFE600", font: "impact" },
    caption_style: { style: "capcut_pop", font: "arial_black", size: 64, accent: "#FFE600", y_pct: 76 },
    export: { resolution: "1440p", fps: 60, crf: 14, seamless_loop: false },
    segments: [{ id: "seg_1", start: 0, end: dur, speed: 1, zoom_mode: "none", zoom_amount: 1, transition: "none", transition_dur: 0 }],
    captions: [],
    sfx: [],
    seo: { titles: [], description: "" },
  };
}

async function loadSource(sourceType, sourceId, presetOverride = null, markKept = false, userClick = false) {
  const reqSeq = ++activeLoadSeq;
  if (activeView !== "editor") {
    switchWorkspaceView("editor");
  }
  stopBgmPlayback();
  state.activeSourceKey = `${sourceType}:${sourceId}`;
  updateActiveCardHighlights();

  // 1. Immediately switch <video> to the clicked clip so the user sees & hears it right away!
  const vid = $("src-video");
  const fastUrl = buildMediaUrl(sourceType, sourceId);
  state.project = buildInstantPreviewProject(sourceType, sourceId);
  state.source = {
    source_type: sourceType,
    source_id: sourceId,
    source_name: sourceId.split("/").pop() || sourceId,
    media_url: fastUrl,
    probe: { width: 1920, height: 1080, duration: state.project.segments[0].end, fps: 60 },
    waveform: [],
    peaks: [],
    silences: [],
  };
  state.selectedSegIdx = 0;
  state.timelineTime = 0;
  $("empty-stage").classList.add("hidden");
  $("render-done-box").classList.add("hidden");

  vid.pause();
  vid.src = fastUrl;
  vid.load();
  if (userClick) {
    state.playing = true;
    $("btn-play").innerHTML = "&#10074;&#10074; Pause";
    vid.play().catch(() => {});
  } else {
    state.playing = false;
    $("btn-play").innerHTML = "&#9654; Play";
  }

  try {
    const preset = presetOverride || $("preset-select").value;
    // Fast analysis (auto_caption: false) returns in ~0.2s (or <5ms cached) without waiting on Whisper!
    const data = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify({
        source_type: sourceType,
        source_id: sourceId,
        preset,
        auto_caption: false,
        mark_kept: Boolean(markKept),
      }),
    });
    if (reqSeq !== activeLoadSeq) return;

    state.source = data;
    state.project = {
      title: (data.meta && data.meta.streamer ? `${data.meta.streamer}_gta6` : data.source_name.replace(/\.[^.]+$/, "")),
      ...data.plan,
    };
    state.selectedSegIdx = 0;
    state.timelineTime = 0;
    state.undoStack = [];
    state.redoStack = [];

    // Wait until at least the first video frame is decoded (or 1.2s fallback)
    await new Promise((resolve) => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        vid.removeEventListener("loadeddata", done);
        vid.removeEventListener("canplay", done);
        resolve();
      };
      if (vid.readyState >= 2) return done();
      vid.addEventListener("loadeddata", done);
      vid.addEventListener("canplay", done);
      setTimeout(done, 1200);
    });
    if (reqSeq !== activeLoadSeq) return;

    $("empty-stage").classList.add("hidden");
    $("btn-export-top").disabled = false;
    $("btn-export-main").disabled = false;
    $("clip-meta-pill").textContent = `${data.probe.width}x${data.probe.height} • ${data.probe.duration.toFixed(1)}s • ${data.probe.fps}fps`;

    updateActiveCardHighlights();
    syncInspectorFromProject();
    renderTimeline();
    seekTimeline(0, true);
    if (userClick) {
      startPlayback();
    }

    const tr = data.transcript || {};
    if (tr.ok) {
      $("whisper-status").textContent = `Whisper (${tr.model}) auto-captioned ${state.project.captions.length} spoken lines.`;
      showToast(`Loaded ${data.meta?.streamer || data.source_name} (${state.project.captions.length} captions + ${state.project.vibe_label || "viral BGM"})`);
    } else if (tr.pending) {
      $("whisper-status").textContent = "Whisper auto-captioning speech in background...";
      showToast(`Playing ${data.meta?.streamer || data.source_name} — Whisper captions loading in background...`);
      // Non-blocking background Whisper transcription
      api("/api/transcribe", {
        method: "POST",
        body: JSON.stringify({
          source_type: sourceType,
          source_id: sourceId,
          segments: state.project.segments,
        }),
      })
        .then((trRes) => {
          if (reqSeq !== activeLoadSeq || !state.project) return;
          state.project.captions = trRes.cues || [];
          $("whisper-status").textContent = trRes.ok
            ? `Whisper (${trRes.model}) auto-captioned ${state.project.captions.length} spoken lines.`
            : "No spoken words detected — click + Add at Playhead anytime.";
          renderCaptionsEditor();
          renderTimeline();
        })
        .catch(() => {
          if (reqSeq !== activeLoadSeq) return;
          $("whisper-status").textContent = "Click Re-Sync Speech to generate spoken captions.";
        });
    } else {
      $("whisper-status").textContent = "No spoken words detected — click + Add at Playhead anytime.";
      showToast(`Loaded ${data.meta?.streamer || data.source_name}`);
    }
  } catch (err) {
    if (reqSeq === activeLoadSeq) showToast(err.message, true);
  }
}

/* ---------------- INSPECTOR SYNC ---------------- */
function syncInspectorFromProject() {
  const p = state.project;
  if (!p) return;

  // Framing
  const f = p.framing;
  $("framing-mode-row").querySelectorAll("[data-mode]").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === f.mode);
  });
  const usesCam = f.mode === "streamer_top" || f.mode === "split_cam";
  $("split-cam-controls").classList.toggle("hidden", !usesCam);
  $("row-game-h").classList.toggle("hidden", f.mode !== "streamer_top");
  $("row-top-pad").classList.toggle("hidden", f.mode !== "streamer_top");
  $("in-zoom").value = f.zoom;
  $("lbl-zoom").textContent = `${Number(f.zoom).toFixed(2)}x`;
  $("in-panx").value = f.pan_x;
  $("lbl-panx").textContent = `${Math.round(f.pan_x * 100)}%`;
  $("in-pany").value = f.pan_y;
  $("lbl-pany").textContent = `${Math.round(f.pan_y * 100)}%`;
  $("in-split").value = f.split_ratio ?? 0.31;
  $("lbl-split").textContent = `${Math.round((f.split_ratio ?? 0.31) * 100)}%`;
  $("in-gameh").value = f.game_height ?? 0.42;
  $("lbl-gameh").textContent = `${Math.round((f.game_height ?? 0.42) * 100)}%`;
  $("in-toppad").value = f.top_pad ?? 0.05;
  $("lbl-toppad").textContent = `${Math.round((f.top_pad ?? 0.05) * 100)}%`;
  $("in-camx").value = f.cam_x;
  $("lbl-camx").textContent = `${Math.round(f.cam_x * 100)}%`;
  $("in-camy").value = f.cam_y;
  $("lbl-camy").textContent = `${Math.round(f.cam_y * 100)}%`;
  $("in-camw").value = f.cam_w;
  $("lbl-camw").textContent = `${Math.round(f.cam_w * 100)}%`;
  $("in-camh").value = f.cam_h;
  $("lbl-camh").textContent = `${Math.round(f.cam_h * 100)}%`;

  // Selected Segment
  const seg = p.segments[state.selectedSegIdx] || p.segments[0];
  if (seg) {
    $("lbl-seg-id").textContent = `#${state.selectedSegIdx + 1} (${seg.start.toFixed(1)}s–${seg.end.toFixed(1)}s)`;
    $("in-seg-speed").value = seg.speed || 1;
    $("lbl-seg-speed").textContent = `${Number(seg.speed || 1).toFixed(2)}x`;
    $("in-seg-zoom-mode").value = seg.zoom_mode || "none";
    $("in-seg-zamt").value = seg.zoom_amount || 1.1;
    $("lbl-seg-zamt").textContent = `${Number(seg.zoom_amount || 1.1).toFixed(2)}x`;
    $("in-seg-trans").value = seg.transition || "none";
  }

  // Color
  const c = p.color;
  $("look-grid").querySelectorAll("[data-look]").forEach((b) => {
    b.classList.toggle("active", b.dataset.look === c.preset);
  });
  $("in-exp").value = c.exposure;
  $("lbl-exp").textContent = Number(c.exposure).toFixed(2);
  $("in-con").value = c.contrast;
  $("lbl-con").textContent = Number(c.contrast).toFixed(2);
  $("in-sat").value = c.saturation;
  $("lbl-sat").textContent = Number(c.saturation).toFixed(2);
  $("in-warm").value = c.warmth;
  $("lbl-warm").textContent = Number(c.warmth).toFixed(2);
  $("in-sharp").value = c.sharpen;
  $("lbl-sharp").textContent = Number(c.sharpen).toFixed(2);
  $("in-vignette").checked = Boolean(c.vignette);

  // Hook & Captions
  $("in-hook-on").checked = Boolean(p.hook.enabled);
  $("in-hook-text").value = p.hook.text || "";
  $("in-hook-start").value = p.hook.start ?? 0;
  $("in-hook-end").value = p.hook.end ?? 3.2;

  $("in-cap-style").value = p.caption_style.style || "capcut_pop";
  $("in-cap-font").value = p.caption_style.font || "arial_black";
  $("in-cap-sz").value = p.caption_style.size || 66;
  $("lbl-cap-sz").textContent = `${p.caption_style.size || 66}px`;
  $("in-cap-accent").value = p.caption_style.accent || "#ffe600";
  $("in-cap-y").value = p.caption_style.y_pct || 70;
  $("lbl-cap-y").textContent = `${Math.round(p.caption_style.y_pct || 70)}%`;
  renderCaptionsEditor();

  // Audio
  const a = p.audio;
  $("vibe-badge").textContent = `Auto Vibe: ${p.vibe_label || "Custom Audio Mix"}`;
  $("in-a-hp").checked = Boolean(a.highpass);
  $("in-a-dn").checked = Boolean(a.denoise);
  $("in-a-eq").checked = Boolean(a.presence_eq);
  $("in-a-comp").checked = Boolean(a.compressor);
  $("in-a-lufs").checked = Boolean(a.loudnorm);
  $("in-a-vol").value = a.master_volume ?? 1;
  $("lbl-a-vol").textContent = `${Math.round((a.master_volume ?? 1) * 100)}%`;
  $("in-bgm-select").value = a.bgm_file || "";
  $("in-bgm-vol").value = a.bgm_volume ?? 0.16;
  $("lbl-bgm-vol").textContent = `${Math.round((a.bgm_volume ?? 0.16) * 100)}%`;
  $("in-bgm-duck").checked = Boolean(a.bgm_ducking);
  renderSfxTimelineList();

  // Export & SEO
  $("in-proj-title").value = p.title || "short";
  $("in-exp-res").value = String(p.export.resolution || "1440p");
  $("in-exp-fps").value = String(p.export.fps || 60);
  $("in-exp-crf").value = String(p.export.crf || 14);
  $("in-exp-loop").checked = Boolean(p.export.seamless_loop);

  const seo = p.seo || { titles: [], description: "" };
  $("seo-titles").innerHTML = (seo.titles || [])
    .map((t) => `<button class="seo-title-btn" data-copy-txt="${esc(t)}">${esc(t)}</button>`)
    .join("");
  $("seo-titles").querySelectorAll("[data-copy-txt]").forEach((btn) => {
    btn.addEventListener("click", () => {
      navigator.clipboard?.writeText(btn.dataset.copyTxt).catch(() => {});
      showToast("Copied YouTube Shorts title!");
    });
  });
  $("seo-desc").value = seo.description || "";
}

function renderCaptionsEditor() {
  const wrap = $("captions-editor-list");
  if (!state.project) return;
  const list = state.project.captions || [];
  if (!list.length) {
    wrap.innerHTML = `<div class="muted" style="font-size:11px;">No caption cues yet. Click "+ Add at Playhead".</div>`;
    return;
  }
  wrap.innerHTML = list
    .map(
      (c, i) => `
      <div class="cue-item" data-cap-idx="${i}">
        <div class="cue-top">
          <input type="number" step="0.1" min="0" value="${Number(c.start).toFixed(1)}" data-cap-field="start" />
          <span class="muted">&rarr;</span>
          <input type="number" step="0.1" min="0.1" value="${Number(c.end).toFixed(1)}" data-cap-field="end" />
          <button class="btn btn-xs btn-ghost" data-cap-jump="${c.start}">Go</button>
          <button class="btn btn-xs btn-danger" data-cap-del="${i}" style="margin-left:auto;">&times;</button>
        </div>
        <input type="text" class="text-in" value="${esc(c.text)}" data-cap-field="text" placeholder="Caption text..." />
      </div>`
    )
    .join("");

  wrap.querySelectorAll("[data-cap-idx]").forEach((item) => {
    const idx = Number(item.dataset.capIdx);
    item.querySelectorAll("[data-cap-field]").forEach((inp) => {
      inp.addEventListener("input", () => {
        const f = inp.dataset.capField;
        state.project.captions[idx][f] = f === "text" ? inp.value : Math.max(0, Number(inp.value) || 0);
        renderTimeline();
      });
    });
  });
  wrap.querySelectorAll("[data-cap-jump]").forEach((b) => {
    b.addEventListener("click", () => seekTimeline(Number(b.dataset.capJump)));
  });
  wrap.querySelectorAll("[data-cap-del]").forEach((b) => {
    b.addEventListener("click", () => {
      pushUndo();
      state.project.captions.splice(Number(b.dataset.capDel), 1);
      renderCaptionsEditor();
      renderTimeline();
    });
  });
}

function renderSfxTimelineList() {
  const wrap = $("sfx-timeline-list");
  if (!state.project) return;
  const list = state.project.sfx || [];
  if (!list.length) {
    wrap.innerHTML = `<div class="muted" style="font-size:11px;">No SFX placed yet. Click "+ Drop" above.</div>`;
    return;
  }
  wrap.innerHTML = list
    .map(
      (s, i) => `
      <div class="cue-item">
        <div class="cue-top">
          <strong class="amber mono" style="font-size:11px; min-width:54px;">${esc(s.sfx.toUpperCase())}</strong>
          <input type="number" step="0.1" min="0" value="${Number(s.time).toFixed(2)}" data-sfx-time="${i}" />
          <span class="muted">s</span>
          <button class="btn btn-xs btn-ghost" data-sfx-go="${s.time}">Go</button>
          <button class="btn btn-xs btn-danger" data-sfx-del="${i}" style="margin-left:auto;">&times;</button>
        </div>
      </div>`
    )
    .join("");
  wrap.querySelectorAll("[data-sfx-time]").forEach((inp) => {
    inp.addEventListener("input", () => {
      state.project.sfx[Number(inp.dataset.sfxTime)].time = Math.max(0, Number(inp.value) || 0);
      renderTimeline();
    });
  });
  wrap.querySelectorAll("[data-sfx-go]").forEach((b) => {
    b.addEventListener("click", () => seekTimeline(Number(b.dataset.sfxGo)));
  });
  wrap.querySelectorAll("[data-sfx-del]").forEach((b) => {
    b.addEventListener("click", () => {
      pushUndo();
      state.project.sfx.splice(Number(b.dataset.sfxDel), 1);
      renderSfxTimelineList();
      renderTimeline();
    });
  });
}

/* ---------------- TIMELINE RENDERING ---------------- */
function renderTimeline() {
  const { spans, total } = getSegmentSpans();
  $("tc-tot").textContent = fmtTime(total);
  $("tl-summary").textContent = `${spans.length} segment${spans.length === 1 ? "" : "s"} • ${total.toFixed(1)}s output • ${(state.project?.captions || []).length} captions • ${(state.project?.sfx || []).length} SFX`;

  // Ruler
  const ruler = $("tl-ruler");
  const rw = ruler.clientWidth || 800;
  ruler.width = rw;
  ruler.height = 24;
  const rctx = ruler.getContext("2d");
  rctx.clearRect(0, 0, rw, 24);
  if (total > 0) {
    const step = total <= 15 ? 1 : total <= 40 ? 2 : 5;
    rctx.fillStyle = "#8896b3";
    rctx.strokeStyle = "#2c364b";
    rctx.font = "10px ui-monospace, monospace";
    for (let t = 0; t <= total; t += step) {
      const x = (t / total) * rw;
      rctx.beginPath();
      rctx.moveTo(x, 12);
      rctx.lineTo(x, 24);
      rctx.stroke();
      rctx.fillText(`${t}s`, x + 3, 11);
    }
  }

  // Track V1 (Video Segments)
  const v1 = $("track-v1");
  v1.innerHTML = spans
    .map((sp) => {
      const left = total > 0 ? (sp.tlStart / total) * 100 : 0;
      const width = total > 0 ? (sp.outDur / total) * 100 : 0;
      const tags = [];
      if (Math.abs(sp.speed - 1) > 0.02) tags.push(`${sp.speed.toFixed(1)}x`);
      if (sp.seg.zoom_mode && sp.seg.zoom_mode !== "none") tags.push(sp.seg.zoom_mode.toUpperCase());
      if (sp.seg.transition && sp.seg.transition !== "none") tags.push(sp.seg.transition.toUpperCase());
      return `
        <div class="seg-block ${sp.idx === state.selectedSegIdx ? "selected" : ""}"
             style="left:${left}%; width:calc(${width}% - 2px);" data-seg-idx="${sp.idx}">
          <div class="seg-block-head">
            <span>#${sp.idx + 1}</span>
            <span class="mono">${sp.outDur.toFixed(1)}s</span>
          </div>
          <div class="seg-tags">${esc(tags.join(" • ") || "CUT")}</div>
        </div>`;
    })
    .join("");
  v1.querySelectorAll("[data-seg-idx]").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      state.selectedSegIdx = Number(el.dataset.segIdx);
      syncInspectorFromProject();
      renderTimeline();
    });
  });

  // Track A1 (Audio Waveform + Dead Air + Peaks mapped to timeline)
  const a1 = $("track-a1");
  const aw = a1.clientWidth || 800;
  a1.width = aw;
  a1.height = 46;
  const actx = a1.getContext("2d");
  actx.clearRect(0, 0, aw, 46);
  if (state.source && total > 0) {
    const wf = state.source.waveform || [];
    const srcDur = Math.max(0.1, state.source.probe.duration);
    const silences = state.source.silences || [];
    const peaks = state.source.peaks || [];

    // Draw waveform bars sampled per output pixel column
    const barStep = 3;
    for (let x = 0; x < aw; x += barStep) {
      const tlT = (x / aw) * total;
      const mapped = timelineToSource(tlT);
      if (!mapped) continue;
      const wIdx = Math.min(wf.length - 1, Math.max(0, Math.floor((mapped.srcTime / srcDur) * wf.length)));
      const amp = wf[wIdx] || 0.05;
      const inSilence = silences.some((s) => mapped.srcTime >= s.start && mapped.srcTime <= s.end);
      const h = Math.max(3, amp * 38);
      actx.fillStyle = inSilence ? "rgba(244,63,94,0.45)" : "rgba(56,189,248,0.75)";
      actx.fillRect(x, (46 - h) / 2, 2, h);
    }

    // Highlight viral peaks in amber
    peaks.forEach((pk) => {
      for (const sp of spans) {
        if (pk.time >= sp.seg.start && pk.time <= sp.seg.end) {
          const outT = sp.tlStart + (pk.time - sp.seg.start) / sp.speed;
          const px = (outT / total) * aw;
          actx.fillStyle = "#ffe600";
          actx.fillRect(px - 1, 0, 2, 46);
        }
      }
    });
  }

  // Track T1 (Hook + Captions)
  const t1 = $("track-t1");
  const tBlocks = [];
  if (state.project && total > 0) {
    const hk = state.project.hook;
    if (hk && hk.enabled && hk.text) {
      const l = (Math.max(0, hk.start) / total) * 100;
      const w = (Math.max(0.2, Math.min(total, hk.end) - Math.max(0, hk.start)) / total) * 100;
      tBlocks.push(`<div class="cap-block hook-block" style="left:${l}%;width:${w}%;">HOOK: ${esc(hk.text)}</div>`);
    }
    (state.project.captions || []).forEach((c) => {
      const l = (Math.max(0, c.start) / total) * 100;
      const w = (Math.max(0.2, Math.min(total, c.end) - Math.max(0, c.start)) / total) * 100;
      tBlocks.push(`<div class="cap-block" style="left:${l}%;width:${w}%;">${esc(c.text)}</div>`);
    });
  }
  t1.innerHTML = tBlocks.join("");

  // Track FX (SFX pins)
  const fx = $("track-fx");
  fx.innerHTML =
    total > 0 && state.project
      ? (state.project.sfx || [])
          .map((s) => {
            const l = (Math.max(0, Math.min(total, s.time)) / total) * 100;
            return `<div class="sfx-pin" style="left:${l}%;">${esc(s.sfx.toUpperCase())}</div>`;
          })
          .join("")
      : "";

  updatePlayheadUI();
}

function updatePlayheadUI() {
  const { total } = getSegmentSpans();
  const pct = total > 0 ? (Math.max(0, Math.min(total, state.timelineTime)) / total) * 100 : 0;
  $("playhead").style.left = `${pct}%`;
  $("tc-cur").textContent = fmtTime(state.timelineTime);
}

/* ---------------- PLAYBACK & 60FPS CANVAS COMPOSITOR ---------------- */
function seekTimeline(t, force = false) {
  const { total } = getSegmentSpans();
  state.timelineTime = Math.max(0, Math.min(Math.max(0, total - 0.01), t));
  const mapped = timelineToSource(state.timelineTime);
  const vid = $("src-video");
  if (mapped && vid.readyState >= 1) {
    vid.playbackRate = mapped.span.speed;
    const targetT = Math.max(0.001, mapped.srcTime);
    if (force || Math.abs(vid.currentTime - targetT) > 0.04) {
      try {
        vid.currentTime = targetT;
      } catch {}
    }
  }
  updatePlayheadUI();
}

function startPlayback() {
  if (!state.project || !state.source) return;
  const { total } = getSegmentSpans();
  if (state.timelineTime >= total - 0.08) state.timelineTime = 0;
  state.playing = true;
  state.lastPlayedSfx.clear();
  $("btn-play").innerHTML = "&#10074;&#10074; Pause";
  const mapped = timelineToSource(state.timelineTime);
  const vid = $("src-video");
  if (mapped) {
    vid.playbackRate = mapped.span.speed;
    vid.currentTime = mapped.srcTime;
    vid.volume = Math.min(1, Number(state.project.audio.master_volume ?? 1));
    vid.play().catch(() => {});
  }
  startBgmForPlayback();
}

function pausePlayback() {
  state.playing = false;
  $("btn-play").innerHTML = "&#9654; Play";
  $("src-video").pause();
  stopBgmPlayback();
}

function togglePlayback() {
  if (state.playing) pausePlayback();
  else startPlayback();
}

let lastFrameTs = performance.now();
function renderLoop(nowTs) {
  const dt = Math.min(0.1, (nowTs - lastFrameTs) / 1000);
  lastFrameTs = nowTs;

  if (state.playing && state.project) {
    const { spans, total } = getSegmentSpans();
    const prevT = state.timelineTime;
    state.timelineTime += dt;
    if (state.timelineTime >= total) {
      state.timelineTime = 0;
      state.lastPlayedSfx.clear();
      if (!state.project.export.seamless_loop) {
        pausePlayback();
      }
    }
    const mapped = timelineToSource(state.timelineTime);
    const vid = $("src-video");
    if (mapped && state.playing) {
      if (Math.abs(vid.playbackRate - mapped.span.speed) > 0.02) {
        vid.playbackRate = mapped.span.speed;
      }
      if (Math.abs(vid.currentTime - mapped.srcTime) > 0.22) {
        vid.currentTime = mapped.srcTime;
      }
      if (vid.paused) vid.play().catch(() => {});
    }
    // Trigger live SFX audio preview as playhead crosses pins!
    (state.project.sfx || []).forEach((s) => {
      if (s.time >= prevT && s.time < state.timelineTime && !state.lastPlayedSfx.has(s.id)) {
        state.lastPlayedSfx.add(s.id);
        playSfxAudio(s.sfx, null, s.volume ?? 0.5);
      }
    });
    updatePlayheadUI();
  }

  drawPreviewCanvas();
  requestAnimationFrame(renderLoop);
}

function drawPreviewCanvas() {
  const canvas = $("preview-canvas");
  const ctx = canvas.getContext("2d");
  const cw = canvas.width;
  const ch = canvas.height;
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, cw, ch);

  const vid = $("src-video");
  if (!state.project || !vid.videoWidth || vid.readyState < 1) return;

  const p = state.project;
  const mapped = timelineToSource(state.timelineTime);
  const seg = mapped ? mapped.span.seg : p.segments[0];
  const localT = mapped ? mapped.local : 0;
  const segDur = mapped ? mapped.span.outDur : 1;

  // Dynamic zoom multiplier matching FFmpeg render_engine.py math
  let dynZoom = 1.0;
  const zm = seg?.zoom_mode || "none";
  const zAmt = Math.max(1, Number(seg?.zoom_amount || 1));
  if (zm === "punch") dynZoom = 1 + (zAmt - 1) * Math.sin(Math.PI * Math.min(localT / 0.45, 1));
  else if (zm === "push_in") dynZoom = 1 + (zAmt - 1) * Math.min(localT / Math.max(0.3, segDur), 1);
  else if (zm === "zoom_hold") dynZoom = 1 + (zAmt - 1) * (1 - 0.45 * Math.min(localT / Math.max(0.3, segDur), 1));

  // Color filter preview
  const lk = LOOK_CSS[p.color.preset] || LOOK_CSS.neutral;
  const bright = Math.max(0.4, lk.b + Number(p.color.exposure || 0));
  const cont = Math.max(0.4, lk.c * Number(p.color.contrast || 1));
  const sat = Math.max(0, lk.s * Number(p.color.saturation || 1));
  const warmSepia = Math.max(0, lk.sepia + Math.max(0, Number(p.color.warmth || 0)) * 0.9);
  const coolHue = lk.hue + Math.min(0, Number(p.color.warmth || 0)) * 45;

  ctx.save();
  ctx.filter = `brightness(${bright.toFixed(2)}) contrast(${cont.toFixed(2)}) saturate(${sat.toFixed(2)}) sepia(${warmSepia.toFixed(2)}) hue-rotate(${coolHue.toFixed(1)}deg)`;

  // Apply dynamic zoom around center
  ctx.translate(cw / 2, ch / 2);
  ctx.scale(dynZoom, dynZoom);
  ctx.translate(-cw / 2, -ch / 2);

  const vw = vid.videoWidth;
  const vh = vid.videoHeight;
  const f = p.framing;
  const baseZoom = Math.max(1, Number(f.zoom || 1));

  if (f.mode === "streamer_top") {
    const topPad = Math.round(ch * Number(f.top_pad ?? 0.05));
    const camH = Math.round(ch * Number(f.split_ratio ?? 0.31));
    let gameH = Math.round(ch * Number(f.game_height ?? 0.42));
    if (topPad + camH + gameH > ch - 20) gameH = ch - 20 - topPad - camH;
    const gameY = topPad + camH;

    // 1. Blurred darkened ambient gameplay background (top notch + bottom YouTube HUD zone)
    ctx.save();
    ctx.filter = `${ctx.filter} blur(16px) brightness(0.68)`;
    drawCover(ctx, vid, 0, 0, vw, vh, 0, 0, cw, ch, 0.5, 0.5, 1.0);
    ctx.restore();

    // 2. Streamer Facecam cleanly framed at the TOP right above the gameplay
    const sx = vw * Number(f.cam_x ?? 0.0);
    const sy = vh * Number(f.cam_y ?? 0.40);
    const sw = Math.max(20, vw * Number(f.cam_w ?? 0.21));
    const sh = Math.max(20, vh * Number(f.cam_h ?? 0.24));
    drawCover(ctx, vid, sx, sy, sw, sh, 0, topPad, cw, camH, 0.5, 0.5, 1.0);

    // 3. Wide crisp Gameplay right in the MIDDLE beneath the streamer
    drawCover(
      ctx, vid, 0, 0, vw, vh, 0, gameY, cw, gameH,
      (Number(f.pan_x || 0) + 1) / 2,
      (Number(f.pan_y || 0) + 1) / 2,
      baseZoom
    );

    // Crisp divider bars
    ctx.fillStyle = "rgba(0,0,0,0.95)";
    ctx.fillRect(0, gameY - 2, cw, 4);
    ctx.fillRect(0, gameY + gameH - 1, cw, 3);
  } else if (f.mode === "split_cam") {
    const topH = Math.round(ch * Number(f.split_ratio || 0.34));
    const botH = ch - topH;
    // Top facecam crop
    const sx = vw * Number(f.cam_x || 0.0);
    const sy = vh * Number(f.cam_y || 0.40);
    const sw = Math.max(20, vw * Number(f.cam_w || 0.21));
    const sh = Math.max(20, vh * Number(f.cam_h || 0.24));
    drawCover(ctx, vid, sx, sy, sw, sh, 0, 0, cw, topH, 0.5, 0.5, 1.0);
    // Bottom gameplay crop
    drawCover(ctx, vid, 0, 0, vw, vh, 0, topH, cw, botH, (Number(f.pan_x || 0) + 1) / 2, 0.5, baseZoom);
    ctx.fillStyle = "#000";
    ctx.fillRect(0, topH - 2, cw, 4);
  } else if (f.mode === "blur_bg") {
    ctx.save();
    ctx.filter = `${ctx.filter} blur(14px) brightness(0.76)`;
    drawCover(ctx, vid, 0, 0, vw, vh, 0, 0, cw, ch, 0.5, 0.5, 1.0);
    ctx.restore();
    const fgW = cw * baseZoom;
    const fgH = (vh / vw) * fgW;
    const oy = Number(f.pan_y || 0) * (ch * 0.14);
    ctx.drawImage(vid, (cw - fgW) / 2, (ch - fgH) / 2 + oy, fgW, fgH);
  } else {
    drawCover(
      ctx, vid, 0, 0, vw, vh, 0, 0, cw, ch,
      (Number(f.pan_x || 0) + 1) / 2,
      (Number(f.pan_y || 0) + 1) / 2,
      baseZoom
    );
  }
  ctx.restore();

  // Also keep the interactive 16:9 facecam crop-box picker canvas live!
  drawCamPickerCanvas(vid, f);

  // Vignette preview
  if (p.color.vignette || p.color.preset === "teal_orange" || p.color.preset === "vice_neon" || p.color.preset === "vintage_film") {
    const grd = ctx.createRadialGradient(cw / 2, ch / 2, cw * 0.28, cw / 2, ch / 2, ch * 0.62);
    grd.addColorStop(0, "rgba(0,0,0,0)");
    grd.addColorStop(1, "rgba(0,0,0,0.48)");
    ctx.fillStyle = grd;
    ctx.fillRect(0, 0, cw, ch);
  }

  // Flash White transition preview
  if (seg && seg.transition === "flash" && localT < 0.22) {
    ctx.fillStyle = `rgba(255,255,255,${(1 - localT / 0.22) * 0.88})`;
    ctx.fillRect(0, 0, cw, ch);
  }

  // Live 3-Second Retention Hook Banner Preview
  const hk = p.hook;
  if (hk && hk.enabled && hk.text && state.timelineTime >= hk.start && state.timelineTime <= hk.end) {
    let hookY = ch * 0.135;
    if (f.mode === "streamer_top") {
      hookY = ch * (Number(f.top_pad ?? 0.05) + Number(f.split_ratio ?? 0.31)) - 26;
    } else if (f.mode === "split_cam") {
      hookY = ch * Number(f.split_ratio ?? 0.34) - 26;
    }
    drawHookPreview(ctx, cw, ch, hk.text, p.caption_style.accent || "#ffe600", hookY);
  }

  // Live Animated Captions Preview
  const activeCue = (p.captions || []).find((c) => state.timelineTime >= c.start && state.timelineTime <= c.end);
  if (activeCue && activeCue.text) {
    drawCaptionPreview(ctx, cw, ch, activeCue, p.caption_style, state.timelineTime);
  }
}

function drawCover(ctx, img, sx, sy, sw, sh, dx, dy, dw, dh, panX, panY, zoom) {
  const srcRatio = sw / sh;
  const dstRatio = dw / dh;
  let cropW = sw;
  let cropH = sh;
  if (srcRatio > dstRatio) cropW = sh * dstRatio;
  else cropH = sw / dstRatio;
  cropW /= zoom;
  cropH /= zoom;
  const cx = sx + (sw - cropW) * panX;
  const cy = sy + (sh - cropH) * panY;
  ctx.drawImage(img, cx, cy, cropW, cropH, dx, dy, dw, dh);
}

function drawCamPickerCanvas(vid, f) {
  const c = $("cam-picker-canvas");
  if (!c || c.offsetParent === null || !vid.videoWidth) return;
  const ctx = c.getContext("2d");
  const w = c.width;
  const h = c.height;
  ctx.drawImage(vid, 0, 0, w, h);
  // Darken outside the selected webcam rectangle so it pops visually
  const rx = Math.round(Number(f.cam_x || 0) * w);
  const ry = Math.round(Number(f.cam_y || 0) * h);
  const rw = Math.max(12, Math.round(Number(f.cam_w || 0.21) * w));
  const rh = Math.max(12, Math.round(Number(f.cam_h || 0.24) * h));
  ctx.fillStyle = "rgba(0,0,0,0.48)";
  ctx.fillRect(0, 0, w, ry);
  ctx.fillRect(0, ry + rh, w, h - ry - rh);
  ctx.fillRect(0, ry, rx, rh);
  ctx.fillRect(rx + rw, ry, w - rx - rw, rh);
  ctx.strokeStyle = "#ffe600";
  ctx.lineWidth = 2;
  ctx.strokeRect(rx + 1, ry + 1, rw - 2, rh - 2);
  ctx.fillStyle = "#ffe600";
  ctx.font = "bold 9px ui-monospace, monospace";
  ctx.fillText("STREAMER CAM", Math.min(w - 72, Math.max(4, rx + 3)), Math.max(11, ry - 4));
}

function drawHookPreview(ctx, cw, ch, text, accent, customY = null) {
  const up = text.trim().toUpperCase();
  ctx.save();
  ctx.font = "900 26px Impact, 'Arial Black', sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const maxW = cw - 90;
  const lines = wrapCanvasText(ctx, up, maxW).slice(0, 2);
  const boxW = Math.min(cw - 44, Math.max(...lines.map((l) => ctx.measureText(l).width)) + 44);
  const boxH = lines.length * 34 + 18;
  const x = (cw - boxW) / 2;
  const y = Math.max(45, Math.min(ch - boxH - 160, customY !== null ? customY : ch * 0.135));

  ctx.fillStyle = "rgba(14, 16, 24, 0.9)";
  ctx.strokeStyle = accent;
  ctx.lineWidth = 3;
  roundRect(ctx, x, y, boxW, boxH, 12);
  ctx.fill();
  ctx.stroke();

  lines.forEach((ln, i) => {
    ctx.lineWidth = 4;
    ctx.strokeStyle = "#000";
    ctx.fillStyle = i === 0 ? accent : "#fff";
    const ly = y + 26 + i * 34;
    ctx.strokeText(ln, cw / 2, ly);
    ctx.fillText(ln, cw / 2, ly);
  });
  ctx.restore();
}

function drawCaptionPreview(ctx, cw, ch, cue, styleCfg, tlTime) {
  const style = styleCfg.style || "capcut_pop";
  const accent = styleCfg.accent || "#ffe600";
  const sz = Math.round((Number(styleCfg.size || 66) / 2) * 0.96);
  const upper = style !== "clean_minimal";
  const raw = upper ? cue.text.trim().toUpperCase() : cue.text.trim();
  const words = raw.split(/\s+/).filter(Boolean);
  if (!words.length) return;

  // Use exact Whisper per-word timestamps when present so every syllable pops on beat!
  let activeIdx = -1;
  if (style === "capcut_pop") {
    const wt = cue.words || [];
    if (wt.length === words.length) {
      activeIdx = wt.findIndex((w) => tlTime >= Number(w.start) && tlTime <= Number(w.end) + 0.08);
      if (activeIdx === -1) {
        for (let i = wt.length - 1; i >= 0; i--) {
          if (tlTime >= Number(wt[i].start)) { activeIdx = i; break; }
        }
        if (activeIdx === -1) activeIdx = 0;
      }
    } else {
      const progress = Math.max(0, Math.min(0.999, (tlTime - cue.start) / Math.max(0.1, cue.end - cue.start)));
      activeIdx = Math.floor(progress * words.length);
    }
  }

  ctx.save();
  ctx.font = `900 ${sz}px 'Arial Black', Impact, sans-serif`;
  ctx.textBaseline = "middle";
  const safeLeft = 36;
  const safeRight = cw - 78;
  const maxW = safeRight - safeLeft;
  const cx = safeLeft + maxW / 2;
  const cy = ch * (Number(styleCfg.y_pct || 70) / 100);

  // Word-level wrapping
  const lines = [];
  let cur = [];
  words.forEach((w, idx) => {
    const trial = [...cur.map((x) => x.w), w].join(" ");
    if (cur.length && ctx.measureText(trial).width > maxW) {
      lines.push(cur);
      cur = [{ idx, w }];
    } else cur.push({ idx, w });
  });
  if (cur.length) lines.push(cur);

  const lineH = sz * 1.24;
  const topY = cy - ((lines.length - 1) * lineH) / 2;
  const spW = ctx.measureText(" ").width;

  if (style === "variety_pill") {
    const widest = Math.max(...lines.map((ln) => ctx.measureText(ln.map((x) => x.w).join(" ")).width));
    ctx.fillStyle = "rgba(10,12,18,0.84)";
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    roundRect(ctx, cx - widest / 2 - 16, topY - sz * 0.72, widest + 32, lines.length * lineH + 14, 12);
    ctx.fill();
    ctx.stroke();
  }

  lines.forEach((ln, li) => {
    const lw = ln.reduce((acc, x) => acc + ctx.measureText(x.w).width, 0) + spW * Math.max(0, ln.length - 1);
    let x = cx - lw / 2;
    const y = topY + li * lineH;
    ln.forEach(({ idx, w }) => {
      const ww = ctx.measureText(w).width;
      const isAct = idx === activeIdx;
      if (style === "capcut_pop" && isAct) {
        ctx.fillStyle = accent;
        roundRect(ctx, x - 6, y - sz * 0.56, ww + 12, sz * 1.12, 8);
        ctx.fill();
        ctx.fillStyle = "#090b0f";
        ctx.fillText(w, x, y);
      } else {
        ctx.lineWidth = style === "clean_minimal" ? 3 : 6;
        ctx.strokeStyle = "#000";
        ctx.fillStyle = isAct ? accent : "#fff";
        ctx.strokeText(w, x, y);
        ctx.fillText(w, x, y);
      }
      x += ww + spW;
    });
  });
  ctx.restore();
}

function wrapCanvasText(ctx, text, maxW) {
  const words = text.split(/\s+/);
  const lines = [];
  let cur = "";
  for (const w of words) {
    const t = cur ? `${cur} ${w}` : w;
    if (cur && ctx.measureText(t).width > maxW) {
      lines.push(cur);
      cur = w;
    } else cur = t;
  }
  if (cur) lines.push(cur);
  return lines;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* ---------------- NLE TIMELINE EDITING OPERATIONS ---------------- */
function splitAtPlayhead() {
  if (!state.project) return;
  const mapped = timelineToSource(state.timelineTime);
  if (!mapped) return;
  const { span, srcTime } = mapped;
  const seg = span.seg;
  if (srcTime - seg.start < 0.25 || seg.end - srcTime < 0.25) {
    return showToast("Move playhead inside a segment (at least 0.25s from edges) to split", true);
  }
  pushUndo();
  const splitPt = Number(srcTime.toFixed(2));
  const segA = { ...seg, end: splitPt };
  const segB = { ...seg, id: `seg_${Date.now()}`, start: splitPt, transition: "none", transition_dur: 0 };
  state.project.segments.splice(span.idx, 1, segA, segB);
  state.selectedSegIdx = span.idx + 1;
  syncInspectorFromProject();
  renderTimeline();
  showToast(`Razor split at ${fmtTime(state.timelineTime)}`);
}

function deleteSelectedSegment() {
  if (!state.project) return;
  if (state.project.segments.length <= 1) {
    return showToast("Cannot delete the last remaining segment", true);
  }
  pushUndo();
  state.project.segments.splice(state.selectedSegIdx, 1);
  state.selectedSegIdx = Math.max(0, state.selectedSegIdx - 1);
  syncInspectorFromProject();
  renderTimeline();
  seekTimeline(Math.min(state.timelineTime, getSegmentSpans().total - 0.05));
  showToast("Ripple-deleted segment");
}

function trimSelectedEdge(which) {
  if (!state.project) return;
  const mapped = timelineToSource(state.timelineTime);
  if (!mapped) return;
  const seg = state.project.segments[state.selectedSegIdx];
  if (!seg) return;
  if (mapped.span.idx !== state.selectedSegIdx) {
    return showToast("Move playhead over the selected segment to trim", true);
  }
  pushUndo();
  const pt = Number(mapped.srcTime.toFixed(2));
  if (which === "in" && seg.end - pt >= 0.3) seg.start = pt;
  else if (which === "out" && pt - seg.start >= 0.3) seg.end = pt;
  else return showToast("Trim range too short (min 0.3s)", true);
  syncInspectorFromProject();
  renderTimeline();
  showToast(`Trimmed segment ${which.toUpperCase()} to ${pt.toFixed(2)}s`);
}

function cutDeadAirNow() {
  if (!state.source || !state.project) return;
  const sils = state.source.silences || [];
  if (!sils.length) return showToast("No long silent pauses found in this clip!");
  pushUndo();
  const dur = state.source.probe.duration;
  const pad = 0.14;
  const out = [];
  let cur = 0;
  for (const s of [...sils].sort((a, b) => a.start - b.start)) {
    const ss = Math.max(cur, s.start + pad);
    const se = Math.min(dur, s.end - pad);
    if (se - ss < 0.45) continue;
    if (ss - cur >= 0.4) {
      out.push({ id: `seg_${out.length + 1}`, start: Number(cur.toFixed(2)), end: Number(ss.toFixed(2)), speed: 1, zoom_mode: out.length % 2 ? "punch" : "none", zoom_amount: 1.1, transition: "none", transition_dur: 0 });
    }
    cur = Math.max(cur, se);
  }
  if (dur - cur >= 0.4) {
    out.push({ id: `seg_${out.length + 1}`, start: Number(cur.toFixed(2)), end: Number(dur.toFixed(2)), speed: 1, zoom_mode: "none", zoom_amount: 1, transition: "none", transition_dur: 0 });
  }
  if (out.length) {
    state.project.segments = out;
    state.selectedSegIdx = 0;
    syncInspectorFromProject();
    renderTimeline();
    seekTimeline(0);
    showToast(`Cut ${sils.length} dead-air pause(s) into ${out.length} snappy segments!`);
  }
}

/* ---------------- UPLOAD & EXPORT ---------------- */
async function uploadMediaFile(file, kind = "video") {
  if (!file) return;
  showToast(`Uploading ${file.name}...`);
  try {
    const res = await fetch(`/api/upload?kind=${encodeURIComponent(kind)}&filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      body: file,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Upload failed");
    await loadLibrary();
    if (kind === "bgm") {
      if (state.project) {
        state.project.audio.bgm_file = data.name;
        $("in-bgm-select").value = data.name;
      }
      showToast(`Uploaded BGM: ${data.name}`);
    } else {
      showToast(`Uploaded ${data.name} — loading into studio...`);
      await loadSource("upload", data.source_id);
    }
  } catch (err) {
    showToast(err.message, true);
  }
}

async function startExport() {
  if (!state.source || !state.project) return;
  pausePlayback();
  // Automatically pin/keep a bot clip when the user actually chooses to export/render it
  if (state.source.source_type === "bot" && state.source.source_id) {
    api("/api/clips/keep", {
      method: "POST",
      body: JSON.stringify({ source_id: state.source.source_id, kept: true }),
    })
      .then((res) => {
        if (state.library && res.bot_clips) {
          state.library.bot_clips = res.bot_clips;
          renderBotClips(res.bot_clips);
        }
      })
      .catch(() => {});
  }
  // Switch inspector to Export tab so user sees live progress
  switchInspectorTab("export");
  $("btn-export-top").disabled = true;
  $("btn-export-main").disabled = true;
  $("render-progress-box").classList.remove("hidden");
  $("render-done-box").classList.add("hidden");
  $("render-bar").style.width = "4%";
  $("render-pct").textContent = "4%";
  $("render-stage").textContent = "Submitting render job...";

  try {
    const job = await api("/api/render", {
      method: "POST",
      body: JSON.stringify({
        source_type: state.source.source_type,
        source_id: state.source.source_id,
        project: state.project,
      }),
    });
    pollRenderJob(job.job_id);
  } catch (err) {
    $("btn-export-top").disabled = false;
    $("btn-export-main").disabled = false;
    showToast(err.message, true);
  }
}

function pollRenderJob(jobId) {
  clearInterval(state.renderPollTimer);
  state.renderPollTimer = setInterval(async () => {
    try {
      const j = await api(`/api/render/${jobId}`);
      $("render-bar").style.width = `${j.progress || 0}%`;
      $("render-pct").textContent = `${Math.round(j.progress || 0)}%`;
      $("render-stage").textContent = j.stage || "Encoding...";

      if (j.status === "done") {
        clearInterval(state.renderPollTimer);
        $("btn-export-top").disabled = false;
        $("btn-export-main").disabled = false;
        const r = j.result;
        $("render-done-box").innerHTML = `
          <strong>Rendered ${r.width}&times;${r.height} Master Short Ready!</strong>
          <div class="mono" style="font-size:11px;">${r.duration_sec}s &bull; ${r.fps}fps &bull; ${r.size_mb} MB${r.bitrate_mbps ? ` (${r.bitrate_mbps} Mbps)` : ""}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <a class="btn btn-primary btn-xs" href="${esc(r.mp4_url)}" download style="text-decoration:none;">Download MP4</a>
            <a class="btn btn-amber btn-xs" href="${esc(r.cover_url)}" download style="text-decoration:none;">Cover JPG</a>
            <a class="btn btn-ghost btn-xs" href="${esc(r.srt_url)}" download style="text-decoration:none;">.SRT</a>
            <button class="btn btn-ghost btn-xs" id="btn-watch-fresh">Preview</button>
          </div>`;
        $("render-done-box").classList.remove("hidden");
        $("btn-watch-fresh").addEventListener("click", () => openExportModal(r));
        showToast(`Exported ${r.mp4_name}!`);
        loadLibrary();
      } else if (j.status === "error") {
        clearInterval(state.renderPollTimer);
        $("btn-export-top").disabled = false;
        $("btn-export-main").disabled = false;
        showToast(`Render failed: ${j.error}`, true);
      }
    } catch {}
  }, 600);
}

/* ---------------- UI WIRING ---------------- */
function switchInspectorTab(key) {
  $("insp-tabs").querySelectorAll("[data-insp]").forEach((b) => b.classList.toggle("active", b.dataset.insp === key));
  document.querySelectorAll(".insp-panel").forEach((p) => p.classList.toggle("active", p.id === `insp-${key}`));
}

function bindControls() {
  // Workspace mode switcher (Merged Shorts Editor <-> 24/7 Live Clipper Bot!)
  $("view-switch").querySelectorAll("[data-view]").forEach((b) => {
    b.addEventListener("click", () => switchWorkspaceView(b.dataset.view));
  });
  $("sync-status-pill").addEventListener("click", () => switchWorkspaceView(activeView === "bot" ? "editor" : "bot"));
  $("bot-back-editor").addEventListener("click", () => switchWorkspaceView("editor"));
  $("bot-poll-btn").addEventListener("click", async () => {
    await api("/api/bot/poll", { method: "POST", body: "{}" });
    showToast("Polling all streamers live...");
    setTimeout(loadBotView, 1800);
  });
  $("bot-add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/bot/streamers/add", {
        method: "POST",
        body: JSON.stringify({
          name: $("bot-in-name").value.trim(),
          platform: $("bot-in-plat").value,
          watch_any_game: $("bot-in-any").checked,
        }),
      });
      e.target.reset();
      showToast("Added streamer to 24/7 watcher!");
      loadBotView();
    } catch (err) { showToast(err.message, true); }
  });
  $("bot-sync-save").addEventListener("click", async () => {
    const res = await api("/api/sync/config", {
      method: "POST",
      body: JSON.stringify({
        auto_edit_enabled: $("chk-auto-sync").checked,
        min_viral_score: Number($("bot-sync-score").value || 70),
        auto_delete_minutes: Number($("bot-sync-expire").value ?? 10),
        preset: $("bot-sync-preset").value,
      }),
    });
    updateSyncPill(res.sync);
    loadLibrary(true);
    showToast(`Saved rules (Auto-Delete unchosen clips: ${res.config.auto_delete_minutes}m)`);
  });

  // Bin & Inspector tabs
  $("bin-tabs").querySelectorAll("[data-bin]").forEach((b) => {
    b.addEventListener("click", () => {
      $("bin-tabs").querySelectorAll("[data-bin]").forEach((x) => x.classList.toggle("active", x === b));
      document.querySelectorAll(".bin-panel").forEach((p) => p.classList.toggle("active", p.id === `bin-${b.dataset.bin}`));
    });
  });

  // Delegated click handlers for Bot Clips & Uploads (immune to background 7s refreshes!)
  $("bot-clips-list").addEventListener("click", async (ev) => {
    const keepEl = ev.target.closest("[data-keep-id]");
    if (keepEl) {
      ev.stopPropagation();
      const kept = keepEl.dataset.keepNext === "1";
      try {
        const res = await api("/api/clips/keep", {
          method: "POST",
          body: JSON.stringify({ source_id: keepEl.dataset.keepId, kept }),
        });
        if (state.library) state.library.bot_clips = res.bot_clips;
        renderBotClips(res.bot_clips);
        showToast(kept ? "Clip pinned — won't auto-delete!" : "Clip unpinned — 10-min auto-delete timer active");
      } catch (err) {
        showToast(err.message, true);
      }
      return;
    }
    const card = ev.target.closest(".media-card[data-src-id]");
    if (card) {
      loadSource(card.dataset.srcType, card.dataset.srcId, null, false, true);
    }
  });

  $("uploads-list").addEventListener("click", (ev) => {
    const card = ev.target.closest(".media-card[data-src-id]");
    if (card) {
      loadSource(card.dataset.srcType, card.dataset.srcId, null, false, true);
    }
  });

  $("insp-tabs").querySelectorAll("[data-insp]").forEach((b) => {
    b.addEventListener("click", () => switchInspectorTab(b.dataset.insp));
  });

  // Topbar actions
  $("btn-refresh-lib").addEventListener("click", loadLibrary);
  $("btn-auto-edit").addEventListener("click", () => {
    if (!state.source) return showToast("Load a clip first", true);
    loadSource(state.source.source_type, state.source.source_id, $("preset-select").value);
  });
  $("btn-cut-deadair").addEventListener("click", cutDeadAirNow);
  $("btn-undo").addEventListener("click", undo);
  $("btn-redo").addEventListener("click", redo);
  $("chk-safezone").addEventListener("change", (e) => {
    $("safezone-overlay").classList.toggle("hidden", !e.target.checked);
  });
  $("btn-export-top").addEventListener("click", startExport);
  $("btn-export-main").addEventListener("click", startExport);
  $("btn-copy-exports").addEventListener("click", () => {
    const p = $("exports-dir-label").textContent;
    navigator.clipboard?.writeText(p).catch(() => {});
    showToast("Copied Exports folder path!");
  });

  // Transport
  $("btn-play").addEventListener("click", togglePlayback);
  $("btn-jump-back").addEventListener("click", () => seekTimeline(state.timelineTime - 2));
  $("btn-jump-fwd").addEventListener("click", () => seekTimeline(state.timelineTime + 2));
  $("btn-step-back").addEventListener("click", () => { pausePlayback(); seekTimeline(state.timelineTime - 1 / 30); });
  $("btn-step-fwd").addEventListener("click", () => { pausePlayback(); seekTimeline(state.timelineTime + 1 / 30); });
  $("btn-split").addEventListener("click", splitAtPlayhead);
  $("btn-trim-in").addEventListener("click", () => trimSelectedEdge("in"));
  $("btn-trim-out").addEventListener("click", () => trimSelectedEdge("out"));
  $("btn-del-seg").addEventListener("click", deleteSelectedSegment);
  $("btn-reset-tl").addEventListener("click", () => {
    if (!state.source || !state.project) return;
    pushUndo();
    state.project.segments = [{ id: "seg_1", start: 0, end: state.source.probe.duration, speed: 1, zoom_mode: "none", zoom_amount: 1, transition: "none", transition_dur: 0 }];
    state.selectedSegIdx = 0;
    syncInspectorFromProject();
    renderTimeline();
    seekTimeline(0);
    showToast("Reset timeline to full source clip");
  });

  // Scrubbing on Timeline
  let scrubbing = false;
  const scrubToEvent = (ev) => {
    const rect = $("tl-tracks").getBoundingClientRect();
    const { total } = getSegmentSpans();
    if (!total) return;
    const pct = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    seekTimeline(pct * total);
  };
  $("tl-tracks").addEventListener("mousedown", (ev) => {
    scrubbing = true;
    scrubToEvent(ev);
  });
  window.addEventListener("mousemove", (ev) => { if (scrubbing) scrubToEvent(ev); });
  window.addEventListener("mouseup", () => { scrubbing = false; });

  // Framing sliders
  $("framing-mode-row").querySelectorAll("[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!state.project) return;
      pushUndo();
      state.project.framing.mode = btn.dataset.mode;
      syncInspectorFromProject();
    });
  });
  const bindNum = (id, fn) => $(id).addEventListener("input", () => { if (state.project) { fn(Number($(id).value)); syncInspectorFromProject(); } });
  bindNum("in-zoom", (v) => (state.project.framing.zoom = v));
  bindNum("in-panx", (v) => (state.project.framing.pan_x = v));
  bindNum("in-pany", (v) => (state.project.framing.pan_y = v));
  bindNum("in-split", (v) => (state.project.framing.split_ratio = v));
  bindNum("in-gameh", (v) => (state.project.framing.game_height = v));
  bindNum("in-toppad", (v) => (state.project.framing.top_pad = v));
  bindNum("in-camx", (v) => (state.project.framing.cam_x = v));
  bindNum("in-camy", (v) => (state.project.framing.cam_y = v));
  bindNum("in-camw", (v) => (state.project.framing.cam_w = v));
  bindNum("in-camh", (v) => (state.project.framing.cam_h = v));

  // 1-Click Streamer Facecam Position Preset Chips
  $("cam-preset-chips").querySelectorAll("[data-cam-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!state.project) return;
      pushUndo();
      const [cx, cy, cw, ch] = btn.dataset.camPreset.split(",").map(Number);
      const autoPan = cx < 0.25 ? 0.72 : cx > 0.55 ? -0.68 : 0.0;
      Object.assign(state.project.framing, { cam_x: cx, cam_y: cy, cam_w: cw, cam_h: ch, pan_x: autoPan });
      syncInspectorFromProject();
      showToast(`Locked facecam box: ${btn.textContent}`);
    });
  });

  // Interactive Drag-to-Select Facecam Box directly on the 16:9 mini preview
  let camDragStart = null;
  const camCanvas = $("cam-picker-canvas");
  const normPoint = (ev) => {
    const r = camCanvas.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)),
      y: Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height)),
    };
  };
  camCanvas.addEventListener("mousedown", (ev) => {
    if (!state.project) return;
    pushUndo();
    camDragStart = normPoint(ev);
  });
  window.addEventListener("mousemove", (ev) => {
    if (!camDragStart || !state.project) return;
    const pt = normPoint(ev);
    const x0 = Math.min(camDragStart.x, pt.x);
    const y0 = Math.min(camDragStart.y, pt.y);
    const w = Math.max(0.08, Math.abs(pt.x - camDragStart.x));
    const h = Math.max(0.08, Math.abs(pt.y - camDragStart.y));
    Object.assign(state.project.framing, {
      cam_x: Number(x0.toFixed(3)),
      cam_y: Number(y0.toFixed(3)),
      cam_w: Number(Math.min(1 - x0, w).toFixed(3)),
      cam_h: Number(Math.min(1 - y0, h).toFixed(3)),
    });
    syncInspectorFromProject();
  });
  window.addEventListener("mouseup", () => { camDragStart = null; });

  // Segment controls
  bindNum("in-seg-speed", (v) => { state.project.segments[state.selectedSegIdx].speed = v; renderTimeline(); });
  $("in-seg-zoom-mode").addEventListener("change", () => {
    if (!state.project) return;
    state.project.segments[state.selectedSegIdx].zoom_mode = $("in-seg-zoom-mode").value;
    renderTimeline();
  });
  bindNum("in-seg-zamt", (v) => { state.project.segments[state.selectedSegIdx].zoom_amount = v; });
  $("in-seg-trans").addEventListener("change", () => {
    if (!state.project) return;
    const v = $("in-seg-trans").value;
    state.project.segments[state.selectedSegIdx].transition = v;
    state.project.segments[state.selectedSegIdx].transition_dur = v === "none" ? 0 : 0.22;
    renderTimeline();
  });

  // Color controls
  bindNum("in-exp", (v) => (state.project.color.exposure = v));
  bindNum("in-con", (v) => (state.project.color.contrast = v));
  bindNum("in-sat", (v) => (state.project.color.saturation = v));
  bindNum("in-warm", (v) => (state.project.color.warmth = v));
  bindNum("in-sharp", (v) => (state.project.color.sharpen = v));
  $("in-vignette").addEventListener("change", () => { if (state.project) state.project.color.vignette = $("in-vignette").checked; });

  // Hook & Captions
  $("in-hook-on").addEventListener("change", () => { if (state.project) { state.project.hook.enabled = $("in-hook-on").checked; renderTimeline(); } });
  $("in-hook-text").addEventListener("input", () => { if (state.project) { state.project.hook.text = $("in-hook-text").value; renderTimeline(); } });
  $("in-hook-start").addEventListener("input", () => { if (state.project) { state.project.hook.start = Number($("in-hook-start").value) || 0; renderTimeline(); } });
  $("in-hook-end").addEventListener("input", () => { if (state.project) { state.project.hook.end = Number($("in-hook-end").value) || 3; renderTimeline(); } });
  $("in-cap-style").addEventListener("change", () => { if (state.project) state.project.caption_style.style = $("in-cap-style").value; });
  $("in-cap-font").addEventListener("change", () => { if (state.project) state.project.caption_style.font = $("in-cap-font").value; });
  bindNum("in-cap-sz", (v) => (state.project.caption_style.size = v));
  $("in-cap-accent").addEventListener("input", () => {
    if (!state.project) return;
    state.project.caption_style.accent = $("in-cap-accent").value;
    state.project.hook.accent = $("in-cap-accent").value;
  });
  bindNum("in-cap-y", (v) => (state.project.caption_style.y_pct = v));

  $("btn-add-cap").addEventListener("click", () => {
    if (!state.project) return;
    pushUndo();
    const s = Number(state.timelineTime.toFixed(2));
    state.project.captions.push({ id: `cap_${Date.now()}`, start: s, end: Number((s + 1.8).toFixed(2)), text: "Caption text" });
    renderCaptionsEditor();
    renderTimeline();
  });

  $("btn-retranscribe").addEventListener("click", async () => {
    if (!state.source || !state.project) return showToast("Load a clip first", true);
    const btn = $("btn-retranscribe");
    btn.disabled = true;
    btn.textContent = "Transcribing...";
    try {
      const tr = await api("/api/transcribe", {
        method: "POST",
        body: JSON.stringify({
          source_type: state.source.source_type,
          source_id: state.source.source_id,
          segments: state.project.segments,
        }),
      });
      pushUndo();
      state.project.captions = tr.cues || [];
      $("whisper-status").textContent = tr.ok
        ? `Whisper (${tr.model}) synced ${state.project.captions.length} spoken lines to your current cuts.`
        : (tr.error || "No speech found.");
      renderCaptionsEditor();
      renderTimeline();
      showToast(`Synced ${state.project.captions.length} spoken captions!`);
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Re-Sync Speech";
    }
  });

  $("chk-auto-sync").addEventListener("change", async () => {
    try {
      const res = await api("/api/sync/config", {
        method: "POST",
        body: JSON.stringify({ auto_edit_enabled: $("chk-auto-sync").checked }),
      });
      updateSyncPill(res.sync);
      showToast($("chk-auto-sync").checked ? "Auto-Edit Sync ON: new viral bot clips will auto-render!" : "Auto-Edit Sync paused (manual editing still synced)");
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-bgm-preview").addEventListener("click", () => {
    if (bgmAuditioning) {
      stopBgmPlayback();
      return;
    }
    const fname = $("in-bgm-select").value;
    if (!fname) return showToast("Select a viral BGM track first");
    const url = getBgmUrl(fname);
    stopBgmPlayback();
    bgmAudioEl = new Audio(url);
    bgmAudioEl.dataset.url = url;
    bgmAudioEl.loop = true;
    bgmAudioEl.volume = Math.min(0.6, Math.max(0.15, Number($("in-bgm-vol").value) * 2.2));
    bgmAudioEl.play().catch(() => {});
    bgmAuditioning = true;
    $("btn-bgm-preview").innerHTML = "&#9632;";
  });

  // Audio controls
  $("in-a-hp").addEventListener("change", () => { if (state.project) state.project.audio.highpass = $("in-a-hp").checked; });
  $("in-a-dn").addEventListener("change", () => { if (state.project) state.project.audio.denoise = $("in-a-dn").checked; });
  $("in-a-eq").addEventListener("change", () => { if (state.project) state.project.audio.presence_eq = $("in-a-eq").checked; });
  $("in-a-comp").addEventListener("change", () => { if (state.project) state.project.audio.compressor = $("in-a-comp").checked; });
  $("in-a-lufs").addEventListener("change", () => { if (state.project) state.project.audio.loudnorm = $("in-a-lufs").checked; });
  bindNum("in-a-vol", (v) => { state.project.audio.master_volume = v; $("src-video").volume = Math.min(1, v); });
  $("in-bgm-select").addEventListener("change", () => {
    if (!state.project) return;
    state.project.audio.bgm_file = $("in-bgm-select").value;
    stopBgmPlayback();
    if (state.playing && state.project.audio.bgm_file) startBgmForPlayback();
  });
  bindNum("in-bgm-vol", (v) => {
    state.project.audio.bgm_volume = v;
    if (bgmAudioEl) bgmAudioEl.volume = Math.max(0.02, Math.min(0.6, v));
  });
  $("in-bgm-duck").addEventListener("change", () => { if (state.project) state.project.audio.bgm_ducking = $("in-bgm-duck").checked; });

  // Export settings
  $("in-proj-title").addEventListener("input", () => { if (state.project) state.project.title = $("in-proj-title").value; });
  $("in-exp-res").addEventListener("change", () => { if (state.project) state.project.export.resolution = $("in-exp-res").value; });
  $("in-exp-fps").addEventListener("change", () => { if (state.project) state.project.export.fps = Number($("in-exp-fps").value); });
  $("in-exp-crf").addEventListener("change", () => { if (state.project) state.project.export.crf = Number($("in-exp-crf").value); });
  $("in-exp-loop").addEventListener("change", () => { if (state.project) state.project.export.seamless_loop = $("in-exp-loop").checked; });
  $("btn-copy-seo").addEventListener("click", () => {
    navigator.clipboard?.writeText($("seo-desc").value).catch(() => {});
    showToast("Copied SEO description & hashtags!");
  });

  // Upload handlers (click + drag & drop)
  $("file-video-input").addEventListener("change", (e) => uploadMediaFile(e.target.files[0], "video"));
  $("file-bgm-input").addEventListener("change", (e) => uploadMediaFile(e.target.files[0], "bgm"));
  const dz = $("video-dropzone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault();
    dz.classList.remove("dragover");
    if (e.dataTransfer.files[0]) uploadMediaFile(e.dataTransfer.files[0], "video");
  });

  // Modal close
  $("modal-close").addEventListener("click", () => {
    $("export-modal").classList.add("hidden");
    $("modal-body").innerHTML = "";
  });

  // Pro NLE Keyboard Shortcuts (disabled while typing inside inputs/textareas)
  window.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) redo();
      else undo();
      return;
    }
    if (e.code === "Space" || e.key.toLowerCase() === "k") { e.preventDefault(); togglePlayback(); }
    else if (e.key.toLowerCase() === "j") { e.preventDefault(); seekTimeline(state.timelineTime - 2); }
    else if (e.key.toLowerCase() === "l") { e.preventDefault(); seekTimeline(state.timelineTime + 2); }
    else if (e.key === ",") { e.preventDefault(); pausePlayback(); seekTimeline(state.timelineTime - 1 / 30); }
    else if (e.key === ".") { e.preventDefault(); pausePlayback(); seekTimeline(state.timelineTime + 1 / 30); }
    else if (e.key.toLowerCase() === "c") { e.preventDefault(); splitAtPlayhead(); }
    else if (e.key.toLowerCase() === "i") { e.preventDefault(); trimSelectedEdge("in"); }
    else if (e.key.toLowerCase() === "o") { e.preventDefault(); trimSelectedEdge("out"); }
    else if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); deleteSelectedSegment(); }
  });

  window.addEventListener("resize", renderTimeline);
}

document.addEventListener("DOMContentLoaded", () => {
  bindControls();
  loadLibrary(false);
  // Keep synced live with the 24/7 GTA 6 Clipper Bot every 7 seconds!
  setInterval(() => {
    loadLibrary(true).catch(() => {});
    if (activeView === "bot") loadBotView().catch(() => {});
  }, 7000);
  // Smooth 1-second visual countdown tick on unchosen bot clips
  setInterval(() => {
    document.querySelectorAll("[data-exp-sec]").forEach((el) => {
      const next = Math.max(0, Number(el.dataset.expSec) - 1);
      el.dataset.expSec = String(next);
      el.textContent = `\u23F1 ${fmtCountdown(next)}`;
      if (next < 120) el.classList.add("urgent");
    });
  }, 1000);
  requestAnimationFrame(renderLoop);
});
