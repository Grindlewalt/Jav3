"""macOS backend: CoreAudio for volume, media keys for transport.

Deliberately NOT osascript. AppleScript can `do shell script "..."`, so routing
volume through `osascript -e` would put a scripting interpreter — and with it a
shell — back on the path this client exists to keep closed. Everything here is
either a direct C call into CoreAudio through ctypes (no subprocess at all) or a
synthesized media key through Quartz.

Constants are the four-char codes from the macOS SDK headers, verified against
CoreAudio.framework/Headers/AudioHardware.h and AudioHardwareBase.h:

    kAudioObjectSystemObject                  1
    kAudioHardwarePropertyDefaultOutputDevice 'dOut'
    kAudioDevicePropertyVolumeScalar          'volm'
    kAudioDevicePropertyMute                  'mute'
    kAudioObjectPropertyScopeGlobal           'glob'
    kAudioObjectPropertyScopeOutput           'outp'
    kAudioObjectPropertyElementMaster         0
    kAudioHardwareServiceDeviceProperty_VirtualMainVolume  'vmvc'

'vmvc' (the virtual main volume) is tried first: plenty of devices have no
master channel, and it is the property Apple added to paper over exactly that.
'volm' on the master element is the fallback.

WHAT HAS ACTUALLY RUN ON A MAC: volume and the media keys have, and the mute bug
below was found there rather than reasoned about. The device enumeration and the
default-output switch are new and have NOT been run on real hardware — the
constants and ownership rules are from the headers, so treat the first run as the
test. `--selftest` prints all of it, which is the cheap way to find out.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import struct

# --- four-char codes ---------------------------------------------------------


def fourcc(code: str) -> int:
    """'dOut' -> 0x644F7574. FourCharCode is the ASCII packed big-endian."""
    return struct.unpack(">I", code.encode("ascii"))[0]


kAudioObjectSystemObject = 1
kDefaultOutputDevice = fourcc("dOut")
kVolumeScalar = fourcc("volm")
kVirtualMainVolume = fourcc("vmvc")
kMute = fourcc("mute")
kScopeGlobal = fourcc("glob")
kScopeOutput = fourcc("outp")
kElementMaster = 0

# Enumeration and identity, for choosing WHICH speaker rather than only how
# loud the current one is:
#   kAudioHardwarePropertyDevices   'dev#'  every audio device on the machine
#   kAudioDevicePropertyStreams     'stm#'  scoped output: is this a speaker?
#   kAudioObjectPropertyName        'lnam'  the name in Sound preferences
#   kAudioDevicePropertyDeviceUID   'uid '  stable id across reboots
#
# The UID is what identifies a device here, and deliberately so: it is also
# exactly what mpv calls the device ("coreaudio/<uid>"), so the mixer list and
# the playback list finally use one vocabulary. Before this the mixer list was a
# single hardcoded row called "default" and nothing could be chosen at all.
kDevices = fourcc("dev#")
kStreams = fourcc("stm#")
kObjectName = fourcc("lnam")
kDeviceUID = fourcc("uid ")


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


class CoreAudioError(RuntimeError):
    pass


_ca = None


def _lib():
    """Load CoreAudio once. ctypes only — no process is spawned."""
    global _ca
    if _ca is not None:
        return _ca
    path = (ctypes.util.find_library("CoreAudio")
            or "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    try:
        lib = ctypes.cdll.LoadLibrary(path)
    except OSError as e:
        # off-Darwin, or a Mac where the framework cannot be loaded. Raising the
        # module's own error keeps the failure inside the one exception type the
        # caller catches — a bare OSError escapes MacOS.volume() and reaches the
        # model as an unhandled traceback instead of a plain refusal.
        raise CoreAudioError(
            f"cannot load CoreAudio ({e}). Volume control needs macOS.") from e

    lib.AudioObjectGetPropertyData.restype = ctypes.c_int32
    lib.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]

    lib.AudioObjectSetPropertyData.restype = ctypes.c_int32
    lib.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_void_p]

    lib.AudioObjectHasProperty.restype = ctypes.c_bool
    lib.AudioObjectHasProperty.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress)]

    # needed to enumerate: the device list is variable-length, so its size has
    # to be asked for before the array can be allocated
    lib.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    lib.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(AudioObjectPropertyAddress),
        ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    _ca = lib
    return lib


_cf = None


def _cflib():
    """CoreFoundation, for turning a CFStringRef into a str.

    Device names and UIDs come back as CFStringRef — an opaque pointer, not a C
    string — so reading them means one more framework. Kept best-effort: a name
    that cannot be read degrades to the numeric id rather than breaking the
    enumeration, because an unnamed device is still a device you can be sent to.
    """
    global _cf
    if _cf is not None:
        return _cf
    path = (ctypes.util.find_library("CoreFoundation")
            or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    lib = ctypes.cdll.LoadLibrary(path)
    lib.CFStringGetCString.restype = ctypes.c_bool
    lib.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_long, ctypes.c_uint32]
    lib.CFRelease.argtypes = [ctypes.c_void_p]
    _cf = lib
    return lib


kCFStringEncodingUTF8 = 0x08000100


def _cfstring(ref) -> str:
    """Read a CFStringRef and release it.

    The release matters: CoreAudio hands out these strings +1 retained ("the
    caller is responsible for releasing"), and this is called every time the
    device list is enumerated — on a 30-second cache, forever. Leaking one
    string per device per probe is small and permanent, which is the worst
    shape a leak comes in.
    """
    if not ref:
        return ""
    try:
        cf = _cflib()
    except OSError:
        return ""
    buf = ctypes.create_string_buffer(1024)
    ok = cf.CFStringGetCString(ctypes.c_void_p(ref), buf, len(buf),
                               kCFStringEncodingUTF8)
    try:
        cf.CFRelease(ctypes.c_void_p(ref))
    except Exception:
        pass
    return buf.value.decode("utf-8", "replace") if ok else ""


def _addr(selector, scope=kScopeOutput, element=kElementMaster):
    return AudioObjectPropertyAddress(selector, scope, element)


def default_output_device() -> int:
    lib = _lib()
    a = _addr(kDefaultOutputDevice, kScopeGlobal)
    dev = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(dev))
    st = lib.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(a), 0, None,
        ctypes.byref(size), ctypes.byref(dev))
    if st != 0 or dev.value == 0:
        raise CoreAudioError(f"no default output device (OSStatus {st})")
    return dev.value


def _volume_property(dev: int):
    """Whichever volume property this device actually has."""
    lib = _lib()
    for sel in (kVirtualMainVolume, kVolumeScalar):
        a = _addr(sel)
        if lib.AudioObjectHasProperty(dev, ctypes.byref(a)):
            return sel
    raise CoreAudioError(
        "the default output device exposes no settable volume — some "
        "aggregate and HDMI devices are controlled downstream instead")


def get_volume(dev: int | None = None) -> float:
    lib, dev = _lib(), (dev if dev is not None else default_output_device())
    a = _addr(_volume_property(dev))
    val = ctypes.c_float(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    st = lib.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                        ctypes.byref(size), ctypes.byref(val))
    if st != 0:
        raise CoreAudioError(f"reading volume failed (OSStatus {st})")
    return max(0.0, min(1.0, val.value))


def set_volume(scalar: float, dev: int | None = None) -> float:
    lib, dev = _lib(), (dev if dev is not None else default_output_device())
    scalar = max(0.0, min(1.0, scalar))
    a = _addr(_volume_property(dev))
    val = ctypes.c_float(scalar)
    st = lib.AudioObjectSetPropertyData(dev, ctypes.byref(a), 0, None,
                                        ctypes.sizeof(val), ctypes.byref(val))
    if st != 0:
        raise CoreAudioError(f"setting volume failed (OSStatus {st})")
    return scalar


def set_mute(on: bool, dev: int | None = None) -> bool:
    lib, dev = _lib(), (dev if dev is not None else default_output_device())
    a = _addr(kMute)
    if not lib.AudioObjectHasProperty(dev, ctypes.byref(a)):
        # no hardware mute: 0% is the honest equivalent, and unmute has no
        # level to return to, so the caller is told rather than lied to
        raise CoreAudioError(
            "this output device has no mute control — set the volume instead")
    val = ctypes.c_uint32(1 if on else 0)
    st = lib.AudioObjectSetPropertyData(dev, ctypes.byref(a), 0, None,
                                        ctypes.sizeof(val), ctypes.byref(val))
    if st != 0:
        raise CoreAudioError(f"setting mute failed (OSStatus {st})")
    return on


def get_mute(dev: int | None = None) -> bool:
    """Is this output muted? False when the device has no mute control — which
    is not a lie: nothing is being silenced by a flag that does not exist."""
    lib, dev = _lib(), (dev if dev is not None else default_output_device())
    a = _addr(kMute)
    if not lib.AudioObjectHasProperty(dev, ctypes.byref(a)):
        return False
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    st = lib.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                        ctypes.byref(size), ctypes.byref(val))
    return st == 0 and bool(val.value)


def clear_mute(dev: int | None = None) -> bool:
    """Unmute, and say whether that changed anything. Never raises.

    This exists because of a specific, maddening failure: on a muted Mac the
    volume could be set to any level and the machine stayed silent. Mute is a
    separate CoreAudio property from the volume scalar, so writing a level over
    a muted device is a number changing behind a switch that is still off —
    Jarvis reported "set to 40%" and the operator heard nothing, with the slider
    in the menu bar agreeing with Jarvis.

    Silent about a device with no mute control, because there the volume IS the
    only control and there is nothing to report.
    """
    try:
        if not get_mute(dev):
            return False
        set_mute(False, dev)
        return True
    except CoreAudioError:
        return False


# --- which speaker ------------------------------------------------------------

def _device_string(dev: int, selector: int, scope: int = kScopeGlobal) -> str:
    lib = _lib()
    a = _addr(selector, scope)
    if not lib.AudioObjectHasProperty(dev, ctypes.byref(a)):
        return ""
    ref = ctypes.c_void_p(0)
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    st = lib.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                        ctypes.byref(size), ctypes.byref(ref))
    if st != 0:
        return ""
    return _cfstring(ref.value)


def device_label(dev: int) -> str:
    return _device_string(dev, kObjectName) or f"device {dev}"


def device_uid(dev: int) -> str:
    return _device_string(dev, kDeviceUID)


def _is_output(dev: int) -> bool:
    """A device with at least one output stream. Microphones, aggregate inputs
    and Bluetooth devices in their input role all appear in the same list, and
    offering the operator a microphone as a speaker is worse than offering
    nothing."""
    lib = _lib()
    a = _addr(kStreams, kScopeOutput)
    size = ctypes.c_uint32(0)
    st = lib.AudioObjectGetPropertyDataSize(dev, ctypes.byref(a), 0, None,
                                            ctypes.byref(size))
    return st == 0 and size.value > 0


def output_devices() -> list[dict]:
    """Every speaker this Mac can send sound to, as [{id, label, dev, default}].

    `id` is the CoreAudio UID, which is also what mpv calls a device
    ("coreaudio/<uid>"), so a name resolved here means the same thing to the
    player. Returns [] rather than raising: an empty list of speakers is a
    reportable state, an exception in the middle of `status` is not.
    """
    try:
        lib = _lib()
    except CoreAudioError:
        return []
    a = _addr(kDevices, kScopeGlobal)
    size = ctypes.c_uint32(0)
    st = lib.AudioObjectGetPropertyDataSize(
        kAudioObjectSystemObject, ctypes.byref(a), 0, None, ctypes.byref(size))
    if st != 0 or size.value == 0:
        return []
    count = size.value // ctypes.sizeof(ctypes.c_uint32)
    arr = (ctypes.c_uint32 * count)()
    st = lib.AudioObjectGetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(a), 0, None,
        ctypes.byref(size), arr)
    if st != 0:
        return []
    try:
        current = default_output_device()
    except CoreAudioError:
        current = 0
    out = []
    for dev in arr:
        try:
            if not _is_output(dev):
                continue
            uid = device_uid(dev)
            out.append({"id": uid or f"device-{dev}", "label": device_label(dev),
                        "dev": int(dev), "default": dev == current})
        except Exception:
            continue
    return out


def device_by_id(wanted: str) -> int:
    """A UID (or the label, or the fallback device-N form) -> AudioObjectID."""
    devs = output_devices()
    for d in devs:
        if d["id"] == wanted:
            return d["dev"]
    w = (wanted or "").strip().lower()
    for d in devs:
        if d["label"].lower() == w or d["id"].lower() == w:
            return d["dev"]
    raise CoreAudioError(
        f"no output on this Mac matches {wanted!r}. Available: "
        + (", ".join(d["label"] for d in devs) or "none"))


def set_default_output(dev: int) -> str:
    """Move ALL of the system's sound to this device.

    The system default, not a per-app route: this is the menu-bar speaker
    picker, so it moves Spotify and Safari too, which is what "put it on the
    living room speakers" means. Per-application routing is not something
    CoreAudio offers without a virtual driver.
    """
    lib = _lib()
    a = _addr(kDefaultOutputDevice, kScopeGlobal)
    val = ctypes.c_uint32(dev)
    st = lib.AudioObjectSetPropertyData(
        kAudioObjectSystemObject, ctypes.byref(a), 0, None,
        ctypes.sizeof(val), ctypes.byref(val))
    if st != 0:
        raise CoreAudioError(
            f"could not switch the output device (OSStatus {st}) — some "
            f"aggregate and virtual devices refuse to become the default")
    return device_label(dev)


def device_name(dev: int | None = None) -> str:
    """The default output's name, best-effort — never breaks status."""
    try:
        return device_label(dev if dev is not None else default_output_device())
    except Exception:
        return "default output"


# --- transport: synthesized media keys ---------------------------------------
#
# NSSystemDefined events carrying NX_KEYTYPE_* are what the keyboard's own play
# and skip keys send, so this drives whatever has the system's attention rather
# than a named app. Requires Accessibility permission, and on Sequoia (15) that
# has to be re-granted after reboots — preflight() says so plainly instead of
# failing silently.

NSSystemDefined = 14
NX_KEYTYPE_SOUND_UP = 0
NX_KEYTYPE_SOUND_DOWN = 1
NX_KEYTYPE_MUTE = 7
NX_KEYTYPE_PLAY = 16
NX_KEYTYPE_NEXT = 17
NX_KEYTYPE_PREVIOUS = 18

MEDIA_KEYS = {"playpause": NX_KEYTYPE_PLAY, "play": NX_KEYTYPE_PLAY,
              "pause": NX_KEYTYPE_PLAY, "stop": NX_KEYTYPE_PLAY,
              "next": NX_KEYTYPE_NEXT, "previous": NX_KEYTYPE_PREVIOUS}


def _quartz():
    try:
        import Quartz
        from AppKit import NSEvent
        return Quartz, NSEvent
    except ImportError as e:
        raise CoreAudioError(
            "transport control needs PyObjC — pip install "
            "pyobjc-framework-Quartz pyobjc-framework-Cocoa") from e


def preflight() -> tuple[bool, str]:
    """Can we post events? Checked before trying, so the operator gets told to
    grant Accessibility rather than watching nothing happen."""
    try:
        Quartz, _ = _quartz()
    except CoreAudioError as e:
        return False, str(e)
    fn = getattr(Quartz, "CGPreflightPostEventAccess", None)
    if fn is None:
        return True, "cannot check (older macOS); assuming granted"
    if fn():
        return True, "granted"
    req = getattr(Quartz, "CGRequestPostEventAccess", None)
    if req:
        req()      # raises the system prompt once
    return False, ("Accessibility permission is not granted. System Settings > "
                   "Privacy & Security > Accessibility, and allow the terminal "
                   "or app running this client. On macOS 15 this has to be "
                   "re-granted after a reboot.")


def media_key(action: str) -> dict:
    key = MEDIA_KEYS.get(action)
    if key is None:
        raise CoreAudioError(f"{action} has no media key on macOS")
    ok, why = preflight()
    if not ok:
        raise CoreAudioError(why)
    Quartz, NSEvent = _quartz()
    for down in (True, False):
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NSSystemDefined, (0, 0), 0xA00 if down else 0xB00, 0, 0, None, 8,
            (key << 16) | ((0xA if down else 0xB) << 8), -1)
        Quartz.CGEventPost(0, ev.CGEvent())
    return {"ok": True, "action": action, "via": "media key"}


def screens() -> list[dict]:
    try:
        from AppKit import NSScreen
    except ImportError:
        return []
    out = []
    for i, s in enumerate(NSScreen.screens()):
        f = s.frame()
        out.append({"index": i, "id": f"screen{i}",
                    "geometry": f"{int(f.size.width)}x{int(f.size.height)}"})
    return out


def selftest() -> dict:
    """Run this on a real Mac first: it reports what works instead of guessing."""
    report = {}
    try:
        dev = default_output_device()
        report["default_output_device"] = f"{device_label(dev)} ({dev})"
        report["volume_property"] = {
            kVirtualMainVolume: "VirtualMainVolume",
            kVolumeScalar: "VolumeScalar"}.get(_volume_property(dev))
        report["volume"] = round(get_volume() * 100)
        report["muted"] = get_mute(dev)
    except Exception as e:
        report["volume_error"] = f"{e.__class__.__name__}: {e}"
    try:
        outs = output_devices()
        report["outputs"] = ", ".join(
            d["label"] + (" [default]" if d["default"] else "") for d in outs) or "none"
    except Exception as e:
        report["outputs_error"] = f"{e.__class__.__name__}: {e}"
    ok, why = preflight()
    report["can_post_media_keys"] = ok
    report["media_key_note"] = why
    report["screens"] = screens()
    return report
