"""Never run inside somebody else's app container.

A process started from inside a packaged (MSIX) desktop app - a terminal or an
agent hosted by one, say - inherits that app's container. For this app that is
quietly ruinous:

* %LOCALAPPDATA% is virtualised: new files land in a private per-package copy
  (Packages\\<app>\\LocalCache\\Local\\...). Config, logs, the sleep sentinel and
  the helper's token then exist twice, and an instance started the normal way
  and one started from the container never see each other's.
* the process sits in the host app's job object and dies when that app closes
  or updates - while asleep, that leaves the desk dark with nobody to wake it.

So at startup the app checks whether its data folder is such a private copy
and, if it is, starts itself again through Explorer (exactly what a
double-click does: a child of explorer.exe, real files, clean environment) and
exits. Signals between instances use named kernel events rather than files for
the same reason: files written from inside a container are invisible outside.
"""
import ctypes
import ctypes.wintypes as wt
import glob
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

log = logging.getLogger("lightsout.breakout")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenMutexW.restype = wt.HANDLE
k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.CreateEventW.restype = wt.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
k32.OpenEventW.restype = wt.HANDLE
k32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
k32.SetEvent.argtypes = [wt.HANDLE]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.WaitForMultipleObjects.restype = wt.DWORD
k32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL, wt.DWORD]

PANEL_EVENT = "Local\\LightsOut.OpenPanel"
SLEEP_EVENT = "Local\\LightsOut.Sleep"
NOWIN = 0x08000000


def private_view(data_dir):
    """The private per-package directory this process really writes to when it
    creates a file in data_dir, or None when data_dir is the real thing.
    Found by experiment: create a marker and see where it materialised."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    try:
        rel = os.path.relpath(os.path.abspath(data_dir), os.path.abspath(local))
    except ValueError:
        return None
    if rel.startswith(".."):
        return None                              # not under %LOCALAPPDATA%: never virtualised
    marker = ".ctx-%d" % os.getpid()
    path = os.path.join(data_dir, marker)
    try:
        with open(path, "w") as f:
            f.write("x")
    except OSError:
        return None
    try:
        hits = glob.glob(os.path.join(local, "Packages", "*", "LocalCache", "Local", rel, marker))
        return os.path.dirname(hits[0]) if hits else None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def mutex_exists(name):
    h = k32.OpenMutexW(0x00100000, False, name)          # SYNCHRONIZE
    if h:
        k32.CloseHandle(h)
    return bool(h)


def wait_for_mutex(name, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if mutex_exists(name):
            return True
        time.sleep(0.25)
    return False


def relaunch_outside(script, frozen, extra=()):
    """Start this app again as a child of explorer.exe (what a double-click
    does). Returns True if Explorer was asked; the caller checks the result."""
    if frozen:
        exe, args, cwd = sys.executable, list(extra), os.path.dirname(sys.executable)
    else:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        args, cwd = [script] + list(extra), os.path.dirname(script)
    lnk = os.path.join(tempfile.gettempdir(), "LightsOut-launch.lnk")     # Temp is not virtualised
    q = lambda s: s.replace("'", "''")
    arg_str = " ".join('"%s"' % a if (" " in a or "\\" in a) else a for a in args)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';$s.Arguments='%s';"
          "$s.WorkingDirectory='%s';$s.Save();Start-Process explorer.exe -ArgumentList '\"%s\"'"
          % (q(lnk), q(exe), q(arg_str), q(cwd), q(lnk)))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=45, creationflags=NOWIN)
        if r.returncode != 0:
            log.warning("relaunch through Explorer failed: %s", (r.stderr or r.stdout).strip()[:300])
        return r.returncode == 0
    except Exception:
        log.exception("relaunch through Explorer")
        return False


def signal(name):
    """Set a named event of the running instance. False if nobody is listening."""
    h = k32.OpenEventW(0x0002, False, name)              # EVENT_MODIFY_STATE
    if not h:
        return False
    try:
        return bool(k32.SetEvent(h))
    finally:
        k32.CloseHandle(h)


class SignalListener(threading.Thread):
    """Waits on named auto-reset events and calls the matching callback.
    handlers: {event name: callable}. Costs nothing while idle."""

    def __init__(self, handlers):
        super().__init__(name="signals", daemon=True)
        self.names = list(handlers)
        self.handlers = handlers
        self.events = [k32.CreateEventW(None, False, False, n) for n in self.names]

    def run(self):
        live = [(h, n) for h, n in zip(self.events, self.names) if h]
        if not live:
            log.warning("no signal events could be created")
            return
        arr = (wt.HANDLE * len(live))(*[h for h, _ in live])
        while True:
            w = k32.WaitForMultipleObjects(len(live), arr, False, 0xFFFFFFFF)
            if w >= len(live):                           # WAIT_FAILED / abandoned: stop rather than spin
                log.warning("signal wait ended (%s)", w)
                return
            try:
                self.handlers[live[w][1]]()
            except Exception:
                log.exception("signal handler %s", live[w][1])
