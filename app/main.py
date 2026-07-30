"""launchd-dashboard — a local web UI to inventory and control macOS launchd agents.

Run: uvicorn app.main:app --host 127.0.0.1 --port 8787
Binds to localhost by design: the control endpoints (run/stop/enable) mutate real
jobs, so the dashboard is not meant to be exposed beyond your machine.
"""

from __future__ import annotations

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from . import annotations, apps, discover, launchd, ports

# Last discovery scan, server-side. Adoption only ever references these by slug —
# the browser never supplies a directory or command.
_discovered: list = []

app = FastAPI(title="launchd dashboard")


def _app_or_404(slug: str) -> apps.AppSpec:
    for spec in apps.load_apps():
        if spec.slug == slug:
            return spec
    raise HTTPException(status_code=404, detail=f"no app {slug!r} in apps.json")


def _plist_or_404(label: str) -> dict:
    path = launchd.find_plist(label)
    plist = launchd.load_plist(path) if path else None
    if not plist:
        raise HTTPException(status_code=404, detail=f"no agent labelled {label!r}")
    return plist


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/agents")
def api_agents(all: bool = False) -> JSONResponse:
    notes = annotations.load_annotations()
    agents = launchd.list_agents(include_vendor=all)
    for a in agents:
        a["annotation"] = notes.get(a["label"])
    return JSONResponse(agents)


@app.get("/api/agents/{label}/log")
def api_log(label: str, lines: int = 200) -> dict:
    return launchd.read_log_tail(_plist_or_404(label), lines=lines)


@app.post("/api/agents/{label}/run")
def api_run(label: str) -> dict:
    _plist_or_404(label)
    return launchd.run_now(label)


@app.post("/api/agents/{label}/stop")
def api_stop(label: str) -> dict:
    _plist_or_404(label)
    return launchd.stop(label)


@app.post("/api/agents/{label}/enable")
def api_enable(label: str) -> dict:
    _plist_or_404(label)
    return launchd.set_enabled(label, True)


@app.post("/api/agents/{label}/disable")
def api_disable(label: str) -> dict:
    _plist_or_404(label)
    return launchd.set_enabled(label, False)


@app.get("/api/apps")
def api_apps() -> JSONResponse:
    notes = annotations.load_annotations()
    specs = apps.load_apps()
    infos = [(spec, apps.describe(spec)) for spec in specs]

    # Live half of the declared-vs-live port reconciliation: which ports each app's
    # agent ACTUALLY holds. Declared ports drift (Next.js silently steps 3000→3001
    # on a collision), and the Open button must follow reality, not apps.json.
    agent_pids = {a["pid"]: a["label"] for a in launchd.list_agents(include_vendor=True) if a["pid"]}
    for spec, info in infos:
        if info["pid"]:
            agent_pids[info["pid"]] = spec.label
    by_agent = ports.ports_by_agent(ports.list_ports(agent_pids))
    shared = apps.shared_ports(specs)

    out = []
    for spec, info in infos:
        info["annotation"] = notes.get(spec.label)
        live = by_agent.get(spec.label, [])
        open_port, mismatch = apps.port_status(info["port"], live, info["status"] == "running")
        info["live_ports"] = live
        info["open_port"] = open_port
        info["port_mismatch"] = mismatch
        info["port_shared_with"] = [
            s for s in shared.get(info["port"], []) if s != spec.slug
        ]
        out.append(info)
    return JSONResponse(out)


@app.post("/api/apps/{slug}/start")
def api_app_start(slug: str) -> dict:
    return apps.start_app(_app_or_404(slug))


@app.post("/api/apps/{slug}/stop")
def api_app_stop(slug: str) -> dict:
    return apps.stop_app(_app_or_404(slug))


@app.post("/api/apps/{slug}/restart")
def api_app_restart(slug: str) -> dict:
    return apps.restart_app(_app_or_404(slug))


@app.delete("/api/apps/{slug}")
def api_app_remove(slug: str) -> dict:
    """Un-manage an app: stop it, delete its plist, drop its apps.json entry. Its
    claimed port disappears with it (claims are derived from the config). The project
    directory and the app's log are never touched."""
    spec = _app_or_404(slug)
    stopped = apps.remove_app(spec)
    if not stopped["ok"]:
        return stopped
    dropped = apps.remove_entry(slug)
    if not dropped["ok"]:
        return dropped
    return {
        "ok": True,
        "stopped": stopped["stopped"],
        "removed": dropped["removed"],
        "detail": f"removed {slug}",
        "log_path": str(spec.log_path),
    }


@app.get("/api/apps/discover")
def api_apps_discover() -> JSONResponse:
    _discovered.clear()
    _discovered.extend(discover.discover_apps())
    return JSONResponse(_discovered)


@app.post("/api/apps/adopt")
def api_apps_adopt(payload: dict = Body(...)) -> dict:
    slugs = payload.get("slugs")
    if not isinstance(slugs, list) or not all(isinstance(s, str) for s in slugs):
        raise HTTPException(status_code=400, detail="body must be {\"slugs\": [\"...\"]}")
    if not _discovered:
        raise HTTPException(status_code=409, detail="run a scan first (GET /api/apps/discover)")
    return discover.adopt_apps(_discovered, slugs)


@app.get("/api/apps/{slug}/log")
def api_app_log(slug: str, lines: int = 200) -> dict:
    spec = _app_or_404(slug)
    return launchd.read_log_tail({"StandardOutPath": str(spec.log_path)}, lines=lines)


@app.get("/api/ports")
def api_ports(all: bool = False) -> JSONResponse:
    # vendor agents included on purpose: a vendor job holding a port is still
    # the answer to "who has :XXXX?"
    agents = launchd.list_agents(include_vendor=True)
    agent_pids = {a["pid"]: a["label"] for a in agents if a["pid"]}
    # Dashboard-launched apps are filtered out of list_agents (they live in the Apps
    # section), so add their pids here or their ports would lose attribution.
    specs = apps.load_apps()
    for spec in specs:
        info = apps.describe(spec)
        if info["pid"]:
            agent_pids[info["pid"]] = spec.label
    scanned = ports.list_ports(agent_pids)
    # A port held by a hidden *system* listener is still taken, so the claimed set is
    # derived from the whole scan — never from the display-filtered rows.
    live = {e["port"] for e in scanned}
    entries = scanned if all else [e for e in scanned if not e["system"]]
    # Claimed rows: ports a configured app declares but nothing is serving. Without
    # them a project's port disappears from this section the moment it stops, even
    # though the dashboard knows whose port it is. Live rows keep their exact shape
    # (no `kind`), so existing consumers are unaffected.
    claimed = [{**c, "kind": "claimed"} for c in apps.claimed_ports(specs, live)]
    return JSONResponse(entries + claimed)


@app.post("/api/ports/{pid}/kill")
def api_kill_port(pid: int) -> dict:
    return ports.kill_listener(pid)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>launchd dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0f1115; color: #e7e9ee;
         font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  .wrap { max-width: 880px; margin: 0 auto; padding: 24px 20px 64px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .muted { color: #8b909c; }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
  header .title { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 17px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .ok { background: #36c08f; } .run { background: #4a9be8; } .bad { background: #e2554f; }
  .off { background: #6b7280; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 22px; }
  .card { background: #171a21; border-radius: 10px; padding: 14px 16px; }
  .card .k { font-size: 12px; color: #8b909c; } .card .v { font-size: 24px; font-weight: 600; margin-top: 2px; }
  .list { background: #14171d; border: 0.5px solid #272b34; border-radius: 12px; overflow: hidden; }
  .row { display: flex; align-items: center; gap: 12px; padding: 13px 16px; border-top: 0.5px solid #272b34; }
  .row:first-child { border-top: none; }
  .row .meta { flex: 1; min-width: 0; }
  .row .lbl { font-size: 13px; font-weight: 600; }
  .row .sub { font-size: 12px; color: #8b909c; margin-top: 2px; }
  .pill { font-size: 11px; padding: 3px 9px; border-radius: 999px; white-space: nowrap; }
  .pill.ok { background: #15311f; color: #5fd2a0; } .pill.bad { background: #3a1714; color: #f08b86; }
  .pill.off { background: #23262e; color: #9aa0ac; } .pill.run { background: #122436; color: #74b3ee; }
  button { background: #1d212a; color: #d7dae1; border: 0.5px solid #333845; border-radius: 8px;
           padding: 6px 10px; font-size: 13px; cursor: pointer; }
  button:hover { background: #242935; } button:active { transform: scale(.97); }
  button.icon { width: 34px; padding: 6px 0; }
  .section { margin: 22px 0 8px; font-size: 12px; color: #8b909c; text-transform: uppercase; letter-spacing: .04em; }
  /* The panel is MOVED under whichever row was clicked (see openLogPanel), so it needs
     to sit inset when it lands between rows inside a .list. */
  .logwrap { margin: 8px 12px; background: #0c0e12; border: 0.5px solid #272b34; border-radius: 10px;
             display: none; }
  .logwrap.open { display: block; }
  .loghead { display: flex; justify-content: space-between; padding: 9px 14px; border-bottom: 0.5px solid #272b34;
             font-size: 12px; color: #8b909c; }
  pre.log { margin: 0; padding: 12px 14px; font-size: 12px; line-height: 1.7; color: #aeb4c0;
            max-height: 320px; overflow: auto; white-space: pre-wrap; }
  .toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: #1d212a;
           border: 0.5px solid #333845; border-radius: 8px; padding: 9px 14px; font-size: 13px; display: none; }
  .empty { padding: 28px; text-align: center; color: #8b909c; }
  a.vlink { color: #74b3ee; cursor: pointer; text-decoration: underline; }
</style></head>
<body><div class="wrap">
  <header>
    <div class="title"><span>⌁</span> launchd dashboard <span class="muted mono" style="font-size:12px;font-weight:400" id="domain"></span></div>
    <div style="display:flex;gap:8px">
      <label class="muted" style="display:flex;align-items:center;gap:6px;font-size:12px">
        <input type="checkbox" id="showVendor"/> show vendor</label>
      <button class="icon" id="refresh" title="Refresh">↻</button>
    </div>
  </header>
  <div class="cards" id="cards"></div>
  <div class="section" style="display:flex;align-items:center;justify-content:space-between">
    <span>Apps</span>
    <button id="scanBtn" onclick="scanApps()" style="text-transform:none;letter-spacing:0;font-size:12px">⌕ Scan for projects</button>
  </div>
  <div class="list" id="applist"></div>
  <div class="list" id="discover" style="display:none;margin-top:10px"></div>
  <div class="section">Agents</div>
  <div class="list" id="list"><div class="empty">Loading…</div></div>
  <div class="logwrap" id="logwrap">
    <div class="loghead"><span class="mono" id="logpath"></span>
      <span style="display:flex;gap:12px;align-items:center"><span id="lognote"></span>
        <label class="muted" style="display:flex;align-items:center;gap:5px;font-size:12px">
          <input type="checkbox" id="follow"/> follow</label>
      </span>
    </div>
    <pre class="log" id="log"></pre>
  </div>
  <div class="section" style="display:flex;align-items:center;justify-content:space-between">
    <span>Listening ports</span>
    <span style="display:flex;align-items:center;gap:10px;text-transform:none;letter-spacing:0">
      <span id="portverdict"></span>
      <input class="mono" id="portcheck" placeholder="port free?" inputmode="numeric"
             style="width:90px;background:#1d212a;border:0.5px solid #333845;border-radius:8px;
                    color:#d7dae1;padding:5px 9px;font-size:12px"/>
      <label class="muted" style="display:flex;align-items:center;gap:6px;font-size:12px">
        <input type="checkbox" id="showSystem"/> show system</label>
    </span>
  </div>
  <div class="list" id="portlist"><div class="empty">Loading…</div></div>
</div>
<div class="toast" id="toast"></div>
<script>
const $ = (id) => document.getElementById(id);
let openLog = null;

function rel(iso) {
  if (!iso) return "—";
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  const f = (n, u) => `${Math.round(n)}${u}`;
  if (s < 0) { const a = -s; if (a < 3600) return "in " + f(a/60, "m"); if (a < 86400) return "in " + f(a/3600, "h"); return "in " + f(a/86400, "d"); }
  if (s < 60) return "just now"; if (s < 3600) return f(s/60, "m") + " ago";
  if (s < 86400) return f(s/3600, "h") + " ago"; return f(s/86400, "d") + " ago";
}
function statusClass(s) { return s === "running" ? "run" : s === "unloaded" ? "off" : "ok"; }

async function load() {
  const all = $("showVendor").checked;
  const r = await fetch(`/api/agents?all=${all}`);
  const agents = await r.json();
  const healthy = agents.filter(a => a.healthy && a.status !== "unloaded").length;
  const failed = agents.filter(a => !a.healthy).length;
  const next = agents.map(a => a.next_run).filter(Boolean).sort()[0];
  $("cards").innerHTML = [
    ["Agents", agents.length], ["Healthy", healthy], ["Failed", failed],
    ["Next run", next ? new Date(next).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"}) : "—"],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  if (!agents.length) { $("list").innerHTML = `<div class="empty">No agents found.</div>`; return; }
  $("list").innerHTML = agents.map(a => {
    const dot = a.status === "running" ? "run" : a.status === "unloaded" ? "off" : (a.healthy ? "ok" : "bad");
    const exit = a.last_exit === null ? "" : ` · exit ${a.last_exit}`;
    const pill = a.status === "unloaded" ? `<span class="pill off">unloaded</span>`
      : a.healthy ? `<span class="pill ${a.status==='running'?'run':'ok'}">${a.status}</span>`
      : `<span class="pill bad">failed</span>`;
    const next = a.next_run ? ` · next ${rel(a.next_run)}` : "";
    const note = a.annotation?.purpose ? ` · <span style="color:#aeb4c0">${a.annotation.purpose}</span>` : "";
    const hover = a.annotation ? [a.annotation.note, a.annotation.repo].filter(Boolean).join(" — ") : "";
    return `<div class="row" data-log-key="${a.label}" ${hover ? `title="${hover.replace(/"/g, '&quot;')}"` : ""}>
      <span class="dot ${dot}"></span>
      <div class="meta">
        <div class="lbl mono">${a.label}${a.vendor ? ' <span class="muted" style="font-weight:400">· vendor</span>' : ''}</div>
        <div class="sub">${a.schedule}${exit} · ran ${rel(a.last_run)}${next}${note}</div>
      </div>
      ${pill}
      <button class="icon" title="Run now" onclick="act('${a.label}','run')">▶</button>
      <button class="icon" title="Stop" onclick="act('${a.label}','stop')">■</button>
      <button class="icon" title="Logs" onclick="showLog('${a.label}')">≣</button>
    </div>`;
  }).join("");
  reattachLog();
}

async function act(label, what) {
  const r = await fetch(`/api/agents/${encodeURIComponent(label)}/${what}`, { method: "POST" });
  const j = await r.json();
  toast(j.ok ? `${what}: ${label} ✓` : `${what} failed: ${j.detail || j.code}`);
  setTimeout(load, 600);
  if (openLog === label) setTimeout(() => showLog(label, true), 800);
}

let logURL = null;

// The panel gets MOVED under whichever row was clicked, which parks it inside a list
// whose innerHTML the 30s poll replaces — that DESTROYS child nodes, so a fresh
// getElementById would return null (and `row.after(null)` writes a literal "null" into
// the page). Holding the node in a variable keeps it alive while detached, and its
// children must be reached THROUGH it for the same reason.
const logPanel = $("logwrap");
const logPart = (id) => logPanel.querySelector("#" + id);

async function refreshLog(fallbackTitle) {
  if (!openLog || !logURL) return;
  const r = await fetch(logURL);
  const j = await r.json();
  logPart("logpath").textContent = j.path || fallbackTitle || "";
  logPart("lognote").textContent = j.note || "";
  const el = logPart("log");
  // Keep the view pinned to the bottom while following, unless the user scrolled up.
  const pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent = j.text || "(empty)";
  if (pinned) el.scrollTop = el.scrollHeight;
}

/** Park the panel directly under the row it belongs to. Returns false when that row
 * isn't on the page (filtered out, app removed). */
function placeLog() {
  if (!openLog) return false;
  const row = document.querySelector(`[data-log-key="${openLog}"]`);
  if (!row) return false;
  if (row.nextElementSibling !== logPanel) row.after(logPanel);
  return true;
}

// Called after every list render: re-insert the (now detached) panel under its row,
// or give up and close it when the row is gone.
function reattachLog() {
  if (!openLog) return;
  if (!placeLog()) {
    logPanel.classList.remove("open");
    openLog = null;
    logURL = null;
  }
}

async function openLogPanel(key, url, title) {
  if (openLog === key) { logPanel.classList.remove("open"); openLog = null; logURL = null; return; }
  openLog = key;
  logURL = url;
  await refreshLog(title);
  placeLog();
  logPanel.classList.add("open");
  // block:"nearest" is a no-op when the panel is already visible — the old jump came
  // from the panel living at the bottom of the page, not from this call.
  logPanel.scrollIntoView({ block: "nearest" });
}

// The follow ticker no-ops unless the panel is open and the box is checked.
setInterval(() => { if (logPart("follow").checked) refreshLog(); }, 2000);

async function showLog(label, keep) {
  if (keep && openLog === label) { await refreshLog(label); return; }
  await openLogPanel(label, `/api/agents/${encodeURIComponent(label)}/log?lines=300`, label);
}

let toastT;
function toast(msg) { const t = $("toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(toastT); toastT = setTimeout(() => t.style.display = "none", 2600); }

// ---- Apps (dev servers launched as transient launchd agents) ---------------
async function loadApps() {
  const r = await fetch("/api/apps");
  const apps = await r.json();
  if (!apps.length) {
    $("applist").innerHTML = `<div class="empty">No apps configured — scan for projects to add your dev servers, or edit apps.json (see apps.json.example).</div>`;
    return;
  }
  $("applist").innerHTML = apps.map(a => {
    const dot = a.blocked || a.missing ? "bad" : a.status === "running" ? "run" : a.status === "failed" ? "bad" : "off";
    const pill = a.missing ? `<span class="pill bad">path missing</span>`
      : a.blocked ? `<span class="pill bad">blocked</span>`
      : a.status === "running" ? `<span class="pill run">running</span>`
      : a.status === "failed" ? `<span class="pill bad">failed</span>`
      : `<span class="pill off">${a.status}</span>`;
    const port = a.port ? ` <span class="muted" style="font-weight:400">· :${a.port}</span>` : "";
    const note = a.annotation?.purpose ? ` · <span style="color:#aeb4c0">${a.annotation.purpose}</span>` : "";
    const drift = a.port_mismatch
      ? ` · <span style="color:#f0b86e">⚠ serving on :${a.open_port} — declared :${a.port} (another app may hold it)</span>`
      : "";
    const sharedNote = !a.port_mismatch && a.port_shared_with?.length
      ? ` · <span class="muted">port also declared by ${a.port_shared_with.join(", ")}</span>`
      : "";
    const sub = a.missing
      ? `<span style="color:#f08b86">${a.dir} no longer exists — re-scan to repair the path, or ✕ to remove</span>`
      : a.blocked
      ? `<span style="color:#f08b86">${a.dir} is TCC-protected — move it out of Documents/Desktop/Downloads to launch</span>`
      : `${a.command} · ${a.dir}${a.pid ? ` · pid ${a.pid}` : ""}${a.last_exit != null && a.status !== "running" ? ` · exit ${a.last_exit}` : ""}${note}${drift}${sharedNote}`;
    const open = a.status === "running" && a.open_port
      ? `<button onclick="window.open('http://127.0.0.1:${a.open_port}','_blank')" title="Open in browser">↗</button>` : "";
    // A missing dir can only fail to start — offer Logs + Remove and nothing else.
    const action = a.blocked || a.missing ? ""
      : a.status === "running"
        ? `<button class="icon" title="Restart" onclick="appAct('${a.slug}','restart')">↻</button>
           <button class="icon" title="Stop" onclick="appAct('${a.slug}','stop')">■</button>`
        : `<button class="icon" title="Start" onclick="appAct('${a.slug}','start')">▶</button>`;
    const login = a.login ? ' <span class="muted" style="font-weight:400;font-size:11px">· at login</span>' : "";
    const shownPort = a.port_mismatch ? a.open_port : a.port;
    const portTag = shownPort ? ` <span class="muted" style="font-weight:400">· :${shownPort}</span>` : "";
    return `<div class="row" data-log-key="app:${a.slug}">
      <span class="dot ${dot}"></span>
      <div class="meta">
        <div class="lbl mono">${a.name}${login}${portTag}</div>
        <div class="sub">${sub}</div>
      </div>
      ${pill}${open}${action}
      <button class="icon" title="Logs" onclick="showAppLog('${a.slug}')">≣</button>
      <button class="icon" id="rm-${a.slug}" title="Remove from the dashboard (stops it; your project files are untouched)" onclick="removeApp('${a.slug}')">✕</button>
    </div>`;
  }).join("");
  reattachLog();
}

let armedRemove = null; // slug armed for the two-tap confirm (destructive)

async function removeApp(slug) {
  if (armedRemove !== slug) {
    armedRemove = slug;
    const b = $(`rm-${slug}`);
    b.textContent = "sure?"; b.style.width = "auto"; b.style.padding = "6px 8px"; b.style.color = "#f08b86";
    setTimeout(() => { if (armedRemove === slug) { armedRemove = null; loadApps(); } }, 3000);
    return;
  }
  armedRemove = null;
  const r = await fetch(`/api/apps/${encodeURIComponent(slug)}`, { method: "DELETE" });
  const j = await r.json();
  toast(j.ok ? `removed ${slug}${j.stopped ? " (stopped it first)" : ""} — log kept at ${j.log_path}` : `remove failed: ${j.detail}`);
  loadApps();
  loadPorts();
}

async function scanApps() {
  if ($("discover").style.display !== "none") { $("discover").style.display = "none"; return; }
  $("scanBtn").textContent = "scanning…";
  const r = await fetch("/api/apps/discover");
  const cands = await r.json();
  $("scanBtn").textContent = "⌕ Scan for projects";
  if (!cands.length) {
    $("discover").innerHTML = `<div class="empty">No launchable projects found (looked for git repos with dev.sh/run.sh or npm dev/start scripts).</div>`;
    $("discover").style.display = "";
    return;
  }
  $("discover").innerHTML = cands.map(c => {
    const port = c.port ? ` <span class="muted" style="font-weight:400">· :${c.port}</span>` : "";
    const state = c.already ? `<span class="pill off">already added</span>`
      : !c.launchable ? `<span class="pill off">no launch found</span>`
      : c.conflict ? `<span class="pill off">slug in use</span>`
      : c.moved ? `<span class="pill run">moved</span>`
      : c.blocked ? `<span class="pill bad">blocked</span>`
      : `<span class="pill ok">ready</span>`;
    const sub = !c.launchable
      ? `<span class="muted">${c.dir} — ${c.reason}</span>`
      : c.conflict
      ? `<span class="muted">${c.dir} — ${c.reason}</span>`
      : c.moved
      ? `${c.command} · ${c.dir}<br><span style="color:#74b3ee">was ${c.previous_dir} — updates the existing app's path</span>`
      : c.blocked
      ? `<span style="color:#f08b86">${c.command} · ${c.dir} — launchd can't read this folder (TCC); move it to your home root to launch</span>`
      : `${c.command} · ${c.dir}`;
    const inert = c.already || !c.launchable || c.conflict;
    return `<div class="row" ${inert ? 'style="opacity:.55"' : ''}>
      <input type="checkbox" data-adopt="${c.slug}" ${inert ? "disabled" : ""} ${!inert && !c.blocked ? "checked" : ""}/>
      <div class="meta">
        <div class="lbl mono">${c.name}${port}</div>
        <div class="sub">${sub}</div>
      </div>
      ${state}
    </div>`;
  }).join("") + `<div class="row" style="justify-content:flex-end;background:#11141a">
      <span class="muted" style="flex:1;font-size:12px">commands are inferred server-side — hand-tune apps.json afterwards if a project needs env vars or a different port</span>
      <button onclick="adoptApps()">＋ Add selected</button>
    </div>`;
  $("discover").style.display = "";
}

async function adoptApps() {
  const slugs = [...document.querySelectorAll('#discover input[data-adopt]:checked')].map(el => el.dataset.adopt);
  if (!slugs.length) { toast("nothing selected"); return; }
  const r = await fetch("/api/apps/adopt", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ slugs }),
  });
  const j = await r.json();
  const warn = j.warnings?.length ? ` · ⚠ ${j.warnings.join(" · ")}` : "";
  const parts = [
    `added ${j.added?.length ?? 0}`,
    j.updated?.length ? `updated ${j.updated.length}` : "",
    j.skipped?.length ? `${j.skipped.length} skipped` : "",
  ].filter(Boolean);
  toast(j.ok ? `${parts.join(" · ")}${warn}` : `adopt failed: ${j.detail}`);
  $("discover").style.display = "none";
  loadApps();
}

async function appAct(slug, what) {
  const r = await fetch(`/api/apps/${encodeURIComponent(slug)}/${what}`, { method: "POST" });
  const j = await r.json();
  toast(j.ok ? `${what}: ${slug} ✓` : `${what} failed: ${j.detail}`);
  setTimeout(() => { loadApps(); loadPorts(); }, 900);
}

async function showAppLog(slug) {
  await openLogPanel("app:" + slug, `/api/apps/${encodeURIComponent(slug)}/log?lines=300`, slug);
}

// ---- Listening ports ------------------------------------------------------
let portData = [];   // always the FULL list — the free-checker must see hidden system ports too
let armedKill = null; // pid armed for two-tap confirm

async function loadPorts() {
  const r = await fetch(`/api/ports?all=true`);
  portData = await r.json();
  armedKill = null;
  const shown = $("showSystem").checked ? portData : portData.filter(p => p.kind === "claimed" || !p.system);
  if (!shown.length) { $("portlist").innerHTML = `<div class="empty">Nothing is listening.</div>`; checkPort(); return; }
  $("portlist").innerHTML = shown.map(p => {
    // Claimed: a configured app declares this port but nothing is serving it, so the
    // port stays visible (and startable) instead of vanishing when the app stops.
    if (p.kind === "claimed") {
      const who = p.claimed_by.join(", ");
      const start = p.claimed_by.length === 1
        ? `<button class="icon" title="Start ${who}" onclick="appAct('${p.claimed_by[0]}','start')">▶</button>` : "";
      return `<div class="row" style="opacity:.55">
        <span class="dot off"></span>
        <div class="meta">
          <div class="lbl mono">:${p.port}</div>
          <div class="sub">declared by ${who} — not currently served</div>
        </div>
        <span class="pill off">claimed</span>${start}
      </div>`;
    }
    const where = p.project || p.cwd || "";
    const agent = p.agent ? ` <span class="pill run mono">${p.agent}</span>` : "";
    const exposed = p.localhost ? "" : ` <span class="pill bad" title="bound beyond loopback — reachable from the LAN">exposed</span>`;
    const sys = p.system ? ` <span class="pill off">system</span>` : "";
    return `<div class="row">
      <span class="dot ${p.localhost ? "ok" : "bad"}"></span>
      <div class="meta">
        <div class="lbl mono">:${p.port} <span class="muted" style="font-weight:400">· ${p.command}</span></div>
        <div class="sub mono" title="${(p.args || "").replace(/"/g, "&quot;")}">${where || "—"} · pid ${p.pid} · ${p.addresses.join(", ")}</div>
      </div>${agent}${exposed}${sys}
      <button class="icon" id="kill-${p.pid}" title="SIGTERM this process" onclick="killPort(${p.pid})">✕</button>
    </div>`;
  }).join("");
  checkPort();
}

async function killPort(pid) {
  if (armedKill !== pid) {           // two-tap confirm: first tap arms
    armedKill = pid;
    const b = $(`kill-${pid}`);
    b.textContent = "sure?"; b.style.width = "auto"; b.style.padding = "6px 8px"; b.style.color = "#f08b86";
    setTimeout(() => { if (armedKill === pid) { armedKill = null; loadPorts(); } }, 3000);
    return;
  }
  armedKill = null;
  const r = await fetch(`/api/ports/${pid}/kill`, { method: "POST" });
  const j = await r.json();
  toast(j.ok ? `kill: pid ${pid} ✓` : `kill failed: ${j.detail}`);
  setTimeout(loadPorts, 800);
}

function checkPort() {
  const v = $("portcheck").value.trim();
  const el = $("portverdict");
  if (!/^\\d+$/.test(v)) { el.textContent = ""; return; }
  const port = Number(v);
  const hit = portData.find(p => p.port === port && p.kind !== "claimed");
  const claim = portData.find(p => p.port === port && p.kind === "claimed");
  if (hit) { el.innerHTML = `<span class="pill bad">taken · ${hit.command}</span>`; }
  else if (claim) { el.innerHTML = `<span class="pill off">free — declared by ${claim.claimed_by.join(", ")}</span>`; }
  else { el.innerHTML = `<span class="pill ok">free</span>`; }
}

function loadAll() { load(); loadApps(); loadPorts(); }
$("refresh").onclick = loadAll;
$("showVendor").onchange = load;
$("showSystem").onchange = loadPorts;
$("portcheck").oninput = checkPort;
loadAll();
setInterval(loadAll, 30000);
</script></body></html>
"""
