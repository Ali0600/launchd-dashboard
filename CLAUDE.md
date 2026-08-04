# launchd-dashboard — agent notes

Local FastAPI + zero-dependency web UI (127.0.0.1:8787) that inventories macOS `launchd`
agents, tracks listening ports, and launches dev apps as transient agents. See
[README.md](README.md) for the user-facing picture.

## Layout
- `app/launchd.py` — agent discovery, `launchctl` parsing, schedule humanize/next-run, control.
- `app/ports.py` — `lsof`/`ps` parsing, project + agent attribution.
- `app/apps.py` — the app launcher: `apps.json` parsing, plist generation, start/stop/restart/remove.
- `app/discover.py` — "Scan for projects": root scanning, launch inference, adoption.
- `app/annotations.py` — `labels.json` (purpose/repo/note per job).
- `app/netwatch.py` — network watch: connection parsing, remote classification, the
  observe() diff rules, ack allow-lists, osascript notifier, `netwatch.json` state.
- `app/main.py` — routes **plus the entire UI** as one `PAGE` string (HTML + inline JS),
  and the background watcher loop (lifespan task).
- `tests/` — pytest, fixtures only; no live `launchctl`/`lsof` in tests.

## Common commands
- Run: `./run.sh` (creates `.venv` on first run, serves :8787). It self-hosts as the agent
  `com.launchddash.server` — after merging, `launchctl kickstart -k gui/$(id -u)/com.launchddash.server`
  to pick up the new code.
- Tests: `.venv/bin/python -m pytest -q` · Lint: `.venv/bin/ruff check .`
- The UI is a Python string, so there's no JS runner: syntax-check it with
  `python -c "from app.main import PAGE"` → extract `<script>` → `node --check`.

## Workflow
- **Branch → PR → green → squash-merge.** `main` is protected by rulesets (required check
  "Lint + test"; history protection has no bypass).
- **Never pipe the gate**: `gh pr checks … | tail` swallows the exit code and merged a red PR
  once. Run it unpiped so `&&` sees the real status, and confirm `gh run list` after.
- Commit messages: no `Co-Authored-By` trailer. Backticks in `-m` get eaten by zsh — use
  `git commit -F <file>` and `gh pr create --body-file` for anything with code formatting.
- Docs ship with the change; machine-local config (`apps.json`, `labels.json`) is gitignored,
  with a committed `.example`.

## Important notes / gotchas
- **macOS TCC blocks launchd agents from `~/Documents`/`~/Desktop`/`~/Downloads`** — the agent
  gets `EPERM` ("Operation not permitted", *not* the permission-bits `EACCES`) with no prompt,
  while the same command works from Terminal (which holds the folder grant). This repo lives at
  the home root for that reason; `tcc_blocked()` marks such projects **blocked** rather than
  letting them die cryptically. Never place agent-run code back in those folders.
- **Generated plists must bake their environment.** launchd gives a minimal PATH and no shell
  profile, so `robust_path()` assembles homebrew + `~/.local/bin` + the newest fnm node. Same
  reason Python inference prefers `.venv/bin/python` over a bare interpreter name.
- **`stop_app` keeps the plist for `login: true` apps; `remove_app` must always delete it** —
  otherwise a removed app resurrects at next login pointing at a deleted directory. This
  asymmetry is deliberate and test-pinned.
- **`restart` must wait for the job to unload.** `bootout` returns while the process is still
  dying, and `start_app`'s already-running check would then skip the bootstrap, leaving nothing
  running (found live; unit tests all passed).
- **Deduplicate directories by `(st_dev, st_ino)`, never by `Path.resolve()`.** macOS volumes
  are case-insensitive, so `~/projects` and `~/Projects` are one directory that `resolve()`
  reports as two — every project got listed twice. Lowercasing would break case-sensitive volumes.
- **The Apps row must follow reality, not `apps.json`.** A dev server silently lands elsewhere
  when its port is taken (Next.js steps 3000→3001), so `/api/apps` joins live agent ports →
  `open_port` / `port_mismatch`. Declared ports also drive `claimed_ports()`, which is what makes
  a stopped project's port stay visible — and what frees it when the entry is removed.
- **`discover` and `adopt` must agree on "already configured".** They once keyed on directory vs
  slug respectively, so a *moved* project rendered adoptable and then refused. There is an
  invariant test: anything the UI offers with an enabled checkbox must be accepted by `adopt`.
- **Never drop a project silently.** `classify_project` always returns a candidate; when nothing
  can be inferred it carries `launchable: False` + a reason. A silent skip reads as a broken scanner.
- **Script inference stays narrowly `dev.sh`/`run.sh`.** Repos ship task scripts
  (`isolate.sh`, `render_job.sh`, `cleanup.sh`); launching one by accident is worse than not launching.
- **HTTP carries slugs only.** Commands come exclusively from the local config file — keep any
  new endpoint on that side of the trust boundary.
- **`innerHTML =` destroys child nodes.** The log panel is *moved* under the clicked row, so it
  lives inside a list the 30s poll re-renders: it's held in `const logPanel` and its children are
  reached through it (`getElementById` can't see a detached subtree either).
- **Link to `localhost:<port>`, never the IPv4 literal.** `localhost` resolves to `::1` *or*
  `127.0.0.1`, so it reaches the server whichever family it bound; a Vite/Next default bind is
  IPv6-only on macOS, where `http://127.0.0.1:PORT` is refused outright (measured). It's also the
  host dev servers' Host-header allowlists expect — an IP literal trips their cross-origin warning.
- **"exposed" = bound to `*` (`0.0.0.0`/`::`), i.e. reachable from the LAN**, not just this Mac —
  `is_localhost()` requires *every* bound address to be loopback. Note `next dev` binds all
  interfaces unless `-H 127.0.0.1` is passed, so adopted Next apps are exposed by default; Expo's
  `:8081` is exposed on purpose (the phone must reach Metro). Verify a claim like this by curling
  the machine's own LAN IP, not by reading the flag.
- **`osascript display notification` DOES fire from a launchd agent** (spike-verified
  2026-08-04, user confirmed the banner on screen — exit 0 alone proves nothing). The
  script string must be escaped backslashes-FIRST-then-quotes (`_osa_str`): a listener's
  command name is attacker-influenced text and must never break out of the AppleScript
  string. Same reason the watch UI escapes every interpolation and rides ack targets in
  `data-` attributes with one delegated listener — never inline `onclick='…${key}…'`.
- **The network watch seeds SILENTLY on first run** — the value is the diff, and a
  day-one storm of 20 banners about your existing setup would train the user to ignore
  the channel. Corollary: deleting `netwatch.json` re-seeds (no alerts until something
  *changes* again); a corrupt file is parked as `.bak`, never overwritten blind.
- **Exposure outranks attribution in the watch rules.** An agent- or project-attributed
  listener is log-only on loopback but BANNERS when bound `*` — your own `next dev`
  without `-H` is exactly the case to catch. Per-key acks likewise don't survive a
  loopback→exposed flip (a new fact); only `acked_commands` silences a command entirely
  (Chrome binds `*` on a fresh random port per session, so per-key ack can't quiet it).
- **`/api/ports` and the watcher share ONE scan path** (`_scan_ports()` in main.py).
  Don't add a second listener-scan/attribution assembly — two paths answering "who
  holds this port?" will drift, which is the moved-project bug all over again.
- **Dependency floors must stay Python-3.9-installable** (`run.sh` uses the system `python3`).
  `fastapi>=0.129` / `uvicorn>=0.40` / `pytest>=9` need ≥3.10 and are ignored in `dependabot.yml`;
  CI runs 3.12 and cannot catch this — dry-run any floor bump on the 3.9 venv. PRs also run the
  user's own [preflight](https://github.com/Ali0600/preflight) action with `python-version: 3.9`.

## Testing conventions
- Fixtures only — no live `launchctl`/`lsof`; neutral paths (`/Users/dev`), never real ones.
- The UI's invisible constraints are pinned as **text assertions over the `PAGE` string** in
  `tests/test_page.py` (no JS runner exists here).
- **Prove new tests fail-first.** Sabotage, watch it go red, then restore **from a file copy and
  compare checksums** — never `git checkout` on a file with uncommitted work.
- Check the **test count**, not just "passed": an edit once silently merged two tests and the
  suite still read green.

## Verifying against the live dashboard
The agent runs the merged code, so verify through it (`curl` the API, or the browser pane) rather
than trusting unit tests alone — every bug in this repo's history was found that way.
- Re-check `window.innerHeight` before believing any browser geometry: the harness viewport
  degenerates to 0 after a navigate, which makes `scrollIntoView` scroll unconditionally.
- Measure what the user perceives: the clicked row's `getBoundingClientRect().top`, not
  `window.scrollY` — removing content above the fold changes scroll for the same visual position.
- Use a throwaway app entry for destructive checks; never test removal on the user's real apps.
