"""Pure-logic tests for port discovery — fixtures only, no live lsof/ps."""

from app import ports

HOME = "/Users/dev"

# Real-shaped `lsof -nP -iTCP -sTCP:LISTEN -Fpcn` output: an IPv6-only vite,
# a dual-stack uvicorn (same port twice), and an AirPlay system listener.
LSOF_LISTEN = """\
p15110
cnode
f25
n[::1]:5173
p22001
cPython
f7
n127.0.0.1:8787
f8
n[::1]:8787
p410
cControlCe
f12
n*:7000
"""

LSOF_CWD = """\
p15110
fcwd
n/
p22001
fcwd
n/Users/dev/Documents/launchd-dashboard
"""

PS_TABLE = """\
  410     1 /System/Library/CoreServices/ControlCenter.app/Contents/MacOS/ControlCenter
15096  1400 /bin/zsh
15110 15096 node
21990     1 /bin/bash
22001 21990 /Users/dev/Documents/launchd-dashboard/.venv/bin/python
"""

PS_ARGS = """\
15110 node /Users/dev/Documents/3D-globe/node_modules/.bin/vite --port 5173 --strictPort
22001 /Users/dev/Documents/launchd-dashboard/.venv/bin/python -m uvicorn app.main:app --port 8787
"""


def test_parse_lsof_listeners():
    rows = ports.parse_lsof_listeners(LSOF_LISTEN)
    assert {"pid": 15110, "command": "node", "host": "::1", "port": 5173} in rows
    assert {"pid": 410, "command": "ControlCe", "host": "*", "port": 7000} in rows
    assert len([r for r in rows if r["pid"] == 22001]) == 2  # v4 + v6


def test_merge_listeners_collapses_dual_stack():
    merged = ports.merge_listeners(ports.parse_lsof_listeners(LSOF_LISTEN))
    by_port = {e["port"]: e for e in merged}
    assert len(merged) == 3
    assert by_port[8787]["addresses"] == ["127.0.0.1", "::1"]
    assert by_port[5173]["addresses"] == ["::1"]


def test_parse_lsof_cwds():
    cwds = ports.parse_lsof_cwds(LSOF_CWD)
    assert cwds == {15110: "/", 22001: "/Users/dev/Documents/launchd-dashboard"}


def test_parse_ps_table_handles_spaced_comm():
    table = ports.parse_ps_table("  7518 83267 /Applications/Visual Studio Code.app/Contents/MacOS/Code Helper (Plugin)\n")
    assert table[7518]["ppid"] == 83267
    assert table[7518]["comm"].endswith("Code Helper (Plugin)")


def test_parse_ps_args():
    args = ports.parse_ps_args(PS_ARGS)
    assert args[15110].startswith("node /Users/dev/Documents/3D-globe")


def test_is_localhost():
    assert ports.is_localhost(["127.0.0.1", "::1"])
    assert not ports.is_localhost(["*"])
    assert not ports.is_localhost(["127.0.0.1", "*"])  # one wide bind taints it
    assert not ports.is_localhost(["192.168.1.5"])
    assert not ports.is_localhost([])


def test_is_system():
    assert ports.is_system("/System/Library/CoreServices/ControlCenter.app/Contents/MacOS/ControlCenter")
    assert ports.is_system("/usr/libexec/rapportd")
    assert not ports.is_system("node")
    assert not ports.is_system("/Applications/Visual Studio Code.app/Contents/MacOS/Electron")


def test_project_from_cwd():
    assert ports.project_of("/Users/dev/grocery-helper/backend", None, HOME) == "~/grocery-helper/backend"


def test_project_mined_from_args_trims_node_modules():
    # cwd is "/" (how lsof reports this vite), so the args are the only signal
    args = "node /Users/dev/Documents/3D-globe/node_modules/.bin/vite --port 5173"
    assert ports.project_of("/", args, HOME) == "~/Documents/3D-globe"


def test_project_mined_from_args_trims_script_file():
    args = "/Users/dev/proj/.venv/bin/python /Users/dev/proj/serve.py"
    assert ports.project_of(None, args, HOME) == "~/proj"


def test_project_none_when_no_signal():
    assert ports.project_of("/", "rapportd", HOME) is None
    assert ports.project_of(HOME, None, HOME) is None  # $HOME itself is not a project


def test_project_excludes_home_library():
    # app-support dirs are not projects, whether seen as cwd or in the args
    assert ports.project_of("/Users/dev/Library/Application Support/Code", None, HOME) is None
    assert ports.project_of("/", "electron /Users/dev/Library/Caches/x.js", HOME) is None


def test_agent_for_walks_ppid_chain():
    # uvicorn (22001) <- run.sh (21990) which is the agent's pid
    ppid = {22001: 21990, 21990: 1}
    agents = {21990: "com.launchddash.server"}
    assert ports.agent_for(22001, ppid, agents) == "com.launchddash.server"
    assert ports.agent_for(21990, ppid, agents) == "com.launchddash.server"
    assert ports.agent_for(15110, {15110: 15096, 15096: 1400}, agents) is None


def test_agent_for_survives_ppid_cycle():
    assert ports.agent_for(5, {5: 6, 6: 5}, {}) is None
