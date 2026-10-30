// ── Full text of one source, with the passages around it ───────────────────
const token = localStorage.getItem("jwt_token");
const chunkId = location.pathname.split("/").filter(Boolean).pop();

if (!token) {
  location.replace(`/login?next=${encodeURIComponent(location.pathname)}`);
}

const statusEl = document.getElementById("status");
const passagesEl = document.getElementById("passages");
const earlierBtn = document.getElementById("earlier");
const laterBtn = document.getElementById("later");

const STEP = 5;      // passages added per "Show earlier/later" click
const MAX = 20;      // server cap on each side
let before = 3;
let after = 3;

async function load() {
  const resp = await fetch(`/api/sources/${chunkId}?before=${before}&after=${after}`, {
    headers: { "Authorization": `Bearer ${token}` },
  });
  if (resp.status === 401) {
    localStorage.removeItem("jwt_token");
    location.replace(`/login?next=${encodeURIComponent(location.pathname)}`);
    return;
  }
  if (!resp.ok) {
    statusEl.textContent = resp.status === 404 ? "Source not found." : `Could not load source (HTTP ${resp.status}).`;
    return;
  }
  render(await resp.json());
}

function render(data) {
  const c = data.chunk;
  statusEl.hidden = true;
  document.getElementById("head").hidden = false;

  const house = document.getElementById("house");
  const legislation = { bill_text: "Bill", act_text: "Act", bill_memo: "Bill memo" }[c.chunk_type];
  house.className = "house-badge " + (legislation ? "legislation"
    : { "Dáil": "dail", "Seanad": "seanad" }[c.house] || "committee");
  house.textContent = legislation
    || (c.committee
      ? c.committee.replace(/_/g, " ").replace(/^./, ch => ch.toUpperCase())
      : c.house);
  document.getElementById("date").textContent = c.date;
  const badge = document.getElementById("table-badge");
  badge.textContent = legislation ? c.version_label || "" : "TABLE";
  badge.hidden = legislation ? !c.version_label : c.chunk_type !== "table";

  const heading = c.heading_path || c.heading || "(no heading)";
  document.getElementById("heading").textContent = heading;
  document.title = `${heading} — Yes Deputy`;

  if (legislation) {
    document.getElementById("person").textContent =
      [(c.sponsors || []).join(", "), c.bill_status].filter(Boolean).join(" · ");
  } else {
    const person = c.speaker || c.answering_person || "";
    const role = c.speaker ? c.speaker_role : c.answering_role;
    document.getElementById("person").textContent = role ? `${person} · ${role}` : person;
  }

  const qList = document.getElementById("questions");
  qList.replaceChildren();
  for (const q of c.questions || []) {
    const li = document.createElement("li");
    li.textContent = q;
    qList.appendChild(li);
  }
  qList.hidden = !(c.questions && c.questions.length);

  const link = document.getElementById("oireachtas-link");
  if (data.oireachtas_url) {
    link.href = data.oireachtas_url;
    link.hidden = false;
  }
  const pdf = document.getElementById("pdf-link");
  if (c.pdf_url && c.pdf_url.startsWith("https://data.oireachtas.ie/")) {
    pdf.href = c.page ? `${c.pdf_url}#page=${c.page}` : c.pdf_url;
    pdf.hidden = false;
  }

  passagesEl.replaceChildren(
    ...data.before.map(p => passage(p, false)),
    passage(c, true),
    ...data.after.map(p => passage(p, false)),
  );

  // Fewer neighbours than asked for means the start/end of the section was reached
  earlierBtn.hidden = data.before.length < before || before >= MAX;
  laterBtn.hidden = data.after.length < after || after >= MAX;

  if (!render.scrolled) {
    passagesEl.querySelector(".cited")?.scrollIntoView({ block: "center" });
    render.scrolled = true;
  }
}

function passage(p, cited) {
  const div = document.createElement("div");
  div.className = "passage" + (cited ? " cited" : "");
  div.textContent = p.text || "";
  return div;
}

earlierBtn.addEventListener("click", () => { before = Math.min(before + STEP, MAX); load(); });
laterBtn.addEventListener("click", () => { after = Math.min(after + STEP, MAX); load(); });

if (token) load();
