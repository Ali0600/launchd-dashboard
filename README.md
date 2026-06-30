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
- **Self-hostable**: ships a launchd plist template so the dashboard runs as *its own*
  agent and appears in its own list.

## Quickstart
```bash
cd ~/Documents/launchd-dashboard
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

## API
| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/agents?all=false` | list agents (set `all=true` to include vendor jobs) |
| GET | `/api/agents/{label}/log?lines=200` | tail an agent's stdout/stderr log |
| POST | `/api/agents/{label}/run` | run now (`launchctl kickstart -k`) |
| POST | `/api/agents/{label}/stop` | stop (`launchctl kill TERM`) |
| POST | `/api/agents/{label}/{enable,disable}` | toggle |

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

The pure parsers (`humanize_schedule`, `next_run`, `parse_launchctl_list`) are unit-tested
against fixtures, so the logic is verified without a live machine.

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
