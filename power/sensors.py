"""What is the machine doing right now. One sample() every couple of seconds:
CPU / memory (psutil), GPU (nvml), battery, user idle time, the foreground
app and whether it is fullscreen, and which processes are the heavy ones.
Cheap by design: the per-process scan runs only every few samples."""
import ctypes
import ctypes.wintypes as wt
import logging
import time

import psutil

from . import nvml, procsnap

log = logging.getLogger("lightsout.sensors")
user32 = ctypes.windll.user32


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("monitor", wt.RECT), ("work", wt.RECT), ("flags", wt.DWORD)]


def idle_seconds():
    li = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(li))
    return max(0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000)


def foreground():
    """(pid, exe name, fullscreen?) of the foreground window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None, False
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        name = psutil.Process(pid.value).name().lower()
    except Exception:
        name = None
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    mi = MONITORINFO(cb=ctypes.sizeof(MONITORINFO))
    hmon = user32.MonitorFromWindow(hwnd, 2)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    m = mi.monitor
    # covers the whole monitor (not just the work area) and is not the shell
    fullscreen = (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom
                  and name not in (None, "explorer.exe", "searchhost.exe", "lockapp.exe", "shellexperiencehost.exe"))
    return pid.value, name, fullscreen


class Sensors:
    def __init__(self, process_scan_every=3):
        psutil.cpu_percent(interval=None)      # prime
        self.scan_every = process_scan_every
        self.n = 0
        self.tracker = procsnap.CpuTracker()   # one kernel call per scan instead of one per process
        self.top = []                          # [(name, pid, cpu%, rss MB)] heaviest processes
        self.by_name = {}                      # exe name -> summed cpu%
        self.gpu_pids = set()
        self.cpu_count = psutil.cpu_count() or 1

    def sample(self):
        self.n += 1
        s = {"t": time.time(), "cpu": psutil.cpu_percent(interval=None),
             "mem": psutil.virtual_memory().percent, "idle_s": idle_seconds()}
        try:
            b = psutil.sensors_battery()
            s["on_ac"] = True if b is None else bool(b.power_plugged)
            s["battery"] = None if b is None else b.percent
        except Exception:
            s["on_ac"], s["battery"] = True, None
        pid, name, fs = foreground()
        s["fg_pid"], s["fg"], s["fullscreen"] = pid, name, fs
        g = nvml.read()
        if g:
            s["gpu_temp"], s["gpu_util"], s["gpu_power_w"] = g.get("temp"), g.get("util"), g.get("power_w")
        if (self.n - 1) % self.scan_every == 0:
            self._scan_processes()
            self.gpu_pids = nvml.gpu_pids()
        s["fg_on_gpu"] = pid in self.gpu_pids
        s["top"] = self.top
        return s

    def _scan_processes(self):
        per_pid, by_name = self.tracker.update()
        rows = [(name, pid, pct, ws / 1048576) for pid, (name, pct, ws) in per_pid.items() if pid and pct >= 0.5]
        rows.sort(key=lambda r: -r[2])
        self.top = rows[:12]
        by_name.pop("idle", None)
        self.by_name = by_name


if __name__ == "__main__":
    import json
    sens = Sensors(process_scan_every=1)
    sens.sample()
    time.sleep(1.5)
    s = sens.sample()
    s["top"] = s["top"][:5]
    print(json.dumps(s, indent=1, default=str))
