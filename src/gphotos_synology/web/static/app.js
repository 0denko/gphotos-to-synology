(() => {
  const $ = (id) => document.getElementById(id);

  // Supports direct mode (http://nas:8765/) and Synology DSM 7 CGI mode
  // (/webman/3rdparty/gphotos-migrator/proxy.cgi?p=/api/...)
  const IS_CGI_PROXY = window.location.pathname.endsWith(".cgi");
  const BASE_PATH = window.location.pathname.endsWith("/")
    ? window.location.pathname
    : `${window.location.pathname}/`;

  const urlFor = (relPath) => {
    const clean = relPath.replace(/^\/+/, "");
    if (!IS_CGI_PROXY) {
      return `${BASE_PATH}${clean}`;
    }
    const qIdx = clean.indexOf("?");
    const subPath = "/" + (qIdx >= 0 ? clean.slice(0, qIdx) : clean);
    const extraQs = qIdx >= 0 ? clean.slice(qIdx + 1) : "";
    const base = `${window.location.pathname}?p=${encodeURIComponent(subPath)}`;
    return extraQs ? `${base}&${extraQs}` : base;
  };

  let activeProfileId = "default";
  let profilesCache = [];
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
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  async function apiFetch(url, options = {}) {
    const { headers: customHeaders, ...restOptions } = options;
    const resp = await fetch(urlFor(url), {
      ...restOptions,
      headers: {
        "Content-Type": "application/json",
        "X-Profile-Id": activeProfileId,
        "X-Requested-With": "XMLHttpRequest",
        ...(customHeaders || {}),
      },
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
      `EXIF: ${data.exiftool_available ? "ExifTool" : "Built-in"} · Drive: ${data.gdrive_connected ? "Linked" : "Local Inbox Only"}`;

    $("perm-banner").classList.toggle("hidden", data.photos_dir_writable !== false);

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

    // Live worker telemetry (rolling rate, ETA, CPU load, active workers, throttle)
    const rateEl = $("telemetry-rate");
    const etaEl = $("telemetry-eta");
    const cpuEl = $("telemetry-cpu");
    const workersEl = $("telemetry-workers");
    const throttledEl = $("telemetry-throttled");
    const isItemPhase = (state === "indexing_nas" || state === "importing") && progress.total_files > 0;

    if (rateEl) {
      const ipm = Number(progress.items_per_min || 0);
      if (isItemPhase && ipm > 0) {
        rateEl.textContent = `${ipm >= 100 ? Math.round(ipm).toLocaleString() : ipm.toFixed(1)} items/min`;
        rateEl.classList.remove("hidden");
      } else {
        rateEl.classList.add("hidden");
      }
    }
    if (etaEl) {
      const eta = progress.eta_seconds;
      if (isItemPhase && eta !== null && eta !== undefined && Number.isFinite(Number(eta))) {
        const secs = Math.max(0, Math.round(Number(eta)));
        let label = "<1m";
        if (secs < 60) {
          label = `${secs}s`;
        } else if (secs < 3600) {
          const m = Math.floor(secs / 60);
          const s = secs % 60;
          label = s > 0 ? `${m}m ${s}s` : `${m}m`;
        } else if (secs < 86400) {
          const h = Math.floor(secs / 3600);
          const m = Math.floor((secs % 3600) / 60);
          label = m > 0 ? `${h}h ${m}m` : `${h}h`;
        } else {
          const d = Math.floor(secs / 86400);
          const h = Math.floor((secs % 86400) / 3600);
          label = h > 0 ? `${d}d ${h}h` : `${d}d`;
        }
        etaEl.textContent = `ETA: ${label}`;
        etaEl.classList.remove("hidden");
      } else {
        etaEl.classList.add("hidden");
      }
    }
    if (cpuEl) {
      const cpuPct = Number(progress.worker_cpu_pct || 0);
      const cpuCores = Number(progress.worker_cpu_cores || 0);
      cpuEl.textContent = `CPU: ${cpuPct.toFixed(1)}% (${cpuCores.toFixed(2)} cores)`;
      cpuEl.className = "pill " + (cpuPct >= 75 ? "pill-warn" : "pill-muted");
    }
    if (workersEl) {
      const activeW = Number(progress.active_index_workers || 0);
      const cfgW = Number(progress.configured_index_workers || 0);
      if (state === "indexing_nas" && cfgW > 0) {
        workersEl.textContent = `Workers: ${activeW}/${cfgW}`;
        workersEl.classList.remove("hidden");
      } else {
        workersEl.classList.add("hidden");
      }
    }
    if (throttledEl) {
      const isThrottled = state === "indexing_nas" && Boolean(progress.index_throttled);
      const reason = progress.index_throttle_reason ? ` (${progress.index_throttle_reason})` : "";
      throttledEl.textContent = `Throttled${reason}`;
      throttledEl.classList.toggle("hidden", !isThrottled);
    }

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
              <img src="${urlFor(`api/upgrades/${u.id}/preview/kept?profile=${encodeURIComponent(activeProfileId)}`)}" alt="Kept higher quality copy" loading="lazy" />
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
                  ? `<img src="${urlFor(`api/upgrades/${u.id}/preview/quarantined?profile=${encodeURIComponent(activeProfileId)}`)}" alt="Quarantined replaced copy" loading="lazy" />`
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
            <td data-label="Archive" title="${escapeHtml(a.error_message || a.filename)}">${escapeHtml(a.filename)}</td>
            <td data-label="Source">${escapeHtml(a.source)}</td>
            <td data-label="Status"><span class="pill ${cls}">${escapeHtml(a.status)}</span></td>
            <td data-label="Imported">${a.imported_files}</td>
            <td data-label="Dups">${a.duplicate_files}</td>
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
          <td data-label="Album">${escapeHtml(al.name)}</td>
          <td data-label="Total">${al.total_items}</td>
          <td data-label="Synced">${al.synced_items}</td>
          <td data-label="Pending">${al.pending_items}</td>
          <td data-label="Synology ID">${al.synology_album_id ?? '<span class="muted">--</span>'}</td>
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

  function isPrivateIpUrl(urlStr) {
    try {
      const host = (new URL(urlStr).hostname || "").replace(/^\[|\]$/g, "").toLowerCase();
      if (!host) return false;
      const ipv4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
      if (ipv4) {
        const [a, b] = [Number(ipv4[1]), Number(ipv4[2])];
        if (a === 10) return true;
        if (a === 172 && b >= 16 && b <= 31) return true;
        if (a === 192 && b === 168) return true;
        if (a === 169 && b === 254) return true;
        if (a === 100 && b >= 64 && b <= 127) return true;
        return false;
      }
      if (host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80:")) {
        return true;
      }
      return false;
    } catch {
      return false;
    }
  }

  function updateRedirectUriWarning(defaultRedirectUri) {
    const warnEl = $("redirect-uri-ip-warning");
    if (!warnEl) return;
    const input = $("google_redirect_uri");
    const effective = (input && input.value.trim()) || defaultRedirectUri || "";
    warnEl.classList.toggle("hidden", !isPrivateIpUrl(effective));
  }

  async function loadSettings() {
    const defaultRedirectUri = IS_CGI_PROXY
      ? `${window.location.origin}${window.location.pathname.replace(/[^/]+$/, "oauth_callback.cgi")}`
      : `${window.location.origin}${urlFor("api/oauth/google/callback")}`;
    $("computed-redirect-uri").textContent = defaultRedirectUri;
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
      updateRedirectUriWarning(defaultRedirectUri);
      const redirectInput = $("google_redirect_uri");
      if (redirectInput && !redirectInput.dataset.warnBound) {
        redirectInput.dataset.warnBound = "1";
        redirectInput.addEventListener("input", () => updateRedirectUriWarning(defaultRedirectUri));
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

  let currentBuildSha = "";
  let latestAvailableVersion = "";
  const DISMISSED_UPDATE_KEY = "gphotos_dismissed_update_version";

  function renderUpdateStatus(u, notifyIfCurrent = false) {
    currentBuildSha = u.current_sha || "";
    const verLabel = `v${u.current_version}`;
    $("version-badge").textContent = verLabel;

    const banner = $("update-banner");
    const targetVersion = u.latest_version || "";
    const dismissedVer = window.localStorage.getItem(DISMISSED_UPDATE_KEY) || "";

    if (u.update_available && targetVersion) {
      latestAvailableVersion = targetVersion;
      const isSpk = Boolean(u.is_spk_install || IS_CGI_PROXY || !u.auto_updater_configured);
      const hint = isSpk
        ? " — update in DSM Package Center or download the .spk."
        : "";
      $("update-banner-text").textContent =
        `Installed ${u.current_version} → Available ${targetVersion}${hint}`;

      const notesLink = $("link-release-notes");
      if (notesLink) {
        notesLink.href = u.release_url || "#";
      }
      const spkLink = $("link-download-spk");
      if (spkLink) {
        spkLink.href = u.spk_download_url || u.release_url || "#";
      }

      const applyBtn = $("btn-apply-update");
      if (applyBtn) {
        if (u.auto_updater_configured && !u.is_spk_install && !IS_CGI_PROXY) {
          applyBtn.textContent = "Update Now";
          applyBtn.disabled = false;
          applyBtn.classList.remove("hidden");
        } else {
          applyBtn.classList.add("hidden");
        }
      }

      if (!notifyIfCurrent && dismissedVer === targetVersion) {
        banner.classList.add("hidden");
      } else {
        banner.classList.remove("hidden");
      }
    } else {
      latestAvailableVersion = "";
      banner.classList.add("hidden");
      if (notifyIfCurrent) {
        if (u.error) {
          showToast(u.error, true);
        } else {
          showToast(`Up to date (${verLabel}).`);
        }
      }
    }
  }

  const dismissUpdateBtn = $("btn-dismiss-update");
  if (dismissUpdateBtn) {
    dismissUpdateBtn.addEventListener("click", () => {
      if (latestAvailableVersion) {
        window.localStorage.setItem(DISMISSED_UPDATE_KEY, latestAvailableVersion);
      }
      $("update-banner").classList.add("hidden");
    });
  }

  async function checkUpdates(force = false) {
    if (force) {
      window.localStorage.removeItem(DISMISSED_UPDATE_KEY);
    }
    try {
      const data = await apiFetch(`/api/updates?force=${force ? "true" : "false"}`);
      renderUpdateStatus(data, force);
    } catch (err) {
      if (force) showToast(`Update check failed: ${err.message}`, true);
    }
  }

  function waitForReconnectAfterUpdate(previousSha) {
    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      try {
        const health = await apiFetch("/api/health");
        if (health && health.status === "ok" && (health.build_sha !== previousSha || attempts >= 8)) {
          clearInterval(poll);
          showToast("Update complete! Reloading dashboard...");
          setTimeout(() => window.location.reload(), 800);
        }
      } catch {
        // Expected while Watchtower stops & starts the new container
      }
      if (attempts >= 45) {
        clearInterval(poll);
        showToast("Update still restarting — click Reload once the container is back.", true);
      }
    }, 3000);
  }

  $("btn-check-updates").addEventListener("click", () => checkUpdates(true));

  $("btn-apply-update").addEventListener("click", async () => {
    const prevSha = currentBuildSha;
    const btn = $("btn-apply-update");
    btn.disabled = true;
    btn.textContent = "Updating...";
    try {
      const res = await apiFetch("/api/updates/apply", { method: "POST" });
      showToast(res.message || "Update started — restarting container...");
      waitForReconnectAfterUpdate(prevSha);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Update Now";
      showToast(err.message, true);
    }
  });

  function updateProfilePathPill() {
    const found = profilesCache.find((p) => p.id === activeProfileId);
    if (found) {
      $("profile-path-pill").textContent = found.photos_dir;
    }
  }

  function renderProfilesModal(data) {
    const discBox = $("discovered-users-box");
    const chipsEl = $("discovered-users-chips");
    const discovered = data.discovered_synology_users || [];
    if (data.homes_mounted && discovered.length) {
      discBox.classList.remove("hidden");
      chipsEl.innerHTML = discovered
        .map((u) => {
          const disabled = u.already_configured ? "disabled" : "";
          const label = u.already_configured ? `${u.username} (added)` : `+ Add ${u.username}`;
          return `<button class="user-chip" type="button" data-quick-user="${escapeHtml(u.username)}" ${disabled}>${escapeHtml(label)}</button>`;
        })
        .join("");
    } else {
      discBox.classList.add("hidden");
    }

    const tbody = $("profiles-tbody");
    tbody.innerHTML = (data.profiles || [])
      .map((p) => {
        const activeCls = p.enabled ? "pill-ok" : "pill-muted";
        const delBtn = p.is_default
          ? '<span class="muted">Default</span>'
          : `<button class="btn btn-sm btn-danger" type="button" data-del-profile="${escapeHtml(p.id)}">Remove</button>`;
        return `
        <tr>
          <td data-label="Profile"><strong>${escapeHtml(p.display_name)}</strong></td>
          <td data-label="Destination"><code>${escapeHtml(p.photos_dir)}</code></td>
          <td data-label="Migrated">${(p.stats && p.stats.total_media) || 0}</td>
          <td data-label="Active">
            <button class="pill ${activeCls}" style="border:none;cursor:pointer" type="button"
              data-toggle-profile="${escapeHtml(p.id)}" data-enabled="${p.enabled ? "1" : "0"}">
              ${p.enabled ? "Enabled" : "Disabled"}
            </button>
          </td>
          <td data-label="Actions">${delBtn}</td>
        </tr>`;
      })
      .join("");
  }

  async function loadProfiles(selectOnInit = false) {
    try {
      const data = await apiFetch("/api/profiles");
      profilesCache = data.profiles || [];

      if (data.current_dsm_user) {
        const pill = $("dsm-user-pill");
        pill.textContent = `DSM: ${data.current_dsm_user}`;
        pill.classList.remove("hidden");
      }

      if (selectOnInit) {
        const urlProfile = new URLSearchParams(window.location.search).get("profile");
        const savedProfile = window.localStorage.getItem("gphotos_active_profile");
        const candidate = urlProfile || savedProfile || data.recommended_profile_id || "default";
        activeProfileId = profilesCache.some((p) => p.id === candidate) ? candidate : "default";
      } else if (!profilesCache.some((p) => p.id === activeProfileId)) {
        activeProfileId = "default";
      }

      const sel = $("profile-select");
      sel.innerHTML = profilesCache
        .map((p) => {
          const suffix = p.enabled ? "" : " (Paused)";
          return `<option value="${escapeHtml(p.id)}">${escapeHtml(p.display_name)}${suffix}</option>`;
        })
        .join("");
      sel.value = activeProfileId;
      updateProfilePathPill();
      renderProfilesModal(data);
    } catch (err) {
      console.error("Failed loading profiles:", err);
    }
  }

  async function switchProfile(newId) {
    activeProfileId = newId || "default";
    window.localStorage.setItem("gphotos_active_profile", activeProfileId);
    $("profile-select").value = activeProfileId;
    updateProfilePathPill();
    await Promise.all([loadSettings(), refreshDashboard()]);
  }

  $("profile-select").addEventListener("change", (e) => switchProfile(e.target.value));

  $("btn-manage-users").addEventListener("click", async () => {
    await loadProfiles(false);
    $("users-modal").classList.remove("hidden");
  });

  $("btn-close-users-modal").addEventListener("click", () => {
    $("users-modal").classList.add("hidden");
  });

  $("users-modal").addEventListener("click", (e) => {
    if (e.target === $("users-modal")) {
      $("users-modal").classList.add("hidden");
    }
  });

  $("discovered-users-chips").addEventListener("click", async (e) => {
    const chip = e.target.closest("[data-quick-user]");
    if (!chip || chip.disabled) return;
    const username = chip.dataset.quickUser;
    try {
      const res = await apiFetch("/api/profiles", {
        method: "POST",
        body: JSON.stringify({
          dsm_username: username,
          display_name: `${username} (Personal Space)`,
          target_space: "personal",
        }),
      });
      showToast(`Added Synology user '${username}'.`);
      await loadProfiles(false);
      await switchProfile(res.profile.id);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("add-user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    try {
      const res = await apiFetch("/api/profiles", {
        method: "POST",
        body: JSON.stringify({
          target_space: form.elements.target_space.value,
          dsm_username: form.elements.dsm_username.value.trim(),
          display_name: form.elements.display_name.value.trim(),
          custom_subfolder: form.elements.custom_subfolder.value.trim(),
        }),
      });
      form.reset();
      showToast(`Added profile '${res.profile.display_name}'.`);
      await loadProfiles(false);
      await switchProfile(res.profile.id);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  $("profiles-tbody").addEventListener("click", async (e) => {
    const toggleBtn = e.target.closest("[data-toggle-profile]");
    if (toggleBtn) {
      const pid = toggleBtn.dataset.toggleProfile;
      const nextEnabled = toggleBtn.dataset.enabled !== "1";
      try {
        await apiFetch(`/api/profiles/${encodeURIComponent(pid)}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: nextEnabled }),
        });
        await loadProfiles(false);
      } catch (err) {
        showToast(err.message, true);
      }
      return;
    }

    const delBtn = e.target.closest("[data-del-profile]");
    if (delBtn) {
      const pid = delBtn.dataset.delProfile;
      try {
        await apiFetch(`/api/profiles/${encodeURIComponent(pid)}`, { method: "DELETE" });
        showToast("Profile removed (photos on NAS were kept untouched).");
        await loadProfiles(false);
        await switchProfile(activeProfileId);
      } catch (err) {
        showToast(err.message, true);
      }
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

  (async () => {
    await loadProfiles(true);
    await Promise.all([loadSettings(), refreshDashboard(), checkUpdates(false)]);
    setInterval(refreshDashboard, 4000);
    setInterval(() => checkUpdates(false), 30 * 60 * 1000);
  })();
})();
