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
