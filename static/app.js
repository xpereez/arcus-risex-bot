const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const number = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 8 });
const metricNumber = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 0 });
let arcusState = null;
let marketMakingState = null;
let marketMakingAnalytics = null;
let selectedBot = "arcus";
let toastTimer = null;
let arcusRefreshInFlight = false;
let marketMakingRefreshInFlight = false;
let analyticsRefreshInFlight = false;
let analyticsHours = 24;

function price(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return money.format(Number(value));
}

function setText(id, value) {
  const element = $(id);
  if (element) element.textContent = value;
}

function phaseLabel(phase) {
  return {
    STOPPED: "Motor detenido", IDLE: "Preparando ciclo", OPENING_ARCUS: "Orden limit en Arcus",
    HEDGING_LIGHTER: "Cubriendo fill en Lighter", HOLDING: "Posición delta neutral",
    CLOSING_ARCUS: "Cierre limit en Arcus", CLOSING_LIGHTER: "Cerrando hedge en Lighter",
    COOLDOWN: "Pausa entre ciclos", PAUSED: "Pausado", ERROR: "Intervención requerida",
    UNAVAILABLE: "No disponible"
  }[phase] || phase;
}

function renderArcus(data) {
  arcusState = data;
  const stats = data.stats || { session_volume: 0, session_pnl: 0, cycles_completed: 0 };
  const cycle = data.current_cycle;
  setText("phase", phaseLabel(data.phase));
  setText("mode-pill", String(data.mode || "--").toUpperCase());
  $("start-button").disabled = Boolean(data.accepting_cycles);
  $("pause-button").disabled = !data.accepting_cycles;
  $("status-dot").style.background = data.phase === "ERROR" || data.phase === "UNAVAILABLE" ? "var(--red)" : (data.accepting_cycles ? "#59c9ad" : "#9aa4a1");
  setText("cycle-description", data.last_error || describeCycle(cycle, data.phase));

  const totalDelta = Object.values(data.net_delta || {}).reduce((sum, value) => sum + Math.abs(Number(value)), 0);
  setText("net-delta", number.format(totalDelta));
  $("net-delta").className = totalDelta < 1e-7 ? "positive" : "negative";
  setText("volume", money.format(stats.session_volume || 0));
  setText("session-pnl", money.format(stats.session_pnl || 0));
  $("session-pnl").className = Number(stats.session_pnl) >= 0 ? "positive" : "negative";
  setText("cycles", String(stats.cycles_completed || 0));
  setText("notional", money.format(cycle?.notional_usd || 0));
  setText("current-market", cycle ? cycle.symbol : "sin posición");

  setText("arcus-card-detail", data.last_error || phaseLabel(data.phase));
  setText("arcus-card-volume", money.format(stats.session_volume || 0));
  setText("arcus-card-pnl", money.format(stats.session_pnl || 0));
  $("arcus-card-pnl").className = Number(stats.session_pnl) >= 0 ? "positive" : "negative";
  setStatusPill("arcus-card-status", data.phase === "ERROR" || data.phase === "UNAVAILABLE" ? "ERROR" : (data.accepting_cycles ? "ACTIVO" : data.phase), data.phase === "ERROR" || data.phase === "UNAVAILABLE" ? "error" : (data.accepting_cycles ? "online" : "idle"));

  renderWallets(data.accounts || []);
  renderCycle(cycle);
  renderPositions(data.positions || []);
  renderEvents(data.events || []);
  renderConfig(data.config || {});
  renderCountdown();
  renderFleet();
}

function describeCycle(cycle, phase) {
  if (!cycle) return phase === "COOLDOWN" ? "Capital neutral; esperando el próximo mercado" : "Sin exposición abierta";
  return `${cycle.symbol} · ${cycle.arcus_side} Arcus / ${cycle.arcus_side === "BUY" ? "SELL" : "BUY"} Lighter`;
}

function renderWallets(accounts) {
  if (!accounts.length) {
    $("wallets").innerHTML = '<div class="empty-state">Wallets no disponibles</div>';
    return;
  }
  $("wallets").innerHTML = accounts.map((account) => `
    <article class="wallet">
      <div class="wallet-head"><div class="venue-name"><span class="venue-icon">${escapeHtml(account.venue.slice(0, 1).toUpperCase())}</span>${escapeHtml(account.venue)}</div><span class="connected">● ${escapeHtml(account.status || "online")}</span></div>
      <p class="wallet-value">${money.format(account.equity)}</p>
      <div class="wallet-volumes">
        <div class="wallet-volume"><span>Volumen histórico wallet</span><strong>${price(account.lifetime_volume_usd)}</strong><small>total en el protocolo</small></div>
        <div class="wallet-volume tracked"><span>Volumen desde baseline</span><strong>${price(account.volume_since_baseline_usd)}</strong><small>${baselineLabel(account.volume_baseline_started_at)}</small></div>
      </div>
      <div class="wallet-meta"><span>Libre<strong>${money.format(account.free_collateral)}</strong></span><span>PnL no realizado<strong class="${account.unrealized_pnl >= 0 ? "positive" : "negative"}">${money.format(account.unrealized_pnl)}</strong></span></div>
    </article>`).join("");
}

function baselineLabel(value) {
  if (!value) return "punto de inicio no disponible";
  return `desde ${new Date(value).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" })}`;
}

function renderCycle(cycle) {
  $("cycle-empty").classList.toggle("hidden", Boolean(cycle));
  $("cycle-details").classList.toggle("hidden", !cycle);
  if (!cycle) {
    setText("cycle-title", "Sin operación");
    setText("cycle-side", "IDLE");
    $("cycle-side").className = "side-badge neutral";
    return;
  }
  setText("cycle-title", cycle.symbol);
  setText("cycle-side", cycle.arcus_side);
  $("cycle-side").className = `side-badge ${cycle.arcus_side.toLowerCase()}`;
  setText("cycle-size", number.format(cycle.target_size));
  setText("cycle-opened", number.format(cycle.opened_size));
  setText("arcus-entry", price(cycle.arcus_open_price));
  setText("lighter-entry", price(cycle.lighter_open_price));
  const progress = Math.min(100, Math.round((cycle.opened_size / cycle.target_size) * 100) || 0);
  $("cycle-progress").style.width = `${progress}%`;
  setText("cycle-percent", `${progress}%`);
}

function renderPositions(positions) {
  setText("position-count", String(positions.length));
  if (!positions.length) {
    $("positions-body").innerHTML = '<tr><td colspan="8" class="empty-cell">No hay posiciones abiertas</td></tr>';
    return;
  }
  $("positions-body").innerHTML = positions.map((position) => {
    const long = position.signed_size > 0;
    return `<tr>
      <td>${escapeHtml(position.venue)}</td><td>${escapeHtml(position.symbol)}</td>
      <td class="direction ${long ? "positive" : "negative"}">${long ? "LONG" : "SHORT"}</td>
      <td>${number.format(Math.abs(position.signed_size))}</td><td>${price(position.entry_price)}</td>
      <td>${price(position.mark_price)}</td><td>${price(position.liquidation_price)}</td>
      <td class="${position.unrealized_pnl >= 0 ? "positive" : "negative"}">${money.format(position.unrealized_pnl)}</td>
    </tr>`;
  }).join("");
}

function renderEvents(events) {
  if (!events.length) {
    $("events").innerHTML = '<div class="empty-state">Sin actividad todavía</div>';
    return;
  }
  $("events").innerHTML = events.slice(0, 18).map((event) => {
    const date = new Date(event.created_at);
    return `<div class="event ${escapeHtml(event.level)}"><time>${date.toLocaleTimeString("es-ES", {hour: "2-digit", minute: "2-digit", second: "2-digit"})}</time><span class="event-kind">${escapeHtml(event.kind.replaceAll("_", " "))}</span><span class="event-message">${escapeHtml(event.message)}</span></div>`;
  }).join("");
}

function renderConfig(config) {
  const rows = [
    ["Mercados", (config.markets || []).join(", ") || "—"],
    ["Notional", `${money.format(config.min_notional_usd || 0)} – ${money.format(config.max_notional_usd || 0)}`],
    ["Hold", `${config.hold_min_minutes ?? "—"} – ${config.hold_max_minutes ?? "—"} min`],
    ["Entre ciclos", `${config.cycle_pause_min_minutes ?? "—"} – ${config.cycle_pause_max_minutes ?? "—"} min`],
    ["Timeout limit", `${config.order_timeout_seconds ?? "—"} s`],
    ["Slippage máx.", `${config.max_slippage_bps ?? "—"} bps`],
    ["Límite pérdida", money.format(config.max_daily_loss_usd || 0)]
  ];
  $("config").innerHTML = rows.map(([key, value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join("");
}

function renderMarketMaking(data) {
  marketMakingState = data;
  const records = data.records || {};
  const shadow = data.shadow || {};
  const quote = data.quote;
  const runtime = data.runtime || {};
  const quoteActions = data.quote_actions || {};
  const process = data.process || {};
  const displayState = data.state || "UNAVAILABLE";
  const onlineClass = data.running ? "online" : (displayState === "HALTED" || displayState === "UNAVAILABLE" ? "error" : "idle");

  setText("mm-state", marketMakingLabel(displayState));
  setText("mm-description", data.last_reason || (data.running ? "Recorder y libros recibiendo mercado" : (data.available ? "Bot detenido; mostrando la última sesión grabada" : "Recorder no disponible")));
  setText("mm-last-seen", relativeTime(data.last_seen_at));
  setText("mm-inventory", `${number.format(shadow.inventory_base || 0)} BTC`);
  setText("mm-events", integer.format(records.events || 0));
  setText("mm-decisions", integer.format(records.decisions || 0));
  setText("mm-fills", integer.format(records.fills || 0));
  setText("mm-volume", money.format(shadow.volume_usd || 0));
  setText("mm-gaps", integer.format(data.continuity_gaps || 0));
  setText("mm-markouts", integer.format(records.markouts || 0));
  setText("mm-equity", money.format(shadow.equity_usd || 0));
  setText("mm-recorded-time", formatDuration(data.recorded_seconds || 0));
  setText("mm-reason", data.last_reason || "—");
  setText("mm-open-quotes", integer.format(runtime.open_shadow_quotes || 0));
  setText("mm-quote-actions", integer.format((quoteActions.NEW || 0) + (quoteActions.REPLACE || 0) + (quoteActions.CANCEL || 0)));
  setText("mm-fill-rate", `${number.format(shadow.fill_rate_percent || 0)}%`);
  setText("mm-spread-capture", `${number.format(shadow.spread_capture_bps || 0)} bps`);
  setText("mm-rh-book", runtime.rh_book_valid ? "VÁLIDO" : "SINCRONIZANDO");
  setText("mm-reference-book", runtime.reference_book_valid ? "VÁLIDO" : "SINCRONIZANDO");
  $("mm-rh-book").className = runtime.rh_book_valid ? "positive" : "";
  $("mm-reference-book").className = runtime.reference_book_valid ? "positive" : "";
  setText("mm-sessions", integer.format(data.sessions || 0));
  $("mm-start-button").disabled = Boolean(process.running);
  $("mm-stop-button").disabled = !process.running;
  $("mm-status-dot").className = `status-dot ${onlineClass === "online" ? "" : "offline"}`;
  $("mm-status-dot").style.background = onlineClass === "online" ? "#59c9ad" : (onlineClass === "error" ? "var(--red)" : "#9aa4a1");

  setText("mm-card-events", integer.format(records.events || 0));
  setText("mm-card-volume", money.format(shadow.volume_usd || 0));
  setText("mm-card-detail", data.running ? "Grabando mercado ahora" : (data.available ? `Último dato ${relativeTime(data.last_seen_at)}` : "Recorder no encontrado"));
  setStatusPill("mm-card-status", displayState, onlineClass);
  renderQuote(quote);
  renderShadowFills(data.recent_fills || []);
  renderQuoteActions(data.recent_quote_actions || []);
  renderMarkouts(data.markouts || []);
  renderFleet();
}

function marketMakingLabel(state) {
  return { SHADOW: "Cotizando en shadow", STARTING: "Arrancando shadow", STOPPED: "Bot detenido", SYNCING: "Sincronizando libros", PAUSED: "Pausado por riesgo", HALTED: "Detenido por seguridad", UNAVAILABLE: "No disponible", BOOT: "Inicializando" }[state] || state;
}

function renderQuote(quote) {
  if (!quote) {
    ["mm-bid", "mm-fair", "mm-ask"].forEach((id) => setText(id, "-"));
    setText("mm-quote-state", "sin datos");
    return;
  }
  setText("mm-quote-state", "última decisión");
  setText("mm-bid", price(quote.bid?.price));
  setText("mm-bid-size", quote.bid ? `${number.format(quote.bid.size)} BTC` : "suprimido");
  setText("mm-fair", price(quote.fair));
  setText("mm-reservation", `Reserva ${price(quote.reservation)}`);
  setText("mm-ask", price(quote.ask?.price));
  setText("mm-ask-size", quote.ask ? `${number.format(quote.ask.size)} BTC` : "suprimido");
  setText("mm-basis", `${number.format(quote.basis_bps || 0)} bps`);
  setText("mm-volatility", `${number.format(quote.volatility_bps || 0)} bps`);
  setText("mm-toxicity", `${number.format(quote.toxicity_bps || 0)} bps`);
}

function renderShadowFills(fills) {
  setText("mm-fill-count", String(fills.length));
  if (!fills.length) {
    $("mm-fills-body").innerHTML = '<tr><td colspan="6" class="empty-cell">No hay fills simulados</td></tr>';
    return;
  }
  $("mm-fills-body").innerHTML = fills.map((fill) => `<tr>
    <td>${new Date(fill.timestamp_ms).toLocaleTimeString("es-ES")}</td>
    <td class="${fill.side === "BUY" ? "positive" : "negative"}">${escapeHtml(fill.side)}</td>
    <td>${price(fill.price)}</td><td>${number.format(fill.size)}</td><td>${price(fill.fair_at_fill)}</td>
    <td>${money.format(fill.price * fill.size)}</td>
  </tr>`).join("");
}

function renderMarkouts(markouts) {
  if (!markouts.length) {
    $("mm-markout-list").innerHTML = '<div class="empty-state">Sin muestras todavía</div>';
    return;
  }
  $("mm-markout-list").innerHTML = markouts.map((item) => `<div class="markout-row"><span>${item.horizon_ms} ms<small>${item.samples} muestras</small></span><strong class="${item.average_bps >= 0 ? "positive" : "negative"}">${number.format(item.average_bps)} bps</strong><em>${money.format(item.total_usd)}</em></div>`).join("");
}

function renderQuoteActions(actions) {
  if (!actions.length) {
    $("mm-quote-action-list").innerHTML = '<div class="empty-state">Sin acciones todavía</div>';
    return;
  }
  $("mm-quote-action-list").innerHTML = actions.map((action) => `<div class="quote-action-row">
    <time>${new Date(action.timestamp_ms).toLocaleTimeString("es-ES", {hour:"2-digit", minute:"2-digit", second:"2-digit"})}</time>
    <span class="action-${String(action.action).toLowerCase()}">${escapeHtml(action.action)}</span>
    <span>${escapeHtml(action.side)} · ${price(action.price)} · ${number.format(action.size)} BTC</span>
    <em>cola ${number.format(action.queue_ahead)} · ${escapeHtml(action.reason || "—")}</em>
  </div>`).join("");
}

function renderAnalytics(data) {
  marketMakingAnalytics = data;
  const summary = data.summary || {};
  setText("mm-analytics-updated", data.available ? `Actualizado ${relativeTime(data.generated_at)} · ventana ${data.window?.hours || analyticsHours} h` : "Análisis no disponible");
  setText("mm-analysis-volume", money.format(summary.volume_usd || 0));
  setText("mm-analysis-fills", `${integer.format(summary.fills || 0)} fills`);
  setMetric("mm-analysis-markout", summary.markout_5s_bps, "bps");
  setText("mm-analysis-churn", metricNumber.format(summary.quote_actions_per_minute || 0));
  setText("mm-analysis-hours", integer.format(summary.active_hours || 0));
  renderLineChart("mm-flow-chart", data.hourly || [], [
    { key: "events", label: "Eventos", color: "#0b7664" },
    { key: "quote_actions", label: "Quotes", color: "#4f7f98" }
  ], false);
  renderLineChart("mm-quality-chart", data.hourly || [], [
    { key: "spread_capture_bps", label: "Capture", color: "#0b7664" },
    { key: "markout_5s_bps", label: "Markout 5 s", color: "#d56a24" }
  ], true);
  renderRegimes(data.regimes || {});
  renderAnalysisAlerts(data.alerts || [], data.recommendations || []);
  if (window.lucide) window.lucide.createIcons();
}

function setMetric(id, value, suffix) {
  setText(id, value === null || value === undefined ? "—" : `${metricNumber.format(value)} ${suffix}`);
  $(id).className = value === null || value === undefined ? "" : (Number(value) >= 0 ? "positive" : "negative");
}

function renderLineChart(id, rows, series, signed) {
  const target = $(id);
  const visible = rows.filter((row) => series.some((item) => row[item.key] !== null && row[item.key] !== undefined));
  if (!visible.length || !visible.some((row) => series.some((item) => Number(row[item.key]) !== 0))) {
    target.innerHTML = '<div class="empty-state">Sin muestras en esta ventana</div>';
    return;
  }
  const width = 720;
  const height = 230;
  const left = 48;
  const right = 704;
  const top = 20;
  const bottom = 192;
  const values = visible.flatMap((row) => series.map((item) => row[item.key])).filter((value) => value !== null && value !== undefined).map(Number);
  let minimum = signed ? Math.min(0, ...values) : 0;
  let maximum = Math.max(0, ...values);
  if (minimum === maximum) maximum = minimum + 1;
  const x = (index) => left + (right - left) * (visible.length === 1 ? 0.5 : index / (visible.length - 1));
  const y = (value) => bottom - (Number(value) - minimum) / (maximum - minimum) * (bottom - top);
  const grid = [0, 0.5, 1].map((ratio) => {
    const value = maximum - (maximum - minimum) * ratio;
    const yValue = top + (bottom - top) * ratio;
    return `<line x1="${left}" y1="${yValue}" x2="${right}" y2="${yValue}"/><text x="${left - 8}" y="${yValue + 4}" text-anchor="end">${compactNumber(value)}</text>`;
  }).join("");
  const paths = series.map((item) => {
    const points = visible.map((row, index) => row[item.key] === null || row[item.key] === undefined ? null : `${x(index)},${y(row[item.key])}`).filter(Boolean);
    if (!points.length) return "";
    const dots = points.length < 8 ? points.map((point) => { const [cx, cy] = point.split(","); return `<circle cx="${cx}" cy="${cy}" r="3" fill="${item.color}"/>`; }).join("") : "";
    return `<polyline points="${points.join(" ")}" fill="none" stroke="${item.color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>${dots}`;
  }).join("");
  const axisLabels = visible.length === 1
    ? `<text x="${(left + right) / 2}" y="218" text-anchor="middle">${chartTime(visible[0].timestamp_ms)}</text>`
    : `<text x="${left}" y="218">${chartTime(visible[0].timestamp_ms)}</text><text x="${(left + right) / 2}" y="218" text-anchor="middle">${chartTime(visible[Math.floor((visible.length - 1) / 2)].timestamp_ms)}</text><text x="${right}" y="218" text-anchor="end">${chartTime(visible[visible.length - 1].timestamp_ms)}</text>`;
  target.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Gráfico histórico"><g class="chart-grid">${grid}</g>${signed && minimum < 0 && maximum > 0 ? `<line class="zero-line" x1="${left}" y1="${y(0)}" x2="${right}" y2="${y(0)}"/>` : ""}${paths}<g class="chart-axis">${axisLabels}</g></svg>`;
}

function compactNumber(value) {
  const absolute = Math.abs(value);
  if (absolute >= 1000) return `${metricNumber.format(value / 1000)}k`;
  return metricNumber.format(value);
}

function chartTime(timestamp) {
  return new Date(timestamp).toLocaleString("es-ES", { weekday: "short", hour: "2-digit" });
}

function renderRegimes(regimes) {
  const dimensions = [
    ["side", "Lado"], ["volatility", "Volatilidad"], ["basis", "Basis"], ["quoted_spread", "Spread cotizado"]
  ];
  $("mm-regime-grid").innerHTML = dimensions.map(([key, title]) => `<section class="regime-block"><h3>${title}</h3>${(regimes[key] || []).map((item) => `<div class="regime-row ${item.fills ? "" : "empty"}"><span>${escapeHtml(item.label)}<small>${integer.format(item.fills)} fills · ${money.format(item.volume_usd || 0)}</small></span><strong class="${item.spread_capture_bps === null ? "" : (item.spread_capture_bps >= 0 ? "positive" : "negative")}">${item.spread_capture_bps === null ? "—" : `${metricNumber.format(item.spread_capture_bps)} bps`}<small>capture</small></strong><strong class="${item.markout_5s_bps === null ? "" : (item.markout_5s_bps >= 0 ? "positive" : "negative")}">${item.markout_5s_bps === null ? "—" : `${metricNumber.format(item.markout_5s_bps)} bps`}<small>markout 5 s</small></strong></div>`).join("")}</section>`).join("");
}

function renderAnalysisAlerts(alerts, recommendations) {
  $("mm-analysis-alerts").innerHTML = alerts.length ? alerts.map((alert) => `<div class="analysis-alert ${escapeHtml(alert.severity)}"><span></span><div><strong>${escapeHtml(alert.title)}</strong><p>${escapeHtml(alert.detail)}</p></div></div>`).join("") : '<div class="empty-state">Sin alertas</div>';
  $("mm-recommendations").innerHTML = recommendations.length ? `<h3>Recomendaciones automáticas</h3><ol>${recommendations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol>` : "";
}

function setStatusPill(id, label, status) {
  setText(id, label || "--");
  $(id).className = `bot-status ${status || "idle"}`;
}

function renderFleet() {
  if (!arcusState) return;
  const arcusOnline = arcusState.phase !== "UNAVAILABLE";
  const mmOnline = Boolean(marketMakingState?.running);
  const online = Number(arcusOnline) + Number(mmOnline);
  setText("fleet-status", `${online}/2 online`);
  setText("fleet-detail", mmOnline ? "sistemas operativos" : "market maker detenido");
  $("fleet-dot").style.background = online === 2 ? "#59c9ad" : (online === 1 ? "#e59b45" : "var(--red)");
}

function selectBot(bot) {
  selectedBot = bot;
  $("arcus-bot-view").classList.toggle("hidden", bot !== "arcus");
  $("market-making-bot-view").classList.toggle("hidden", bot !== "market-making");
  $("arcus-actions").classList.toggle("hidden", bot !== "arcus");
  document.querySelectorAll("[data-bot]").forEach((element) => element.classList.toggle("active", element.dataset.bot === bot));
  document.querySelector(".bot-view:not(.hidden)")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderCountdown() {
  if (!arcusState?.next_action_at) {
    setText("countdown", "--:--");
    return;
  }
  const seconds = Math.max(0, Math.floor((new Date(arcusState.next_action_at).getTime() - Date.now()) / 1000));
  setText("countdown", `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`);
}

function relativeTime(value) {
  if (!value) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 5) return "ahora";
  if (seconds < 60) return `hace ${seconds} s`;
  if (seconds < 3600) return `hace ${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `hace ${Math.floor(seconds / 3600)} h`;
  return new Date(value).toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });
}

function formatDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

async function action(path, message) {
  try {
    const response = await fetch(path, { method: "POST" });
    if (!response.ok) throw new Error(await response.text());
    showToast(message);
    await refreshArcus();
  } catch (error) {
    showToast(`Error: ${error.message}`);
  }
}

async function marketMakingAction(path, message) {
  try {
    const response = await fetch(path, { method: "POST" });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    renderMarketMaking(await response.json());
    showToast(message);
  } catch (error) {
    showToast(`Error: ${error.message}`);
  }
}

function showToast(message) {
  setText("toast", message);
  $("toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.remove("show"), 2600);
}

async function refreshArcus() {
  if (arcusRefreshInFlight) return;
  arcusRefreshInFlight = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    renderArcus(await response.json());
  } catch (error) {
    setText("cycle-description", error.message);
  } finally {
    arcusRefreshInFlight = false;
  }
  if (window.lucide) window.lucide.createIcons();
}

async function refreshMarketMaking() {
  if (marketMakingRefreshInFlight) return;
  marketMakingRefreshInFlight = true;
  try {
    const response = await fetch("/api/bots/market-making-lighter", { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    renderMarketMaking(await response.json());
  } catch (error) {
    setText("mm-description", error.message);
  } finally {
    marketMakingRefreshInFlight = false;
  }
  if (window.lucide) window.lucide.createIcons();
}

async function refreshMarketMakingAnalytics() {
  if (analyticsRefreshInFlight) return;
  analyticsRefreshInFlight = true;
  try {
    const response = await fetch(`/api/bots/market-making-lighter/analytics?hours=${analyticsHours}`, { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    renderAnalytics(await response.json());
  } catch (error) {
    setText("mm-analytics-updated", error.message);
  } finally {
    analyticsRefreshInFlight = false;
  }
}

document.querySelectorAll("[data-bot]").forEach((element) => element.addEventListener("click", () => selectBot(element.dataset.bot)));
$("start-button").addEventListener("click", () => action("/api/start", "Motor Arcus iniciado"));
$("pause-button").addEventListener("click", () => action("/api/pause", "Pausa Arcus solicitada"));
$("mm-start-button").addEventListener("click", () => marketMakingAction("/api/bots/market-making-lighter/start", "Market maker shadow iniciado"));
$("mm-stop-button").addEventListener("click", () => marketMakingAction("/api/bots/market-making-lighter/stop", "Market maker shadow detenido"));
document.querySelectorAll("[data-analytics-hours]").forEach((button) => button.addEventListener("click", () => {
  analyticsHours = Number(button.dataset.analyticsHours);
  document.querySelectorAll("[data-analytics-hours]").forEach((item) => item.classList.toggle("active", item === button));
  refreshMarketMakingAnalytics();
}));
setInterval(refreshArcus, 10000);
setInterval(refreshMarketMaking, 2000);
setInterval(refreshMarketMakingAnalytics, 60000);
setInterval(renderCountdown, 250);
refreshArcus();
refreshMarketMaking();
refreshMarketMakingAnalytics();
if (window.lucide) window.lucide.createIcons();
