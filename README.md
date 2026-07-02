# launchd dashboard

A small, self-hosted web UI to **inventory and control the `launchd` agents on your
Mac** — see every scheduled job in one place, when it runs next, whether its last run
passed, tail its logs, and run/stop/enable it with a click. Built for turning a laptop
into an always-on personal server without losing track of the jobs piling up in
`~/Library/LaunchAgents`.

No magic: every fact comes from `launchctl` and the plist files directly, so it's
deterministic and read-honest.

## Highlights
- **Auto-discovery** of user LaunchAgents (`~/Library/LaunchAgents`, `/Library/LaunchAgents`),
  with vendor jobs (Apple/Google/etc.) hidden by default.
- **Live status** per agent — running / idle / unloaded, PID, and **last exit code**
  (red when a job failed silently).
- **Human-readable schedule** ("Sun 10:00", "Daily 18:30", "Every 1h") + a computed
  **next run** for calendar jobs.
- **Log tail** straight from each job's `StandardOutPath`, and a **last-run** time from
  the log's mtime.
- **One-click control**: run-now (`kickstart`), stop (`kill`), enable/disable.
- **Port tracker**: every listening TCP port on the machine, attributed to its process,
  **the project directory it belongs to** (cwd, or mined from the command line), and the
  launchd agent it runs under — plus a "is port X free?" checker, an **exposed** flag for
  ports bound beyond loopback, and a two-tap SIGTERM for reclaiming a port. Apple system
  listeners (AirPlay etc.) are hidden by default but still count as "taken".
- **Self-hostable**: ships a launchd plist template so the dashboard runs as *its own*
  agent and appears in its own list.

## Quickstart

> **Clone it somewhere launchd can read.** macOS privacy protection (TCC) blocks
> background agents from `~/Documents`, `~/Desktop`, and `~/Downloads` — a launchd
> agent there dies with `PermissionError: [Errno 1] Operation not permitted` before
> your code even runs (your terminal works only because Terminal.app holds the
> folder grant). Clone to a home-root path like `~/launchd-dashboard` instead.

```bash
cd ~/launchd-dashboard
./run.sh                       # creates .venv on first run, serves on :8787
# open http://127.0.0.1:8787
```

Tests:
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest -q
```

## Run it as an always-on agent
```bash
./run.sh                       # once, to create the .venv
sed "s|/Users/CHANGE_ME|$HOME|g" com.launchddash.server.plist.example \
  > ~/Library/LaunchAgents/com.launchddash.server.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.launchddash.server.plist
```
Now `http://127.0.0.1:8787` is always up, and the dashboard lists itself.

Stop the `./run.sh` instance first if it's running — the agent can't bind :8787 while
it's held (the dashboard's own Listening-ports section will show you the holder).
The template assumes the repo is at `~/launchd-dashboard`; if it's elsewhere, keep it
out of TCC-protected folders (see Quickstart) and adjust the paths.

## API
| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/agents?all=false` | list agents (set `all=true` to include vendor jobs) |
| GET | `/api/agents/{label}/log?lines=200` | tail an agent's stdout/stderr log |
| POST | `/api/agents/{label}/run` | run now (`launchctl kickstart -k`) |
| POST | `/api/agents/{label}/stop` | stop (`launchctl kill TERM`) |
| POST | `/api/agents/{label}/{enable,disable}` | toggle |
| GET | `/api/ports?all=false` | listening TCP ports with process/project/agent attribution (`all=true` includes system listeners) |
| POST | `/api/ports/{pid}/kill` | SIGTERM a listener (refused unless the pid currently holds a listening port) |

## Security
The control endpoints mutate real jobs, so the server **binds to `127.0.0.1` only** —
it is not meant to be exposed beyond your machine. Managing system `LaunchDaemons`
(which need root) is intentionally out of scope for now; this manages your **user**
agents, no `sudo` required.

## How it works
- **Discovery / schedule**: Python `plistlib` parses each `*.plist`; `StartCalendarInterval`
  / `StartInterval` / `RunAtLoad` are turned into a label + a minute-resolution next-run.
- **State**: `launchctl list <label>` → PID + `LastExitStatus`; `launchctl print` semantics
  inform the running/idle/unloaded split.
- **Control**: modern `launchctl` subcommands in the `gui/<uid>` domain
  (`kickstart` / `kill` / `enable` / `disable`).
- **Ports**: `lsof -iTCP -sTCP:LISTEN` in machine-parsable `-F` field mode (no column
  guessing), enriched per pid with `lsof -d cwd` (working directory → project) and `ps`
  (full command line + parent pid). Agent attribution **walks the ppid chain** into the
  agents' pids, because the listener is usually a child of the agent's process
  (`run.sh` → `uvicorn`). No `sudo`: user processes only — which is exactly the
  dev-server population.

The pure parsers (`humanize_schedule`, `next_run`, `parse_launchctl_list`, and everything
in `app/ports.py`) are unit-tested against fixtures, so the logic is verified without a
live machine.

## Experience gained
- Designed and built a **self-hosted observability + control plane** for macOS scheduled
  jobs (FastAPI service + zero-dependency web dashboard), surfacing silent failures via
  last-exit-code monitoring and log tailing.
- Integrated directly with **`launchd`** internals — plist parsing, `launchctl` state
  inspection, and job control (`kickstart`/`kill`/`enable`) in the per-user GUI domain.
- Wrote a **deterministic, fixture-tested core** (schedule humanizing, calendar next-run
  computation, `launchctl` output parsing) separated from the web/subprocess layer for
  testability.
- Packaged the tool to **self-host as a launchd agent**, demonstrating service
  lifecycle management and localhost-only security scoping.
- Built a **network-port observability layer** (`lsof`/`ps` field-mode parsing, process →
  project attribution via working directory and command-line mining, parent-pid chain
  walking to link sockets to their managing service) with guarded process control and
  loopback-vs-LAN bind auditing.
