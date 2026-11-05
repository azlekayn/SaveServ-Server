// SaveServ website: cover grid of saves, detail drawer, profile menu, pairing.
// Plain JS, no build step. All user data is inserted as text, never as HTML.
"use strict";

const $app = document.getElementById("app");
const state = { me: null, saves: [], sort: "recent", query: "", device: "", open: null };
try { state.sort = localStorage.getItem("saveserv.sort") || "recent"; } catch (_) {}

// ---- helpers -------------------------------------------------------------------
function h(tag, attrs, ...kids) {
  const el = document.createElementNS(tag === "svg" || tag === "path" || tag === "circle" || tag === "rect"
    ? "http://www.w3.org/2000/svg" : "http://www.w3.org/1999/xhtml", tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.setAttribute("class", v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ICONS = {
  plus: "M12 5v14M5 12h14",
  user: "M20 21a8 8 0 0 0-16 0M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z",
  shield: "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z",
  devices: "M4 5h16v10H4zM2 19h20M9 15v4",
  people: "M17 20a5 5 0 0 0-10 0M12 12a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM21 20a4 4 0 0 0-4-4M17 5a3 3 0 0 1 0 6",
  out: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h11",
  x: "M6 6l12 12M18 6 6 18",
  chev: "m6 9 6 6 6-6",
  dl: "M12 4v11m0 0-4-4m4 4 4-4M5 20h14",
  restore: "M4 12a8 8 0 1 0 2.5-5.8M4 4v5h5",
  edit: "M4 20h4L19 9l-4-4L4 16v4Z",
  image: "M4 5h16v14H4zM8 11a2 2 0 1 0 0-4 2 2 0 0 0 0 4ZM20 16l-5-5-9 8",
};
function icon(name, cls = "ico") {
  return h("svg", { class: cls, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    h("path", { d: ICONS[name] }));
}
function logo() {
  return h("svg", { class: "mark", viewBox: "0 0 32 32", "aria-hidden": "true" },
    h("rect", { x: 3, y: 3, width: 26, height: 26, rx: 6, fill: "var(--accent)" }),
    h("rect", { x: 9, y: 3, width: 14, height: 9, rx: 1.5, fill: "var(--bg)" }),
    h("rect", { x: 19, y: 5, width: 2.5, height: 5, rx: 1, fill: "var(--accent)" }),
    h("rect", { x: 8, y: 17, width: 16, height: 9, rx: 2, fill: "var(--bg)" }));
}

function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
  return new Date(t * 1000).toLocaleDateString();
}
const when = (t) => new Date(t * 1000).toLocaleString();
function size(n) {
  return n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB` : `${(n / 1048576).toFixed(1)} MB`;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-SaveServ": "1" }, credentials: "same-origin" };
  if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; }
  if (opts.file) { init.body = opts.file; init.headers["Content-Type"] = opts.file.type || "application/octet-stream"; }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !opts.anon) { state.me = null; render(); throw new Error("Please log in again."); }
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

function pickImage() {
  return new Promise((resolve) => {
    const input = h("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp" });
    input.addEventListener("change", () => resolve(input.files[0] || null));
    input.click();
  });
}

// ---- sign-in -------------------------------------------------------------------
async function renderAuth() {
  let status = { register: false, users: 1 };
  try { status = await api("/web/status", { anon: true }); } catch (_) {}
  let mode = status.users === 0 ? "register" : "login";
  const draw = () => {
    const err = h("p", { class: "error" });
    const name = h("input", { class: "input", name: "username", autocomplete: "username", required: true });
    const pw = h("input", { class: "input", type: "password", autocomplete: mode === "login" ? "current-password" : "new-password", required: true, minlength: mode === "register" ? 8 : null });
    const form = h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        err.textContent = "";
        try {
          await api(`/web/${mode}`, { method: "POST", json: { name: name.value, password: pw.value }, anon: true });
          await boot();
        } catch (x) { err.textContent = x.message; }
      },
    },
      h("label", { class: "field" }, h("span", {}, "Name"), name),
      h("label", { class: "field" }, h("span", {}, "Password"), pw),
      err,
      h("button", { class: "btn primary", type: "submit" }, mode === "login" ? "Log in" : "Create account"));
    $app.replaceChildren(h("div", { class: "auth" }, h("div", { class: "panel" },
      h("div", { class: "brand" }, logo(), h("span", {}, "Save", h("b", {}, "Serv"))),
      h("p", { class: "lead" }, mode === "login" ? "Your saves, on every device." : status.users === 0 ? "Create the owner account." : "Create an account."),
      form,
      status.register && status.users > 0 ? h("p", { class: "switch" }, mode === "login" ? "New here? " : "Have an account? ",
        h("button", { type: "button", onclick: () => { mode = mode === "login" ? "register" : "login"; draw(); } },
          mode === "login" ? "Register" : "Log in")) : null)));
    name.focus();
  };
  draw();
}

// ---- main view -------------------------------------------------------------------
function topbar() {
  const menu = h("div", { class: "menu hidden", role: "menu" },
    h("div", { class: "who" }, h("div", {}, h("strong", {}, state.me.display)), h("small", {}, `@${state.me.name}`)),
    h("button", { role: "menuitem", onclick: () => { closeMenu(); profileDialog(); } }, icon("user"), "Profile"),
    h("button", { role: "menuitem", onclick: () => { closeMenu(); securityDialog(); } }, icon("shield"), "Security"),
    h("button", { role: "menuitem", onclick: () => { closeMenu(); devicesDialog(); } }, icon("devices"), "Devices"),
    state.me.owner ? h("button", { role: "menuitem", onclick: () => { closeMenu(); accountsDialog(); } }, icon("people"), "Accounts") : null,
    h("hr"),
    h("button", { role: "menuitem", onclick: logout }, icon("out"), "Log out"));
  const btn = h("button", { class: "user-btn", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => { e.stopPropagation(); menu.classList.contains("hidden") ? openMenu() : closeMenu(); } },
    h("img", { class: "avatar", src: state.me.avatar, alt: "" }),
    h("span", { class: "name" }, state.me.display), icon("chev"));
  function openMenu() { menu.classList.remove("hidden"); btn.setAttribute("aria-expanded", "true"); menu.querySelector("button").focus(); }
  function closeMenu() { menu.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); }
  document.addEventListener("click", (e) => { if (!menu.contains(e.target)) closeMenu(); });
  menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeMenu(); btn.focus(); } });
  return h("header", { class: "topbar" },
    h("a", { class: "brand", href: "/" }, logo(), h("span", {}, "Save", h("b", {}, "Serv"))),
    h("div", { class: "user" }, btn, menu));
}

function sorted() {
  const q = state.query.trim().toLowerCase();
  let list = state.saves.filter((s) =>
    (!q || `${s.title} ${s.version || ""} ${s.game} ${s.slot}`.toLowerCase().includes(q)) &&
    (!state.device || s.device === state.device));
  const by = {
    recent: (a, b) => b.updated - a.updated,
    name: (a, b) => a.title.localeCompare(b.title) || (a.version || "").localeCompare(b.version || ""),
    game: (a, b) => a.game.localeCompare(b.game) || a.title.localeCompare(b.title) || (a.version || "").localeCompare(b.version || ""),
  }[state.sort];
  return list.sort(by);
}

function tile(s) {
  const t = h("button", { class: "tile" + (state.open && state.open.game === s.game && state.open.slot === s.slot ? " open" : ""),
    "data-key": `${s.game}/${s.slot}`, onclick: () => openSave(s) },
    h("div", { class: "cover" },
      h("img", { src: s.cover, alt: "", loading: "lazy" }),
      s.version ? h("span", { class: "badge" }, s.version) : null,
      s.system ? h("span", { class: "badge sys" }, s.system) : null),
    h("div", { class: "cap" },
      h("div", { class: "t" }, s.title),
      h("div", { class: "s" }, `${ago(s.updated)} · ${s.device}`)));
  t.setAttribute("aria-label", `${s.title}${s.version ? " " + s.version : ""}, synced ${ago(s.updated)} from ${s.device}`);
  return t;
}

function renderGrid() {
  const grid = document.querySelector(".grid");
  if (!grid) return;
  const list = sorted();
  const kids = list.map(tile);
  if (!state.saves.length) kids.push(h("p", { class: "empty" }, "No saves yet. Pair a device with the + cover, then play: its saves appear here."));
  else if (!list.length) kids.push(h("p", { class: "empty" }, "No saves match."));
  kids.push(h("button", { class: "tile add", onclick: pairDialog, "aria-label": "Pair a device" },
    h("div", { class: "cover" }, icon("plus")),
    h("div", { class: "cap" }, h("div", { class: "t" }, "Pair a device"), h("div", { class: "s" }, "Enter its PIN"))));
  grid.replaceChildren(...kids);
  const count = document.querySelector(".toolbar h1 small");
  if (count) count.textContent = state.saves.length ? `${state.saves.length}` : "";
}

function renderMain() {
  const devices = [...new Set(state.saves.map((s) => s.device))].sort();
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Sort" },
    ...[["recent", "Recent"], ["name", "Name"], ["game", "Game"]].map(([k, label]) =>
      h("button", { "aria-pressed": String(state.sort === k), onclick: (e) => {
        state.sort = k;
        try { localStorage.setItem("saveserv.sort", k); } catch (_) {}
        seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === e.currentTarget)));
        renderGrid();
      } }, label)));
  const search = h("input", { class: "input search", type: "search", placeholder: "Search saves", value: state.query,
    oninput: (e) => { state.query = e.target.value; renderGrid(); } });
  const deviceSel = devices.length > 1 ? h("select", { class: "input", style: "width:auto", "aria-label": "Device",
    onchange: (e) => { state.device = e.target.value; renderGrid(); } },
    h("option", { value: "" }, "All devices"), ...devices.map((d) => h("option", { value: d, selected: d === state.device }, d))) : null;
  $app.replaceChildren(topbar(), h("main", {},
    h("div", { class: "toolbar" }, h("h1", {}, "Saves", h("small", {})), search, deviceSel, seg),
    h("div", { class: "grid", role: "list" })));
  renderGrid();
}

// Arrow keys move between covers, like JKSV.
document.addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
  const tiles = [...document.querySelectorAll(".grid .tile")];
  const i = tiles.indexOf(document.activeElement);
  if (i < 0 || document.querySelector(".drawer, dialog[open]")) return;
  const cols = Math.max(1, Math.round(document.querySelector(".grid").clientWidth / (tiles[0].offsetWidth + 16)));
  const step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -cols, ArrowDown: cols }[e.key];
  const next = tiles[Math.min(tiles.length - 1, Math.max(0, i + step))];
  if (next) { e.preventDefault(); next.focus(); }
});

// ---- save drawer -------------------------------------------------------------------
async function openSave(s) {
  state.open = s;
  document.querySelectorAll(".tile").forEach((t) => t.classList.toggle("open", t.dataset.key === `${s.game}/${s.slot}`));
  let d;
  try { d = await api(`/web/saves/${encodeURIComponent(s.game)}/${encodeURIComponent(s.slot)}`); }
  catch (x) { toast(x.message); return; }
  closeDrawer();
  const base = `/web/saves/${encodeURIComponent(s.game)}/${encodeURIComponent(s.slot)}`;
  const dl = (rev) => `/download/${encodeURIComponent(s.game)}/${encodeURIComponent(s.slot)}/${rev}`;
  const recent = d.history.slice(0, 4);
  const more = d.history.slice(4);
  const revItem = (r) => h("li", {},
    h("span", { class: "r" }, `r${r.rev}`),
    h("div", { class: "info" }, h("div", { title: when(r.updated) }, ago(r.updated)), h("div", {}, `${r.device} · ${size(r.size)}`)),
    h("button", { class: "btn small", title: "Make this the current save", onclick: () => restore(r.rev) }, icon("restore"), "Restore"),
    h("a", { class: "btn small", href: dl(r.rev), title: "Download", "aria-label": `Download r${r.rev}` }, icon("dl")));
  const revList = h("ul", { class: "revs" }, ...recent.map(revItem));
  const showAll = more.length ? h("button", { class: "btn small", onclick: (e) => { revList.append(...more.map(revItem)); e.currentTarget.remove(); } },
    `Show all ${d.history.length}`) : null;

  async function restore(rev) {
    if (!confirm(`Make r${rev} the current save? Your devices pick it up on their next sync. Nothing is deleted.`)) return;
    try { await api(`${base}/restore`, { method: "POST", json: { rev } }); toast(`Restored r${rev}`); await refresh(); openSave(s); }
    catch (x) { toast(x.message); }
  }
  async function setCover(scope) {
    const f = await pickImage();
    if (!f) return;
    try { await api(`${base}/cover${scope === "game" ? "?scope=game" : ""}`, { method: "PUT", file: f }); toast("Cover updated"); await refresh(); openSave(state.saves.find((x) => x.game === s.game && x.slot === s.slot) || s); }
    catch (x) { toast(x.message); }
  }
  async function resetCover() {
    try {
      await api(`${base}/cover`, { method: "DELETE" });
      await api(`${base}/cover?scope=game`, { method: "DELETE" });
      toast("Cover reset"); await refresh(); openSave(state.saves.find((x) => x.game === s.game && x.slot === s.slot) || s);
    } catch (x) { toast(x.message); }
  }
  async function rename() {
    const title = prompt(d.system ? "Name for this save" : "Name for this version", d.version || d.title);
    if (title === null) return;
    try { await api(`${base}/title`, { method: "POST", json: { title } }); await refresh(); openSave(state.saves.find((x) => x.game === s.game && x.slot === s.slot) || s); }
    catch (x) { toast(x.message); }
  }

  const c = d.current;
  const drawer = h("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": d.title },
    h("header", {},
      h("div", { class: "cover" }, h("img", { src: (state.saves.find((x) => x.game === s.game && x.slot === s.slot) || s).cover, alt: "" })),
      h("div", { style: "min-width:0" },
        h("h2", {}, d.title),
        h("div", { class: "chips" },
          d.version ? h("span", { class: "chip" }, d.version) : null,
          h("span", { class: "chip" }, d.system ? `${d.system.toUpperCase()} · ${d.slot}` : `${d.game} / ${d.slot}`))),
      h("button", { class: "btn small close", onclick: closeDrawer, "aria-label": "Close" }, icon("x"))),
    h("div", { class: "body" },
      h("section", {}, h("h3", {}, "Current save"),
        h("div", { class: "card current" },
          h("div", { class: "big" }, `Revision ${c.rev}`),
          h("dl", { class: "kv" },
            h("dt", {}, "Synced"), h("dd", { title: when(c.updated) }, ago(c.updated)),
            h("dt", {}, "From"), h("dd", {}, c.device),
            h("dt", {}, "Size"), h("dd", {}, size(c.size)),
            h("dt", {}, "Kept"), h("dd", {}, `${d.history.length + 1} revision${d.history.length ? "s" : ""}`)),
          h("a", { class: "btn primary", href: dl(c.rev) }, icon("dl"), "Download"))),
      h("section", {}, h("h3", {}, "Previous saves"),
        recent.length ? revList : h("p", { class: "dim", style: "margin:0" }, "No earlier revisions yet."),
        showAll),
      h("section", {}, h("h3", {}, "Cover and name"),
        h("div", { class: "row" },
          h("button", { class: "btn small", onclick: () => setCover("slot") }, icon("image"), d.system ? "Change cover" : "Cover for this version"),
          d.system ? null : h("button", { class: "btn small", onclick: () => setCover("game") }, icon("image"), "Cover for every version"),
          h("button", { class: "btn small", onclick: rename }, icon("edit"), "Rename"),
          h("button", { class: "btn small", onclick: resetCover }, "Reset cover")))));
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  document.body.append(scrim, drawer);
  drawer.querySelector(".close").focus();
}

function closeDrawer() {
  document.querySelectorAll(".drawer, .scrim").forEach((el) => el.remove());
  const key = state.open && `${state.open.game}/${state.open.slot}`;
  state.open = null;
  document.querySelectorAll(".tile.open").forEach((t) => t.classList.remove("open"));
  const back = key && [...document.querySelectorAll(".tile")].find((t) => t.dataset.key === key);
  if (back) back.focus();
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.querySelector(".drawer") && !document.querySelector("dialog[open]")) closeDrawer(); });

// ---- dialogs -------------------------------------------------------------------
function dialog(title, ...content) {
  const dlg = h("dialog", { "aria-label": title },
    h("header", {}, h("h2", {}, title), h("button", { class: "btn small", onclick: () => dlg.close(), "aria-label": "Close" }, icon("x"))),
    h("div", { class: "content" }, ...content));
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

function pairDialog() {
  const boxes = Array.from({ length: 6 }, (_, i) => h("input", { inputmode: "numeric", maxlength: 1, autocomplete: "one-time-code", "aria-label": `Digit ${i + 1}` }));
  const err = h("p", { class: "error" });
  const go = h("button", { class: "btn primary", type: "submit", style: "width:100%" }, "Pair");
  const fill = (digits, from) => { digits.split("").forEach((ch, k) => { if (boxes[from + k]) boxes[from + k].value = ch; }); boxes[Math.min(5, from + digits.length)].focus(); };
  boxes.forEach((b, i) => {
    b.addEventListener("input", () => { const v = b.value.replace(/\D/g, ""); b.value = ""; if (v) fill(v, i); });
    b.addEventListener("keydown", (e) => { if (e.key === "Backspace" && !b.value && i) { boxes[i - 1].value = ""; boxes[i - 1].focus(); e.preventDefault(); } });
    b.addEventListener("paste", (e) => { e.preventDefault(); fill((e.clipboardData.getData("text") || "").replace(/\D/g, "").slice(0, 6 - i), i); });
  });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    const pin = boxes.map((b) => b.value).join("");
    if (pin.length !== 6) { err.textContent = "Enter all 6 digits."; return; }
    go.disabled = true; err.textContent = "";
    try {
      const r = await api("/web/pair", { method: "POST", json: { pin } });
      form.replaceWith(h("div", { style: "text-align:center" },
        h("p", { style: "font-size:17px;margin:4px 0 8px" }, h("strong", {}, r.device), " is paired."),
        h("p", { class: "dim", style: "margin:0 0 16px" }, "It finishes on its own in a few seconds, then syncs its saves."),
        h("button", { class: "btn primary", onclick: () => dlg.close() }, "Done")));
    } catch (x) { err.textContent = x.message; go.disabled = false; }
  } },
    h("p", { class: "dim", style: "margin-top:0" }, "On the device, open its save sync menu and choose Pair (on the command line: saveserv pair). Type the 6-digit PIN it shows."),
    h("div", { class: "pin" }, ...boxes.slice(0, 3), h("span", { class: "gap" }), ...boxes.slice(3)),
    err, go);
  const dlg = dialog("Pair a device", form);
  boxes[0].focus();
}

function profileDialog() {
  const pic = h("img", { class: "avatar", src: state.me.avatar, alt: "" });
  const err = h("p", { class: "error" });
  const display = h("input", { class: "input", value: state.me.display, maxlength: 40 });
  const changePic = async () => {
    const f = await pickImage();
    if (!f) return;
    try { await api("/web/avatar", { method: "PUT", file: f }); await loadMe(); pic.src = state.me.avatar; renderMain(); toast("Photo updated"); }
    catch (x) { err.textContent = x.message; }
  };
  const removePic = async () => {
    try { await api("/web/avatar", { method: "DELETE" }); await loadMe(); pic.src = state.me.avatar; renderMain(); }
    catch (x) { err.textContent = x.message; }
  };
  dialog("Profile",
    h("div", { class: "profile-pic" }, pic, h("div", { class: "row" },
      h("button", { class: "btn small", onclick: changePic }, "Change photo"),
      h("button", { class: "btn small", onclick: removePic }, "Remove"))),
    h("form", { onsubmit: async (e) => {
      e.preventDefault();
      try { await api("/web/profile", { method: "POST", json: { display: display.value } }); await loadMe(); renderMain(); toast("Profile saved"); }
      catch (x) { err.textContent = x.message; }
    } },
      h("label", { class: "field" }, h("span", {}, "Display name"), display),
      h("label", { class: "field" }, h("span", {}, "Username"), h("input", { class: "input", value: state.me.name, disabled: true })),
      err,
      h("button", { class: "btn primary", type: "submit" }, "Save")));
}

async function securityDialog() {
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password", required: true });
  const nw = h("input", { class: "input", type: "password", autocomplete: "new-password", minlength: 8, required: true });
  const again = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true });
  const err = h("p", { class: "error" });
  const sessions = h("ul", { class: "list" });
  const loadSessions = async () => {
    const list = await api("/web/sessions");
    sessions.replaceChildren(...list.map((s) => h("li", {}, h("div", { class: "info" },
      h("div", {}, describeAgent(s.agent), " ", s.current ? h("span", { class: "tag" }, "This browser") : null),
      h("small", {}, `Signed in ${ago(s.created)} · active ${ago(s.seen)}`)))));
  };
  dialog("Security",
    h("section", {}, h("h3", {}, "Change password"),
      h("form", { onsubmit: async (e) => {
        e.preventDefault(); err.textContent = "";
        if (nw.value !== again.value) { err.textContent = "The new passwords don't match."; return; }
        try { await api("/web/password", { method: "POST", json: { current: cur.value, new: nw.value } }); cur.value = nw.value = again.value = ""; toast("Password changed. Other sessions were signed out."); loadSessions(); }
        catch (x) { err.textContent = x.message; }
      } },
        h("label", { class: "field" }, h("span", {}, "Current password"), cur),
        h("label", { class: "field" }, h("span", {}, "New password (8+ characters)"), nw),
        h("label", { class: "field" }, h("span", {}, "New password again"), again),
        err, h("button", { class: "btn primary", type: "submit" }, "Change password"))),
    h("section", {}, h("h3", {}, "Signed-in browsers"), sessions,
      h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "btn small danger", onclick: async () => {
        await api("/web/sessions/revoke-others", { method: "POST" }); toast("Signed out everywhere else"); loadSessions();
      } }, "Sign out other browsers"))));
  loadSessions().catch((x) => toast(x.message));
}

function describeAgent(a) {
  const os = /Android/.test(a) ? "Android" : /iPhone|iPad/.test(a) ? "iOS" : /Windows/.test(a) ? "Windows" : /Mac OS/.test(a) ? "macOS" : /Linux/.test(a) ? "Linux" : "";
  const br = /Firefox\//.test(a) ? "Firefox" : /Edg\//.test(a) ? "Edge" : /Chrome\//.test(a) ? "Chrome" : /Safari\//.test(a) ? "Safari" : "Browser";
  return os ? `${br} on ${os}` : br;
}

async function devicesDialog() {
  const list = h("ul", { class: "list" });
  const load = async () => {
    const devs = await api("/web/devices");
    list.replaceChildren(...(devs.length ? devs.map((d) => h("li", {},
      icon("devices"),
      h("div", { class: "info" }, h("div", {}, d.name), h("small", {}, `Paired ${ago(d.created)} · last sync ${ago(d.seen)}`)),
      h("button", { class: "btn small danger", onclick: async () => {
        if (!confirm(`Unpair ${d.name}? Its saves stay here; it stops syncing until paired again.`)) return;
        await api(`/web/devices/${d.id}/remove`, { method: "POST" }); load();
      } }, "Unpair"))) : [h("li", {}, h("div", { class: "info dim" }, "No devices yet."))]));
  };
  dialog("Devices", list, h("div", { class: "row", style: "margin-top:14px" },
    h("button", { class: "btn primary", onclick: (e) => { e.target.closest("dialog").close(); pairDialog(); } }, icon("plus"), "Pair a device")));
  load().catch((x) => toast(x.message));
}

async function accountsDialog() {
  const list = h("ul", { class: "list" });
  const name = h("input", { class: "input", autocomplete: "off", required: true });
  const pw = h("input", { class: "input", type: "password", autocomplete: "new-password", minlength: 8, required: true });
  const err = h("p", { class: "error" });
  const draw = (accts) => list.replaceChildren(...accts.map((a) => h("li", {},
    h("img", { class: "avatar", src: `/avatar/${a.id}`, alt: "" }),
    h("div", { class: "info" }, h("div", {}, a.display), h("small", {}, `@${a.name} · joined ${ago(a.created)}`)))));
  dialog("Accounts",
    h("section", {}, list),
    h("section", {}, h("h3", {}, "Add an account"), h("p", { class: "dim", style: "margin-top:0" }, "Each account has its own saves and devices."),
      h("form", { onsubmit: async (e) => {
        e.preventDefault(); err.textContent = "";
        try { draw(await api("/web/accounts", { method: "POST", json: { name: name.value, password: pw.value } })); name.value = pw.value = ""; toast("Account added"); }
        catch (x) { err.textContent = x.message; }
      } },
        h("label", { class: "field" }, h("span", {}, "Name"), name),
        h("label", { class: "field" }, h("span", {}, "Password (8+ characters)"), pw),
        err, h("button", { class: "btn primary", type: "submit" }, "Add account"))));
  api("/web/accounts").then(draw).catch((x) => toast(x.message));
}

async function logout() {
  try { await api("/web/logout", { method: "POST" }); } catch (_) {}
  state.me = null;
  render();
}

// ---- boot -------------------------------------------------------------------
async function loadMe() { state.me = await api("/web/me", { anon: true }); }
async function refresh() { state.saves = await api("/web/saves"); renderGrid(); }

async function boot() {
  try { await loadMe(); } catch (_) { state.me = null; }
  if (state.me) {
    try { state.saves = await api("/web/saves"); } catch (x) { toast(x.message); }
  }
  render();
}
function render() {
  if (!state.me) { closeDrawer(); renderAuth(); return; }
  renderMain();
  if (new URLSearchParams(location.search).has("pair")) { history.replaceState(null, "", "/"); pairDialog(); }
}
// Pick up new syncs while the page stays open.
setInterval(() => { if (state.me && !document.hidden && !document.querySelector(".drawer, dialog[open]")) refresh().catch(() => {}); }, 30000);
boot();
