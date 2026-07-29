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

NOT YET RUN ON REAL HARDWARE — there is no Mac in this setup. The structure and
the constants are from the headers, but treat the first run on a real machine as
the actual test. selftest() exists for that.
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
    lib = ctypes.cdll.LoadLibrary(path)

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
    _ca = lib
    return lib


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


def get_volume() -> float:
    lib, dev = _lib(), default_output_device()
    a = _addr(_volume_property(dev))
    val = ctypes.c_float(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    st = lib.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None,
                                        ctypes.byref(size), ctypes.byref(val))
    if st != 0:
        raise CoreAudioError(f"reading volume failed (OSStatus {st})")
    return max(0.0, min(1.0, val.value))


def set_volume(scalar: float) -> float:
    lib, dev = _lib(), default_output_device()
    scalar = max(0.0, min(1.0, scalar))
    a = _addr(_volume_property(dev))
    val = ctypes.c_float(scalar)
    st = lib.AudioObjectSetPropertyData(dev, ctypes.byref(a), 0, None,
                                        ctypes.sizeof(val), ctypes.byref(val))
    if st != 0:
        raise CoreAudioError(f"setting volume failed (OSStatus {st})")
    return scalar


def set_mute(on: bool) -> bool:
    lib, dev = _lib(), default_output_device()
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


def device_name(dev: int | None = None) -> str:
    """kAudioObjectPropertyName is a CFStringRef; pulling it out means CoreFoundation
    calls, so this stays best-effort and never breaks status."""
    try:
        dev = dev if dev is not None else default_output_device()
        return f"coreaudio device {dev}"
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
        report["default_output_device"] = dev
        report["volume_property"] = {
            kVirtualMainVolume: "VirtualMainVolume",
            kVolumeScalar: "VolumeScalar"}.get(_volume_property(dev))
        report["volume"] = round(get_volume() * 100)
    except Exception as e:
        report["volume_error"] = f"{e.__class__.__name__}: {e}"
    ok, why = preflight()
    report["can_post_media_keys"] = ok
    report["media_key_note"] = why
    report["screens"] = screens()
    return report
