/* Cuentas — Mini App de Telegram. Sin dependencias: gráficas en SVG. */
(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const params = new URLSearchParams(location.search);
  const DEMO = params.has("demo");
  const SVG_NS = "http://www.w3.org/2000/svg";
  const WEEKDAYS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
  const $ = (sel) => document.querySelector(sel);

  const state = { month: null, data: null, currentMonth: null, tab: "resumen", editing: null };

  // ------------------------------------------------------------------ tema
  function applyTheme() {
    const scheme = tg && tg.colorScheme ? tg.colorScheme : params.get("theme");
    if (scheme === "dark" || scheme === "light") document.documentElement.dataset.theme = scheme;
  }
  if (tg) {
    tg.ready();
    tg.expand();
    tg.onEvent("themeChanged", () => { applyTheme(); render(); });
  }
  applyTheme();

  // ------------------------------------------------------------------ util
  function money(v, opts = {}) {
    const currency = (state.data && state.data.profile.currency) || "USD";
    const abs = Math.abs(v);
    try {
      return new Intl.NumberFormat("es", {
        style: "currency", currency, currencyDisplay: "narrowSymbol",
        maximumFractionDigits: abs >= 1000 || Number.isInteger(v) ? 0 : 2,
        notation: opts.compact ? "compact" : "standard",
      }).format(v);
    } catch (_) {
      return `${v.toFixed(0)} ${currency}`;
    }
  }
  const pct = (v) => `${Math.round(v)}%`;
  function el(tag, attrs = {}, text) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function svg(tag, attrs = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  /* Máximo del eje como 4 pasos "redondos" (1, 2, 2.5, 5 × 10^n), para ticks legibles. */
  function niceMax(v) {
    if (v <= 0) return 4;
    const raw = v / 4;
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / p;
    return (n <= 1 ? 1 : n <= 1.5 ? 1.5 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p * 4;
  }
  /* Barra con extremo de datos redondeado (4px) y base recta. */
  function barPath(x, y, w, h, horizontal) {
    if (w <= 0 || h <= 0) return "";
    if (horizontal) {
      const r = Math.min(4, h / 2, w);
      return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
    }
    const r = Math.min(4, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  function monthLabel(ym) {
    const [y, m] = ym.split("-").map(Number);
    const text = new Date(y, m - 1, 1).toLocaleDateString("es", { month: "long", year: "numeric" });
    return text.charAt(0).toUpperCase() + text.slice(1);
  }
  function shiftMonth(ym, delta) {
    const [y, m] = ym.split("-").map(Number);
    const d = new Date(y, m - 1 + delta, 1);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  }
  function haptic(type) { try { tg && tg.HapticFeedback.notificationOccurred(type); } catch (_) { /* opcional */ } }

  // ------------------------------------------------------------------ tooltip
  const tip = $("#tooltip");
  function showTip(evt, title, rows) {
    tip.replaceChildren();
    tip.append(el("div", { class: "muted" }, title));
    for (const [label, value, strong] of rows) {
      if (strong) {
        tip.append(el("strong", {}, value));
        if (label) tip.append(el("div", { class: "muted" }, label));
      } else {
        const row = el("div", { class: "row" });
        row.append(el("span", { class: "muted" }, label), el("span", {}, value));
        tip.append(row);
      }
    }
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    let x = evt.clientX + 12;
    let y = evt.clientY - box.height - 12;
    if (x + box.width > innerWidth - 8) x = evt.clientX - box.width - 12;
    if (y < 8) y = evt.clientY + 16;
    tip.style.left = `${Math.max(8, x)}px`;
    tip.style.top = `${y}px`;
  }
  const hideTip = () => { tip.hidden = true; };
  document.addEventListener("scroll", hideTip, { passive: true });

  function attachTip(node, getContent) {
    node.setAttribute("tabindex", "0");
    const show = (e) => { const [t, r] = getContent(); showTip(e, t, r); };
    node.addEventListener("pointermove", show);
    node.addEventListener("pointerdown", show);
    node.addEventListener("pointerleave", hideTip);
    node.addEventListener("focus", () => {
      const b = node.getBoundingClientRect();
      show({ clientX: b.left + b.width / 2, clientY: b.top });
    });
    node.addEventListener("blur", hideTip);
  }

  // ------------------------------------------------------------------ API
  async function api(path, options = {}) {
    if (DEMO) return demoApi(path, options);
    const res = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", Authorization: `tma ${tg ? tg.initData : ""}`, ...(options.headers || {}) },
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `Error ${res.status}`);
    return body;
  }

  async function load() {
    const status = $("#status");
    status.textContent = "Cargando…";
    try {
      const query = state.month ? `?month=${state.month}` : "";
      state.data = await api(`/api/dashboard${query}`);
      state.month = state.data.month;
      if (state.data.is_current_month) state.currentMonth = state.data.month;
      status.textContent = "";
      render();
    } catch (err) {
      status.textContent = err.message === "Falta autorización" || !tg || !tg.initData
        ? "Abre el dashboard desde el bot en Telegram."
        : `No pude cargar tus datos: ${err.message}`;
    }
  }

  // ------------------------------------------------------------------ render
  function render() {
    const d = state.data;
    if (!d) return;
    $("#month-title").textContent = monthLabel(d.month);
    $("#next").disabled = d.is_current_month || (state.currentMonth && d.month >= state.currentMonth);
    document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== state.tab; });
    renderTiles(d);
    renderBudgets(d);
    renderDaily(d);
    renderCategories(d);
    renderHistory(d);
    renderWeekday(d);
    renderInsights(d);
    renderTransactions(d);
    renderSubscriptions(d);
    renderReservesAndDebts(d);
    renderAccounts(d);
    renderSettings(d);
  }

  function tile(label, value, note, noteClass = "") {
    const t = el("div", { class: "tile" });
    t.append(el("div", { class: "label" }, label), el("div", { class: "value" }, value));
    if (note) t.append(el("div", { class: `note ${noteClass}` }, note));
    return t;
  }

  function renderTiles(d) {
    const t = d.totals;
    const box = $("#tiles");
    box.replaceChildren();
    box.append(tile("Gastado", money(t.expense), t.discretionary ? `${money(t.discretionary)} no esencial` : ""));
    if (t.remaining !== null && t.remaining !== undefined) {
      const ok = t.remaining >= 0;
      box.append(tile(ok ? "Te queda" : "Excedido", money(Math.abs(t.remaining)),
        `de ${money(t.spendable)} disponibles`, ok ? "good" : "bad"));
    } else {
      box.append(tile("Te queda", "—", "Cuéntale al bot tu ingreso"));
    }
    if (t.projection !== null && t.projection !== undefined) {
      const over = t.spendable && t.projection > t.spendable;
      box.append(tile("Proyección a fin de mes", money(t.projection),
        over ? "▲ por encima de tu disponible" : "dentro de lo disponible", over ? "bad" : "good"));
    } else {
      box.append(tile("Fijos mensuales", money(t.fixed_monthly), "suscripciones activas"));
    }
    box.append(tile("Ingresos", money(t.income), t.income ? `Balance ${money(t.net)}` : ""));
  }

  function renderBudgets(d) {
    const card = $("#budgets-card");
    const list = $("#budgets");
    list.replaceChildren();
    card.hidden = d.budgets.length === 0;
    for (const b of d.budgets.sort((a, c) => c.pct - a.pct)) {
      const li = el("li");
      const head = el("div", { class: "budget-head" });
      head.append(el("span", {}, b.label), el("span", {}, `${money(b.spent)} / ${money(b.limit)}`));
      const meter = el("div", { class: "meter", role: "meter", "aria-valuenow": Math.round(b.pct),
        "aria-valuemin": "0", "aria-valuemax": "100", "aria-label": b.label });
      const fill = el("div");
      const [color, icon, word] = b.pct >= 100 ? ["var(--critical)", "⛔", "Excedido"]
        : b.pct >= 80 ? ["var(--warning)", "⚠️", "Cerca del tope"] : ["var(--good)", "✓", "En orden"];
      fill.style.width = `${Math.min(100, b.pct)}%`;
      fill.style.background = color;
      meter.append(fill);
      li.append(head, meter, el("div", { class: "status-tag" }, `${icon} ${word} · ${pct(b.pct)}`));
      list.append(li);
    }
  }

  function chartWidth(container) {
    return Math.max(280, Math.round(container.clientWidth || container.parentElement.clientWidth || 320));
  }

  /* Línea de gasto acumulado con referencia del disponible, cruceta y tooltip. */
  function renderDaily(d) {
    const box = $("#daily-chart");
    box.replaceChildren();
    const pts = d.daily;
    const ref = d.totals.spendable;
    $("#daily-sub").textContent = ref ? `La línea punteada es tu disponible del mes (${money(ref)}).` : "";
    if (!pts.length) { box.append(el("p", { class: "empty" }, "Sin datos este mes.")); return; }
    const W = chartWidth(box), H = 200, L = 52, R = 12, T = 12, B = 26;
    const maxY = niceMax(Math.max(ref || 0, ...pts.map((p) => p.cumulative)) * 1.05);
    const daysInMonth = new Date(+d.month.slice(0, 4), +d.month.slice(5, 7), 0).getDate();
    const x = (i) => L + (i / Math.max(1, daysInMonth - 1)) * (W - L - R);
    const y = (v) => T + (1 - v / maxY) * (H - T - B);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `Gasto acumulado: ${money(pts[pts.length - 1].cumulative)}` });
    for (let k = 0; k <= 4; k++) {
      const v = (maxY / 4) * k;
      s.append(svg("line", { class: k ? "gridline" : "baseline", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
      s.append(svg("text", { class: "axis-label", x: L - 6, y: y(v) + 4, "text-anchor": "end" }, money(v, { compact: true })));
    }
    [1, 8, 15, 22, daysInMonth].forEach((day) => {
      s.append(svg("text", { class: "axis-label", x: x(day - 1), y: H - 6, "text-anchor": "middle" }, String(day)));
    });
    if (ref) {
      s.append(svg("line", { x1: L, x2: W - R, y1: y(ref), y2: y(ref), stroke: "var(--text-muted)",
        "stroke-width": 1.5, "stroke-dasharray": "4 4" }));
      s.append(svg("text", { class: "axis-label", x: W - R, y: y(ref) - 6, "text-anchor": "end" }, "Disponible"));
    }
    const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.cumulative).toFixed(1)}`).join("");
    s.append(svg("path", { d: line, fill: "none", stroke: "var(--series-1)", "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = pts.length - 1;
    s.append(svg("text", { class: "data-label", x: Math.min(x(last) + 6, W - R), y: y(pts[last].cumulative) - 8,
      "text-anchor": x(last) > W - 80 ? "end" : "start" }, money(pts[last].cumulative)));

    const cross = svg("line", { stroke: "var(--text-muted)", "stroke-width": 1, y1: T, y2: H - B, visibility: "hidden" });
    const dot = svg("circle", { r: 4, fill: "var(--series-1)", stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden" });
    const overlay = svg("rect", { class: "hit", x: L, y: T, width: W - L - R, height: H - T - B });
    s.append(cross, dot, overlay);
    const move = (e) => {
      const rect = s.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      const i = Math.max(0, Math.min(last, Math.round(((px - L) / (W - L - R)) * (daysInMonth - 1))));
      const p = pts[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.cumulative)); dot.setAttribute("visibility", "visible");
      const date = new Date(`${p.date}T12:00:00`).toLocaleDateString("es", { weekday: "short", day: "numeric", month: "short" });
      showTip(e, date, [["acumulado", money(p.cumulative), true], ["Ese día", money(p.spent)]]);
    };
    overlay.addEventListener("pointermove", move);
    overlay.addEventListener("pointerdown", move);
    overlay.addEventListener("pointerleave", () => {
      hideTip(); cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
    });
    box.append(s);
  }

  /* Barras horizontales por categoría, etiquetadas directamente, con marca del promedio aprendido. */
  function renderCategories(d) {
    const box = $("#cat-chart");
    box.replaceChildren();
    const cats = d.categories;
    if (!cats.length) { box.append(el("p", { class: "empty" }, "Aún no hay gastos este mes.")); return; }
    const W = chartWidth(box), row = 46, H = cats.length * row;
    const maxV = Math.max(...cats.map((c) => Math.max(c.amount, c.baseline || 0)));
    const total = cats.reduce((a, c) => a + c.amount, 0);
    const scale = (v) => (v / maxV) * (W - 2);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Gasto por categoría" });
    cats.forEach((c, i) => {
      const top = i * row;
      s.append(svg("text", { class: "cat-name", x: 0, y: top + 16 }, c.label));
      s.append(svg("text", { class: "data-label", x: W, y: top + 16, "text-anchor": "end" },
        `${money(c.amount)} · ${pct((c.amount / total) * 100)}`));
      s.append(svg("rect", { x: 0, y: top + 24, width: W, height: 12, rx: 4, fill: "var(--grid)", opacity: 0.5 }));
      const bar = svg("path", { class: "bar", d: barPath(0, top + 24, Math.max(2, scale(c.amount)), 12, true), fill: "var(--series-1)" });
      s.append(bar);
      if (c.baseline) {
        const bx = scale(c.baseline);
        s.append(svg("line", { x1: bx, x2: bx, y1: top + 20, y2: top + 40, stroke: "var(--text-primary)", "stroke-width": 2 }));
      }
      const hit = svg("rect", { class: "hit", x: 0, y: top, width: W, height: row - 4 });
      attachTip(hit, () => [c.label, [
        ["", money(c.amount), true],
        ["Del total", pct((c.amount / total) * 100)],
        ...(c.baseline ? [["Promedio mensual", money(c.baseline)]] : []),
      ]]);
      s.append(hit);
    });
    box.append(s);
  }

  /* Columnas agrupadas ingresos vs gastos por mes. */
  function renderHistory(d) {
    const box = $("#hist-chart");
    const legend = $("#hist-legend");
    box.replaceChildren();
    legend.replaceChildren();
    const months = d.monthly_history.slice(-6);
    if (!months.length) { box.append(el("p", { class: "empty" }, "Sin historial todavía.")); return; }
    for (const [label, color] of [["Ingresos", "var(--series-1)"], ["Gastos", "var(--series-2)"]]) {
      const sp = el("span", {}, label);
      sp.style.setProperty("--swatch", color);
      legend.append(sp);
    }
    const W = chartWidth(box), H = 190, L = 52, R = 4, T = 10, B = 24;
    const maxY = niceMax(Math.max(...months.map((m) => Math.max(m.income, m.expense))) * 1.05);
    const band = (W - L - R) / months.length;
    const bw = Math.min(24, (band - 14) / 2);
    const y = (v) => T + (1 - v / maxY) * (H - T - B);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Ingresos y gastos por mes" });
    for (let k = 0; k <= 4; k++) {
      const v = (maxY / 4) * k;
      s.append(svg("line", { class: k ? "gridline" : "baseline", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
      s.append(svg("text", { class: "axis-label", x: L - 6, y: y(v) + 4, "text-anchor": "end" }, money(v, { compact: true })));
    }
    months.forEach((m, i) => {
      const cx = L + band * i + band / 2;
      const x1 = cx - bw - 1, x2 = cx + 1; // 2px de separación entre barras vecinas
      s.append(svg("path", { d: barPath(x1, y(m.income), bw, y(0) - y(m.income)), fill: "var(--series-1)" }));
      s.append(svg("path", { d: barPath(x2, y(m.expense), bw, y(0) - y(m.expense)), fill: "var(--series-2)" }));
      const [yy, mm] = m.month.split("-").map(Number);
      const label = new Date(yy, mm - 1, 1).toLocaleDateString("es", { month: "short" });
      s.append(svg("text", { class: "axis-label", x: cx, y: H - 6, "text-anchor": "middle" }, label));
      const hit = svg("rect", { class: "hit", x: L + band * i, y: T, width: band, height: H - T - B });
      attachTip(hit, () => [monthLabel(m.month), [
        ["Ingresos", money(m.income)], ["Gastos", money(m.expense)], ["Balance", money(m.income - m.expense)],
      ]]);
      s.append(hit);
    });
    box.append(s);
  }

  function renderWeekday(d) {
    const box = $("#weekday-chart");
    box.replaceChildren();
    const vals = d.weekday;
    const max = Math.max(...vals);
    if (!max) { box.append(el("p", { class: "empty" }, "Aún no hay suficientes datos.")); return; }
    const W = chartWidth(box), H = 150, T = 20, B = 22;
    const band = W / 7, bw = Math.min(24, band - 12);
    const y = (v) => T + (1 - v / max) * (H - T - B);
    const total = vals.reduce((a, b) => a + b, 0);
    const peak = vals.indexOf(max);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Día de más gasto: ${WEEKDAYS[peak]}` });
    s.append(svg("line", { class: "baseline", x1: 0, x2: W, y1: H - B, y2: H - B }));
    vals.forEach((v, i) => {
      const cx = band * i + band / 2;
      s.append(svg("path", { d: barPath(cx - bw / 2, y(v), bw, H - B - y(v)), fill: "var(--series-1)" }));
      s.append(svg("text", { class: "axis-label", x: cx, y: H - 6, "text-anchor": "middle" }, WEEKDAYS[i]));
      if (i === peak) s.append(svg("text", { class: "data-label", x: cx, y: y(v) - 6, "text-anchor": "middle" }, pct((v / total) * 100)));
      const hit = svg("rect", { class: "hit", x: band * i, y: 0, width: band, height: H });
      attachTip(hit, () => [WEEKDAYS[i], [["", money(v), true], ["Del gasto variable", pct((v / total) * 100)]]]);
      s.append(hit);
    });
    box.append(s);
  }

  function renderInsights(d) {
    $("#insights-card").hidden = !d.insights.length;
    const list = $("#insights");
    list.replaceChildren(...d.insights.map((t) => el("li", {}, t)));
  }

  function renderTransactions(d) {
    const filter = $("#cat-filter");
    const selected = filter.value;
    if (filter.options.length === 1) {
      for (const [key, label] of Object.entries(d.category_catalog)) filter.append(el("option", { value: key }, label));
    }
    const list = $("#tx-list");
    list.replaceChildren();
    const txs = d.transactions.filter((t) => !selected || t.category === selected);
    if (!txs.length) { list.append(el("p", { class: "empty" }, "No hay movimientos.")); return; }
    const byDay = new Map();
    for (const t of txs) { if (!byDay.has(t.date)) byDay.set(t.date, []); byDay.get(t.date).push(t); }
    for (const [day, items] of byDay) {
      const group = el("div", { class: "day-group" });
      const spent = items.filter((t) => t.kind === "expense").reduce((a, t) => a + t.amount, 0);
      const head = el("div", { class: "day-head" });
      head.append(el("span", {}, new Date(`${day}T12:00:00`).toLocaleDateString("es", { weekday: "long", day: "numeric", month: "short" })),
        el("span", {}, spent ? money(spent) : ""));
      group.append(head);
      for (const t of items) {
        const label = d.category_catalog[t.category] || incomeLabel(t.category);
        const [emoji, ...rest] = label.split(" ");
        const b = el("button", { class: "tx", type: "button" });
        const main = el("div", { class: "main" });
        main.append(el("div", { class: "desc" }, t.description || rest.join(" ")),
          el("div", { class: "cat" }, `${rest.join(" ")}${t.account && d.account_catalog ? ` · ${d.account_catalog[t.account] || t.account}` : ""}${t.source === "voice" ? " · 🎙️" : ""}${t.source === "image" ? " · 📸" : ""}${t.source === "subscription" ? " · 🔁" : ""}`));
        b.append(el("span", { class: "emoji", "aria-hidden": "true" }, emoji), main,
          el("span", { class: `amt ${t.kind}` }, `${t.kind === "income" ? "+" : "−"}${money(t.amount)}`));
        b.addEventListener("click", () => openTx(t));
        group.append(b);
      }
      list.append(group);
    }
  }
  const INCOME = { salario: "💼 Salario", freelance: "🧾 Trabajos extra", inversiones: "📈 Rendimientos", otros_ingresos: "💰 Otros ingresos" };
  const incomeLabel = (k) => INCOME[k] || `📦 ${k}`;

  function openTx(t) {
    state.editing = t;
    $("#dlg-title").textContent = t.description || "Movimiento";
    $("#dlg-sub").textContent = `${money(t.amount)} · ${t.date}`;
    const sel = $("#dlg-cat");
    sel.replaceChildren();
    const options = t.kind === "income" ? INCOME : state.data.category_catalog;
    for (const [k, label] of Object.entries(options)) sel.append(el("option", { value: k }, label));
    sel.value = t.category;
    const acc = $("#dlg-acc");
    acc.replaceChildren(el("option", { value: "" }, "Sin cuenta"));
    for (const [k, label] of Object.entries(state.data.account_catalog || {})) acc.append(el("option", { value: k }, label));
    acc.value = t.account || "";
    $("#tx-dialog").showModal();
  }

  $("#tx-dialog").addEventListener("close", async () => {
    const action = $("#tx-dialog").returnValue;
    const t = state.editing;
    if (!t || action === "cancel" || !action) return;
    try {
      if (action === "delete") {
        await api(`/api/transactions/${t.id}`, { method: "DELETE" });
      } else if (action === "save") {
        const body = {};
        if ($("#dlg-cat").value !== t.category) body.category = $("#dlg-cat").value;
        if ($("#dlg-acc").value !== (t.account || "")) body.account = $("#dlg-acc").value;
        if (Object.keys(body).length) await api(`/api/transactions/${t.id}`, { method: "PATCH", body: JSON.stringify(body) });
      }
      haptic("success");
      await load();
    } catch (err) {
      haptic("error");
      alert(err.message);
    }
  });

  function renderSubscriptions(d) {
    const list = $("#subs");
    list.replaceChildren();
    $("#subs-total").textContent = `Costo fijo mensual: ${money(d.totals.fixed_monthly)}`;
    if (!d.subscriptions.length) { list.append(el("li", { class: "empty" }, "Sin suscripciones registradas.")); return; }
    const cycles = { weekly: "semanal", monthly: "mensual", yearly: "anual" };
    for (const s of d.subscriptions) {
      const li = el("li");
      const left = el("div");
      const next = new Date(`${s.next_charge}T12:00:00`).toLocaleDateString("es", { day: "numeric", month: "short" });
      left.append(el("div", {}, s.name), el("div", { class: "meta" }, `${cycles[s.cycle] || s.cycle} · próximo ${next}`));
      const right = el("div", { class: "amt" });
      right.append(el("div", {}, money(s.amount)));
      if (s.cycle !== "monthly") right.append(el("div", { class: "meta" }, `${money(s.monthly_cost)}/mes`));
      li.append(left, right);
      list.append(li);
    }
  }

  function renderAccounts(d) {
    const list = $("#accounts");
    list.replaceChildren();
    const balances = d.accounts || [];
    const spent = d.spent_by_account || [];
    $("#accounts-card").hidden = !balances.length && !spent.some((x) => x.label !== "Sin cuenta");
    for (const a of balances) {
      const li = el("li");
      const left = el("div");
      left.append(el("div", {}, a.label), el("div", { class: "meta" }, a.updated ? `saldo al ${a.updated.slice(8)}/${a.updated.slice(5, 7)}` : "saldo"));
      li.append(left, el("div", { class: "amt" }, money(a.balance)));
      list.append(li);
    }
    for (const x of spent) {
      const li = el("li");
      li.append(el("div", { class: "meta" }, `Gastado con ${x.label}`), el("div", { class: "amt meta" }, money(x.amount)));
      list.append(li);
    }
  }

  function renderReservesAndDebts(d) {
    const res = $("#reserves");
    res.replaceChildren();
    if (!(d.reserves || []).length) res.append(el("li", { class: "empty" }, "Nada apartado todavía."));
    for (const r of d.reserves || []) {
      const li = el("li");
      const head = el("div", { class: "budget-head" });
      head.append(el("span", {}, r.nombre.charAt(0).toUpperCase() + r.nombre.slice(1)),
        el("span", {}, `${money(r.gastado)} / ${money(r.monto)}`));
      const pct = r.monto ? Math.min(100, (r.gastado / r.monto) * 100) : 0;
      const meter = el("div", { class: "meter", role: "meter", "aria-valuenow": Math.round(pct),
        "aria-valuemin": "0", "aria-valuemax": "100", "aria-label": r.nombre });
      const fill = el("div");
      fill.style.width = `${pct}%`;
      fill.style.background = "var(--series-1)";
      meter.append(fill);
      li.append(head, meter, el("div", { class: "status-tag" }, `Quedan ${money(r.pendiente)} apartados`));
      res.append(li);
    }
    const list = $("#debts");
    list.replaceChildren();
    if (!(d.debts || []).length) { list.append(el("li", { class: "empty" }, "Sin préstamos abiertos.")); return; }
    for (const debt of d.debts) {
      const li = el("li");
      const left = el("div");
      const label = debt.direction === "me_deben" ? `${debt.person} te debe` : `Le debes a ${debt.person}`;
      let meta = debt.paid ? `abonado ${money(debt.paid)}` : "sin abonos";
      if (debt.due_date) {
        const due = new Date(`${debt.due_date}T12:00:00`);
        const days = Math.round((due - new Date()) / 86400000);
        meta += ` · ${days < 0 ? `vencido hace ${-days} d` : `vence ${due.toLocaleDateString("es", { day: "numeric", month: "short" })}`}`;
      }
      left.append(el("div", {}, label), el("div", { class: "meta" }, meta));
      const right = el("div", { class: `amt ${debt.direction === "me_deben" ? "income" : ""}` }, money(debt.balance));
      li.append(left, right);
      list.append(li);
    }
  }

  function renderSettings(d) {
    const form = $("#profile-form");
    if (document.activeElement && form.contains(document.activeElement)) return;
    form.monthly_income.value = d.profile.monthly_income ?? "";
    form.monthly_income.placeholder = d.profile.learned_income ? `Aprendido: ${money(d.profile.learned_income)}` : "";
    form.savings_goal_pct.value = d.profile.savings_goal_pct;
    form.payday.value = d.profile.payday ?? "";
    const sel = $("#budget-cat");
    if (!sel.options.length) {
      for (const [k, label] of Object.entries(d.category_catalog)) sel.append(el("option", { value: k }, label));
    }
  }

  $("#profile-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = {
      monthly_income: f.monthly_income.value ? Number(f.monthly_income.value) : null,
      savings_goal_pct: Number(f.savings_goal_pct.value || 0),
      payday: f.payday.value ? Number(f.payday.value) : null,
    };
    try { await api("/api/profile", { method: "PUT", body: JSON.stringify(body) }); haptic("success"); await load(); }
    catch (err) { haptic("error"); alert(err.message); }
  });

  $("#budget-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api(`/api/budgets/${f.category.value}`, { method: "PUT", body: JSON.stringify({ amount: Number(f.amount.value) }) });
      haptic("success");
      f.amount.value = "";
      await load();
    } catch (err) { haptic("error"); alert(err.message); }
  });

  // ------------------------------------------------------------------ navegación
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
    state.tab = b.dataset.tab;
    document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    hideTip();
    render();
  }));
  $("#cat-filter").addEventListener("change", () => renderTransactions(state.data));
  $("#prev").addEventListener("click", () => { state.month = shiftMonth(state.month, -1); load(); });
  $("#next").addEventListener("click", () => { state.month = shiftMonth(state.month, 1); load(); });
  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });

  // ------------------------------------------------------------------ demo (?demo=1) para desarrollo local
  function demoApi(path) {
    if (!path.startsWith("/api/dashboard")) return Promise.resolve({ ok: true });
    const cats = { vivienda: "🏠 Vivienda", supermercado: "🛒 Supermercado", restaurantes: "🍽️ Restaurantes y cafés",
      transporte: "🚌 Transporte", ocio: "🎉 Ocio y salidas", suscripciones: "🔁 Suscripciones", salud: "🩺 Salud", otros: "📦 Otros" };
    const daily = []; let acc = 0;
    for (let i = 1; i <= 15; i++) {
      const spent = i === 1 ? 1200000 : [42000, 18000, 95000, 0, 23000, 160000, 31000][i % 7];
      acc += spent;
      daily.push({ date: `2026-10-${String(i).padStart(2, "0")}`, spent, cumulative: acc });
    }
    const tx = (id, date, amount, category, description, kind = "expense", source = "text") =>
      ({ id, date, amount, category, description, kind, source, merchant: "", necessity: "discrecional", currency: "COP", created_at: date });
    return Promise.resolve({
      profile: { name: "Ana", currency: "COP", monthly_income: 4200000, learned_income: null, savings_goal_pct: 15, payday: 30 },
      month: "2026-10", is_current_month: true,
      totals: { income: 4200000, expense: acc, net: 4200000 - acc, discretionary: 610000, spendable: 3570000,
        committed: acc + 62000, remaining: 3570000 - acc - 62000, projection: 3910000, fixed_monthly: 211900 },
      categories: [
        { category: "vivienda", label: cats.vivienda, amount: 1200000, baseline: 1200000 },
        { category: "supermercado", label: cats.supermercado, amount: 410000, baseline: 520000 },
        { category: "restaurantes", label: cats.restaurantes, amount: 296000, baseline: 240000 },
        { category: "ocio", label: cats.ocio, amount: 214000, baseline: 150000 },
        { category: "transporte", label: cats.transporte, amount: 132000, baseline: 180000 },
        { category: "suscripciones", label: cats.suscripciones, amount: 61900, baseline: 61900 },
      ],
      daily,
      monthly_history: [["2026-05", 4000000, 3300000], ["2026-06", 4000000, 3650000], ["2026-07", 4200000, 3480000],
        ["2026-08", 4200000, 3720000], ["2026-09", 4200000, 3390000], ["2026-10", 4200000, acc]]
        .map(([month, income, expense]) => ({ month, income, expense })),
      weekday: [310000, 280000, 240000, 330000, 690000, 910000, 520000],
      budgets: [
        { category: "ocio", label: cats.ocio, limit: 250000, spent: 214000, pct: 85.6 },
        { category: "restaurantes", label: cats.restaurantes, limit: 280000, spent: 296000, pct: 105.7 },
        { category: "supermercado", label: cats.supermercado, limit: 700000, spent: 410000, pct: 58.6 },
      ],
      subscriptions: [
        { id: "s1", name: "Netflix", amount: 44900, cycle: "monthly", next_charge: "2026-10-18", monthly_cost: 44900, category: "suscripciones" },
        { id: "s2", name: "Spotify", amount: 17000, cycle: "monthly", next_charge: "2026-10-22", monthly_cost: 17000, category: "suscripciones" },
        { id: "s3", name: "Gimnasio", amount: 150000, cycle: "monthly", next_charge: "2026-11-01", monthly_cost: 150000, category: "salud" },
      ],
      transactions: [
        { ...tx("a", "2026-10-15", 31000, "restaurantes", "Almuerzo con el equipo", "expense", "voice"), account: "nequi" },
        tx("b", "2026-10-14", 160000, "ocio", "Concierto"),
        tx("c", "2026-10-14", 23000, "transporte", "Uber al centro", "expense", "voice"),
        tx("d", "2026-10-12", 95000, "supermercado", "Mercado semanal"),
        tx("e", "2026-10-01", 4200000, "salario", "Sueldo", "income"),
        tx("f", "2026-10-01", 1200000, "vivienda", "Arriendo"),
      ],
      insights: [
        "Donde más se le va el dinero al mes: 🏠 Vivienda (~$1.200.000 COP), 🛒 Supermercado (~$520.000 COP), 🍽️ Restaurantes y cafés (~$240.000 COP).",
        "Suele gastar más los sábados; el fin de semana concentra el 43% de sus gastos variables.",
        "Ahorra en promedio el 14% de sus ingresos (no cumple su meta del 15%).",
        "Prefiere comunicarse con notas de voz.",
      ],
      category_catalog: cats,
      accounts: [{ key: "nequi", label: "💜 Nequi", balance: 312000, updated: "2026-10-15" },
                 { key: "efectivo", label: "💵 Efectivo", balance: 45000, updated: "2026-10-14" }],
      spent_by_account: [{ label: "💜 Nequi", amount: 980000 }, { label: "🟣 Nu", amount: 610000 }, { label: "💵 Efectivo", amount: 348000 }],
      account_catalog: { nequi: "💜 Nequi", efectivo: "💵 Efectivo", nu: "🟣 Nu", daviplata: "❤️ Daviplata" },
      reserves: [{ nombre: "gasolina", monto: 200000, gastado: 120000, pendiente: 80000, categoria: "transporte" }],
      debts: [
        { id: "d1", direction: "me_deben", person: "Juan", amount: 50000, paid: 20000, balance: 30000, date: "2026-10-02", due_date: "2026-10-17" },
        { id: "d2", direction: "debo", person: "Mi papá", amount: 300000, paid: 0, balance: 300000, date: "2026-09-20", due_date: "2026-10-30" },
      ],
    });
  }

  load();
})();
