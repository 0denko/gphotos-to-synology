(() => {
  const $ = (id) => document.getElementById(id);

  // Supports both root (http://nas:8765/) and Synology DSM proxy (/webman/3rdparty/gphotos-migrator/proxy.cgi/)
  const BASE_PATH = window.location.pathname.endsWith("/")
    ? window.location.pathname
    : `${window.location.pathname}/`;
  const urlFor = (relPath) => `${BASE_PATH}${relPath.replace(/^\/+/, "")}`;

  let isPaused = false;
  let toastTimer = null;

  function showToast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.style.borderColor = isError ? "#ef4444" : "#3b82f6";
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 4500);
  }

  function formatBytes(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let val = bytes;
    while (val >= 1024 && i < units.length - 1) {
      val /= 1024;
      i++;
    }
    return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
  }

  function formatTime(iso) {
    if (!iso) return "--";
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text ?? "";
    return div.innerHTML;
  }

  async function apiFetch(url, options = {}) {
    const resp = await fetch(urlFor(url), {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || `Request failed (${resp.status})`);
    }
    return data;
  }

  // Tab switching
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`tab-${btn.dataset.tab}`).classList.add("active");
    });
  });

  function renderStatus(data) {
    const { progress, stats, disk } = data;
    isPaused = Boolean(data.worker_paused);

    // Worker badge
    const badge = $("worker-badge");
    const state = progress.state || "idle";
    badge.textContent = isPaused ? "Paused" : state.replace("_", " ").toUpperCase();
    badge.className = "pill " + (
      isPaused ? "pill-warn" :
      state === "error" ? "pill-err" :
      state === "idle" ? "pill-ok" : "pill-warn"
    );

    $("btn-toggle-pause").textContent = isPaused ? "Resume" : "Pause";

    // Drive badge
    const driveBadge = $("gdrive-badge");
    if (data.gdrive_connected) {
      driveBadge.textContent = "Connected";
      driveBadge.className = "pill pill-ok";
    } else if (data.gdrive_oauth_configured) {
      driveBadge.textContent = "Ready to Authorize";
      driveBadge.className = "pill pill-warn";
    } else {
      driveBadge.textContent = "Not Configured";
      driveBadge.className = "pill pill-muted";
    }

    // KPIs
    $("kpi-media").textContent = stats.total_media.toLocaleString();
    $("kpi-bytes").textContent = `${formatBytes(stats.total_bytes)} · ${stats.exif_repaired} EXIF repaired`;
    $("kpi-dups").textContent = stats.total_duplicates_skipped.toLocaleString();
    $("kpi-archives").textContent =
      `${stats.existing_nas_indexed.toLocaleString()} existing NAS indexed · ${stats.completed_archives} archives`;
    $("kpi-albums").textContent = stats.total_albums.toLocaleString();
    $("kpi-album-sync").textContent = `${stats.synced_album_items} synced · ${stats.pending_album_items} pending`;
    $("kpi-disk").textContent = `${disk.photos_free_gb} GB`;
    $("kpi-health").textContent =
      `ExifTool: ${data.exiftool_available ? "OK" : "Missing"} · Drive: ${data.gdrive_connected ? "Linked" : "Local Inbox Only"}`;

    // Progress bar
    $("worker-step").textContent = progress.current_step || "Idle";
    $("last-run").textContent = `Last run: ${formatTime(progress.last_run_at)}`;
    $("next-run").textContent = `Next poll: ${formatTime(progress.next_run_at)}`;

    let pct = 0;
    let detail = "Idle";
    if (state === "downloading") {
      pct = progress.download_pct || 0;
      detail = `Downloading ${progress.current_archive} (${pct.toFixed(1)}%)`;
    } else if ((state === "importing" || state === "indexing_nas") && progress.total_files > 0) {
      pct = (progress.processed_files / progress.total_files) * 100;
      const prefix = state === "indexing_nas" ? "Existing NAS index" : progress.current_archive;
      detail = `${prefix}: ${progress.processed_files} / ${progress.total_files} items`;
    } else if (state === "extracting" || state === "scanning" || state === "syncing_albums") {
      pct = 100;
      detail = progress.current_step;
    } else if (progress.last_error) {
      detail = `Last error: ${progress.last_error}`;
    }

    $("progress-bar").style.width = `${Math.min(100, Math.max(0, pct))}%`;
    $("progress-detail").textContent = detail;

    // Renewal banner
    $("renewal-banner").classList.toggle("hidden", !progress.renewal_reminder_due);

    // Quality upgrades quarantine badge on tab
    const qCount = stats.quarantined_count || 0;
    const upBadge = $("upgrades-tab-badge");
    upBadge.textContent = String(qCount);
    upBadge.className = "pill " + (qCount > 0 ? "pill-warn" : "pill-muted");
    upBadge.classList.toggle("hidden", qCount === 0);
  }

  function renderUpgrades(upgrades) {
    const container = $("upgrades-list");
    if (!upgrades.length) {
      container.innerHTML = '<div class="muted center">No visual quality upgrades recorded yet.</div>';
      return;
    }
    container.innerHTML = upgrades
      .map((u) => {
        const statusCls =
          u.status === "quarantined" ? "pill-warn" : u.status === "restored" ? "pill-muted" : "pill-ok";
        const isQuarantined = u.status === "quarantined";
        return `
        <div class="upgrade-card">
          <div class="upgrade-head">
            <div>
              <strong>${escapeHtml(u.kept_rel_path)}</strong>
              <span class="pill ${statusCls}">${escapeHtml(u.status)}</span>
              <div class="muted" style="font-size:0.84rem;margin-top:3px;">
                ${escapeHtml(u.upgrade_reason)} · Match: ${escapeHtml(u.match_reason)}
              </div>
            </div>
            ${
              isQuarantined
                ? `<div class="inline-actions" style="margin-top:0">
                     <button class="btn btn-sm btn-secondary" data-restore-id="${u.id}" type="button">Restore Replaced Copy</button>
                     <button class="btn btn-sm btn-danger" data-purge-id="${u.id}" type="button">Purge Quarantined</button>
                   </div>`
                : `<span class="muted" style="font-size:0.8rem">${formatTime(u.resolved_at || u.created_at)}</span>`
            }
          </div>
          <div class="compare-grid">
            <div class="compare-pane">
              <div><span class="pill pill-ok">Kept in Synology Photos</span> <strong>${escapeHtml(u.new_filename)}</strong></div>
              <img src="${urlFor(`api/upgrades/${u.id}/preview/kept`)}" alt="Kept higher quality copy" loading="lazy" />
              <div class="compare-meta">
                <span>${u.new_width}×${u.new_height}</span>
                <span>${formatBytes(u.new_file_size)}</span>
                <span>Clarity: ${Number(u.new_sharpness || 0).toFixed(1)}</span>
              </div>
            </div>
            <div class="compare-pane">
              <div><span class="pill pill-warn">Quarantined (Replaced)</span> <strong>${escapeHtml(u.old_filename)}</strong></div>
              ${
                isQuarantined
                  ? `<img src="${urlFor(`api/upgrades/${u.id}/preview/quarantined`)}" alt="Quarantined replaced copy" loading="lazy" />`
                  : `<div class="muted center" style="padding:48px 10px">Quarantined file ${escapeHtml(u.status)}</div>`
              }
              <div class="compare-meta">
                <span>${u.old_width}×${u.old_height}</span>
                <span>${formatBytes(u.old_file_size)}</span>
                <span>Clarity: ${Number(u.old_sharpness || 0).toFixed(1)}</span>
              </div>
            </div>
          </div>
        </div>`;
      })
      .join("");
  }

  function renderArchives(archives) {
    const tbody = $("archives-tbody");
    if (!archives.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted center">No archives processed yet.</td></tr>';
      return;
    }
    tbody.innerHTML = archives
      .map((a) => {
        const cls = a.status === "completed" ? "pill-ok" : a.status === "failed" ? "pill-err" : "pill-warn";
        return `
          <tr>
            <td title="${escapeHtml(a.error_message || a.filename)}">${escapeHtml(a.filename)}</td>
            <td>${escapeHtml(a.source)}</td>
            <td><span class="pill ${cls}">${escapeHtml(a.status)}</span></td>
            <td>${a.imported_files}</td>
            <td>${a.duplicate_files}</td>
          </tr>`;
      })
      .join("");
  }

  function renderAlbums(albums) {
    const tbody = $("albums-tbody");
    if (!albums.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted center">No albums detected yet.</td></tr>';
      return;
    }
    tbody.innerHTML = albums
      .map(
        (al) => `
        <tr>
          <td>${escapeHtml(al.name)}</td>
          <td>${al.total_items}</td>
          <td>${al.synced_items}</td>
          <td>${al.pending_items}</td>
          <td>${al.synology_album_id ?? '<span class="muted">--</span>'}</td>
        </tr>`
      )
      .join("");
  }

  function renderLogs(logs) {
    const container = $("logs-container");
    if (!logs.length) {
      container.innerHTML = '<div class="muted center">No activity logged yet.</div>';
      return;
    }
    container.innerHTML = logs
      .map(
        (entry) => `
        <div class="log-item log-level-${escapeHtml(entry.level)}">
          <div class="log-meta">
            <span>[${escapeHtml(entry.component)}] ${escapeHtml(entry.level)}</span>
            <span>${formatTime(entry.created_at)}</span>
          </div>
          <div>${escapeHtml(entry.message)}</div>
        </div>`
      )
      .join("");
  }

  async function refreshDashboard() {
    try {
      const [statusData, archivesData, albumsData, logsData, upgradesData] = await Promise.all([
        apiFetch("/api/status"),
        apiFetch("/api/archives?limit=25"),
        apiFetch("/api/albums"),
        apiFetch("/api/logs?limit=60"),
        apiFetch("/api/upgrades?limit=50"),
      ]);
      renderStatus(statusData);
      renderArchives(archivesData.archives || []);
      renderAlbums(albumsData.albums || []);
      renderLogs(logsData.logs || []);
      renderUpgrades(upgradesData.upgrades || []);
    } catch (err) {
      console.error("Dashboard refresh error:", err);
    }
  }

  async function loadSettings() {
    $("computed-redirect-uri").textContent = `${window.location.origin}${urlFor("api/oauth/google/callback")}`;
    try {
      const { settings } = await apiFetch("/api/settings");
      const form = $("settings-form");
      for (const [key, val] of Object.entries(settings)) {
        const el = form.elements.namedItem(key);
        if (!el) continue;
        if (el.type === "checkbox") {
          el.checked = Boolean(val);
        } else {
          el.value = val ?? "";
        }
      }
    } catch (err) {
      showToast(`Could not load settings: ${err.message}`, true);
    }
  }

  async function saveSettings(e) {
    if (e) e.preventDefault();
    const form = $("settings-form");
    const updates = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      if (el.type === "checkbox") {
        updates[el.name] = el.checked;
      } else if (el.type === "number") {
        updates[el.name] = Number(el.value);
      } else {
        updates[el.name] = el.value;
      }
    }
    try {
      await apiFetch("/api/settings", {
        method: "PUT",
        body: JSON.stringify({ updates }),
      });
      showToast("Settings saved and applied.");
      await refreshDashboard();
      return true;
    } catch (err) {
      showToast(err.message, true);
      return false;
    }
  }

  // Bind controls
  $("settings-form").addEventListener("submit", saveSettings);

  $("btn-scan").addEventListener("click", async () => {
    try {
      await apiFetch("/api/actions/scan", { method: "POST" });
      showToast("Background scan started.");
      await refreshDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-toggle-pause").addEventListener("click", async () => {
    try {
      await apiFetch(isPaused ? "/api/actions/resume" : "/api/actions/pause", { method: "POST" });
      showToast(isPaused ? "Background worker resumed." : "Background worker paused.");
      await refreshDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-index-existing").addEventListener("click", async () => {
    try {
      await apiFetch("/api/actions/index-existing", { method: "POST" });
      showToast("Incremental existing NAS library index queued.");
      await refreshDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  const handleSyncAlbums = async () => {
    try {
      await apiFetch("/api/actions/sync-albums", { method: "POST" });
      showToast("Synology Photos album sync queued.");
      await refreshDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  };
  $("btn-sync-albums").addEventListener("click", handleSyncAlbums);
  $("btn-sync-albums-2").addEventListener("click", handleSyncAlbums);

  $("btn-reset-renewal").addEventListener("click", async () => {
    try {
      await apiFetch("/api/actions/reset-renewal", { method: "POST" });
      showToast("Annual Takeout schedule timer reset.");
      await refreshDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-gdrive-connect").addEventListener("click", async () => {
    const saved = await saveSettings();
    if (!saved) return;
    try {
      const { authorization_url } = await apiFetch("/api/oauth/google/start");
      if (window.self !== window.top) {
        // Google OAuth blocks iframe embedding; open consent in a new tab and auto-refresh here
        window.open(authorization_url, "_blank", "noopener");
        showToast("Complete Google sign-in in the new tab — this DSM window will update automatically.");
      } else {
        window.location.href = authorization_url;
      }
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-gdrive-disconnect").addEventListener("click", async () => {
    try {
      await apiFetch("/api/oauth/google/disconnect", { method: "POST" });
      showToast("Disconnected Google Drive.");
      await refreshDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-test-synology").addEventListener("click", async () => {
    const saved = await saveSettings();
    if (!saved) return;
    try {
      const res = await apiFetch("/api/actions/test-synology", { method: "POST" });
      showToast(`Connected to Synology Photos (${res.space} space, ${res.album_count} albums).`);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("btn-test-notify").addEventListener("click", async () => {
    const saved = await saveSettings();
    if (!saved) return;
    try {
      await apiFetch("/api/actions/test-notification", { method: "POST" });
      showToast("Test notification delivered!");
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("upgrades-list").addEventListener("click", async (e) => {
    const restoreBtn = e.target.closest("[data-restore-id]");
    if (restoreBtn) {
      const id = restoreBtn.dataset.restoreId;
      try {
        await apiFetch(`/api/upgrades/${id}/restore`, { method: "POST" });
        showToast("Restored original copy back into Synology Photos.");
        await refreshDashboard();
      } catch (err) {
        showToast(err.message, true);
      }
      return;
    }
    const purgeBtn = e.target.closest("[data-purge-id]");
    if (purgeBtn) {
      const id = purgeBtn.dataset.purgeId;
      try {
        await apiFetch(`/api/upgrades/${id}/purge`, { method: "POST" });
        showToast("Quarantined duplicate purged.");
        await refreshDashboard();
      } catch (err) {
        showToast(err.message, true);
      }
    }
  });

  $("btn-purge-all").addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/upgrades/purge-all", { method: "POST" });
      showToast(`Purged ${res.purged} quarantined duplicate(s).`);
      await refreshDashboard();
    } catch (err) {
      showToast(err.message, true);
    }
  });

  // Check OAuth callback query status
  const params = new URLSearchParams(window.location.search);
  if (params.get("oauth") === "success") {
    showToast("Google Drive connected! Initial background scan started.");
    window.history.replaceState({}, "", BASE_PATH);
  } else if (params.get("oauth") === "error") {
    showToast(`Google OAuth error: ${params.get("detail") || "unknown"}`, true);
    window.history.replaceState({}, "", BASE_PATH);
  }

  loadSettings();
  refreshDashboard();
  setInterval(refreshDashboard, 4000);
})();
