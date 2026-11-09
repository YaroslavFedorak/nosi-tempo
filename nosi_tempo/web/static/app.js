"use strict";

const tg = window.Telegram?.WebApp;
const $ = (sel, root = document) => root.querySelector(sel);
const main = $("#main");

const S = {
  tz: "",
  today: "",
  offset: 0,          // різниця між годинником сервера і клієнта, с
  running: null,
  projects: [],
  open: null,
  tick: null,
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const nowSec = () => Math.floor(Date.now() / 1000) + S.offset;

const MONTHS = ["січ", "лют", "бер", "квіт", "трав", "черв", "лип", "серп", "вер", "жовт", "лист", "груд"];
const WD = ["нд", "пн", "вт", "ср", "чт", "пт", "сб"];

function dur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  if (h && m) return `${h} год ${m} хв`;
  if (h) return `${h} год`;
  if (m) return `${m} хв`;
  return "<1 хв";
}
function hm(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}`;
}
function clock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
function parseDay(iso) { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); }
function dayLabel(iso) {
  if (!iso) return "";
  const diff = Math.round((parseDay(iso) - parseDay(S.today)) / 86400000);
  if (diff === 0) return "сьогодні";
  if (diff === 1) return "завтра";
  if (diff === -1) return "вчора";
  const d = parseDay(iso);
  return `${WD[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}
function timeOf(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
function plural(n, one, few, many) {
  const a = n % 10, b = n % 100;
  if (a === 1 && b !== 11) return one;
  if (a >= 2 && a <= 4 && (b < 12 || b > 14)) return few;
  return many;
}
const RECUR = { daily: "щодня", weekdays: "по буднях", weekly: "щотижня", monthly: "щомісяця" };
const recurLabel = (r) => (!r ? "" : r.startsWith("every:") ? `кожні ${r.split(":")[1]} дн.` : RECUR[r] || r);
const haptic = (kind = "light") => { try { tg?.HapticFeedback?.impactOccurred(kind); } catch (_) {} };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-Init-Data": tg?.initData || "" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.error || `Помилка ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(text, undo) {
  const el = $("#toast"), btn = $("#toast-undo");
  $("#toast-text").textContent = text;
  btn.hidden = !undo;
  btn.onclick = undo ? async () => { el.hidden = true; await undo(); } : null;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), undo ? 5000 : 2500);
}

function setRunning(entry, serverNow) {
  if (serverNow) S.offset = serverNow - Math.floor(Date.now() / 1000);
  S.running = entry || null;
  const box = $("#clock");
  clearInterval(S.tick);
  if (!S.running) { box.hidden = true; return; }
  box.hidden = false;
  $("#clock-title").textContent = S.running.title;
  const bits = [`з ${timeOf(S.running.started_at)}`];
  if (S.running.project) bits.unshift(`#${S.running.project}`);
  $("#clock-meta").textContent = bits.join(", ");
  const paint = () => ($("#clock-time").textContent = clock(nowSec() - S.running.started_at));
  paint();
  S.tick = setInterval(paint, 1000);
}

$("#clock-stop").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.setAttribute("aria-busy", "true");
  try {
    const { stopped } = await api("/api/timer/stop", { method: "POST" });
    haptic("medium");
    setRunning(null);
    if (stopped) toast(`Записано ${dur(stopped.stopped_at - stopped.started_at)}`);
    route();
  } catch (err) { toast(err.message); }
  btn.removeAttribute("aria-busy");
});

async function startTimer(body) {
  const { running } = await api("/api/timer/start", { method: "POST", body });
  haptic("medium");
  setRunning(running);
  return running;
}

const skeleton = (n = 5) => `<div class="skeleton" aria-hidden="true">${"<span></span>".repeat(n)}</div><p class="visually-hidden">Завантаження</p>`;
const errorState = (msg) => `<div class="state state--error"><p>${esc(msg)}</p><button class="btn" type="button" data-retry>Спробувати ще раз</button></div>`;

main.addEventListener("click", (e) => { if (e.target.closest("[data-retry]")) route(); });

const VIEWS = [["today", "Сьогодні"], ["inbox", "Вхідні"], ["upcoming", "Наступні"]];
const EMPTY = {
  today: ["На сьогодні нічого.", "Додай задачу зі словом «сьогодні» або часом, і вона буде тут."],
  inbox: ["Вхідні порожні.", "Задачі без #проєкту потрапляють сюди."],
  upcoming: ["На два тижні вперед нічого.", "Напиши «завтра», «пт» або дату, щоб запланувати."],
  project: ["У проєкті немає відкритих задач.", "Додай першу: проєкт підставиться сам."],
};

function taskMeta(t) {
  const bits = [];
  if (t.due_date) {
    let due = dayLabel(t.due_date) + (t.due_time ? ` ${t.due_time}` : "");
    bits.push(t.due_date < S.today ? `<span class="late">${esc(due)}</span>` : esc(due));
  }
  if (t.project) bits.push(esc("#" + t.project));
  if (t.recur) bits.push(esc(recurLabel(t.recur)));
  if (t.tracked) bits.push(`<span class="measure">${hm(t.tracked)}</span>`);
  return bits.join(", ");
}

const PLAY = `<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M3 1.5v11l9-5.5z"/></svg>`;
const STOP = `<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M2.5 2.5h9v9h-9z"/></svg>`;

function taskRow(t) {
  const runningHere = S.running && S.running.task_id === t.id;
  const meta = taskMeta(t);
  return `<li class="row${t.done_at ? " is-done" : ""}" data-id="${t.id}">
    <div class="row__line">
      <button class="check" type="button" data-act="done" data-p="${t.priority}" aria-pressed="${!!t.done_at}" aria-label="Виконано: ${esc(t.title)}"></button>
      <button class="row__open" type="button" data-act="edit" aria-expanded="${S.open === t.id}">
        <span class="row__title">${esc(t.title)}</span>
        ${meta ? `<span class="row__meta">${meta}</span>` : ""}
      </button>
      <button class="play" type="button" data-act="${runningHere ? "stop" : "play"}" aria-pressed="${runningHere}" aria-label="${runningHere ? "Зупинити таймер" : "Запустити таймер"}">${runningHere ? STOP : PLAY}</button>
    </div>
    ${S.open === t.id ? editor(t) : ""}
  </li>`;
}

function editor(t) {
  const projects = S.projects.map((p) => `<option value="${esc(p.name)}"${p.id === t.project_id ? " selected" : ""}>#${esc(p.name)}</option>`).join("");
  const rec = [["", "Не повторювати"], ["daily", "Щодня"], ["weekdays", "По буднях"], ["weekly", "Щотижня"], ["monthly", "Щомісяця"]];
  if (t.recur && t.recur.startsWith("every:")) rec.push([t.recur, recurLabel(t.recur)]);
  return `<form class="editor" data-form="${t.id}">
    <label class="field wide"><span>Назва</span><input class="input" name="title" value="${esc(t.title)}" required maxlength="300" autocomplete="off"></label>
    <label class="field"><span>Дата</span><input class="input" type="date" name="due_date" value="${t.due_date || ""}"></label>
    <label class="field"><span>Час</span><input class="input" type="time" name="due_time" value="${t.due_time || ""}"></label>
    <label class="field"><span>Проєкт</span><select class="input" name="project"><option value="">Вхідні</option>${projects}</select></label>
    <label class="field"><span>Пріоритет</span><select class="input" name="priority">${[1, 2, 3, 4].map((p) => `<option value="${p}"${p === t.priority ? " selected" : ""}>${p}${p === 1 ? ", найвищий" : p === 4 ? ", звичайний" : ""}</option>`).join("")}</select></label>
    <label class="field wide"><span>Повтор</span><select class="input" name="recur">${rec.map(([v, l]) => `<option value="${v}"${(t.recur || "") === v ? " selected" : ""}>${l}</option>`).join("")}</select></label>
    <label class="field wide"><span>Нотатка</span><textarea class="input" name="notes" rows="3">${esc(t.notes)}</textarea></label>
    ${t.tracked ? `<p class="wide muted">Витрачено: <span class="measure">${dur(t.tracked)}</span></p>` : ""}
    <div class="editor__actions">
      <button class="btn btn--quiet btn--danger" type="button" data-act="delete">Видалити</button>
      <div><button class="btn btn--quiet" type="button" data-act="close">Скасувати</button><button class="btn" type="submit">Зберегти</button></div>
    </div>
  </form>`;
}

async function viewTasks(view, pid) {
  const views = VIEWS.map(([v, l]) => `<button class="seg__btn" type="button" data-view="${v}" aria-pressed="${v === view}">${l}</button>`).join("");
  main.innerHTML = `
    <form class="add" id="add" autocomplete="off">
      <div class="adder">
        <input class="input" name="text" id="add-text" placeholder="Нова задача: звіт завтра о 10 #робота !2" aria-label="Нова задача" enterkeyhint="done">
        <button class="btn btn--primary" type="submit">Додати</button>
      </div>
      <p class="hint" id="add-hint">Дати словами: <code>завтра</code>, <code>пт</code>, <code>12.11</code>. Повтор: <code>щодня</code>. Проєкт: <code>#назва</code>.</p>
    </form>
    <div class="bar">
      <div class="seg" role="group" aria-label="Список">${views}</div>
      <select class="input" id="project-pick" aria-label="Проєкт" style="width:auto"><option value="">Проєкти</option></select>
    </div>
    <section id="list">${skeleton()}</section>`;

  $("#add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#add-text"), text = input.value.trim();
    if (!text) { input.focus(); return; }
    const btn = e.submitter; btn?.setAttribute("aria-busy", "true");
    try {
      const body = { text };
      if (view === "project") { const p = S.projects.find((x) => x.id === pid); if (p) body.project = p.name; }
      const { task } = await api("/api/tasks", { method: "POST", body });
      input.value = "";
      haptic();
      const where = task.due_date ? dayLabel(task.due_date) : task.project ? "#" + task.project : "вхідні";
      toast(`Додано: ${where}`);
      await loadList(view, pid);
    } catch (err) { toast(err.message); }
    btn?.removeAttribute("aria-busy");
  });

  main.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => (location.hash = `#tasks/${b.dataset.view}`)));
  $("#project-pick").addEventListener("change", (e) => { if (e.target.value) location.hash = `#tasks/project/${e.target.value}`; });
  await loadList(view, pid);
}

async function loadList(view, pid) {
  const list = $("#list");
  if (!list) return;
  try {
    const q = new URLSearchParams({ view });
    if (pid) q.set("project", pid);
    const data = await api(`/api/state?${q}`);
    S.today = data.today; S.tz = data.tz; S.projects = data.projects;
    setRunning(data.running, data.now);
    paintTopDate();

    const pick = $("#project-pick");
    if (pick) {
      pick.innerHTML = `<option value="">Проєкти</option>` + S.projects.map((p) => `<option value="${p.id}"${p.id === pid ? " selected" : ""}>#${esc(p.name)} ${p.open}</option>`).join("");
      pick.hidden = !S.projects.length;
    }

    const title = view === "project" ? "#" + (S.projects.find((p) => p.id === pid)?.name || "проєкт") : VIEWS.find(([v]) => v === view)?.[1];
    const tasks = data.tasks;
    if (!tasks.length) {
      const [a, b] = EMPTY[view] || EMPTY.project;
      list.innerHTML = `<div class="head"><h2>${esc(title)}</h2></div><div class="state"><p>${a} ${b}</p><button class="btn" type="button" data-focus-add>Додати задачу</button></div>`;
      list.querySelector("[data-focus-add]").addEventListener("click", () => $("#add-text").focus());
      return;
    }
    const tracked = tasks.reduce((s, t) => s + (t.tracked || 0), 0);
    list.innerHTML = `<div class="head"><h2>${esc(title)}</h2><span class="muted">${tasks.length} ${plural(tasks.length, "задача", "задачі", "задач")}${tracked ? `, <span class="measure">${hm(tracked)}</span> год` : ""}</span></div>
      <ul class="rows">${tasks.map(taskRow).join("")}</ul>`;
    bindRows(list, tasks, view, pid);
  } catch (err) {
    list.innerHTML = errorState(err.message);
  }
}

function bindRows(list, tasks, view, pid) {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  // присвоюємо, а не addEventListener: вузол списку живе між перезавантаженнями
  list.onclick = async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const row = btn.closest(".row"), id = Number(row.dataset.id), t = byId.get(id);
    const act = btn.dataset.act;
    try {
      if (act === "done") {
        const { task } = await api(`/api/tasks/${id}/done`, { method: "POST" });
        haptic("medium");
        if (task.recur) toast(`Наступного разу: ${dayLabel(task.due_date)}`);
        else toast("Виконано", async () => { await api(`/api/tasks/${id}/undo`, { method: "POST" }); loadList(view, pid); });
        S.open = null;
        await loadList(view, pid);
      } else if (act === "edit") {
        S.open = S.open === id ? null : id;
        await loadList(view, pid);
        if (S.open) $(`[data-form="${id}"] input[name=title]`)?.focus();
      } else if (act === "close") {
        S.open = null; await loadList(view, pid);
      } else if (act === "play") {
        await startTimer({ task_id: id }); await loadList(view, pid);
      } else if (act === "stop") {
        await api("/api/timer/stop", { method: "POST" }); setRunning(null); await loadList(view, pid);
      } else if (act === "delete") {
        if (!(await confirmAsk(`Видалити «${t.title}»?`))) return;
        await api(`/api/tasks/${id}`, { method: "DELETE" });
        S.open = null; toast("Видалено"); await loadList(view, pid);
      }
    } catch (err) { toast(err.message); }
  };
  list.onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target, id = Number(form.dataset.form);
    const f = Object.fromEntries(new FormData(form));
    const body = {
      title: f.title.trim(), notes: f.notes, priority: Number(f.priority),
      due_date: f.due_date || null, due_time: f.due_time || null,
      recur: f.recur || null, project: f.project || null,
    };
    if (body.recur && !body.due_date) body.due_date = S.today;
    if (body.due_time && !body.due_date) body.due_date = S.today;
    try {
      await api(`/api/tasks/${id}`, { method: "PATCH", body });
      S.open = null; toast("Збережено"); await loadList(view, pid);
    } catch (err) { toast(err.message); }
  };
}

function confirmAsk(text) {
  if (tg?.showConfirm) return new Promise((ok) => tg.showConfirm(text, ok));
  return Promise.resolve(window.confirm(text));
}

async function viewTimer() {
  main.innerHTML = `
    <form id="start" autocomplete="off">
      <div class="adder">
        <input class="input" name="title" placeholder="Що робиш? #проєкт" aria-label="Над чим працюєш">
        <button class="btn btn--primary" type="submit">Старт</button>
      </div>
      <p class="hint">Запуск нового таймера зупиняє попередній.</p>
    </form>
    <div id="recent"></div>
    <form id="log" autocomplete="off">
      <label class="field"><span>Дописати час вручну</span>
        <div class="adder">
          <input class="input" name="text" placeholder="45m рев'ю коду #робота" aria-label="Тривалість і назва">
          <button class="btn" type="submit">Записати</button>
        </div>
      </label>
    </form>
    <section id="entries">${skeleton(4)}</section>`;

  $("#start").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = e.target.title;
    try { await startTimer({ title: input.value.trim() }); input.value = ""; await loadEntries(); } catch (err) { toast(err.message); }
  });
  $("#log").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = e.target.text;
    if (!input.value.trim()) { input.focus(); return; }
    try {
      const { entry } = await api("/api/entries", { method: "POST", body: { text: input.value } });
      input.value = ""; toast(`Записано ${dur(entry.stopped_at - entry.started_at)}`); await loadEntries();
    } catch (err) { toast(err.message); input.focus(); }
  });
  await loadEntries();
}

async function loadEntries() {
  const box = $("#entries");
  if (!box) return;
  try {
    const data = await api("/api/entries?days=7");
    setRunning(data.running, data.now);
    const rec = $("#recent");
    rec.innerHTML = data.recent.length && !data.running
      ? `<div class="recent" role="group" aria-label="Продовжити">${data.recent.map((r, i) => `<button class="btn" type="button" data-again="${i}">${esc(r.title)}${r.project ? ` <span class="muted">#${esc(r.project)}</span>` : ""}</button>`).join("")}</div>`
      : "";
    rec.querySelectorAll("[data-again]").forEach((b) => b.addEventListener("click", async () => {
      const r = data.recent[Number(b.dataset.again)];
      try { await startTimer({ title: r.title, task_id: r.task_id, project_id: r.project_id }); await loadEntries(); } catch (err) { toast(err.message); }
    }));

    const done = data.entries.filter((e) => e.stopped_at);
    if (!done.length) {
      box.innerHTML = `<div class="head"><h2>Цей тиждень</h2></div><div class="state"><p>Записів ще немає. Запусти таймер вище або кнопкою біля задачі.</p></div>`;
      return;
    }
    const groups = new Map();
    for (const e of done) {
      const d = new Date(e.started_at * 1000);
      const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      if (!groups.has(iso)) groups.set(iso, []);
      groups.get(iso).push(e);
    }
    box.innerHTML = [...groups].map(([iso, list]) => {
      const total = list.reduce((s, e) => s + e.stopped_at - e.started_at, 0);
      return `<div class="head"><h2>${esc(cap(dayLabel(iso)))}</h2><span class="measure">${hm(total)}</span></div>
        ${list.map((e) => `<div class="entry" data-entry="${e.id}">
          <span class="entry__what"><span class="entry__title">${esc(e.title)}</span>
            <span class="entry__meta">${timeOf(e.started_at)}–${timeOf(e.stopped_at)}${e.project ? `, #${esc(e.project)}` : ""}</span></span>
          <span class="entry__dur measure">${hm(e.stopped_at - e.started_at)}</span>
          <button class="x" type="button" data-del="${e.id}" aria-label="Видалити запис">×</button>
        </div>`).join("")}`;
    }).join("");
    box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmAsk("Видалити цей запис часу?"))) return;
      try { await api(`/api/entries/${b.dataset.del}`, { method: "DELETE" }); toast("Видалено"); await loadEntries(); } catch (err) { toast(err.message); }
    }));
  } catch (err) {
    box.innerHTML = errorState(err.message);
  }
}

async function viewReport(range) {
  const ranges = [["today", "Сьогодні"], ["week", "7 днів"], ["month", "30 днів"]];
  main.innerHTML = `
    <div class="seg" role="group" aria-label="Період">${ranges.map(([v, l]) => `<button class="seg__btn" type="button" data-range="${v}" aria-pressed="${v === range}">${l}</button>`).join("")}</div>
    <section id="report">${skeleton(4)}</section>`;
  main.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => (location.hash = `#report/${b.dataset.range}`)));

  const box = $("#report");
  try {
    const r = await api(`/api/report?range=${range}`);
    if (!r.total) {
      box.innerHTML = `<div class="state"><p>За цей період часу не записано.</p><a class="btn" href="#timer">Запустити таймер</a></div>`;
      return;
    }
    const h = Math.floor(r.total / 3600), m = Math.floor((r.total % 3600) / 60);
    const meter = (name, sec, top, cls = "") => `<div class="meter ${cls}">
        <span class="meter__name">${esc(name)}</span>
        <span class="meter__track"><span class="meter__fill" style="--v:${top ? (sec / top).toFixed(3) : 0}"></span></span>
        <span class="meter__val measure">${sec ? hm(sec) : "–"}</span></div>`;
    const topP = r.by_project[0]?.seconds || 0;
    const topD = Math.max(...r.by_day.map((d) => d.seconds), 0);
    const topT = r.by_task[0]?.seconds || 0;
    box.innerHTML = `
      <p class="total"><span class="measure">${h}:${String(m).padStart(2, "0")}</span><span class="unit">год разом</span></p>
      <div class="head"><h2>Проєкти</h2></div>
      ${r.by_project.map((p) => meter(p.name, p.seconds, topP)).join("")}
      ${r.days > 1 ? `<div class="head"><h2>Дні</h2></div>
        ${r.by_day.map((d) => meter(cap(dayLabel(d.date)), d.seconds, topD, "meter--day" + (d.date === r.to ? " is-today" : ""))).join("")}` : ""}
      <div class="head"><h2>Найбільше часу</h2></div>
      ${r.by_task.slice(0, 8).map((t) => meter(t.name, t.seconds, topT)).join("")}`;
  } catch (err) {
    box.innerHTML = errorState(err.message);
  }
}

async function viewAI() {
  main.innerHTML = `<section id="ai">${skeleton(3)}</section>`;
  const box = $("#ai");
  let data;
  try { data = await api("/api/ai"); } catch (err) { box.innerHTML = errorState(err.message); return; }

  if (!data.providers.length) {
    box.innerHTML = `<div class="state"><h2>Асистента ще не підключено</h2>
      <p>Додай ключ у файл .env на сервері: GEMINI_API_KEY (безкоштовний ключ з Google AI Studio), ANTHROPIC_API_KEY або OPENAI_API_KEY. Потім перезапусти бота.</p></div>`;
    return;
  }
  let provider = data.current;
  const seg = data.providers.length > 1
    ? `<div class="bar"><div class="seg" role="group" aria-label="Модель">${data.providers.map((p) => `<button class="seg__btn" type="button" data-prov="${p.id}" aria-pressed="${p.id === provider}">${p.label}</button>`).join("")}</div>
       <button class="btn btn--quiet" type="button" id="forget">Очистити розмову</button></div>`
    : `<div class="bar"><span class="muted">${data.providers[0].label}</span><button class="btn btn--quiet" type="button" id="forget">Очистити розмову</button></div>`;

  box.innerHTML = `${seg}
    <div class="chat" id="chat"></div>
    <form class="composer" id="ask" autocomplete="off">
      <textarea class="input" name="message" rows="1" placeholder="Що зробити першим? Розплануй мені тиждень" aria-label="Питання асистенту" enterkeyhint="send"></textarea>
      <button class="btn btn--primary" type="submit">Надіслати</button>
    </form>`;

  const chat = $("#chat");
  const bubble = (role, text, added = []) => {
    const el = document.createElement("div");
    el.className = `msg msg--${role}`;
    el.textContent = text;
    if (added.length) {
      const a = document.createElement("p");
      a.className = "msg__added";
      a.textContent = "Додано задачі: " + added.map((t) => t.title).join("; ");
      el.append(a);
    }
    chat.append(el);
    return el;
  };
  const strip = (t) => t.replace(/^\s*ADD:.*$/gm, "").trim();
  if (!data.history.length) {
    chat.innerHTML = `<div class="state"><p>Асистент бачить твої відкриті задачі, таймер і час за тиждень. Може порадити, з чого почати, або розбити велику задачу на кроки й додати їх.</p></div>`;
  } else {
    data.history.forEach((m) => bubble(m.role, m.role === "assistant" ? strip(m.content) : m.content));
  }

  box.querySelectorAll("[data-prov]").forEach((b) => b.addEventListener("click", () => {
    provider = b.dataset.prov;
    box.querySelectorAll("[data-prov]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  }));
  $("#forget").addEventListener("click", async () => {
    if (!(await confirmAsk("Очистити історію розмови?"))) return;
    await api("/api/ai", { method: "DELETE" }); viewAI();
  });

  const form = $("#ask"), area = form.message;
  area.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = area.value.trim();
    if (!text) { area.focus(); return; }
    if (!chat.querySelector(".msg")) chat.innerHTML = "";
    bubble("user", text);
    area.value = "";
    const btn = form.querySelector("button");
    btn.disabled = true; area.disabled = true;
    const wait = bubble("assistant", "Думаю…");
    try {
      const r = await api("/api/ai", { method: "POST", body: { message: text, provider } });
      wait.remove();
      bubble("assistant", r.reply, r.added);
      if (r.added.length) haptic("medium");
    } catch (err) {
      wait.textContent = err.message;
    }
    btn.disabled = false; area.disabled = false; area.focus();
    wait.scrollIntoView?.({ block: "end" });
  });
}

function paintTopDate() {
  if (!S.today) return;
  const d = parseDay(S.today);
  $("#top-date").textContent = `${WD[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}${S.tz ? `, ${S.tz}` : ""}`;
}

async function route() {
  const [tab = "tasks", a = "", b = ""] = location.hash.replace(/^#/, "").split("/");
  document.querySelectorAll("[data-tab]").forEach((el) => {
    if (el.dataset.tab === tab) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current");
  });
  if (tab === "timer") return viewTimer();
  if (tab === "report") return viewReport(["today", "week", "month"].includes(a) ? a : "week");
  if (tab === "ai") return viewAI();
  if (a === "project" && b) return viewTasks("project", Number(b));
  return viewTasks(["today", "inbox", "upcoming"].includes(a) ? a : "today");
}

window.addEventListener("hashchange", () => { S.open = null; route(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) route(); });

if (tg) {
  tg.ready();
  tg.expand();
  const paper = getComputedStyle(document.documentElement).getPropertyValue("--tg-paper-hex").trim();
  try { tg.setHeaderColor(paper); tg.setBackgroundColor(paper); } catch (_) {}
}
route();
