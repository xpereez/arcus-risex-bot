const $ = (id) => document.getElementById(id);

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

async function refresh() {
  const response = await fetch("/api/state", { cache: "no-store" });
  const state = await response.json();
  $("phase").textContent = state.phase || "—";
  $("mode").textContent = state.mode || "—";
  $("net-delta").textContent = pretty(state.net_delta || {});
  $("cycles").textContent = state.stats?.cycles_completed ?? "—";
  $("pnl").textContent = state.stats?.session_pnl ?? "—";
  $("error").textContent = state.last_error || "—";
  $("cycle-title").textContent = state.current_cycle?.symbol || "Sin ciclo";
  $("cycle").textContent = state.current_cycle ? pretty(state.current_cycle) : "—";
  $("positions").textContent = pretty(state.positions || []);
}

async function control(path) {
  await fetch(path, { method: "POST" });
  await refresh();
}

$("start-button").addEventListener("click", () => control("/api/start"));
$("pause-button").addEventListener("click", () => control("/api/pause"));
refresh().catch((error) => { $("error").textContent = error.message; });
setInterval(() => refresh().catch(() => {}), 3000);
