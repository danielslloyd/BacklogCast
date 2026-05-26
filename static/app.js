// BacklogCast — vanilla JS, no build step.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).error || detail; } catch {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

// --- tabs -----------------------------------------------------------------
$$(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach(b => b.classList.toggle("active", b === btn));
    $$(".tab-panel").forEach(p => {
      p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`);
    });
    if (btn.dataset.tab === "settings") { loadConfig(); loadTokens(); }
  });
});

// --- add article ---------------------------------------------------------
$("#add-url-btn").addEventListener("click", async () => {
  const url = $("#add-url").value.trim();
  if (!url) return;
  setStatus("#add-status", "Fetching...");
  try {
    const meta = await api("POST", "/api/articles", { url });
    $("#add-url").value = "";
    setStatus("#add-status", `Added: ${meta.title}`, "ok");
    await refreshArticles();
    openPreview(meta.slug);
  } catch (e) {
    setStatus("#add-status", `Error: ${e.message}`, "err");
  }
});

$("#paste-btn").addEventListener("click", async () => {
  const text = $("#paste-text").value.trim();
  if (!text) return;
  const title = $("#paste-title").value.trim();
  setStatus("#add-status", "Saving...");
  try {
    const meta = await api("POST", "/api/articles", { title, text });
    $("#paste-text").value = "";
    $("#paste-title").value = "";
    setStatus("#add-status", `Added: ${meta.title}`, "ok");
    await refreshArticles();
    openPreview(meta.slug);
  } catch (e) {
    setStatus("#add-status", `Error: ${e.message}`, "err");
  }
});

function setStatus(sel, text, kind) {
  const el = $(sel);
  el.textContent = text;
  el.style.color = kind === "err" ? "var(--danger)" : kind === "ok" ? "var(--ok)" : "";
}

// --- articles list -------------------------------------------------------
$("#refresh-btn").addEventListener("click", refreshArticles);
$("#state-filter").addEventListener("change", refreshArticles);

async function refreshArticles() {
  const state = $("#state-filter").value;
  const path = state ? `/api/articles?state=${encodeURIComponent(state)}` : "/api/articles";
  const data = await api("GET", path);
  const tbody = $("#articles-table tbody");
  tbody.innerHTML = "";
  for (const m of data.items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><a href="#" data-slug="${escapeHtml(m.slug)}" class="open-preview">${escapeHtml(m.title || m.slug)}</a></td>
      <td><span class="state ${escapeHtml(m.state)}">${escapeHtml(m.state)}</span></td>
      <td>${m.source_url ? `<a href="${escapeAttr(m.source_url)}" target="_blank" rel="noopener">link</a>` : "—"}</td>
      <td>${m.duration_seconds ? fmtHMS(m.duration_seconds) : "—"}</td>
      <td><div class="row"></div></td>
    `;
    const actions = $("td:last-child .row", tr);
    actions.appendChild(makeBtn("preview", () => openPreview(m.slug)));
    if (["fetched", "needs_review"].includes(m.state)) {
      actions.appendChild(makeBtn("approve", () => doAction(m.slug, "approve")));
    }
    if (m.state === "failed") {
      actions.appendChild(makeBtn("retry", () => doAction(m.slug, "retry")));
    }
    if (m.state === "ready") {
      actions.appendChild(makeBtn("publish", () => doAction(m.slug, "publish"), "primary"));
    }
    if (m.state === "published") {
      actions.appendChild(makeBtn("unpublish", () => doAction(m.slug, "unpublish")));
    }
    actions.appendChild(makeBtn("delete", () => confirmDelete(m.slug), "danger"));
    tbody.appendChild(tr);
  }
  $$(".open-preview").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    openPreview(e.target.dataset.slug);
  }));
}

function makeBtn(label, onClick, cls) {
  const b = document.createElement("button");
  b.textContent = label;
  if (cls) b.className = cls;
  b.addEventListener("click", onClick);
  return b;
}

async function doAction(slug, action) {
  try {
    await api("POST", `/api/articles/${encodeURIComponent(slug)}/${action}`);
    await refreshArticles();
    if (currentSlug === slug) await openPreview(slug);
  } catch (e) {
    alert(`Action failed: ${e.message}`);
  }
}

async function confirmDelete(slug) {
  if (!confirm(`Delete "${slug}"? This removes the audio and metadata.`)) return;
  try {
    await api("DELETE", `/api/articles/${encodeURIComponent(slug)}`);
    if (currentSlug === slug) closePreview();
    await refreshArticles();
  } catch (e) {
    alert(`Delete failed: ${e.message}`);
  }
}

// --- preview pane --------------------------------------------------------
let currentSlug = null;

async function openPreview(slug) {
  currentSlug = slug;
  $("#preview-card").classList.remove("hidden");
  $("#preview-card").scrollIntoView({ behavior: "smooth", block: "start" });
  const m = await api("GET", `/api/articles/${encodeURIComponent(slug)}`);
  $("#preview-title").textContent = m.title || slug;
  $("#edit-title").value = m.title || "";
  $("#edit-author").value = m.author || "";
  $("#edit-body").value = m.body || "";
  renderPreview(m.body || "");
  const estMin = Math.round((m.estimated_seconds || 0) / 60);
  $("#preview-meta").textContent = `state: ${m.state} · ${m.word_count} words · ~${estMin} min · source: ${m.source_url || "(none)"} · method: ${m.extraction_method}${m.error ? " · error: " + m.error : ""}`;
  // audio
  if (m.state === "published" || m.state === "ready") {
    // ready audio isn't behind a token — we only stream via published. For local preview, fetch it directly via a temporary inline player from filesystem-only? We don't have such an endpoint. Just show player when published.
  }
  const aw = $("#audio-wrap");
  if (m.state === "published") {
    // pick any token to preview
    const t = await api("GET", "/api/tokens");
    const tok = t.tokens[0]?.token;
    const ext = (m.audio_filename || "audio.mp3").split(".").pop();
    if (tok) {
      aw.classList.remove("hidden");
      $("#audio-player").src = `/audio/${encodeURIComponent(tok)}/${encodeURIComponent(slug)}.${ext}`;
    } else {
      aw.classList.add("hidden");
    }
  } else {
    aw.classList.add("hidden");
    $("#audio-player").removeAttribute("src");
  }
}

function renderPreview(md) {
  $("#rendered").innerHTML = marked.parse(md || "");
}

$("#edit-body").addEventListener("input", e => renderPreview(e.target.value));

$("#preview-close").addEventListener("click", closePreview);
function closePreview() {
  currentSlug = null;
  $("#preview-card").classList.add("hidden");
}

$("#save-btn").addEventListener("click", async () => {
  if (!currentSlug) return;
  try {
    await api("PUT", `/api/articles/${encodeURIComponent(currentSlug)}`, {
      title: $("#edit-title").value,
      author: $("#edit-author").value,
      body: $("#edit-body").value,
    });
    await refreshArticles();
    await openPreview(currentSlug);
  } catch (e) { alert(`Save failed: ${e.message}`); }
});

for (const [id, action] of [
  ["reextract-btn", "reextract"],
  ["approve-btn", "approve"],
  ["retry-btn", "retry"],
  ["publish-btn", "publish"],
  ["unpublish-btn", "unpublish"],
]) {
  $(`#${id}`).addEventListener("click", () => currentSlug && doAction(currentSlug, action));
}
$("#delete-btn").addEventListener("click", () => currentSlug && confirmDelete(currentSlug));

// --- settings ------------------------------------------------------------
async function loadConfig() {
  const c = await api("GET", "/api/config");
  $("#cfg-title").value = c.podcast_title;
  $("#cfg-description").value = c.podcast_description;
  $("#cfg-language").value = c.podcast_language;
  $("#cfg-author").value = c.podcast_author;
  $("#cfg-owner-name").value = c.owner_name;
  $("#cfg-owner-email").value = c.owner_email;
  $("#cfg-category").value = c.category;
  $("#cfg-explicit").checked = !!c.explicit;
  $("#cfg-base-url").value = c.public_base_url || "";
  $("#cfg-wpm").value = c.wpm;
}

$("#save-config").addEventListener("click", async () => {
  try {
    await api("PUT", "/api/config", {
      podcast_title: $("#cfg-title").value,
      podcast_description: $("#cfg-description").value,
      podcast_language: $("#cfg-language").value,
      podcast_author: $("#cfg-author").value,
      owner_name: $("#cfg-owner-name").value,
      owner_email: $("#cfg-owner-email").value,
      category: $("#cfg-category").value,
      explicit: $("#cfg-explicit").checked,
      public_base_url: $("#cfg-base-url").value,
      wpm: parseInt($("#cfg-wpm").value || "160", 10),
    });
    alert("Saved.");
  } catch (e) { alert(`Save failed: ${e.message}`); }
});

async function loadTokens() {
  const data = await api("GET", "/api/tokens");
  const tbody = $("#tokens-table tbody");
  tbody.innerHTML = "";
  const base = location.origin;
  for (const t of data.tokens) {
    const tr = document.createElement("tr");
    const url = `${base}/feed/${encodeURIComponent(t.token)}.xml`;
    tr.innerHTML = `
      <td>${escapeHtml(t.name)}</td>
      <td><code>${escapeHtml(url)}</code></td>
      <td></td>
    `;
    const actions = $("td:last-child", tr);
    actions.appendChild(makeBtn("copy", () => navigator.clipboard.writeText(url)));
    actions.appendChild(makeBtn("revoke", async () => {
      if (!confirm(`Revoke token "${t.name}"?`)) return;
      await api("DELETE", `/api/tokens/${encodeURIComponent(t.token)}`);
      await loadTokens();
    }, "danger"));
    tbody.appendChild(tr);
  }
}

$("#create-token-btn").addEventListener("click", async () => {
  const name = $("#new-token-name").value.trim();
  if (!name) return;
  try {
    await api("POST", "/api/tokens", { name });
    $("#new-token-name").value = "";
    await loadTokens();
  } catch (e) { alert(`Create failed: ${e.message}`); }
});

// --- helpers -------------------------------------------------------------
function fmtHMS(sec) {
  sec = parseInt(sec || 0, 10);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return (h ? `${h}:` : "") + `${String(m).padStart(h ? 2 : 1, "0")}:${String(s).padStart(2, "0")}`;
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

// --- auto-refresh while there are in-flight items -----------------------
setInterval(async () => {
  try {
    const data = await api("GET", "/api/articles");
    const busy = data.items.some(m => m.state === "approved" || m.state === "synthesizing");
    if (busy) await refreshArticles();
  } catch {}
}, 5000);

// initial load
refreshArticles();
