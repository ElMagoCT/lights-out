"""Monitor enumeration, brightness and power via DDC/CI (dxva2) with a WMI
fallback for a laptop panel.

Why DDC/CI and not SC_MONITORPOWER: on this machine SC_MONITORPOWER drops
Windows into Modern Standby (the "sleep that never wakes"). VCP 0xD6 talks to
the monitor's own controller and never touches Windows' power state.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import subprocess
import time

log = logging.getLogger("lightsout.monitors")
user32 = ctypes.windll.user32
dxva2 = ctypes.windll.dxva2

VCP_BRIGHTNESS = 0x10
VCP_POWER = 0xD6
POWER_ON = 1
POWER_OFF = 4


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("handle", wt.HANDLE), ("desc", ctypes.c_wchar * 128)]


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("monitor", wt.RECT), ("work", wt.RECT),
                ("flags", wt.DWORD), ("device", ctypes.c_wchar * 32)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                                     ctypes.POINTER(wt.RECT), wt.LPARAM)


# --- DisplayConfig, used only to get the monitor's real name and HDR state ---
class LUID(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.LONG)]


class PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapter", LUID), ("id", wt.DWORD), ("mode", wt.DWORD), ("status", wt.DWORD)]


class PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapter", LUID), ("id", wt.DWORD), ("mode", wt.DWORD), ("outTech", wt.DWORD),
                ("rot", wt.DWORD), ("scal", wt.DWORD), ("rrN", wt.DWORD), ("rrD", wt.DWORD),
                ("scan", wt.DWORD), ("avail", wt.BOOL), ("status", wt.DWORD)]


class PATH_INFO(ctypes.Structure):
    _fields_ = [("src", PATH_SOURCE), ("tgt", PATH_TARGET), ("flags", wt.DWORD)]


class SOURCE_NAME(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("adapter", LUID), ("id", wt.DWORD),
                ("gdi", ctypes.c_wchar * 32)]


class TARGET_NAME(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("adapter", LUID), ("id", wt.DWORD),
                ("flags", wt.DWORD), ("tech", wt.DWORD), ("edidMfg", wt.WORD), ("edidProd", wt.WORD),
                ("conn", wt.DWORD), ("name", ctypes.c_wchar * 64), ("path", ctypes.c_wchar * 128)]


class ADVANCED_COLOR(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("adapter", LUID), ("id", wt.DWORD),
                ("value", wt.DWORD), ("colorEnc", wt.DWORD), ("bits", wt.DWORD)]


class SET_ADVANCED_COLOR(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("adapter", LUID), ("id", wt.DWORD),
                ("value", wt.DWORD)]


class SDR_WHITE_GET(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("adapter", LUID), ("id", wt.DWORD),
                ("level", wt.ULONG)]


class SDR_WHITE_SET(ctypes.Structure):
    # Undocumented setter behind Settings > Display > HDR > "SDR content brightness".
    # type 0xFFFFFFEE, level = nits * 1000 / 80 (80..480 nits), finalValue = 1.
    _fields_ = [("type", wt.DWORD), ("size", wt.DWORD), ("adapter", LUID), ("id", wt.DWORD),
                ("level", wt.UINT), ("final", ctypes.c_ubyte)]


SDR_MIN_NITS, SDR_MAX_NITS = 80, 480
VCP_CONTRAST = 0x12


def _display_config():
    """Map GDI device name (\\\\.\\DISPLAYn) -> (friendly name, hdr_enabled)."""
    out = {}
    try:
        np_, nm = wt.UINT(), wt.UINT()
        if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(np_), ctypes.byref(nm)):
            return out
        paths = (PATH_INFO * np_.value)()
        modes = ((ctypes.c_byte * 64) * nm.value)()
        if user32.QueryDisplayConfig(2, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None):
            return out
        for p in paths[:np_.value]:
            s = SOURCE_NAME(type=1, size=ctypes.sizeof(SOURCE_NAME), adapter=p.src.adapter, id=p.src.id)
            t = TARGET_NAME(type=2, size=ctypes.sizeof(TARGET_NAME), adapter=p.tgt.adapter, id=p.tgt.id)
            c = ADVANCED_COLOR(type=9, size=ctypes.sizeof(ADVANCED_COLOR), adapter=p.tgt.adapter, id=p.tgt.id)
            user32.DisplayConfigGetDeviceInfo(ctypes.byref(s))
            user32.DisplayConfigGetDeviceInfo(ctypes.byref(t))
            ok = user32.DisplayConfigGetDeviceInfo(ctypes.byref(c)) == 0
            hdr = ok and bool(c.value & 2)
            hdr_supported = ok and bool(c.value & 1)
            # INTERNAL (0x80000000) or DISPLAYPORT_EMBEDDED (11) = a laptop panel
            internal = p.tgt.outTech in (0x80000000, 11)
            out[s.gdi] = dict(name=t.name or "", hdr=hdr, hdr_supported=hdr_supported, internal=internal,
                              adapter=(p.tgt.adapter.lo, p.tgt.adapter.hi), target=p.tgt.id)
    except Exception:
        log.exception("DisplayConfig query failed")
    return out


class Monitor:
    def __init__(self, device, rect, primary, handle, name, hdr):
        self.device = device        # \\.\DISPLAY4
        self.rect = rect            # (left, top, right, bottom)
        self.primary = primary
        self.handle = handle        # physical monitor handle (DDC) as an int; None = no handle.
                                    # dxva2 hands out small integers and 0 is a valid one.
        self.name = name or "Display"
        self.hdr = hdr
        self.ddc = False            # brightness works over DDC/CI
        self.wmi = False            # brightness works over WMI (laptop panel)
        self.min = 0
        self.max = 100
        self.brightness = None
        self.flaky = False          # has a DDC handle but did not answer a read
        self.hdr_supported = False
        self.adapter = None         # DisplayConfig (LUID lo, hi) and target id, for HDR / SDR white
        self.target = None

    # -- HDR / SDR white level (DisplayConfig) ----------------------------------
    def _hdr_header(self, cls, type_):
        if self.adapter is None:
            return None
        r = cls()
        r.type, r.size, r.id = type_, ctypes.sizeof(cls), self.target
        r.adapter.lo, r.adapter.hi = self.adapter
        return r

    def sdr_white_nits(self):
        """SDR content brightness in nits (80..480) on an HDR display, or None."""
        r = self._hdr_header(SDR_WHITE_GET, 11)
        if r is None or user32.DisplayConfigGetDeviceInfo(ctypes.byref(r)) != 0 or not r.level:
            return None
        return r.level * 80 / 1000

    def set_sdr_white_nits(self, nits):
        nits = int(max(SDR_MIN_NITS, min(SDR_MAX_NITS, nits)))
        nits -= nits % 4
        r = self._hdr_header(SDR_WHITE_SET, 0xFFFFFFEE)
        if r is None:
            return False
        r.level, r.final = nits * 1000 // 80, 1
        rc = user32.DisplayConfigSetDeviceInfo(ctypes.byref(r))
        if rc:
            log.warning("set SDR white %s=%s nits failed rc=%s", self.name, nits, rc)
        return rc == 0

    def set_hdr(self, on):
        """Toggle HDR. A real mode change: the monitor blanks for about a second."""
        r = self._hdr_header(SET_ADVANCED_COLOR, 10)
        if r is None:
            return False
        r.value = 1 if on else 0
        rc = user32.DisplayConfigSetDeviceInfo(ctypes.byref(r))
        log.info("HDR %s on %s -> rc %s", "on" if on else "off", self.name, rc)
        if rc == 0:
            self.hdr = bool(on)
        return rc == 0

    # -- contrast (DDC/CI) ------------------------------------------------------
    def contrast(self):
        if self.handle is None:
            return None
        t, cur, mx = ctypes.c_uint(), wt.DWORD(), wt.DWORD()
        if dxva2.GetVCPFeatureAndVCPFeatureReply(self.handle, VCP_CONTRAST, ctypes.byref(t),
                                                 ctypes.byref(cur), ctypes.byref(mx)):
            return cur.value
        return None

    def set_contrast(self, value):
        if self.handle is None:
            return False
        return bool(dxva2.SetVCPFeature(self.handle, VCP_CONTRAST, int(max(0, min(100, value)))))

    # -- brightness ---------------------------------------------------------
    def read(self):
        if self.handle is not None:
            mn, cur, mx = wt.DWORD(), wt.DWORD(), wt.DWORD()
            if dxva2.GetMonitorBrightness(self.handle, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)):
                self.ddc, self.min, self.max, self.brightness = True, mn.value, mx.value, cur.value
                return self.brightness
        val = _wmi_brightness()
        if val is not None:
            self.wmi, self.brightness = True, val
            return val
        return None

    def set(self, value):
        value = int(max(self.min, min(self.max, value)))
        ok = False
        if self.ddc and self.handle is not None:
            for attempt in range(3):
                ok = bool(dxva2.SetVCPFeature(self.handle, VCP_BRIGHTNESS, value))
                if ok:
                    break
                time.sleep(0.2)
        elif self.wmi:
            ok = _wmi_set_brightness(value)
        if ok:
            self.brightness = value
        else:
            log.warning("set brightness %s=%s failed", self.name, value)
        return ok

    # -- power --------------------------------------------------------------
    def power(self, on):
        if self.handle is None:
            return False
        ok = bool(dxva2.SetVCPFeature(self.handle, VCP_POWER, POWER_ON if on else POWER_OFF))
        log.info("power %s %s -> %s", self.name, "on" if on else "off", ok)
        return ok

    def close(self):
        if self.handle is not None:
            arr = (PHYSICAL_MONITOR * 1)()
            arr[0].handle = self.handle or None
            dxva2.DestroyPhysicalMonitors(1, arr)
            self.handle = None

    def __repr__(self):
        return (f"<{self.name} {self.device} {self.rect} ddc={self.ddc} "
                f"wmi={self.wmi} hdr={self.hdr} b={self.brightness}>")


_PS_FLAGS = 0x08000000  # CREATE_NO_WINDOW


def _wmi_brightness():
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
             "-ErrorAction Stop | Select -First 1).CurrentBrightness"],
            capture_output=True, text=True, timeout=15, creationflags=_PS_FLAGS)
        return int(r.stdout.strip())
    except Exception:
        return None


def _wmi_set_brightness(value):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods "
             "-ErrorAction Stop | Select -First 1) | Invoke-CimMethod -MethodName WmiSetBrightness "
             "-Arguments @{Timeout=0;Brightness=%d} | Out-Null" % int(value)],
            capture_output=True, text=True, timeout=15, creationflags=_PS_FLAGS)
        return r.returncode == 0
    except Exception:
        return False


def enumerate_monitors(probe_wmi=True, retries=6):
    """Fresh list every call: the layout on this machine changes mid-session.

    probe_wmi: only consult WMI (a slow PowerShell call) for monitors that do
    not answer DDC/CI, i.e. a laptop panel.
    """
    names = _display_config()
    hmons = []
    user32.EnumDisplayMonitors(
        None, None, MONITORENUMPROC(lambda h, dc, r, l: (hmons.append(h), True)[1]), 0)
    mons = []
    wmi_used = False
    for h in hmons:
        mi = MONITORINFOEX(cb=ctypes.sizeof(MONITORINFOEX))
        user32.GetMonitorInfoW(h, ctypes.byref(mi))
        n = wt.DWORD()
        dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(h, ctypes.byref(n))
        handle = None
        if n.value:
            arr = (PHYSICAL_MONITOR * n.value)()
            if dxva2.GetPhysicalMonitorsFromHMONITOR(h, n, arr):
                handle = arr[0].handle or 0     # ctypes gives None for a NULL (=0) handle
                if n.value > 1:   # only the first physical monitor is used
                    extra = (PHYSICAL_MONITOR * (n.value - 1))()
                    for i in range(1, n.value):
                        extra[i - 1] = arr[i]
                    dxva2.DestroyPhysicalMonitors(n.value - 1, extra)
            else:
                log.warning("GetPhysicalMonitorsFromHMONITOR failed for %s", mi.device)
        else:
            log.warning("no physical monitors reported for %s", mi.device)
        info = names.get(mi.device, {})
        name, hdr, internal = info.get("name", ""), info.get("hdr", False), info.get("internal", False)
        rect = (mi.monitor.left, mi.monitor.top, mi.monitor.right, mi.monitor.bottom)
        m = Monitor(mi.device, rect, bool(mi.flags & 1), handle, name, hdr)
        m.hdr_supported = info.get("hdr_supported", False)
        m.adapter, m.target = info.get("adapter"), info.get("target")
        # DDC first (retried: DisplayLink DDC reads fail now and then); WMI only
        # for a laptop panel, never for an external monitor that merely blinked.
        if m.handle is not None:
            mn, cur, mx = wt.DWORD(), wt.DWORD(), wt.DWORD()
            for attempt in range(retries):
                if dxva2.GetMonitorBrightness(m.handle, ctypes.byref(mn), ctypes.byref(cur), ctypes.byref(mx)):
                    m.ddc, m.min, m.max, m.brightness = True, mn.value, mx.value, cur.value
                    break
                log.warning("DDC brightness read failed for %s (attempt %d)", m.name, attempt + 1)
                time.sleep(0.25)
            if not m.ddc:
                # The DisplayLink-attached monitor here answers DDC in bursts: a
                # read can fail for a while and then recover. Keep it controllable
                # (sets are retried too) instead of greying it out.
                m.ddc, m.flaky = True, True
        if not m.ddc and internal and probe_wmi and not wmi_used:
            val = _wmi_brightness()
            if val is not None:
                m.wmi, m.brightness, wmi_used = True, val, True
                if not m.name:
                    m.name = "Laptop screen"
        if not (m.ddc or m.wmi):
            m.name = (m.name or "Display") + " (no brightness control)"
        mons.append(m)
    mons.sort(key=lambda m: (not m.primary, m.rect[0]))
    log.info("monitors: %s", mons)
    return mons


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for m in enumerate_monitors():
        print(m)
        m.close()
