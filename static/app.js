// Chat UI + citation viewer. Vanilla JS, no build step. All user/model text is inserted
// with textContent (never innerHTML) so answers and documents cannot inject markup.
"use strict";
const $ = (id) => document.getElementById(id);
const state = { sessionId: null, busy: false, activeCite: null };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

// "Doc title > Section" -> "Section" (the title is already shown next to it)
function sectionOf(c) {
  const s = c.section || "";
  return s.startsWith(c.title + " > ") ? s.slice(c.title.length + 3) : s === c.title ? "" : s;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

// ---------------------------------------------------------------- messages
function renderAnswerText(bubble, text, citations) {
  // Turn [1], [1][3], [1, 3] into clickable chips linked to their citation.
  const byN = new Map(citations.map((c) => [c.n, c]));
  const re = /\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    bubble.appendChild(document.createTextNode(text.slice(last, m.index)));
    for (const part of m[1].split(/\s*[,;]\s*/)) {
      const c = byN.get(Number(part));
      if (!c) { bubble.appendChild(document.createTextNode(`[${part}]`)); continue; }
      const chip = el("button", "cite", part);
      chip.type = "button";
      chip.title = `${c.title}${sectionOf(c) ? " › " + sectionOf(c) : ""}`;
      chip.dataset.chunk = c.chunk_id;
      chip.onclick = () => openCitation(c);
      bubble.appendChild(chip);
    }
    last = re.lastIndex;
  }
  bubble.appendChild(document.createTextNode(text.slice(last)));
}

function addMessage(role, text, extra = {}) {
  $("empty")?.remove();
  const box = $("messages");
  const msg = el("div", `msg ${role}`);
  const bubble = el("div", "bubble");
  const citations = extra.citations || [];
  if (role === "assistant") renderAnswerText(bubble, text, citations); else bubble.textContent = text;
  msg.appendChild(bubble);

  if (role === "assistant" && citations.length) {
    const list = el("div", "sources");
    for (const c of citations) {
      const b = el("button", "source");
      b.type = "button";
      b.dataset.chunk = c.chunk_id;
      b.appendChild(el("b", "", `[${c.n}]`));
      b.appendChild(el("span", "", c.title));
      const where = c.doc_format === "paged" && c.pages.length ? `p. ${c.pages.join(", ")}`
        : c.span && c.span.line_start ? `lines ${c.span.line_start}–${c.span.line_end}` : "";
      if (where) b.appendChild(el("span", "meta", where));
      b.onclick = () => openCitation(c);
      list.appendChild(b);
    }
    msg.appendChild(list);
  }
  if (role === "assistant" && extra.request_id) {
    const meta = el("div", "meta-line");
    if (extra.history_turns_used !== undefined) meta.appendChild(el("span", "", `history: ${extra.history_turns_used} turn(s)`));
    if (extra.summary_used) meta.appendChild(el("span", "", "+ summary of earlier turns"));
    if (extra.memories_used) meta.appendChild(el("span", "", `memories: ${extra.memories_used}`));
    if (extra.standalone_query) meta.appendChild(el("span", "", `searched: “${extra.standalone_query}”`));
    for (const [label, rating] of [["👍", 1], ["👎", -1]]) {
      const b = el("button", "", label);
      b.type = "button";
      b.title = rating > 0 ? "Helpful" : "Not helpful";
      b.onclick = async () => {
        const comment = rating < 0 ? prompt("What was wrong? (optional)") : null;
        await api("/api/feedback", { method: "POST", body: JSON.stringify({ request_id: extra.request_id, rating, comment }) });
        meta.querySelectorAll("button").forEach((x) => x.remove());
        meta.appendChild(el("span", "", "thanks for the feedback"));
      };
      meta.appendChild(b);
    }
    msg.appendChild(meta);
  }
  box.appendChild(msg);
  box.scrollTop = box.scrollHeight;
  return msg;
}

// ---------------------------------------------------------------- citation viewer
function markActive(chunkId) {
  document.querySelectorAll(".cite.active, .source.active").forEach((e) => e.classList.remove("active"));
  document.querySelectorAll(`[data-chunk="${chunkId}"]`).forEach((e) => e.classList.add("active"));
}

async function openCitation(c) {
  state.activeCite = c.chunk_id;
  markActive(c.chunk_id);
  const viewer = $("viewer"), body = $("viewerBody");
  viewer.hidden = false;
  $("viewerLabel").textContent = `Source [${c.n}]`;
  $("viewerTitle").textContent = `${c.title}${sectionOf(c) ? " › " + sectionOf(c) : ""}`;
  body.replaceChildren(el("p", "spinner", "Loading source…"));
  try {
    if (c.doc_format === "paged") await showPages(c, body);
    else await showText(c, body);
  } catch (err) {
    body.replaceChildren(el("p", "spinner", "Could not load the source: " + err.message));
  }
}

async function showPages(c, body) {
  // Boxes travel with the citation; the detail call adds page image URLs and sizes.
  const detail = await api(`/api/citations/${c.chunk_id}`);
  if (state.activeCite !== c.chunk_id) return;
  body.replaceChildren(el("p", "viewer-note",
    "Highlighted boxes show exactly where this passage is in the document. Dashed boxes are estimated (part of a longer paragraph)."));
  let firstBox = null;
  for (const p of detail.pages) {
    const page = el("div", "page");
    const img = el("img");
    img.alt = `${detail.title}, page ${p.page}`;
    img.src = p.image_url;
    img.loading = "lazy";
    page.appendChild(img);
    page.appendChild(el("span", "num", `page ${p.page}`));
    for (const b of p.boxes) {
      const r = el("div", "bbox" + (b.estimated ? " estimated" : ""));
      r.style.left = `${b.x0 * 100}%`;
      r.style.top = `${b.y0 * 100}%`;
      r.style.width = `${(b.x1 - b.x0) * 100}%`;
      r.style.height = `${(b.y1 - b.y0) * 100}%`;
      page.appendChild(r);
      firstBox = firstBox || r;
    }
    const link = el("a", "viewer-note", `Download page ${p.page} with boxes (PNG)`);
    link.href = `/api/citations/${c.chunk_id}/pages/${p.page}/annotated.png`;
    link.target = "_blank";
    link.rel = "noopener";
    body.appendChild(page);
    body.appendChild(link);
    img.onload = () => { if (firstBox) firstBox.scrollIntoView({ block: "center", behavior: "smooth" }); };
  }
}

async function showText(c, body) {
  if (!c.span) { body.replaceChildren(el("p", "spinner", "No location stored for this passage.")); return; }
  const doc = await api(`/api/documents/${c.doc_id}/text`);
  if (state.activeCite !== c.chunk_id) return;
  const text = doc.source_text || "";
  const pre = el("div", "textdoc");
  pre.appendChild(document.createTextNode(text.slice(0, c.span.char_start)));
  const mark = el("mark", "", text.slice(c.span.char_start, c.span.char_end));
  pre.appendChild(mark);
  pre.appendChild(document.createTextNode(text.slice(c.span.char_end)));
  body.replaceChildren(el("p", "viewer-note",
    `Highlighted: lines ${c.span.line_start}–${c.span.line_end} of ${doc.path ? doc.path.split("/").pop() : "the document"}.`), pre);
  mark.scrollIntoView({ block: "center", behavior: "smooth" });
}

$("closeViewer").onclick = () => { $("viewer").hidden = true; state.activeCite = null; markActive("__none__"); };

// ---------------------------------------------------------------- sessions
async function loadSessions() {
  const list = await api("/api/sessions");
  const nav = $("sessions");
  nav.replaceChildren();
  for (const s of list) {
    const row = el("div", "session" + (s.session_id === state.sessionId ? " active" : ""));
    row.setAttribute("role", "button");
    row.tabIndex = 0;
    row.appendChild(el("span", "t", s.title || "New chat"));
    const del = el("button", "del", "✕");
    del.type = "button";
    del.title = "Delete conversation";
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this conversation?")) return;
      await api(`/api/sessions/${s.session_id}`, { method: "DELETE" });
      if (s.session_id === state.sessionId) newChat(); else loadSessions();
    };
    row.appendChild(del);
    row.onclick = () => openSession(s);
    row.onkeydown = (e) => { if (e.key === "Enter") openSession(s); };
    nav.appendChild(row);
  }
}

async function openSession(s) {
  state.sessionId = s.session_id;
  $("chatTitle").textContent = s.title || "Conversation";
  $("messages").replaceChildren();
  for (const m of await api(`/api/sessions/${s.session_id}/messages`)) addMessage(m.role, m.content, m);
  $("sidebar").classList.remove("open");
  loadSessions();
}

const emptyTemplate = $("empty").cloneNode(true);

function bindSuggestions(root) {
  root.querySelectorAll(".suggestions .chip").forEach((b) => { b.onclick = () => send(b.textContent); });
}

function newChat() {
  state.sessionId = null;
  $("chatTitle").textContent = "New chat";
  const empty = emptyTemplate.cloneNode(true);
  bindSuggestions(empty);
  $("messages").replaceChildren(empty);
  $("viewer").hidden = true;
  loadSessions();
}

// ---------------------------------------------------------------- send
async function send(text) {
  if (!text || state.busy) return;
  state.busy = true; $("send").disabled = true;
  addMessage("user", text);
  const pending = addMessage("assistant", "Searching the knowledge base…");
  try {
    const r = await api("/api/chat", { method: "POST", body: JSON.stringify({
      message: text, session_id: state.sessionId, client_message_id: crypto.randomUUID() }) });
    pending.remove();
    addMessage("assistant", r.answer, r);
    const isNew = !state.sessionId;
    state.sessionId = r.session_id;
    if (r.citations.length) openCitation(r.citations[0]);
    setTimeout(loadSessions, isNew ? 2500 : 0);
  } catch (err) {
    pending.querySelector(".bubble").textContent = "Error: " + err.message;
  } finally {
    state.busy = false; $("send").disabled = false; $("input").focus();
  }
}

$("composer").onsubmit = (e) => { e.preventDefault(); const t = $("input").value.trim(); $("input").value = ""; send(t); };
$("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
bindSuggestions(document);

// ---------------------------------------------------------------- long-term memory
async function showMemories() {
  const list = $("memList");
  list.replaceChildren(el("li", "spinner", "Loading…"));
  $("memDialog").showModal();
  const mems = await api("/api/memories");
  list.replaceChildren();
  if (!mems.length) list.appendChild(el("li", "", "Nothing yet. Facts you share about yourself (office, role, preferences) appear here."));
  for (const m of mems) {
    const li = el("li");
    li.appendChild(el("span", "", m.content));
    const b = el("button", "btn", "Forget");
    b.type = "button";
    b.onclick = async () => { await api(`/api/memories/${m.memory_id}`, { method: "DELETE" }); showMemories(); };
    li.appendChild(b);
    list.appendChild(li);
  }
}
$("memBtn").onclick = showMemories;
$("closeMem").onclick = () => $("memDialog").close();
$("forgetAll").onclick = async () => {
  if (confirm("Delete everything the assistant remembers about you?")) {
    await api("/api/memories", { method: "DELETE" });
    showMemories();
  }
};
$("newChat").onclick = newChat;
$("menuBtn").onclick = () => $("sidebar").classList.toggle("open");

api("/api/me").then((me) => {
  $("who").textContent = me.user_id;
  $("historyInfo").textContent = `Uses the last ${me.history_turns} turns as context`;
  $("assistantName").textContent = me.assistant_name;
  document.title = me.assistant_name;
}).catch(() => {});
loadSessions().catch(() => {});
