const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("es-ES", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const number = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 8 });
let state = null;
let toastTimer = null;
let refreshInFlight = false;

function price(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return money.format(Number(value));
}

function setText(id, value) { $(id).textContent = value; }

function phaseLabel(phase) {
  return {
    STOPPED: "Motor detenido", IDLE: "Preparando ciclo", OPENING_ARCUS: "Orden limit en Arcus",
    HEDGING_LIGHTER: "Cubriendo fill en Lighter", HOLDING: "Posición delta neutral",
    CLOSING_ARCUS: "Cierre limit en Arcus", CLOSING_LIGHTER: "Cerrando hedge en Lighter",
    COOLDOWN: "Pausa entre ciclos", PAUSED: "Pausado", ERROR: "Intervención requerida"
  }[phase] || phase;
}

function render(data) {
  state = data;
  const cycle = data.current_cycle;
  setText("phase", phaseLabel(data.phase));
  setText("side-status", data.phase === "ERROR" ? "Error" : (data.accepting_cycles ? "Activo" : "Pausado"));
  setText("side-mode", data.mode);
  setText("mode-pill", data.mode.toUpperCase());
  $("start-button").disabled = data.accepting_cycles;
  $("pause-button").disabled = !data.accepting_cycles;
  $("status-dot").style.background = data.phase === "ERROR" ? "var(--red)" : (data.accepting_cycles ? "#59c9ad" : "#9aa4a1");
  setText("cycle-description", data.last_error || describeCycle(cycle, data.phase));

  const totalDelta = Object.values(data.net_delta || {}).reduce((sum, value) => sum + Math.abs(Number(value)), 0);
  setText("net-delta", number.format(totalDelta));
  $("net-delta").className = totalDelta < 1e-7 ? "positive" : "negative";
  setText("volume", money.format(data.stats.session_volume));
  setText("session-pnl", money.format(data.stats.session_pnl));
  $("session-pnl").className = data.stats.session_pnl >= 0 ? "positive" : "negative";
  setText("cycles", String(data.stats.cycles_completed));
  setText("notional", money.format(cycle?.notional_usd || 0));
  setText("current-market", cycle ? cycle.symbol : "sin posición");

  renderWallets(data.accounts || []);
  renderCycle(cycle);
  renderPositions(data.positions || []);
  renderEvents(data.events || []);
  renderConfig(data.config || {});
  renderCountdown();
  if (window.lucide) window.lucide.createIcons();
}

function describeCycle(cycle, phase) {
  if (!cycle) return phase === "COOLDOWN" ? "Capital neutral; esperando el próximo mercado" : "Sin exposición abierta";
  return `${cycle.symbol} · ${cycle.arcus_side} Arcus / ${cycle.arcus_side === "BUY" ? "SELL" : "BUY"} Lighter`;
}

function renderWallets(accounts) {
  $("wallets").innerHTML = accounts.map((account) => `
    <article class="wallet">
      <div class="wallet-head"><div class="venue-name"><span class="venue-icon">${account.venue.slice(0, 1).toUpperCase()}</span>${account.venue}</div><span class="connected">● online</span></div>
      <p class="wallet-value">${money.format(account.equity)}</p>
      <div class="wallet-volumes">
        <div class="wallet-volume"><span>Volumen histórico wallet</span><strong>${price(account.lifetime_volume_usd)}</strong><small>total en el protocolo</small></div>
        <div class="wallet-volume tracked"><span>Volumen desde ahora</span><strong>${price(account.volume_since_baseline_usd)}</strong><small>${baselineLabel(account.volume_baseline_started_at)}</small></div>
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
      <td>${position.venue}</td><td>${position.symbol}</td>
      <td class="direction ${long ? "positive" : "negative"}">${long ? "LONG" : "SHORT"}</td>
      <td>${number.format(Math.abs(position.signed_size))}</td><td>${price(position.entry_price)}</td>
      <td>${price(position.mark_price)}</td><td>${price(position.liquidation_price)}</td>
      <td class="${position.unrealized_pnl >= 0 ? "positive" : "negative"}">${money.format(position.unrealized_pnl)}</td>
    </tr>`;
  }).join("");
}

function renderEvents(events) {
  if (!events.length) { $("events").innerHTML = '<div class="empty-state">Sin actividad todavía</div>'; return; }
  $("events").innerHTML = events.slice(0, 18).map((event) => {
    const date = new Date(event.created_at);
    return `<div class="event ${event.level}"><time>${date.toLocaleTimeString("es-ES", {hour: "2-digit", minute: "2-digit", second: "2-digit"})}</time><span class="event-kind">${event.kind.replaceAll("_", " ")}</span><span class="event-message">${escapeHtml(event.message)}</span></div>`;
  }).join("");
}

function renderConfig(config) {
  const rows = [
    ["Mercados", (config.markets || []).join(", ")],
    ["Notional", `${money.format(config.min_notional_usd)} – ${money.format(config.max_notional_usd)}`],
    ["Hold", `${config.hold_min_minutes} – ${config.hold_max_minutes} min`],
    ["Entre ciclos", `${config.cycle_pause_min_minutes} – ${config.cycle_pause_max_minutes} min`],
    ["Timeout limit", `${config.order_timeout_seconds} s`],
    ["Slippage máx.", `${config.max_slippage_bps} bps`],
    ["Límite pérdida", money.format(config.max_daily_loss_usd)],
    ["Velocidad paper", `x${config.paper_time_scale}`]
  ];
  $("config").innerHTML = rows.map(([key, value]) => `<div><dt>${key}</dt><dd>${value}</dd></div>`).join("");
}

function renderCountdown() {
  if (!state?.next_action_at) { setText("countdown", "--:--"); return; }
  const seconds = Math.max(0, Math.floor((new Date(state.next_action_at).getTime() - Date.now()) / 1000));
  setText("countdown", `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

async function action(path, message) {
  try {
    const response = await fetch(path, { method: "POST" });
    if (!response.ok) throw new Error(await response.text());
    showToast(message);
    await refresh();
  } catch (error) { showToast(`Error: ${error.message}`); }
}

function showToast(message) {
  setText("toast", message); $("toast").classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").classList.remove("show"), 2600);
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
    render(await response.json());
  } catch (error) {
    setText("side-status", "Desconectado");
    setText("cycle-description", error.message);
  } finally {
    refreshInFlight = false;
  }
}

$("start-button").addEventListener("click", () => action("/api/start", "Motor iniciado"));
$("pause-button").addEventListener("click", () => action("/api/pause", "Pausa solicitada"));
setInterval(refresh, 10000);
setInterval(renderCountdown, 250);
refresh();
if (window.lucide) window.lucide.createIcons();
