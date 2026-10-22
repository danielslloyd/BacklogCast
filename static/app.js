// BacklogCast — vanilla JS, no build step.
//
// One kanban board polled from /api/board: every article is a card in the
// column for its state, and the two worker queues are the columns that carry
// live progress. Settings and the article editor are popups over it.

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

function setStatus(sel, text, kind) {
  const el = $(sel);
  el.textContent = text;
  el.style.color = kind === "err" ? "var(--danger)" : kind === "ok" ? "var(--ok)" : "";
}

// --- state vocabulary -----------------------------------------------------
// Must track app/articles.py STATES.

const STATE_LABEL = {
  queued: "queued",
  extracting: "extracting",
  book_built: "text ready",
  needs_review: "needs review",
  approved: "waiting to narrate",
  synthesizing: "narrating",
  stitched: "audio ready",
  published: "published",
  failed: "failed",
  text_ready: "text ready",
  cancelled: "cancelled",
  removed: "removed",
};

// The one action that moves a card forward, by state.
const PRIMARY_ACTION = {
  text_ready: ["Narrate", "approve"],
  book_built: ["Narrate", "approve"],
  needs_review: ["Narrate", "approve"],
  stitched: ["Publish", "publish"],
  published: ["Unpublish", "unpublish"],
  failed: ["Retry", "retry"],
  cancelled: ["Narrate", "approve"],
};

// --- overlays (settings popup, article editor) ----------------------------

function openOverlay(id) {
  $(`#${id}`).classList.remove("hidden");
  document.body.classList.add("locked");
}

function closeOverlay(id) {
  $(`#${id}`).classList.add("hidden");
  if (!$$(".overlay:not(.hidden)").length) document.body.classList.remove("locked");
  if (id === "preview-overlay") currentSlug = null;
}

$$("[data-close-overlay]").forEach(btn => {
  btn.addEventListener("click", () => closeOverlay(btn.dataset.closeOverlay));
});

$$(".overlay").forEach(ov => {
  ov.addEventListener("click", e => { if (e.target === ov) closeOverlay(ov.id); });
});

document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  const open = $$(".overlay:not(.hidden)");
  if (open.length) closeOverlay(open[open.length - 1].id);
});

$("#menu-btn").addEventListener("click", async () => {
  const btn = $("#menu-btn");
  const isOpen = !$("#settings-overlay").classList.contains("hidden");
  if (isOpen) {
    closeOverlay("settings-overlay");
    btn.setAttribute("aria-expanded", "false");
    return;
  }
  openOverlay("settings-overlay");
  btn.setAttribute("aria-expanded", "true");
  setStatus("#config-status", "");
  try {
    await Promise.all([loadConfig(), loadTokens()]);
  } catch (e) {
    setStatus("#config-status", `Couldn't load settings: ${e.message}`, "err");
  }
});

// --- add article ---------------------------------------------------------

$("#add-url-btn").addEventListener("click", addFromUrl);
$("#add-url").addEventListener("keydown", e => { if (e.key === "Enter") addFromUrl(); });

async function addFromUrl() {
  const url = $("#add-url").value.trim();
  if (!url) return;
  setStatus("#add-status", "Fetching…");
  try {
    const meta = await api("POST", "/api/articles", { url });
    $("#add-url").value = "";
    setStatus("#add-status", `Added: ${meta.title}`, "ok");
    await refreshBoard(true);
  } catch (e) {
    setStatus("#add-status", `Error: ${e.message}`, "err");
  }
}

$("#paste-toggle").addEventListener("click", () => {
  $("#paste-row").classList.toggle("hidden");
  if (!$("#paste-row").classList.contains("hidden")) $("#paste-text").focus();
});
$("#paste-cancel").addEventListener("click", () => $("#paste-row").classList.add("hidden"));

$("#paste-btn").addEventListener("click", async () => {
  const text = $("#paste-text").value.trim();
  if (!text) return;
  const title = $("#paste-title").value.trim();
  setStatus("#add-status", "Saving…");
  try {
    const meta = await api("POST", "/api/articles", { title, text });
    $("#paste-text").value = "";
    $("#paste-title").value = "";
    $("#paste-row").classList.add("hidden");
    setStatus("#add-status", `Added: ${meta.title}`, "ok");
    await refreshBoard(true);
  } catch (e) {
    setStatus("#add-status", `Error: ${e.message}`, "err");
  }
});

// --- the board -----------------------------------------------------------

let lastSignature = "";
let searchTerm = "";

$("#search").addEventListener("input", e => {
  searchTerm = e.target.value.trim().toLowerCase();
  lastSignature = "";  // force a redraw with the new filter
  if (lastBoard) renderBoard(lastBoard);
});

let lastBoard = null;

async function refreshBoard(force = false) {
  try {
    const data = await api("GET", "/api/board");
    lastBoard = data;
    if (force) lastSignature = "";
    renderBoard(data);
    setStatus("#board-status", data.henty_up ? "" : "Henty offline",
      data.henty_up ? "" : "err");
  } catch (e) {
    setStatus("#board-status", `Offline: ${e.message}`, "err");
  }
}

function matchesSearch(item) {
  if (!searchTerm) return true;
  return `${item.title || ""} ${item.slug}`.toLowerCase().includes(searchTerm);
}

function renderBoard(data) {
  // Redraw only on real change, so hovering a card doesn't fight the poll --
  // except while a job runs, where the "for 2m" clock has to keep ticking.
  const running = data.columns.some(c => c.items.some(m => m.running));
  const signature = JSON.stringify([data.columns, data.active, data.henty_up, searchTerm,
                                    running ? data.now : ""]);
  if (signature === lastSignature) return;
  lastSignature = signature;

  const board = $("#board");
  const scrolls = {};
  $$(".col-cards", board).forEach(ul => { scrolls[ul.dataset.key] = ul.scrollTop; });

  board.replaceChildren();
  for (const col of data.columns) {
    const shown = col.items.filter(matchesSearch);
    const section = document.createElement("section");
    section.className = "column";
    section.dataset.key = col.key;

    const head = document.createElement("header");
    head.className = "col-head";
    head.innerHTML = `<h2>${escapeHtml(col.label)}</h2>` +
      `<span class="count">${shown.length}${shown.length !== col.items.length ? ` / ${col.items.length}` : ""}</span>`;
    section.append(head);

    if (col.note) {
      const note = document.createElement("p");
      note.className = "col-note";
      note.textContent = col.note;
      section.append(note);
    }

    const ul = document.createElement("ul");
    ul.className = "col-cards";
    ul.dataset.key = col.key;
    if (!shown.length) {
      const li = document.createElement("li");
      li.className = "col-empty";
      li.textContent = col.items.length ? "Nothing matches the filter." : "Empty.";
      ul.append(li);
    }
    shown.forEach(item => ul.append(card(item, data)));
    section.append(ul);
    board.append(section);
  }

  $$(".col-cards", board).forEach(ul => {
    if (scrolls[ul.dataset.key]) ul.scrollTop = scrolls[ul.dataset.key];
  });
}

function card(m, data) {
  const li = document.createElement("li");
  li.className = "card-item";
  li.dataset.slug = m.slug;
  if (m.running) li.classList.add("running");
  if (m.state === "failed") li.classList.add("failed");
  if (!m.due) li.classList.add("backoff");
  if (m.state === "removed") li.classList.add("muted");

  const title = document.createElement("button");
  title.className = "card-title";
  title.textContent = m.title || m.slug;
  title.addEventListener("click", () => openPreview(m.slug));
  li.append(title);

  const by = [m.author, m.publication].filter(Boolean).join(" · ");
  if (by) {
    const el = document.createElement("div");
    el.className = "card-by";
    el.textContent = by;
    li.append(el);
  }

  const bits = statusBits(m, data);
  if (bits.length) {
    const el = document.createElement("div");
    el.className = "card-status";
    el.textContent = bits.join(" · ");
    li.append(el);
  }

  const a = data.active || {};
  if (m.running && a.slug === m.slug) {
    const bar = document.createElement("div");
    bar.className = a.total ? "bar" : "bar indeterminate";
    bar.innerHTML = `<span style="width:${a.total ? Math.round(100 * (a.done || 0) / a.total) : 30}%"></span>`;
    li.append(bar);
  }

  const primary = PRIMARY_ACTION[m.state];
  const actions = document.createElement("div");
  actions.className = "card-actions";
  const pill = document.createElement("span");
  pill.className = `state ${m.state}`;
  pill.textContent = STATE_LABEL[m.state] || m.state;
  actions.append(pill);
  if (m.source_url) {
    const link = document.createElement("a");
    link.href = m.source_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.className = "card-link";
    link.textContent = "source";
    actions.append(link);
  }
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  actions.append(spacer);
  if (primary) {
    actions.append(makeBtn(primary[0], () => doAction(m.slug, primary[1]),
      m.state === "stitched" ? "primary" : ""));
  }
  li.append(actions);
  return li;
}

function statusBits(m, data) {
  const bits = [];
  const a = data.active || {};
  if (m.running) {
    if (a.slug === m.slug) {
      bits.push(a.step + (a.total ? ` — chunk ${a.done || 0} of ${a.total}` : ""));
      bits.push(`for ${fmtElapsed(a.started, data.now)}`);
    } else {
      bits.push("starting…");
    }
    return bits;
  }
  if (!m.due && m.retry_after) {
    const stage = m.state === "approved" ? "synth" : "extract";
    const attempts = stage === "synth" ? m.synth_attempts : m.extract_attempts;
    bits.push(`retry at ${fmtWhen(m.retry_after)} (attempt ${(attempts || 0) + 1} of 4)`);
  } else if (m.queue_pos && m.queue_pos > 1) {
    bits.push(`#${m.queue_pos} in line`);
  } else if (m.queue_pos === 1) {
    bits.push("next up");
  }
  if (m.state === "failed" && m.error) bits.push(m.error);
  if (m.state === "removed") bits.push("taken off the reading list");
  if (m.state === "needs_review" && m.review_reason) bits.push(m.review_reason);
  if (m.duration_seconds) bits.push(fmtHMS(m.duration_seconds));
  else if (m.n_chunks) bits.push(`${m.n_chunks} chunks`);
  if (m.narrate && ["text_ready", "book_built", "queued"].includes(m.state)) {
    bits.push("narration requested");
  }
  return bits;
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
    await refreshBoard(true);
    if (currentSlug === slug) await openPreview(slug);
  } catch (e) {
    alert(`Action failed: ${e.message}`);
  }
}

async function confirmDeleteAudio(slug) {
  if (!confirm(
    `Delete the audio for "${slug}"?\n\n` +
    `The article text is kept. This also clears Henty's project folder ` +
    `(every per-chunk take) and removes it from the feed.\n\n` +
    `Use "Narrate" to re-narrate it later.`
  )) return;
  try {
    const m = await api("POST", `/api/articles/${encodeURIComponent(slug)}/delete-audio`);
    setStatus("#add-status", `Deleted audio for "${slug}" — freed ${fmtBytes(m.freed_bytes)}.`, "ok");
    await refreshBoard(true);
    if (currentSlug === slug) await openPreview(slug);
  } catch (e) {
    alert(`Delete audio failed: ${e.message}`);
  }
}

async function confirmDelete(slug) {
  if (!confirm(`Delete "${slug}"? This removes the audio and metadata.`)) return;
  try {
    await api("DELETE", `/api/articles/${encodeURIComponent(slug)}`);
    if (currentSlug === slug) closeOverlay("preview-overlay");
    await refreshBoard(true);
  } catch (e) {
    alert(`Delete failed: ${e.message}`);
  }
}

// --- article editor ------------------------------------------------------

let currentSlug = null;

async function openPreview(slug) {
  currentSlug = slug;
  openOverlay("preview-overlay");
  const m = await api("GET", `/api/articles/${encodeURIComponent(slug)}`);
  if (currentSlug !== slug) return;  // closed while loading
  $("#preview-title").textContent = m.title || slug;
  $("#edit-title").value = m.title || "";
  $("#edit-author").value = m.author || "";
  $("#edit-body").value = m.body || "";
  renderPreview(m.body || "");
  const estMin = Math.round((m.estimated_seconds || 0) / 60);
  $("#preview-meta").textContent =
    `${STATE_LABEL[m.state] || m.state} · ${m.word_count} words · ~${estMin} min` +
    ` · ${m.extraction_method}${m.source_url ? " · " + m.source_url : ""}` +
    `${m.error ? " · error: " + m.error : ""}`;
  showPreviewActions(m);

  const aw = $("#audio-wrap");
  if (m.state === "published") {
    // Audio is only served behind a feed token, so borrow one to preview it.
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

function showPreviewActions(m) {
  const show = {
    "approve-btn": ["text_ready", "book_built", "needs_review", "cancelled"].includes(m.state),
    "retry-btn": m.state === "failed" || !!m.retry_after,
    "publish-btn": m.state === "stitched",
    "unpublish-btn": m.state === "published",
    "delete-audio-btn": !!m.audio_filename || ["stitched", "published"].includes(m.state),
    "reextract-btn": !!m.source_url,
  };
  for (const [id, on] of Object.entries(show)) {
    $(`#${id}`).classList.toggle("hidden", !on);
  }
}

function renderPreview(md) {
  $("#rendered").innerHTML = marked.parse(md || "");
}

$("#edit-body").addEventListener("input", e => renderPreview(e.target.value));

$("#save-btn").addEventListener("click", async () => {
  if (!currentSlug) return;
  try {
    await api("PUT", `/api/articles/${encodeURIComponent(currentSlug)}`, {
      title: $("#edit-title").value,
      author: $("#edit-author").value,
      body: $("#edit-body").value,
    });
    await refreshBoard(true);
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
$("#delete-audio-btn").addEventListener("click", () => currentSlug && confirmDeleteAudio(currentSlug));
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
    setStatus("#config-status", "Saved.", "ok");
  } catch (e) {
    setStatus("#config-status", `Save failed: ${e.message}`, "err");
  }
});

async function loadTokens() {
  const data = await api("GET", "/api/tokens");
  const tbody = $("#tokens-table tbody");
  tbody.replaceChildren();
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
  } catch (e) {
    setStatus("#config-status", `Create failed: ${e.message}`, "err");
  }
});

// --- helpers -------------------------------------------------------------

function fmtHMS(sec) {
  sec = parseInt(sec || 0, 10);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return (h ? `${h}:` : "") + `${String(m).padStart(h ? 2 : 1, "0")}:${String(s).padStart(2, "0")}`;
}

function fmtWhen(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
}

function fmtElapsed(fromIso, nowIso) {
  if (!fromIso) return "a moment";
  const s = Math.max(0, (Date.parse(nowIso) - Date.parse(fromIso)) / 1000);
  return s < 60 ? `${Math.round(s)}s` : fmtHMS(s);
}

function fmtBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// --- live updates --------------------------------------------------------

setInterval(() => { if (!document.hidden) refreshBoard(); }, 2000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshBoard(); });

refreshBoard();
