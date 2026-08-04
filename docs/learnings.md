# Learnings

## `lsof -F`: machine-parsable field output beats column scraping

`lsof` (and friends) offer a field mode (`-F pcn` → one `p<pid>` / `c<command>` /
`n<name>` item per line) designed for programs to parse, instead of the human table.

**Why it came up:** the port tracker parses `lsof -iTCP -sTCP:LISTEN`. The table format
breaks on process names with spaces — a live listener here was literally
`Code Helper (Plugin)`, which `awk`-style column splitting shreds into four fields.
Field mode has no columns to mis-split.

**Takeaway:** when a CLI tool has a "for programs" output mode (`-F`, `--porcelain`,
`--json`), parse that — never the human-readable table.

## Attribute a socket to a service by walking the parent-pid chain

The process holding a port is usually a *child* of the process a service manager knows
about (launchd spawns `run.sh`, which execs/spawns `uvicorn`; the listener's pid ≠ the
agent's pid).

**Why it came up:** linking listening ports to the launchd agent that owns them — a
direct pid comparison missed every agent that starts via a wrapper script. Walking
`pid → ppid → …` (from one `ps -axo pid=,ppid=`, with a cycle guard) until an ancestor
matches an agent pid attributes them correctly.

**Takeaway:** to map a resource (socket/file/child) back to a managed service, compare
against the service pid's whole *ancestry*, not just the pid itself.

## launchd agents get EPERM in TCC-protected folders — EPERM ≠ EACCES

macOS privacy protection (TCC) guards `~/Documents`, `~/Desktop`, and `~/Downloads` per
*app*. Terminal.app holds a grant the user approved once, so everything launched from a
shell inherits it — but a launchd agent runs under `launchd`, gets no grant and no prompt,
and any file read in those folders fails with `PermissionError: [Errno 1] Operation not
permitted`.

**Why it came up:** self-hosting this dashboard as `com.launchddash.server` failed with
exit 256 while `./run.sh` from the terminal worked perfectly — the repo lived in
`~/Documents`, so the agent's Python couldn't even read `.venv/pyvenv.cfg`. Moving the
repo to `~/launchd-dashboard` (home root, like `~/grocery-helper`, whose weekly agent
always worked) fixed it with no settings changes.

**Takeaway:** put anything a background agent must read outside TCC-protected folders —
and read the errno: `Operation not permitted` (EPERM) with correct Unix permission bits
means a sandbox/TCC layer, not `chmod`.

## What "detecting an intruder" means without root: diff the lsof scan, don't promise an IDS

User-level polling of `lsof` can genuinely detect three things: a **new process
listening** (backdoors, but also SSH/Screen Sharing being switched on), a listener
**flipping from loopback to LAN-exposed**, and **established inbound connections**
(classified loopback / private / public via stdlib `ipaddress`). It structurally cannot
see port scans (packet-level, needs root), connections shorter than the poll interval
(sampling blind spot), or outbound traffic.

**Why it came up:** the user asked to be alerted "if someone is listening on a port or
if someone is hacking". The always-on dashboard agent already scanned listeners every
30s — the missing piece was only a persisted baseline to diff against, plus alert rules.
Naming it "Network watch" (not intrusion detection) keeps the promise honest.

**Takeaway:** a monitoring feature is a *diff against a baseline* plus an honest
statement of the sampling blind spots — seed the baseline silently (a day-one alert
storm trains users to ignore the channel), alert on transitions only, and say plainly
what the instrument cannot see.

## macOS banners from a daemon: osascript works, but treat the text as hostile

`osascript -e 'display notification …'` fires real banners from a launchd agent
(spike-verified — and exit 0 alone is not proof; a human confirmed the banner rendered).
The notification text embeds process command names, which any local process chooses for
itself, so the AppleScript string must be escaped backslashes-first-then-quotes or a
crafted name breaks out of the literal.

**Why it came up:** the network watch posts banners naming the listener that triggered
them. The same data flows into the dashboard HTML, where it likewise needs entity
escaping and `data-`-attribute event delegation instead of inline `onclick='…${key}…'`.

**Takeaway:** anything a monitored process can name itself with is untrusted input to
every sink downstream of the monitor — escape per-context (AppleScript, HTML, JS), and
prove the escaping with an injection-shaped test.
