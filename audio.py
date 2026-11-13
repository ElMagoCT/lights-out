"""Windows audio: list playback/recording devices, read and set the default,
read and set volume/mute, and auto-switch the default output between a pair of
devices (headphones vs speakers) as one is plugged in or removed.

Core Audio (MMDevice / IAudioEndpointVolume) is used through hand-defined COM
interfaces so nothing but comtypes is needed and PyInstaller bundles it without
a generated type-library cache. Switching the default endpoint is the one
undocumented bit: IPolicyConfig::SetDefaultEndpoint, the same call the Sound
control panel and tools like NirCmd use. All COM lives on one worker thread
(AudioHub); the UI only reads a cached snapshot and posts actions to it.
"""
import ctypes
import logging
import queue
import re
import threading
import time
from ctypes import HRESULT, POINTER, Structure, Union, byref, c_float, c_void_p, c_wchar_p
from ctypes.wintypes import BOOL, DWORD, LPCWSTR, UINT, WORD

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown

log = logging.getLogger("lightsout.audio")

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
CLSID_PolicyConfigClient = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
CLSCTX_ALL = 23

# EDataFlow / ERole / device-state / storage-access constants
RENDER, CAPTURE, ALL_FLOW = 0, 1, 2
ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMS = 0, 1, 2
STATE_ACTIVE, STATE_DISABLED, STATE_NOTPRESENT, STATE_UNPLUGGED = 1, 2, 4, 8
STATE_MASK_ALL = 0xF
STGM_READ = 0

# Form factors we care about for guessing which device is "headphones"
FF_SPEAKERS, FF_HEADPHONES, FF_HEADSET = 1, 3, 5
FORM_FACTOR = {0: "network", 1: "speakers", 2: "line", 3: "headphones", 4: "mic",
               5: "headset", 6: "handset", 7: "passthrough", 8: "spdif", 9: "hdmi", 10: ""}
STATE_NAME = {STATE_ACTIVE: "active", STATE_DISABLED: "disabled",
              STATE_NOTPRESENT: "not present", STATE_UNPLUGGED: "unplugged"}

# A mic named in "unmute_on_connect" that turns up muted is unmuted for this long after
# it connects (or the app starts); a mute after that is the user's own and stays.
UNMUTE_GRACE_S = 20


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
PKEY_AudioEndpoint_FormFactor = PROPERTYKEY(GUID("{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E}"), 0)


class _PVUnion(Union):
    _fields_ = [("pwszVal", c_wchar_p), ("ulVal", ctypes.c_ulong), ("llVal", ctypes.c_longlong)]


class PROPVARIANT(Structure):
    # 24 bytes on x64: VARTYPE + 3 reserved WORDs, the union, and tail padding.
    _fields_ = [("vt", WORD), ("r1", WORD), ("r2", WORD), ("r3", WORD),
                ("u", _PVUnion), ("tail", c_void_p)]


VT_LPWSTR, VT_UI4 = 31, 19


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (['in'], POINTER(DWORD), "c")),
        COMMETHOD([], HRESULT, "GetAt", (['in'], DWORD, "i"), (['in'], POINTER(PROPERTYKEY), "k")),
        COMMETHOD([], HRESULT, "GetValue", (['in'], POINTER(PROPERTYKEY), "k"), (['in'], POINTER(PROPVARIANT), "v")),
        COMMETHOD([], HRESULT, "SetValue", (['in'], POINTER(PROPERTYKEY), "k"), (['in'], POINTER(PROPVARIANT), "v")),
        COMMETHOD([], HRESULT, "Commit"),
    ]


class IAudioEndpointVolume(IUnknown):
    _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
    _methods_ = [
        COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (['in'], c_void_p, "p")),
        COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (['in'], c_void_p, "p")),
        COMMETHOD([], HRESULT, "GetChannelCount", (['in'], POINTER(UINT), "p")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (['in'], c_float, "l"), (['in'], POINTER(GUID), "c")),
        COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (['in'], c_float, "l"), (['in'], POINTER(GUID), "c")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (['in'], POINTER(c_float), "p")),
        COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (['in'], POINTER(c_float), "p")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (['in'], UINT, "n"), (['in'], c_float, "l"), (['in'], POINTER(GUID), "c")),
        COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (['in'], UINT, "n"), (['in'], c_float, "l"), (['in'], POINTER(GUID), "c")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (['in'], UINT, "n"), (['in'], POINTER(c_float), "p")),
        COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (['in'], UINT, "n"), (['in'], POINTER(c_float), "p")),
        COMMETHOD([], HRESULT, "SetMute", (['in'], BOOL, "m"), (['in'], POINTER(GUID), "c")),
        COMMETHOD([], HRESULT, "GetMute", (['in'], POINTER(BOOL), "p")),
        COMMETHOD([], HRESULT, "GetVolumeStepInfo", (['in'], POINTER(UINT), "a"), (['in'], POINTER(UINT), "b")),
        COMMETHOD([], HRESULT, "VolumeStepUp", (['in'], POINTER(GUID), "c")),
        COMMETHOD([], HRESULT, "VolumeStepDown", (['in'], POINTER(GUID), "c")),
        COMMETHOD([], HRESULT, "QueryHardwareSupport", (['in'], POINTER(DWORD), "p")),
        COMMETHOD([], HRESULT, "GetVolumeRange", (['in'], POINTER(c_float), "a"), (['in'], POINTER(c_float), "b"), (['in'], POINTER(c_float), "c")),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate",
                  (['in'], POINTER(GUID), "iid"), (['in'], DWORD, "ctx"),
                  (['in'], POINTER(PROPVARIANT), "params"),
                  (['out'], POINTER(POINTER(IAudioEndpointVolume)), "pp")),
        COMMETHOD([], HRESULT, "OpenPropertyStore", (['in'], DWORD, "access"),
                  (['out'], POINTER(POINTER(IPropertyStore)), "pp")),
        COMMETHOD([], HRESULT, "GetId", (['in'], POINTER(c_wchar_p), "pp")),
        COMMETHOD([], HRESULT, "GetState", (['in'], POINTER(DWORD), "p")),
    ]


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (['in'], POINTER(UINT), "p")),
        COMMETHOD([], HRESULT, "Item", (['in'], UINT, "i"), (['out'], POINTER(POINTER(IMMDevice)), "pp")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (['in'], DWORD, "flow"), (['in'], DWORD, "mask"),
                  (['out'], POINTER(POINTER(IMMDeviceCollection)), "pp")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (['in'], DWORD, "flow"), (['in'], DWORD, "role"),
                  (['out'], POINTER(POINTER(IMMDevice)), "pp")),
        COMMETHOD([], HRESULT, "GetDevice", (['in'], LPCWSTR, "id"),
                  (['out'], POINTER(POINTER(IMMDevice)), "pp")),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (['in'], c_void_p, "p")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (['in'], c_void_p, "p")),
    ]


class IPolicyConfig(IUnknown):
    _iid_ = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetMixFormat", (['in'], LPCWSTR, "a"), (['in'], c_void_p, "b")),
        COMMETHOD([], HRESULT, "GetDeviceFormat", (['in'], LPCWSTR, "a"), (['in'], ctypes.c_int, "b"), (['in'], c_void_p, "c")),
        COMMETHOD([], HRESULT, "ResetDeviceFormat", (['in'], LPCWSTR, "a")),
        COMMETHOD([], HRESULT, "SetDeviceFormat", (['in'], LPCWSTR, "a"), (['in'], c_void_p, "b"), (['in'], c_void_p, "c")),
        COMMETHOD([], HRESULT, "GetProcessingPeriod", (['in'], LPCWSTR, "a"), (['in'], ctypes.c_int, "b"), (['in'], c_void_p, "c"), (['in'], c_void_p, "d")),
        COMMETHOD([], HRESULT, "SetProcessingPeriod", (['in'], LPCWSTR, "a"), (['in'], c_void_p, "b")),
        COMMETHOD([], HRESULT, "GetShareMode", (['in'], LPCWSTR, "a"), (['in'], c_void_p, "b")),
        COMMETHOD([], HRESULT, "SetShareMode", (['in'], LPCWSTR, "a"), (['in'], c_void_p, "b")),
        COMMETHOD([], HRESULT, "GetPropertyValue", (['in'], LPCWSTR, "a"), (['in'], ctypes.c_int, "b"), (['in'], POINTER(PROPERTYKEY), "c"), (['in'], c_void_p, "d")),
        COMMETHOD([], HRESULT, "SetPropertyValue", (['in'], LPCWSTR, "a"), (['in'], ctypes.c_int, "b"), (['in'], POINTER(PROPERTYKEY), "c"), (['in'], c_void_p, "d")),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint", (['in'], LPCWSTR, "deviceId"), (['in'], DWORD, "role")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility", (['in'], LPCWSTR, "a"), (['in'], BOOL, "b")),
    ]


ole32 = ctypes.windll.ole32


def _co_init():
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass


def _prop_str(store, key):
    pv = PROPVARIANT()
    try:
        store.GetValue(byref(key), byref(pv))
        return pv.u.pwszVal if pv.vt == VT_LPWSTR and pv.u.pwszVal else ""
    except Exception:
        return ""
    finally:
        ole32.PropVariantClear(byref(pv))


def _prop_uint(store, key):
    pv = PROPVARIANT()
    try:
        store.GetValue(byref(key), byref(pv))
        return int(pv.u.ulVal) if pv.vt == VT_UI4 else None
    except Exception:
        return None
    finally:
        ole32.PropVariantClear(byref(pv))


def _dev_id(dev):
    p = c_wchar_p()
    dev.GetId(byref(p))
    s = p.value
    if p:
        ole32.CoTaskMemFree(ctypes.cast(p, c_void_p))
    return s


def _enumerator():
    return comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)


def _default_id(enum, flow, role=ROLE_MULTIMEDIA):
    try:
        dev = enum.GetDefaultAudioEndpoint(flow, role)
        return _dev_id(dev)
    except Exception:
        return ""                                   # no default device for this flow


def _endpoint_volume(enum, device_id):
    dev = enum.GetDevice(device_id)
    return dev.Activate(byref(IAudioEndpointVolume._iid_), CLSCTX_ALL, None)


def list_devices(flow):
    """[{id, name, state, state_name, form, formfactor, default}] for RENDER or CAPTURE."""
    enum = _enumerator()
    default = _default_id(enum, flow)
    coll = enum.EnumAudioEndpoints(flow, STATE_MASK_ALL)
    n = UINT()
    coll.GetCount(byref(n))
    out = []
    for i in range(n.value):
        dev = coll.Item(i)
        state = DWORD()
        dev.GetState(byref(state))
        store = dev.OpenPropertyStore(STGM_READ)
        name = _prop_str(store, PKEY_Device_FriendlyName)
        ff = _prop_uint(store, PKEY_AudioEndpoint_FormFactor)
        did = _dev_id(dev)
        out.append({"id": did, "name": name, "state": state.value,
                    "state_name": STATE_NAME.get(state.value, str(state.value)),
                    "formfactor": ff, "form": FORM_FACTOR.get(ff, ""),
                    "default": did == default})
    out.sort(key=lambda d: (d["state"] != STATE_ACTIVE, d["name"].lower()))
    return out


def set_default(device_id, roles=(ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMS)):
    pc = comtypes.CoCreateInstance(CLSID_PolicyConfigClient, IPolicyConfig, comtypes.CLSCTX_ALL)
    for role in roles:
        pc.SetDefaultEndpoint(device_id, role)
    return True


def get_volume(device_id):
    """(volume 0..100, muted bool) for a device, or (None, None)."""
    try:
        vol = _endpoint_volume(_enumerator(), device_id)
        f = c_float()
        vol.GetMasterVolumeLevelScalar(byref(f))
        m = BOOL()
        vol.GetMute(byref(m))
        return round(f.value * 100), bool(m.value)
    except Exception:
        return None, None


def set_volume(device_id, percent):
    vol = _endpoint_volume(_enumerator(), device_id)
    vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, percent / 100.0)), None)
    return True


def set_mute(device_id, muted):
    vol = _endpoint_volume(_enumerator(), device_id)
    vol.SetMute(1 if muted else 0, None)
    return True


def default_id(flow=RENDER, role=ROLE_MULTIMEDIA):
    return _default_id(_enumerator(), flow, role)


class AudioHub(threading.Thread):
    """Owns every COM call on one thread. Keeps a cached snapshot of devices and
    volume, applies actions posted from the UI at once, and polls to auto-switch
    the default output between the configured headphones and speakers as one
    becomes available or goes away."""

    def __init__(self, cfg, on_change=None):
        super().__init__(name="audio", daemon=True)
        self.cfg = cfg                               # the "audio" dict (live)
        self.on_change = on_change                   # called (no args) after each refresh; marshal to UI yourself
        self.q = queue.Queue()
        self.snapshot = {"outputs": [], "inputs": [], "out_default": "", "in_default": "",
                         "volume": None, "muted": None, "ts": 0.0}
        self._halt = threading.Event()
        self._last_hp = None
        self._auto_note = ""
        self._present = None                         # active input ids at the last refresh (None: none yet)
        self._arrived = {}                           # input id -> when it connected

    # -- UI-facing (thread-safe) ----------------------------------------------------
    def devices(self):
        return self.snapshot

    def post(self, action, *args):
        self.q.put((action, args))

    def stop(self):
        self._halt.set()
        self.q.put(("_wake", ()))

    # -- worker ---------------------------------------------------------------------
    def run(self):
        _co_init()
        self._refresh()
        while not self._halt.is_set():
            timeout = max(1, int(self.cfg.get("poll_seconds", 3)))
            try:
                action, args = self.q.get(timeout=timeout)
                self._do(action, args)
                # coalesce a burst of actions (e.g. a volume drag) before refreshing
                while True:
                    try:
                        a2, ar2 = self.q.get_nowait()
                        self._do(a2, ar2)
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            if not self._halt.is_set():
                self._refresh()

    def _do(self, action, args):
        try:
            if action == "default":
                set_default(args[0])
                log.info("default output -> %s", self._devname(args[0]))
            elif action == "default_in":
                set_default(args[0])
                log.info("default input -> %s", self._devname(args[0]))
                if get_volume(args[0])[1]:           # picking a mic means using it
                    set_mute(args[0], False)
                    log.info("...and unmuted it")
            elif action == "volume":
                set_volume(args[0], args[1])
            elif action == "mute":
                set_mute(args[0], args[1])
        except Exception:
            log.exception("audio action %s", action)

    def _devname(self, did):
        for d in self.snapshot.get("outputs", []) + self.snapshot.get("inputs", []):
            if d["id"] == did:
                return d["name"]
        return did[:40]

    def _refresh(self):
        try:
            outs = list_devices(RENDER)
            ins = list_devices(CAPTURE)
            for d in ins:                            # each real mic's mute, for the menus and _keep_unmuted
                if d["state"] == STATE_ACTIVE and not _is_virtual(d["name"]):
                    d["muted"] = get_volume(d["id"])[1]
            self._keep_unmuted(ins)
            out_default = next((d["id"] for d in outs if d["default"]), "")
            vol, mute = (get_volume(out_default) if out_default else (None, None))
            self.snapshot = {"outputs": outs, "inputs": ins, "out_default": out_default,
                             "in_default": next((d["id"] for d in ins if d["default"]), ""),
                             "volume": vol, "muted": mute, "ts": time.time(), "auto": self._auto_note}
            self._auto_switch(outs)
            if self.on_change:
                self.on_change()
        except Exception:
            log.exception("audio refresh")

    def _keep_unmuted(self, ins):
        """Unmute the mics named in "unmute_on_connect" when they connect. Some USB
        mics keep their mute in the device and come back from every reconnect muted
        (a Poly Studio P5 webcam does, each time its USB hub drops out); Windows leaves
        that alone and every app records digital silence. For UNMUTE_GRACE_S after a
        mic appears - or the app starts - a mute is undone; after that it is the
        user's own and stays."""
        now = time.time()
        active = {d["id"]: d for d in ins if d["state"] == STATE_ACTIVE}
        for did in active:
            if self._present is None or did not in self._present:
                self._arrived[did] = now
        for did in [i for i in self._arrived if i not in active]:
            del self._arrived[did]
        self._present = set(active)
        want = {norm_name(n) for n in (self.cfg.get("unmute_on_connect") or [])}
        if not want:
            return
        for did, d in active.items():
            if d.get("muted") and norm_name(d["name"]) in want and now - self._arrived[did] <= UNMUTE_GRACE_S:
                try:
                    set_mute(did, False)
                    d["muted"] = False
                    log.info("unmuted %s: it connected muted", d["name"])
                except Exception:
                    log.exception("unmute %s", d["name"])

    def _auto_switch(self, outs):
        a = self.cfg
        if not a.get("auto_switch"):
            self._last_hp = None
            return
        hp, sp = a.get("headphone_id", ""), a.get("speaker_id", "")
        if not hp or not sp:
            return
        by_id = {d["id"]: d for d in outs}
        hp_dev, sp_dev = by_id.get(hp), by_id.get(sp)
        if not hp_dev or not sp_dev:
            return                                   # one of the pair is absent entirely
        hp_on = hp_dev["state"] == STATE_ACTIVE
        if self._last_hp is None:
            self._last_hp = hp_on            # baseline at startup; never move the user's current output on launch
            return
        if hp_on == self._last_hp:
            return                           # no change in headphone state
        self._last_hp = hp_on
        target = hp if hp_on else sp
        cur = self.snapshot.get("out_default", "")
        if cur == target:
            return
        try:
            set_default(target)
            if a.get("switch_mic"):
                mic = a.get("mic_on_headphones_id") if hp_on else a.get("mic_on_speakers_id")
                if mic:
                    set_default(mic)
            self._auto_note = f"auto: {'headphones' if hp_on else 'speakers'}"
            log.info("auto-switch output -> %s (%s turned %s)", self._devname(target),
                     hp_dev["name"], "on" if hp_on else "off")
            self._refresh_light(target)
        except Exception:
            log.exception("auto-switch")

    def _refresh_light(self, new_default):
        # cheap re-read of volume/default after an auto switch, without re-enumerating twice
        try:
            vol, mute = get_volume(new_default)
            s = dict(self.snapshot)
            s["out_default"] = new_default
            s["volume"], s["muted"] = vol, mute
            for d in s.get("outputs", []):
                d["default"] = d["id"] == new_default
            self.snapshot = s
        except Exception:
            pass


# Virtual / pass-through endpoints that are never a physical pair to switch between.
_VIRTUAL = ("voicemeeter", "vb-audio", "steam streaming", "nvidia", "amd high definition",
            "digital output", "spdif")


def _is_virtual(name):
    n = name.lower()
    return any(k in n for k in _VIRTUAL)


def norm_name(name):
    """A device name without the "2- " Windows puts before a second copy of the same
    model ("Headset Microphone (2- CORSAIR ...)"), lower-cased, so a saved name still
    matches after the device is re-enumerated or moved to another port."""
    return re.sub(r"\(\d+- ", "(", name or "").strip().lower()


def guess_unmute_on_connect(inputs):
    """Names of the connected webcam mics - the usual kind that keeps its own mute and
    comes back muted. Used once, to fill in "unmute_on_connect" on first run."""
    return [d["name"] for d in inputs if d.get("state") == STATE_ACTIVE and not _is_virtual(d["name"])
            and re.search(r"(?i)webcam|camera|\bcam\b", d["name"])]


def guess_pair(outputs):
    """Best guess at (headphone_id, speaker_id) from a device list, by form factor,
    name and current-default, ignoring virtual/HDMI endpoints. Returns ids or ''."""
    real = [d for d in outputs if not _is_virtual(d["name"])]

    def score_hp(d):
        f, n, s = d.get("formfactor"), d["name"].lower(), 0
        if f in (FF_HEADPHONES, FF_HEADSET):
            s += 10
        for kw in ("headphone", "headset", "earphone", "earbud", "buds", "airpod", "corsair", "void",
                   "steelseries", "hyperx", "arctis", "wh-", "wf-"):
            if kw in n:
                s += 5
        if d["state"] == STATE_ACTIVE:
            s += 1
        return s

    def score_sp(d):
        f, n, s = d.get("formfactor"), d["name"].lower(), 0
        if "logitech" in n:
            s += 10
        if f == FF_SPEAKERS:
            s += 4
        if d.get("default"):
            s += 3
        for kw in ("speaker", "monitor", "edifier", "bose", "klipsch", "audioengine"):
            if kw in n:
                s += 2
        if "laptop" in n or ("realtek" in n and f == FF_SPEAKERS):   # onboard, rarely the chosen speakers
            s -= 3
        return s

    hp = max(real, key=score_hp, default=None)
    hp_id = hp["id"] if hp and score_hp(hp) > 0 else ""
    sp_candidates = [d for d in real if d["id"] != hp_id]
    sp = max(sp_candidates, key=score_sp, default=None)
    sp_id = sp["id"] if sp and score_sp(sp) > 0 else ""
    return hp_id, sp_id


def guess_mic_pair(inputs):
    """(mic_on_headphones_id, mic_on_speakers_id): the headset's own mic, and the
    best standalone mic to fall back to."""
    real = [d for d in inputs if not _is_virtual(d["name"])]

    def hp_mic(d):
        n, s = d["name"].lower(), 0
        for kw in ("headset", "corsair", "void", "steelseries", "hyperx", "arctis", "airpod"):
            if kw in n:
                s += 5
        if d.get("formfactor") == FF_HEADSET:
            s += 4
        return s

    def sp_mic(d):
        n, s = d["name"].lower(), 0
        if d.get("default"):
            s += 2
        if "array" in n or "realtek" in n:
            s += 3
        if "usb" in n or "webcam" in n:
            s += 1
        return s

    hp = max(real, key=hp_mic, default=None)
    hp_id = hp["id"] if hp and hp_mic(hp) > 0 else ""
    sp = max([d for d in real if d["id"] != hp_id and d["state"] == STATE_ACTIVE], key=sp_mic, default=None)
    sp_id = sp["id"] if sp else ""
    return hp_id, sp_id


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    comtypes.CoInitialize()
    for label, flow in (("OUTPUT", RENDER), ("INPUT", CAPTURE)):
        print(f"\n=== {label} ===")
        devs = list_devices(flow)
        for d in devs:
            mark = "*" if d["default"] else " "
            print(f" {mark} [{d['state_name']:>11}] {d['form']:>10}  {d['name']}")
        if flow == RENDER:
            hp, sp = guess_pair(devs)
            nm = {d["id"]: d["name"] for d in devs}
            print(f"   guess headphones: {nm.get(hp, '(none)')}")
            print(f"   guess speakers:   {nm.get(sp, '(none)')}")
        else:
            hp, sp = guess_mic_pair(devs)
            nm = {d["id"]: d["name"] for d in devs}
            print(f"   guess headset mic: {nm.get(hp, '(none)')}")
            print(f"   guess other mic:   {nm.get(sp, '(none)')}")
