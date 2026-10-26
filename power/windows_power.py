"""Windows-side power levers that work for any PC without elevation:

* Power mode overlay (Settings > Power > Power mode): PowerSetActiveOverlayScheme.
* Processor settings on the active power plan via powercfg: boost mode and
  minimum processor state. The original values are backed up once and put
  back by restore(), so the engine never leaves the plan changed.
"""
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import re
import subprocess

log = logging.getLogger("lightsout.winpower")
_pp = ctypes.WinDLL("powrprof")
_NOWIN = 0x08000000

OVERLAY = {
    "battery": "961cc777-2547-4f9d-8174-7d86181b8a7a",    # best power efficiency
    "balanced": "00000000-0000-0000-0000-000000000000",
    "performance": "ded574b5-45a0-4f42-8737-46345c09c238", # best performance
}
SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
PERFBOOSTMODE = "be337238-0d82-4146-a960-4f3749d470c7"
PROCTHROTTLEMIN = "893dee8e-2bef-41e0-89c6-b55d0929964c"
PROCTHROTTLEMAX = "bc5038f7-23e0-4960-96da-33ab7f63e0e7"
# boost mode values: 0 disabled, 1 enabled, 2 aggressive, 3 efficient enabled,
# 4 efficient aggressive, 5 aggressive at guaranteed, 6 efficient aggressive at guaranteed
BOOST = {"quiet": 3, "balanced": 3, "performance": 2, "max": 5}
MIN_STATE = {"quiet": 5, "balanced": 5, "performance": 5, "max": 5}


class GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]


def _guid(s):
    g = GUID()
    p = s.split("-")
    g.d1, g.d2, g.d3 = int(p[0], 16), int(p[1], 16), int(p[2], 16)
    raw = bytes.fromhex(p[3] + p[4])
    for i in range(8):
        g.d4[i] = raw[i]
    return g


def _gstr(g):
    return "%08x-%04x-%04x-%s-%s" % (g.d1, g.d2, g.d3, bytes(g.d4[:2]).hex(), bytes(g.d4[2:]).hex())


def get_overlay():
    g = GUID()
    if _pp.PowerGetActualOverlayScheme(ctypes.byref(g)) != 0:
        return None
    s = _gstr(g)
    return next((k for k, v in OVERLAY.items() if v == s), s)


def set_overlay(name):
    rc = _pp.PowerSetActiveOverlayScheme(ctypes.byref(_guid(OVERLAY[name])))
    if rc:
        log.warning("set overlay %s rc=%s", name, rc)
    return rc == 0


def _powercfg(*args):
    r = subprocess.run(["powercfg"] + list(args), capture_output=True, text=True, timeout=20, creationflags=_NOWIN)
    return r.returncode, r.stdout


def active_scheme():
    rc, out = _powercfg("/getactivescheme")
    m = re.search(r"([0-9a-f-]{36})", out)
    return m.group(1) if m else None


ALIAS = {PERFBOOSTMODE: "PERFBOOSTMODE", PROCTHROTTLEMIN: "PROCTHROTTLEMIN", PROCTHROTTLEMAX: "PROCTHROTTLEMAX"}


def read_setting(setting_guid):
    """(ac_index, dc_index) of a processor setting on the active plan. Hidden
    settings (boost mode) are invisible to powercfg /query, so this reads the
    plan straight from the registry, which needs no elevation."""
    import winreg
    scheme = active_scheme()
    if scheme:
        try:
            key = "SYSTEM\\CurrentControlSet\\Control\\Power\\User\\PowerSchemes\\%s\\%s\\%s" % (
                scheme, SUB_PROCESSOR, setting_guid)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                return (winreg.QueryValueEx(k, "ACSettingIndex")[0], winreg.QueryValueEx(k, "DCSettingIndex")[0])
        except OSError:
            pass
    rc, out = _powercfg("/query", "SCHEME_CURRENT", "SUB_PROCESSOR", ALIAS.get(setting_guid, setting_guid))
    ac = re.search(r"Current AC Power Setting Index: 0x([0-9a-f]+)", out)
    dc = re.search(r"Current DC Power Setting Index: 0x([0-9a-f]+)", out)
    return (int(ac.group(1), 16) if ac else None, int(dc.group(1), 16) if dc else None)


def write_setting(setting_guid, ac=None, dc=None, apply=True):
    ok = True
    name = ALIAS.get(setting_guid, setting_guid)
    if ac is not None:
        ok &= _powercfg("/setacvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", name, str(ac))[0] == 0
    if dc is not None:
        ok &= _powercfg("/setdcvalueindex", "SCHEME_CURRENT", "SUB_PROCESSOR", name, str(dc))[0] == 0
    if apply:
        ok &= _powercfg("/setactive", "SCHEME_CURRENT")[0] == 0
    return ok


class PlanTuner:
    """Profile -> boost mode + min processor state on the active plan, with a
    one-time backup so the plan can be put back exactly."""

    def __init__(self, backup_path):
        self.backup_path = backup_path
        self.current = None
        self.scheme = None

    def _backup(self):
        if os.path.exists(self.backup_path):
            return
        scheme = active_scheme()
        data = {"scheme": scheme, "boost": read_setting(PERFBOOSTMODE), "min": read_setting(PROCTHROTTLEMIN)}
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        log.info("power plan backup: %s", data)

    def apply(self, profile):
        if profile == self.current:
            return True
        self._backup()
        ok = write_setting(PERFBOOSTMODE, ac=BOOST[profile], dc=min(BOOST[profile], 3), apply=False)
        ok &= write_setting(PROCTHROTTLEMIN, ac=MIN_STATE[profile], dc=5, apply=True)
        self.current = profile if ok else None
        log.info("plan tuned for %s -> %s", profile, ok)
        return ok

    def restore(self):
        try:
            with open(self.backup_path, encoding="utf-8") as f:
                b = json.load(f)
        except Exception:
            return False
        # boost mode is hidden and often unset on the plan (reads None): Windows'
        # own default for it is 2 = aggressive, so that is what "restore" means then
        boost = [2 if v is None else v for v in b["boost"]]
        ok = write_setting(PERFBOOSTMODE, *boost, apply=False) and write_setting(PROCTHROTTLEMIN, *b["min"], apply=True)
        if ok:
            os.remove(self.backup_path)
            self.current = None
        log.info("power plan restored -> %s", ok)
        return ok


if __name__ == "__main__":
    print("overlay:", get_overlay(), "scheme:", active_scheme())
    print("boost:", read_setting(PERFBOOSTMODE), "min:", read_setting(PROCTHROTTLEMIN), "max:", read_setting(PROCTHROTTLEMAX))
