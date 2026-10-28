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
const sourcesCountTab = document.getElementById("sources-count-tab");
const mainEl       = document.getElementById("main");
const keyHint      = document.getElementById("key-hint");
const settingsBtn  = document.getElementById("settings-btn");
const navSettings  = document.getElementById("nav-settings");
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

  // Populate model dropdown. "paid" models need the user's own key.
  try {
    const health = await healthResp.json();
    for (const m of health.models) {
      models[m.key] = m;
      const opt = document.createElement("option");
      opt.value = m.key;
      opt.textContent = m.tier === "paid" ? `${m.display_name} (own key)` : m.display_name;
      modelSelect.appendChild(opt);
    }
    const firstFree = health.models.find(m => m.tier === "free");
    if (firstFree) modelSelect.value = firstFree.key;
  } catch {
    const opt = document.createElement("option");
    opt.value = "claude-haiku";
    opt.textContent = "Claude Haiku (fast)";
    modelSelect.appendChild(opt);
  }
  updateKeyHint();

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

// ── Model choice and BYOK key ──────────────────────────────────────────────
const models = {};  // key -> {display_name, tier, provider} from /api/health
const PROVIDER_NAMES = { claude: "Anthropic", openai: "OpenAI", deepseek: "DeepSeek", kimi: "Kimi" };

function updateKeyHint() {
  const m = models[modelSelect.value];
  if (!m) return;
  const name = PROVIDER_NAMES[m.provider] || m.provider;
  keyInput.placeholder = m.tier === "paid" ? `Your ${name} API key` : `Own ${name} key (optional)`;
  const needsKey = m.tier === "paid" && !keyInput.value.trim();
  keyHint.textContent = needsKey ? `${m.display_name} needs your own ${name} key.` : "";
  keyHint.hidden = !needsKey;
}

modelSelect.addEventListener("change", updateKeyHint);

keyInput.addEventListener("input", () => {
  updateKeyHint();
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

// ── Phones: settings panel and Chat | Sources switcher ─────────────────────
settingsBtn.addEventListener("click", () => {
  const open = navSettings.classList.toggle("open");
  settingsBtn.setAttribute("aria-expanded", String(open));
});

function showView(view) {
  mainEl.dataset.view = view;
  document.querySelectorAll(".view-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.view === view));
}

document.querySelectorAll(".view-btn").forEach(btn => {
  btn.addEventListener("click", () => showView(btn.dataset.view));
});

function isPhoneLayout() {
  return window.matchMedia("(max-width: 768px)").matches;
}

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

// e.g. "DeepSeek Flash is unavailable right now, so this answer is from Claude Haiku."
function appendNotice(message) {
  const div = document.createElement("div");
  div.className = "notice-msg";
  div.textContent = message;
  messagesEl.appendChild(div);
}

// ── Source card rendering ──────────────────────────────────────────────────
function setSourcesCount(text) {
  sourcesCount.textContent = text;
  sourcesCountTab.textContent = text;
}

function renderSourceCards(cards) {
  sourcesList.replaceChildren();
  setSourcesCount(cards.length);
  if (cards.length && mainEl.dataset.view === "chat") {
    sourcesCountTab.classList.remove("pulse");
    void sourcesCountTab.offsetWidth;  // restart the animation
    sourcesCountTab.classList.add("pulse");
  }
  if (!cards.length) {
    setSourcesEmpty("No relevant sources found.");
    return;
  }
  cards.forEach((card, i) => {
    const el = buildSourceCard(card, i + 1);
    el.dataset.n = i + 1;
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
  const fullText = document.createElement("a");
  fullText.className = "card-link";
  fullText.href = `/source/${encodeURIComponent(card.chunk_id)}`;
  fullText.target = "_blank";
  fullText.rel = "noopener";
  fullText.textContent = "Read full text ↗";
  fullText.addEventListener("click", e => e.stopPropagation());

  el.appendChild(excerpt);
  if (card.excerpt && card.excerpt.length >= 295) el.appendChild(showMore);
  el.appendChild(fullText);
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
            bubble._cards = obj;  // citations in this answer refer to these cards
            renderSourceCards(obj);
          } else if (currentEvent === "token") {
            assistantText += obj.text;
            bubble.textContent = assistantText;
            messagesEl.scrollTop = messagesEl.scrollHeight;
          } else if (currentEvent === "notice") {
            appendNotice(obj.message);
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
    wrapTables(bubble);
    linkCitations(bubble);
  }

  return assistantText;
}

// ── Answer post-processing ─────────────────────────────────────────────────
function wrapTables(root) {
  for (const table of root.querySelectorAll("table")) {
    const wrap = document.createElement("div");
    wrap.className = "table-scroll";
    table.replaceWith(wrap);
    wrap.appendChild(table);
  }
}

// "Source 3", "Sources 2 and 5", "Sources 1, 4 & 6": the number(s) become links
const CITE_RE = /\bSources?\s+\d+(?:(?:\s*,\s*|\s+(?:and|&)\s+)\d+)*/g;

function linkCitations(root) {
  // Collect first: replacing nodes while walking would confuse the walker
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.parentElement.closest("a, code, pre")) nodes.push(node);
  }
  for (const node of nodes) {
    const text = node.nodeValue;
    CITE_RE.lastIndex = 0;
    if (!CITE_RE.test(text)) continue;
    CITE_RE.lastIndex = 0;

    const frag = document.createDocumentFragment();
    let last = 0;
    for (const match of text.matchAll(CITE_RE)) {
      frag.append(text.slice(last, match.index));
      // Within the match, link each number; keep the words and separators as text
      let pos = 0;
      for (const num of match[0].matchAll(/\d+/g)) {
        frag.append(match[0].slice(pos, num.index));
        const a = document.createElement("a");
        a.className = "cite";
        a.href = "#";
        a.dataset.n = num[0];
        a.textContent = num[0];
        frag.append(a);
        pos = num.index + num[0].length;
      }
      frag.append(match[0].slice(pos));
      last = match.index + match[0].length;
    }
    frag.append(text.slice(last));
    node.replaceWith(frag);
  }
}

// One listener for all citation links, in old and new answers
messagesEl.addEventListener("click", e => {
  const a = e.target.closest("a.cite");
  if (!a) return;
  e.preventDefault();
  const bubble = a.closest(".bubble");
  if (bubble && bubble._cards) renderSourceCards(bubble._cards);
  if (isPhoneLayout()) showView("sources");
  const card = sourcesList.querySelector(`.source-card[data-n="${a.dataset.n}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("highlight");
  setTimeout(() => card.classList.remove("highlight"), 1800);
});

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
  setSourcesCount("…");

  try {
    const assistantText = await streamChat(text, bubble);
    if (assistantText) {
      state.history.push({ role: "user", content: text });
      // The server rejects history messages over 10,000 characters
      state.history.push({ role: "assistant", content: assistantText.slice(0, 10000) });
      if (state.history.length > 12) state.history = state.history.slice(-12);
    }
  } catch (err) {
    bubble.classList.remove("streaming");
    bubble.textContent = "";
    appendError(err.message || "Request failed");
    setSourcesEmpty("No sources loaded.");
    setSourcesCount("0");
  } finally {
    state.streaming = false;
    sendBtn.disabled = false;
    chatInput.focus();
  }
}

// ── Boot ───────────────────────────────────────────────────────────────────
init();
