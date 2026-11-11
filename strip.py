"""The taskbar controls strip: sleep, one brightness slider for every monitor,
a power-mode slider and live stats, docked over the left end of the taskbar.

Windows 11 has no API for putting controls inside the taskbar, so this is a
borderless, no-activate tool window held at the taskbar's left edge (empty on
a centred taskbar). Three things keep it honest:

* Z-order. The taskbar is an ordinary topmost window (same band as ours) and
  puts itself back on top whenever it is used, so the strip re-inserts itself
  directly above it: at once on a foreground change (WinEvent hook) and on a
  light 0.25 s check. "Directly above the taskbar" rather than "top of
  everything", so the taskbar's own menus and tooltips still draw over it.
* It mirrors the taskbar. Taskbar hidden (sleep, auto-hide) or a fullscreen
  app covering it: the strip hides too, and comes back with it.
* Typed Win32 calls. The first version called SetWindowPos through an untyped
  ctypes prototype: HWND_TOPMOST (-1) went over as a 32-bit value, the call
  failed without a sound, and the taskbar covered the strip for good the first
  time it was clicked.

Lightweight: stats come from the engine's existing 2 s snapshot, brightness
is written only when a drag ends, and the z-order check is a handful of user32
calls.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import tkinter as tk
from tkinter import ttk

log = logging.getLogger("lightsout.strip")
BG, PANEL, FG, DIM, ACCENT = "#101114", "#1a1c21", "#e8e8ea", "#8a8d96", "#f2c14e"

# our own user32 handle: prototypes declared here cannot clash with other modules' untyped calls
u32 = ctypes.WinDLL("user32", use_last_error=True)
HWND, UINT, DWORD, BOOL, LONG = wt.HWND, wt.UINT, wt.DWORD, wt.BOOL, wt.LONG
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, DWORD, HWND, LONG, LONG, DWORD, DWORD)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", DWORD)]


for _name, _res, _args in (
        ("FindWindowW", HWND, [wt.LPCWSTR, wt.LPCWSTR]),
        ("IsWindow", BOOL, [HWND]),
        ("IsWindowVisible", BOOL, [HWND]),
        ("GetWindowRect", BOOL, [HWND, ctypes.POINTER(wt.RECT)]),
        ("GetAncestor", HWND, [HWND, UINT]),
        ("GetWindow", HWND, [HWND, UINT]),
        ("GetWindowLongW", LONG, [HWND, ctypes.c_int]),
        ("SetWindowLongW", LONG, [HWND, ctypes.c_int, LONG]),
        ("SetWindowPos", BOOL, [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT]),
        ("ShowWindow", BOOL, [HWND, ctypes.c_int]),
        ("GetForegroundWindow", HWND, []),
        ("GetClassNameW", ctypes.c_int, [HWND, wt.LPWSTR, ctypes.c_int]),
        ("MonitorFromWindow", wt.HANDLE, [HWND, DWORD]),
        ("GetMonitorInfoW", BOOL, [wt.HANDLE, ctypes.POINTER(MONITORINFO)]),
        ("SetWinEventHook", wt.HANDLE, [DWORD, DWORD, wt.HMODULE, WINEVENTPROC, DWORD, DWORD, DWORD]),
        ("UnhookWinEvent", BOOL, [wt.HANDLE])):
    _f = getattr(u32, _name)
    _f.restype, _f.argtypes = _res, _args

GA_ROOT, GW_HWNDPREV = 2, 3
GWL_EXSTYLE = -20
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
HWND_TOPMOST = HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
SWP_Z = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0, 0x2
MONITOR_DEFAULTTONEAREST = 2
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(96)
    u32.GetClassNameW(hwnd, buf, 96)
    return buf.value


def covers(rect, mon):
    """True if a window rectangle covers a whole monitor rectangle (fullscreen).
    Both are (left, top, right, bottom). A maximised window does not: it stops
    at the work area, above the taskbar."""
    return rect[0] <= mon[0] and rect[1] <= mon[1] and rect[2] >= mon[2] and rect[3] >= mon[3]


def above(hwnd, other, limit=96):
    """True if hwnd is somewhere above other in the Z-order."""
    h, n = u32.GetWindow(other, GW_HWNDPREV), 0
    while h and n < limit:
        if h == hwnd:
            return True
        h, n = u32.GetWindow(h, GW_HWNDPREV), n + 1
    return False


def fullscreen_over(tray, ignore=()):
    """Is the foreground window a fullscreen one on the taskbar's monitor? (The
    shell drops the taskbar behind such a window; the strip must follow.)"""
    fg = u32.GetForegroundWindow()
    if not fg or fg == tray or fg in ignore or class_name(fg) in SHELL_CLASSES:
        return False
    mon_t = u32.MonitorFromWindow(tray, MONITOR_DEFAULTTONEAREST)
    if mon_t != u32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST):
        return False
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    r = wt.RECT()
    if not u32.GetMonitorInfoW(mon_t, ctypes.byref(mi)) or not u32.GetWindowRect(fg, ctypes.byref(r)):
        return False
    m = mi.rcMonitor
    return covers((r.left, r.top, r.right, r.bottom), (m.left, m.top, m.right, m.bottom))


class TaskbarStrip:
    MODES = ["auto", "quiet", "balanced", "performance", "max"]

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg.get("taskbar_strip", {})
        self.hwnd = None
        self.tray = None
        self.shown = True
        self._geom = None
        self._hook = None
        self._hook_cb = None
        self._closed = False
        self.win = tk.Toplevel(app.root, bg=BG)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self._build()
        vals = [v for v in app.last.values() if isinstance(v, (int, float))]
        if vals:
            iv = int(sum(vals) / len(vals))
            self.bright.set(iv)
            self.bright_lab.config(text=f"{iv}%")
        self.win.update_idletasks()
        self.hwnd = u32.GetAncestor(self.win.winfo_id(), GA_ROOT)
        ex = u32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        u32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        try:
            self._hook_cb = WINEVENTPROC(self._on_foreground)
            self._hook = u32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._hook_cb,
                                             0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        except Exception:
            log.exception("foreground hook")
        self._reposition()
        self.zfix()
        self._fast_tick()
        self._stats_tick()

    # -- widgets ------------------------------------------------------------------
    def _build(self):
        f = tk.Frame(self.win, bg=BG)
        f.pack(fill="both", expand=True, padx=8, pady=5)
        self.sleep_btn = tk.Button(f, text="☾ Sleep", bg=PANEL, fg=FG, bd=0, relief="flat",
                                   font=("Segoe UI", 9, "bold"), activebackground=ACCENT,
                                   activeforeground="#101114", cursor="hand2",
                                   command=lambda: self.app.post(self.app.sleep))
        self.sleep_btn.pack(side="left", padx=(0, 10), ipadx=6, ipady=2)
        tk.Label(f, text="☀", bg=BG, fg=DIM, font=("Segoe UI", 11)).pack(side="left")
        self.bright = tk.DoubleVar(value=50)
        self.bright_scale = ttk.Scale(f, from_=0, to=100, variable=self.bright, length=110, command=self._on_bright)
        self.bright_scale.pack(side="left", padx=(3, 3))
        self.bright_scale.bind("<ButtonRelease-1>", self._commit_bright)
        self.bright_lab = tk.Label(f, text="--%", bg=BG, fg=FG, width=4, font=("Segoe UI", 9))
        self.bright_lab.pack(side="left", padx=(0, 10))
        tk.Label(f, text="⚡", bg=BG, fg=DIM, font=("Segoe UI", 11)).pack(side="left")
        self.power = tk.DoubleVar(value=0)
        self.power_scale = ttk.Scale(f, from_=0, to=4, variable=self.power, length=96, command=self._on_power)
        self.power_scale.pack(side="left", padx=(3, 3))
        self.power_scale.bind("<ButtonRelease-1>", self._commit_power)
        self.power_lab = tk.Label(f, text="Auto", bg=BG, fg=FG, width=10, anchor="w", font=("Segoe UI", 9))
        self.power_lab.pack(side="left", padx=(0, 10))
        self.stats = tk.Label(f, text="", bg=BG, fg="#9aa0ad", anchor="w", font=("Consolas", 9))
        self.stats.pack(side="left", fill="x", expand=True)

    # brightness: the label follows the drag, the monitors are written once on release
    def _on_bright(self, _=None):
        self.bright_lab.config(text=f"{int(float(self.bright.get()))}%")

    def _commit_bright(self, _=None):
        v = int(float(self.bright.get()))
        self.app.post(lambda: self.app.set_brightness_all(v))

    # power: snaps to a detent, applied on release
    def _on_power(self, _=None):
        i = max(0, min(4, int(round(float(self.power.get())))))
        self.power_lab.config(text=self.MODES[i].capitalize())

    def _commit_power(self, _=None):
        i = max(0, min(4, int(round(float(self.power.get())))))
        self.power.set(i)
        self.app.post(lambda m=self.MODES[i]: self.app.set_power_mode(m))

    # -- placement ----------------------------------------------------------------
    def _tray(self):
        if not self.tray or not u32.IsWindow(self.tray):
            self.tray = u32.FindWindowW("Shell_TrayWnd", None)
        return self.tray

    def _reposition(self):
        """Follow the taskbar's rectangle (Tk call: timer only, never from the hook)."""
        tray = self._tray()
        r = wt.RECT()
        if tray and u32.GetWindowRect(tray, ctypes.byref(r)):
            geom = "%dx%d+%d+%d" % (int(self.cfg.get("width", 660)), r.bottom - r.top,
                                    r.left + int(self.cfg.get("x_offset", 0)), r.top)
            if geom != self._geom:
                self.win.geometry(geom)
                self._geom = geom

    def want_shown(self):
        tray = self._tray()
        if not tray or not u32.IsWindowVisible(tray):
            return False                         # no taskbar on screen (asleep, auto-hidden, explorer restarting)
        if not u32.GetWindowLongW(tray, GWL_EXSTYLE) & WS_EX_TOPMOST:
            return False                         # the shell has dropped it behind a fullscreen app
        return not fullscreen_over(tray, ignore=(self.hwnd,))

    def zfix(self):
        """Pure Win32 (safe from the WinEvent hook): mirror the taskbar's
        visibility and sit directly above it in the Z-order."""
        if self._closed or not self.hwnd:
            return
        show = self.want_shown()
        if show != self.shown:
            u32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if show else SW_HIDE)
            self.shown = show
        if not show:
            return
        tray = self.tray
        if above(self.hwnd, tray):
            return
        prev = u32.GetWindow(tray, GW_HWNDPREV)        # whatever is directly above the taskbar: go right under it
        if not (prev and u32.SetWindowPos(self.hwnd, prev, 0, 0, 0, 0, SWP_Z) and above(self.hwnd, tray)):
            u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_Z)

    def _on_foreground(self, hook, event, hwnd, id_object, id_child, thread, ms):
        try:
            self.zfix()
        except Exception:
            pass

    def _fast_tick(self):
        if self._closed or not self.win.winfo_exists():
            return
        try:
            self._reposition()
            self.zfix()
        except Exception:
            log.exception("strip placement")
        self.win.after(250, self._fast_tick)

    # -- stats ----------------------------------------------------------------------
    def _stats_tick(self):
        if self._closed or not self.win.winfo_exists():
            return
        try:
            if self.shown:
                st = self.app.engine.snapshot()
                mode = self.app.cfg["power"].get("mode", "auto")
                i = self.MODES.index(mode) if mode in self.MODES else 0
                if int(round(float(self.power.get()))) != i:
                    self.power.set(i)
                prof = st.get("profile") or "-"
                self.power_lab.config(text=(f"Auto·{prof[:4]}" if mode == "auto" else mode.capitalize()))
                bits = []
                if st.get("cpu") is not None:
                    bits.append(f"CPU{st['cpu']:>3.0f}%" + (f" {st['cpu_temp']:>2.0f}°" if st.get("cpu_temp") else ""))
                if st.get("mem") is not None:
                    bits.append(f"RAM{st['mem']:>3.0f}%")
                if st.get("gpu_util") is not None:
                    bits.append(f"GPU{st['gpu_util']:>3.0f}%")
                if st.get("thermal"):
                    bits.append("⚠")
                self.stats.config(text="  ".join(bits))
        except Exception:
            log.exception("strip stats")
        self.win.after(2000, self._stats_tick)

    def alive(self):
        try:
            return not self._closed and bool(self.win.winfo_exists())
        except Exception:
            return False

    def destroy(self):
        self._closed = True
        if self._hook:
            try:
                u32.UnhookWinEvent(self._hook)
            except Exception:
                pass
            self._hook = None
        try:
            self.win.destroy()
        except Exception:
            pass
