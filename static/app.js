// ── Auth guard ─────────────────────────────────────────────────────────────
const token = localStorage.getItem("jwt_token");
if (!token) {
  window.location.replace("/login");
}

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  history: [],
  streaming: false,
};

// ── DOM refs ───────────────────────────────────────────────────────────────
const messagesEl   = document.getElementById("messages");
const chatInput    = document.getElementById("chat-input");
const sendBtn      = document.getElementById("send-btn");
const sourcesList  = document.getElementById("sources-list");
const sourcesCount = document.getElementById("sources-count");
const modelSelect  = document.getElementById("model-select");
const keyInput     = document.getElementById("key-input");
const yearFrom     = document.getElementById("year-from");
const yearTo       = document.getElementById("year-to");
const quotaBadge   = document.getElementById("quota-badge");
const userEmail    = document.getElementById("user-email");
const logoutBtn    = document.getElementById("logout-btn");

// ── Auth header for API calls ──────────────────────────────────────────────
function authHeaders() {
  return {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${token}`,
  };
}

// ── Helpers ────────────────────────────────────────────────────────────────
function setSourcesEmpty(msg) {
  sourcesList.replaceChildren();
  const p = document.createElement("p");
  p.className = "sources-empty";
  p.textContent = msg;
  sourcesList.appendChild(p);
}

function updateQuotaBadge(remaining, limit, hasByok) {
  if (hasByok) {
    quotaBadge.textContent = "Unlimited (own key)";
    quotaBadge.className = "quota-badge";
    return;
  }
  quotaBadge.textContent = `${remaining} / ${limit} queries left today`;
  quotaBadge.className = "quota-badge" + (remaining === 0 ? " zero" : remaining <= 3 ? " low" : "");
}

// ── Init ───────────────────────────────────────────────────────────────────
async function init() {
  // Restore BYOK key
  const savedKey = sessionStorage.getItem("byok_key");
  if (savedKey) keyInput.value = savedKey;

  // Load models + user info in parallel
  const [healthResp, meResp] = await Promise.all([
    fetch("/api/health", { headers: authHeaders() }),
    fetch("/api/auth/me", { headers: authHeaders() }),
  ]);

  if (meResp.status === 401) {
    localStorage.removeItem("jwt_token");
    window.location.replace("/login");
    return;
  }

  // Populate model dropdown
  try {
    const health = await healthResp.json();
    for (const m of health.models) {
      const opt = document.createElement("option");
      opt.value = m.key;
      opt.textContent = m.display_name;
      modelSelect.appendChild(opt);
    }
  } catch {
    for (const [key, name] of [
      ["claude-haiku", "Claude Haiku (fast)"],
      ["claude-sonnet", "Claude Sonnet (best)"],
      ["gpt-4o-mini", "GPT-4o Mini (fast)"],
      ["gpt-4o", "GPT-4o (best)"],
    ]) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = name;
      modelSelect.appendChild(opt);
    }
  }

  // Show user info + quota
  try {
    const me = await meResp.json();
    userEmail.textContent = me.email;
    updateQuotaBadge(me.queries_remaining, me.free_limit, !!keyInput.value.trim());
    if (me.is_admin) {
      const adminLink = document.createElement("a");
      adminLink.href = "/admin";
      adminLink.textContent = "Admin";
      adminLink.style.cssText = "font-size:0.78rem;color:var(--text-muted);text-decoration:none;";
      adminLink.onmouseenter = () => adminLink.style.color = "var(--text)";
      adminLink.onmouseleave = () => adminLink.style.color = "var(--text-muted)";
      document.querySelector(".user-menu").prepend(adminLink);
    }
  } catch { /* ignore */ }
}

// ── BYOK key ───────────────────────────────────────────────────────────────
keyInput.addEventListener("input", () => {
  const v = keyInput.value.trim();
  if (v) sessionStorage.setItem("byok_key", v);
  else sessionStorage.removeItem("byok_key");
  // Re-fetch quota display to switch unlimited/limited
  fetch("/api/auth/me", { headers: authHeaders() })
    .then(r => r.json())
    .then(me => updateQuotaBadge(me.queries_remaining, me.free_limit, !!v))
    .catch(() => {});
});

// ── Logout ─────────────────────────────────────────────────────────────────
logoutBtn.addEventListener("click", () => {
  localStorage.removeItem("jwt_token");
  sessionStorage.removeItem("byok_key");
  window.location.replace("/login");
});

// ── House filter pills ─────────────────────────────────────────────────────
document.querySelectorAll(".pill[data-house]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".pill[data-house]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
  });
});

function getActiveHouse() {
  const active = document.querySelector(".pill[data-house].active");
  return active ? active.dataset.house : "";
}

// ── Input auto-resize ──────────────────────────────────────────────────────
chatInput.addEventListener("input", () => {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 140) + "px";
});

chatInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
});

sendBtn.addEventListener("click", handleSend);

// ── Message rendering ──────────────────────────────────────────────────────
function appendMessage(role, content = "") {
  const div = document.createElement("div");
  div.className = `message ${role}`;

  const avatarDiv = document.createElement("div");
  avatarDiv.className = "avatar";
  avatarDiv.textContent = role === "user" ? "You" : "YD";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = content;

  div.appendChild(avatarDiv);
  div.appendChild(bubble);
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function appendError(message) {
  const div = document.createElement("div");
  div.className = "error-msg";
  div.textContent = message;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ── Source card rendering ──────────────────────────────────────────────────
function renderSourceCards(cards) {
  sourcesList.replaceChildren();
  sourcesCount.textContent = cards.length;
  if (!cards.length) {
    setSourcesEmpty("No relevant sources found.");
    return;
  }
  cards.forEach((card, i) => {
    const el = buildSourceCard(card, i + 1);
    el.style.animationDelay = `${i * 40}ms`;
    sourcesList.appendChild(el);
  });
}

function buildSourceCard(card, n) {
  const houseClass = { "Dáil": "dail", "Seanad": "seanad" }[card.house] || "committee";
  const scorePercent = Math.round(card.score * 100);

  const el = document.createElement("div");
  el.className = "source-card";

  const header = document.createElement("div");
  header.className = "card-header";

  const numBadge = document.createElement("span");
  numBadge.className = "source-num";
  numBadge.textContent = n;

  const houseBadge = document.createElement("span");
  houseBadge.className = `house-badge ${houseClass}`;
  // Committee codes look like "committee_of_public_accounts"
  houseBadge.textContent = card.committee
    ? card.committee.replace(/_/g, " ").replace(/^./, c => c.toUpperCase())
    : card.house;

  const dateChip = document.createElement("span");
  dateChip.className = "date-chip";
  dateChip.textContent = card.date;

  header.appendChild(numBadge);
  header.appendChild(houseBadge);
  header.appendChild(dateChip);

  if (card.chunk_type === "table") {
    const tableBadge = document.createElement("span");
    tableBadge.className = "table-badge";
    tableBadge.textContent = "TABLE";
    header.appendChild(tableBadge);
  }

  const heading = document.createElement("div");
  heading.className = "card-heading";
  heading.textContent = card.heading_path || card.heading || "(no heading)";

  const minister = document.createElement("div");
  minister.className = "card-minister";
  // Debate/committee speeches name the speaker; PQ answers name the minister
  const person = card.speaker || card.answering_person;
  const role = card.speaker ? card.speaker_role : card.answering_role;
  minister.textContent = role ? `${person} · ${role}` : person;

  const excerpt = document.createElement("div");
  excerpt.className = "card-excerpt";
  excerpt.textContent = card.excerpt;

  const showMore = document.createElement("span");
  showMore.className = "show-more";
  showMore.textContent = "Show more";
  showMore.addEventListener("click", e => {
    e.stopPropagation();
    const expanded = excerpt.classList.toggle("expanded");
    showMore.textContent = expanded ? "Show less" : "Show more";
  });

  const bar = document.createElement("div");
  bar.className = "relevance-bar";
  const fill = document.createElement("div");
  fill.className = "relevance-fill";
  fill.style.setProperty("--score", `${scorePercent}%`);
  fill.title = `Relevance: ${scorePercent}%`;
  bar.appendChild(fill);

  el.appendChild(header);
  el.appendChild(heading);
  el.appendChild(minister);
  el.appendChild(excerpt);
  if (card.excerpt && card.excerpt.length >= 295) el.appendChild(showMore);
  el.appendChild(bar);
  return el;
}

// ── SSE stream consumer ────────────────────────────────────────────────────
async function streamChat(userText, bubble) {
  const byok = keyInput.value.trim();
  const payload = {
    message: userText,
    history: state.history,
    model_key: modelSelect.value,
    api_key: byok || null,
    filters: {
      house: getActiveHouse() || null,
      year_from: yearFrom.value ? parseInt(yearFrom.value) : null,
      year_to: yearTo.value ? parseInt(yearTo.value) : null,
    },
  };

  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(payload),
  });

  if (resp.status === 401) {
    localStorage.removeItem("jwt_token");
    window.location.replace("/login");
    return null;
  }

  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "";
  let assistantText = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();

    for (const line of lines) {
      if (line.startsWith("event:")) {
        currentEvent = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        const raw = line.slice(5).trim();
        try {
          const obj = JSON.parse(raw);
          if (currentEvent === "sources") {
            renderSourceCards(obj);
          } else if (currentEvent === "token") {
            assistantText += obj.text;
            bubble.textContent = assistantText;
            messagesEl.scrollTop = messagesEl.scrollHeight;
          } else if (currentEvent === "error") {
            bubble.classList.remove("streaming");
            appendError(obj.message);
            return null;
          } else if (currentEvent === "done" && obj.queries_remaining !== undefined) {
            // Update quota badge from server-confirmed count
            const limit = parseInt(quotaBadge.textContent.split("/")[1]) || 10;
            updateQuotaBadge(obj.queries_remaining, limit, !!byok);
          }
        } catch { /* malformed JSON — ignore */ }
      }
    }
  }

  bubble.classList.remove("streaming");

  // Render markdown → sanitized HTML once streaming is complete
  if (assistantText) {
    bubble.innerHTML = DOMPurify.sanitize(marked.parse(assistantText));
    bubble.classList.add("rendered");
  }

  return assistantText;
}

// ── Main send handler ──────────────────────────────────────────────────────
async function handleSend() {
  const text = chatInput.value.trim();
  if (!text || state.streaming) return;

  state.streaming = true;
  sendBtn.disabled = true;
  chatInput.value = "";
  chatInput.style.height = "auto";

  appendMessage("user", text);
  const bubble = appendMessage("assistant", "");
  bubble.classList.add("streaming");

  setSourcesEmpty("Searching…");
  sourcesCount.textContent = "…";

  try {
    const assistantText = await streamChat(text, bubble);
    if (assistantText) {
      state.history.push({ role: "user", content: text });
      state.history.push({ role: "assistant", content: assistantText });
      if (state.history.length > 12) state.history = state.history.slice(-12);
    }
  } catch (err) {
    bubble.classList.remove("streaming");
    bubble.textContent = "";
    appendError(err.message || "Request failed");
    setSourcesEmpty("No sources loaded.");
    sourcesCount.textContent = "0";
  } finally {
    state.streaming = false;
    sendBtn.disabled = false;
    chatInput.focus();
  }
}

// ── Boot ───────────────────────────────────────────────────────────────────
init();
