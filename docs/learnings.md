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
