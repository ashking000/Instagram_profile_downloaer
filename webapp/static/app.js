"use strict";

// ---- tiny helpers --------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-json (e.g. zip) */ }
  return { ok: res.ok, status: res.status, data };
}

function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  setTimeout(() => t.classList.add("hidden"), 3200);
}

// ---- state ---------------------------------------------------------------
const state = {
  profile: null,       // currently displayed profile
  items: [],           // media items [{name,type,size,url}]
  selected: new Set(), // selected file names
  filter: "all",
  pollTimer: null,
  engine: "instaloader",
  mobile: false,
  browserPollTimer: null,
};

// ---- auth ----------------------------------------------------------------
async function refreshStatus() {
  const { data } = await api("/api/status");
  const on = data && data.logged_in;
  $("#auth-status").textContent = on ? `Logged in as @${data.username}` : "Not logged in";
  $("#auth-status").classList.toggle("on", !!on);
  $("#login-btn").classList.toggle("hidden", !!on);
  $("#logout-btn").classList.toggle("hidden", !on);
}

function openLogin() {
  $("#login-modal").classList.remove("hidden");
  $("#login-step").classList.remove("hidden");
  $("#twofa-step").classList.add("hidden");
  $("#login-error").classList.add("hidden");
}
function closeLogin() { $("#login-modal").classList.add("hidden"); }

function showLoginError(msg) {
  const el = $("#login-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function doLogin() {
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  if (!username || !password) return showLoginError("Enter username and password.");
  $("#do-login").disabled = true;
  const { data } = await api("/api/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  $("#do-login").disabled = false;
  if (!data) return showLoginError("Server error.");
  if (data.status === "ok") {
    closeLogin();
    toast(`Logged in as @${data.username}`, "ok");
    refreshStatus();
  } else if (data.status === "2fa") {
    $("#login-step").classList.add("hidden");
    $("#twofa-step").classList.remove("hidden");
    $("#login-error").classList.add("hidden");
  } else {
    showLoginError(data.message || "Login failed.");
  }
}

async function do2fa() {
  const code = $("#twofa-code").value.trim();
  if (!code) return showLoginError("Enter the 2FA code.");
  $("#do-2fa").disabled = true;
  const { data } = await api("/api/2fa", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  $("#do-2fa").disabled = false;
  if (data && data.status === "ok") {
    closeLogin();
    toast(`Logged in as @${data.username}`, "ok");
    refreshStatus();
  } else {
    showLoginError((data && data.message) || "2FA failed.");
  }
}

async function doBrowserLogin() {
  const browser = $("#browser-select").value;
  const btn = $("#do-browser");
  btn.disabled = true;
  btn.textContent = "Importing…";
  const { data } = await api("/api/import-browser", {
    method: "POST",
    body: JSON.stringify({ browser }),
  });
  btn.disabled = false;
  btn.textContent = "Use browser login";
  if (data && data.status === "ok") {
    closeLogin();
    toast(`Logged in as @${data.username} (via ${browser})`, "ok");
    refreshStatus();
  } else {
    showLoginError((data && data.message) || "Browser import failed.");
  }
}

async function openRealBrowserLogin() {
  const btn = $("#do-real-browser");
  btn.disabled = true;
  const statusEl = $("#real-browser-status");
  statusEl.textContent = "Launching a real browser window…";
  await api("/api/browser/login", {
    method: "POST",
    body: JSON.stringify({ mobile: state.mobile }),
  });
  // poll status until login finishes
  if (state.browserPollTimer) clearInterval(state.browserPollTimer);
  state.browserPollTimer = setInterval(async () => {
    const { data } = await api("/api/browser/status");
    if (!data) return;
    statusEl.textContent = data.status || "";
    if (!data.running) {
      clearInterval(state.browserPollTimer);
      btn.disabled = false;
      if (data.logged_in) {
        toast("Browser login saved. Use the 'Real browser' engine.", "ok");
      }
    }
  }, 1500);
}

async function doLogout() {
  await api("/api/logout", { method: "POST" });
  toast("Logged out.");
  refreshStatus();
}

function updateEngineUI() {
  const isBrowser = state.engine === "browser";
  $("#mobile-label").classList.toggle("hidden", !isBrowser);
}

// ---- download flow -------------------------------------------------------
async function startDownload(evt) {
  if (evt) evt.preventDefault();
  const profile = $("#profile-input").value.trim().replace(/^@/, "");
  if (!profile) return toast("Type a profile username first.", "err");

  const { data } = await api("/api/download", {
    method: "POST",
    body: JSON.stringify({ profile, engine: state.engine, mobile: state.mobile }),
  });
  if (!data || data.status !== "started") {
    return toast((data && data.message) || "Could not start download.", "err");
  }
  showProgress();
  pollJob(data.job.job_id, profile);
}

function showProgress() {
  $("#progress-strip").classList.remove("hidden");
  $("#progress-fill").style.width = "0%";
  $("#progress-title").textContent = "Downloading…";
  $("#progress-detail").textContent = "";
}

function pollJob(jobId, profile) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    const { data } = await api(`/api/download/${jobId}`);
    if (!data) return;
    $("#progress-fill").style.width = (data.percent || 0) + "%";
    $("#progress-title").textContent =
      data.full_name ? `${data.full_name} (@${data.profile})` : `@${data.profile}`;
    $("#progress-detail").textContent =
      data.message + (data.total ? `  •  ${data.done}/${data.total}` : "");

    if (data.state === "done") {
      clearInterval(state.pollTimer);
      $("#progress-detail").textContent = data.message;
      toast("Download complete.", "ok");
      loadGallery(profile);
      setTimeout(() => $("#progress-strip").classList.add("hidden"), 2500);
    } else if (data.state === "error") {
      clearInterval(state.pollTimer);
      $("#progress-title").textContent = "Download failed";
      $("#progress-detail").textContent = data.error || "Unknown error.";
      toast(data.error || "Download failed.", "err");
      // If some media was fetched before the error, still show it.
      loadGallery(profile);
    }
  }, 1200);
}

// ---- gallery -------------------------------------------------------------
async function loadGallery(profile) {
  const { data } = await api(`/api/media/${profile}`);
  if (!data || !data.items) return;
  state.profile = profile;
  state.items = data.items;
  state.selected.clear();
  renderGallery();
}

function renderGallery() {
  const gallery = $("#gallery");
  const items = state.items.filter(
    (it) => state.filter === "all" || it.type === state.filter
  );

  if (!state.items.length) {
    gallery.innerHTML =
      `<div class="empty-state"><div class="empty-icon">∅</div>
       <h2>No media found</h2><p>Nothing was downloaded for this profile yet.</p></div>`;
    $("#toolbar").classList.add("hidden");
    return;
  }

  $("#toolbar").classList.remove("hidden");
  gallery.innerHTML = "";
  for (const it of items) {
    const card = document.createElement("div");
    card.className = "card" + (state.selected.has(it.name) ? " selected" : "");
    card.dataset.name = it.name;

    const media =
      it.type === "video"
        ? `<video src="${it.url}" preload="metadata" muted></video><span class="play-icon">▶</span>`
        : `<img src="${it.url}" loading="lazy" alt="" />`;

    card.innerHTML = `
      ${media}
      <span class="badge">${it.type}</span>
      <input type="checkbox" class="checkbox" ${state.selected.has(it.name) ? "checked" : ""} />
      <div class="card-actions">
        <button data-act="preview">Preview</button>
        <button data-act="download">Download</button>
      </div>`;

    // checkbox toggles selection
    card.querySelector(".checkbox").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleSelect(it.name, e.target.checked);
    });
    // action buttons
    card.querySelector('[data-act="preview"]').addEventListener("click", (e) => {
      e.stopPropagation();
      openLightbox(it);
    });
    card.querySelector('[data-act="download"]').addEventListener("click", (e) => {
      e.stopPropagation();
      downloadSingle(it.name);
    });
    // clicking the card body previews
    card.addEventListener("click", () => openLightbox(it));

    gallery.appendChild(card);
  }
  updateSelectionUI();
}

function toggleSelect(name, on) {
  if (on) state.selected.add(name);
  else state.selected.delete(name);
  const card = $(`.card[data-name="${CSS.escape(name)}"]`);
  if (card) card.classList.toggle("selected", on);
  updateSelectionUI();
}

function updateSelectionUI() {
  const n = state.selected.size;
  $("#selection-count").textContent = `${n} selected`;
  $("#download-selected").disabled = n === 0;
  const visible = state.items.filter(
    (it) => state.filter === "all" || it.type === state.filter
  );
  $("#select-all").checked = visible.length > 0 && visible.every((it) => state.selected.has(it.name));
}

// ---- single + batch downloads -------------------------------------------
function downloadSingle(name) {
  const url = `/api/file/${state.profile}/${encodeURIComponent(name)}?download=1`;
  triggerDownload(url);
}

async function downloadZip(files) {
  if (!state.profile) return;
  toast("Preparing ZIP…");
  const res = await fetch(`/api/zip/${state.profile}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files: files || null }),
  });
  if (!res.ok) {
    let msg = "ZIP failed.";
    try { msg = (await res.json()).message || msg; } catch (_) {}
    return toast(msg, "err");
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  triggerDownload(url, `${state.profile}_media.zip`);
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  toast("ZIP ready.", "ok");
}

function triggerDownload(url, filename) {
  const a = document.createElement("a");
  a.href = url;
  if (filename) a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---- lightbox ------------------------------------------------------------
function openLightbox(it) {
  const box = $("#lightbox-content");
  box.innerHTML =
    it.type === "video"
      ? `<video src="${it.url}" controls autoplay></video>`
      : `<img src="${it.url}" alt="" />`;
  $("#lightbox").classList.remove("hidden");
}
function closeLightbox() {
  $("#lightbox").classList.add("hidden");
  $("#lightbox-content").innerHTML = "";
}

// ---- wire up events ------------------------------------------------------
function init() {
  $("#address-form").addEventListener("submit", startDownload);
  $("#login-btn").addEventListener("click", openLogin);
  $("#logout-btn").addEventListener("click", doLogout);
  $("#close-modal").addEventListener("click", closeLogin);
  $("#do-login").addEventListener("click", doLogin);
  $("#do-2fa").addEventListener("click", do2fa);
  $("#do-browser").addEventListener("click", doBrowserLogin);
  $("#do-real-browser").addEventListener("click", openRealBrowserLogin);

  $("#engine-select").addEventListener("change", (e) => {
    state.engine = e.target.value;
    updateEngineUI();
  });
  $("#mobile-check").addEventListener("change", (e) => {
    state.mobile = e.target.checked;
  });
  updateEngineUI();

  $("#select-all").addEventListener("change", (e) => {
    const visible = state.items.filter(
      (it) => state.filter === "all" || it.type === state.filter
    );
    visible.forEach((it) => toggleSelect(it.name, e.target.checked));
  });

  $("#download-selected").addEventListener("click", () => {
    if (state.selected.size) downloadZip(Array.from(state.selected));
  });
  $("#download-all").addEventListener("click", () => downloadZip(null));

  $$(".chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      $$(".chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      state.filter = chip.dataset.filter;
      renderGallery();
    })
  );

  $("#lightbox-close").addEventListener("click", closeLightbox);
  $("#lightbox").addEventListener("click", (e) => {
    if (e.target.id === "lightbox") closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeLightbox(); closeLogin(); }
  });

  refreshStatus();
}

document.addEventListener("DOMContentLoaded", init);
