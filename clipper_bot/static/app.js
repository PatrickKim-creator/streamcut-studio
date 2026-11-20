/* ViceWatch 24/7 Mission Control Frontend */

const $ = (id) => document.getElementById(id);

function esc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function showToast(msg, isErr = false) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("error", Boolean(isErr));
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), 3400);
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let lastSnapshot = null;
let settingsLoaded = false;

async function fetchStatus() {
  try {
    const snap = await api("/api/status");
    lastSnapshot = snap;
    renderSnapshot(snap);
  } catch (err) {
    console.warn("Poll status error:", err);
  }
}

function renderSnapshot(s) {
  $("kpi-status").textContent = s.bot_running ? "RUNNING 24/7" : "PAUSED";
  $("kpi-poll").textContent = `Last poll: ${s.last_poll_at}`;
  $("kpi-workers").textContent = String(s.active_workers || 0);
  $("kpi-clips").textContent = String((s.clips || []).length);
  $("kpi-folder").textContent = s.clips_dir_abs || "clips_to_edit/";

  if (!settingsLoaded && s.config) {
    settingsLoaded = true;
    const c = s.config;
    $("set-threshold").value = c.viral_score_threshold ?? 70;
    $("set-pre").value = c.pre_roll_sec ?? 15;
    $("set-post").value = c.post_roll_sec ?? 10;
    $("set-max-clip").value = c.max_clip_sec ?? 60;
    $("set-cool").value = c.cooldown_sec ?? 45;
    $("set-max-hr").value = c.max_clips_per_hour ?? 8;
    $("set-warmup").value = c.warmup_sec ?? 90;
    $("set-gb").value = c.max_clips_folder_gb ?? 25;
    $("set-gta-only").checked = Boolean(c.require_gta6_filter);
    $("set-norm-audio").checked = c.normalize_audio !== false;
    $("set-quality").checked = c.quality_check !== false;
    $("set-vertical").checked = Boolean(c.export_vertical_draft);
  }

  renderStreamers(s.streamers || []);
  renderClips(s.clips || []);
  renderEvents(s.events || []);
}

function renderStreamers(list) {
  $("streamer-count").textContent = `${list.length} channel${list.length === 1 ? "" : "s"}`;
  const wrap = $("streamers-list");
  if (!list.length) {
    wrap.innerHTML = `<div class="empty-box">Add Twitch, YouTube, or Kick streamers above to start 24/7 monitoring.</div>`;
    return;
  }

  wrap.innerHTML = list
    .map((st) => {
      const w = st.worker;
      let pill = `<span class="pill-badge pill-off">OFFLINE</span>`;
      if (st.is_live && st.matches_target) {
        pill = `<span class="pill-badge pill-live">WATCHING LIVE &bull; ${Number(st.viewers || 0).toLocaleString()}</span>`;
      } else if (st.is_live) {
        pill = `<span class="pill-badge pill-other">LIVE (Non-GTA6: ${esc(st.game || "Other")})</span>`;
      }

      const tele = w
        ? `<div class="mono" style="font-size:11px; color:var(--cyan); margin-top:4px;">
             ${esc(w.state)}${w.exports_in_progress ? ` &bull; ENCODING(${w.exports_in_progress})` : ""} &bull; Chat: ${w.chat_mps}/s (Z=${w.chat_z}, ${w.chat_reactors || 0} hype, ${w.chat_filtered || 0} spam cut) &bull; Audio Z=${w.audio_z} &bull; Viral=${w.last_viral_score} &bull; Clips=${w.clips_captured}${w.clips_rejected ? ` (${w.clips_rejected} rejected)` : ""}
           </div>`
        : st.title
        ? `<div class="muted" style="font-size:11.5px; margin-top:3px;">${esc(st.title.slice(0, 75))}</div>`
        : "";

      return `
        <div class="streamer-row ${w ? "watching" : ""}">
          <div>
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
              <strong>${esc(st.name)}</strong>
              <span class="mono muted" style="font-size:11px;">[${esc(st.platform)}]</span>
              ${pill}
            </div>
            ${tele}
          </div>
          <div class="row-actions">
            ${w ? `<button class="btn btn-primary btn-sm" data-force="${esc(st.name)}">Force Clip</button>` : ""}
            <button class="btn btn-ghost btn-sm" data-toggle-any="${esc(st.name)}" title="Toggle GTA 6 Only vs Watch Any Game">
              ${st.watch_any_game ? "Mode: Any Game" : "Mode: GTA 6 Only"}
            </button>
            <button class="btn btn-ghost btn-sm" data-remove="${esc(st.name)}" title="Remove streamer">&times;</button>
          </div>
        </div>`;
    })
    .join("");

  wrap.querySelectorAll("[data-force]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const res = await api("/api/force-clip", { method: "POST", body: JSON.stringify({ name: btn.dataset.force }) });
        showToast(res.message || `Manual clip queued for ${btn.dataset.force}`);
        fetchStatus();
        setTimeout(fetchStatus, 5000);
      } catch (err) {
        showToast(err.message, true);
      }
    });
  });

  wrap.querySelectorAll("[data-toggle-any]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/streamers/toggle", {
        method: "POST",
        body: JSON.stringify({ name: btn.dataset.toggleAny, field: "watch_any_game" }),
      });
      showToast(`Toggled game filter for ${btn.dataset.toggleAny}`);
      fetchStatus();
    });
  });

  wrap.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/streamers/remove", { method: "POST", body: JSON.stringify({ name: btn.dataset.remove }) });
      showToast(`Removed ${btn.dataset.remove}`);
      fetchStatus();
    });
  });
}

function renderClips(clips) {
  const wrap = $("clips-gallery");
  if (!clips.length) {
    wrap.innerHTML = `<div class="empty-box">No clips in <code>clips_to_edit/</code> yet. Click <strong>Test Viral Clip Pipeline</strong> in the top-right to verify end-to-end viral detection &amp; MP4 export!</div>`;
    return;
  }

  // Only re-render if clip count or top clip ID changed so playing video doesn't get interrupted!
  const sig = clips.map((c) => c.clip_id).join("|");
  if (wrap.dataset.sig === sig) return;
  wrap.dataset.sig = sig;

  wrap.innerHTML = clips
    .map((c) => {
      const mp4Url = `/clips/${c.mp4_relpath.split("/").map(encodeURIComponent).join("/")}`;
      const thumbUrl = `/clips/${c.thumb_relpath.split("/").map(encodeURIComponent).join("/")}`;
      const vertUrl = c.vertical_relpath
        ? `/clips/${c.vertical_relpath.split("/").map(encodeURIComponent).join("/")}`
        : "";
      const hookHtml = c.chat_hook
        ? `<div class="clip-chat"><strong>Caption Hook:</strong> &ldquo;${esc(c.chat_hook)}&rdquo;</div>`
        : "";
      const chatBox =
        c.top_chat_messages && c.top_chat_messages.length
          ? `<div class="clip-chat"><strong>Hyped Chat at Peak (${c.peak_marker_sec}s):</strong> ${esc(c.top_chat_messages.slice(0, 4).join(" • "))}</div>`
          : "";
      return `
        <article class="clip-card">
          <div class="clip-video-wrap">
            <video src="${mp4Url}" poster="${thumbUrl}" controls preload="metadata"></video>
          </div>
          <div class="clip-meta">
            <div class="clip-top">
              <strong>${esc(c.streamer)} &bull; <span class="amber">Score ${c.viral_score}/100</span></strong>
              <span class="mono muted" style="font-size:11px;">${esc(c.created_at)} &bull; ${c.duration_sec}s</span>
            </div>
            <div class="mono cyan" style="font-size:11px; margin-top:4px;">
              Trigger: ${esc(c.trigger_reason)} &bull; Peak Marker: ${c.peak_marker_sec}s &bull; Chat Z=${c.chat_z} &bull; Audio Z=${c.audio_z}
            </div>
            <div class="muted" style="font-size:12px; margin-top:3px;">${esc(c.stream_title || c.game)}</div>
            ${hookHtml}
            ${chatBox}
            <div class="clip-btns">
              <a class="btn btn-primary btn-sm" href="http://127.0.0.1:8790/?clip=${encodeURIComponent(c.mp4_relpath)}" target="_blank" rel="noopener" style="text-decoration:none;">Edit in Shorts Studio &nearr;</a>
              <button class="btn btn-ghost btn-sm" data-jump-peak="${Number(c.peak_marker_sec || 0)}">Jump to Peak (${c.peak_marker_sec}s)</button>
              <a class="btn btn-ghost btn-sm" href="${mp4Url}" download style="text-decoration:none;">Download 16:9 Master</a>
              ${vertUrl ? `<a class="btn btn-ghost btn-sm" href="${vertUrl}" download style="text-decoration:none;">9:16 Vertical</a>` : ""}
              <button class="btn btn-ghost btn-sm" data-del-clip="${esc(c.mp4_relpath)}">Delete</button>
            </div>
          </div>
        </article>`;
    })
    .join("");

  wrap.querySelectorAll("[data-jump-peak]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const card = btn.closest(".clip-card");
      const vid = card ? card.querySelector("video") : null;
      if (!vid) return;
      const target = Math.max(0, Number(btn.dataset.jumpPeak || 0) - 2.0);
      vid.currentTime = target;
      vid.play().catch(() => {});
    });
  });

  wrap.querySelectorAll("[data-del-clip]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/clips/delete", { method: "POST", body: JSON.stringify({ mp4_relpath: btn.dataset.delClip }) });
      wrap.dataset.sig = "";
      showToast("Clip deleted from clips_to_edit/");
      fetchStatus();
    });
  });
}

function renderEvents(events) {
  const el = $("events-log");
  el.innerHTML = events.length
    ? events.map((e) => `<div>${esc(e)}</div>`).join("")
    : `<div>Waiting for 24/7 events...</div>`;
}

function init() {
  $("btn-poll-now").addEventListener("click", async () => {
    await api("/api/poll", { method: "POST", body: "{}" });
    showToast("Polling all streamers now...");
    setTimeout(fetchStatus, 1800);
  });

  $("btn-simulate").addEventListener("click", async () => {
    const btn = $("btn-simulate");
    btn.disabled = true;
    btn.textContent = "Simulating Stream + Viral Spike...";
    try {
      const res = await api("/api/simulate", { method: "POST", body: JSON.stringify({ streamer: "kaicenat_sim" }) });
      showToast(`Viral clip captured (Score ${res.clip.viral_score}) -> clips_to_edit/`);
      $("clips-gallery").dataset.sig = "";
      renderSnapshot(res.status);
    } catch (err) {
      showToast(err.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Test Viral Clip Pipeline";
    }
  });

  $("btn-copy-path").addEventListener("click", async () => {
    const p = $("kpi-folder").textContent;
    try {
      await navigator.clipboard.writeText(p);
      showToast("Copied clips folder path!");
    } catch {
      showToast(p);
    }
  });

  $("add-streamer-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const snap = await api("/api/streamers/add", {
        method: "POST",
        body: JSON.stringify({
          name: $("in-name").value.trim(),
          platform: $("in-platform").value,
          url: $("in-url").value.trim(),
          watch_any_game: $("in-any-game").checked,
        }),
      });
      e.target.reset();
      showToast("Streamer added to 24/7 roster!");
      renderSnapshot(snap);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const snap = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          viral_score_threshold: Number($("set-threshold").value),
          pre_roll_sec: Number($("set-pre").value),
          post_roll_sec: Number($("set-post").value),
          max_clip_sec: Number($("set-max-clip").value),
          cooldown_sec: Number($("set-cool").value),
          max_clips_per_hour: Number($("set-max-hr").value),
          warmup_sec: Number($("set-warmup").value),
          max_clips_folder_gb: Number($("set-gb").value),
          require_gta6_filter: $("set-gta-only").checked,
          normalize_audio: $("set-norm-audio").checked,
          quality_check: $("set-quality").checked,
          export_vertical_draft: $("set-vertical").checked,
        }),
      });
      showToast("Saved 24/7 bot settings!");
      renderSnapshot(snap);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  fetchStatus();
  setInterval(fetchStatus, 5000);
}

document.addEventListener("DOMContentLoaded", init);
