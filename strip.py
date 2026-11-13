"""The taskbar controls strip: sleep, one brightness slider for every monitor,
a power-mode slider, an audio picker and live stats, docked over the left end
of the taskbar.

Windows 11 has no API for putting controls inside the taskbar, so this is a
borderless, no-activate tool window held at the taskbar's left edge (empty on
a centred taskbar). What keeps it honest:

* See-through. The window's background is a colour key: those pixels are not
  drawn at all, so the taskbar - acrylic and all - shows through and clicks
  there fall through to it. The key is sampled from the taskbar itself, so
  the anti-aliased edges of the (white) text blend into the real colour. The
  controls you click sit on an opaque twin of that colour, one step off the
  key: indistinguishable, but solid enough to catch the mouse.
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
import time
import tkinter as tk
import tkinter.font as tkfont

log = logging.getLogger("lightsout.strip")
FG = "#ffffff"                   # taskbar text is white: match it
ACCENT = "#f2c14e"
POP_BG, POP_BORDER, POP_HOVER, POP_SUB = "#2b2b2b", "#474747", "#3a3a3a", "#c8c8c8"
MUTED_FG = "#ff8f80"              # a muted mic in the audio menu
DEFAULT_TASKBAR = (32, 32, 32)   # Windows 11 dark taskbar, used until a real sample is possible

# Segoe Fluent Icons (Windows 11) / Segoe MDL2 Assets (Windows 10) code points, and plain fallbacks
GLYPH = {"sleep": "", "sun": "", "bolt": "", "speaker": "", "headphone": "",
         "mute": "", "mic": "", "down": "", "check": ""}
PLAIN = {"sleep": "☾", "sun": "☀", "bolt": "⚡", "speaker": "♪", "headphone": "♪", "mute": "×", "mic": "●",
         "down": "▾", "check": "✓"}
HEADSET_WORDS = ("headset", "headphone", "earphone", "earbud", "airpods", "buds")

# our own user32 handle: prototypes declared here cannot clash with other modules' untyped calls
u32 = ctypes.WinDLL("user32", use_last_error=True)
HWND, UINT, DWORD, BOOL, LONG = wt.HWND, wt.UINT, wt.DWORD, wt.BOOL, wt.LONG
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, DWORD, HWND, LONG, LONG, DWORD, DWORD)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", DWORD)]


for _name, _res, _args in (
        ("FindWindowW", HWND, [wt.LPCWSTR, wt.LPCWSTR]),
        ("FindWindowExW", HWND, [HWND, HWND, wt.LPCWSTR, wt.LPCWSTR]),
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


# -- colour ----------------------------------------------------------------------------
def tidy(name, limit=64):
    """A device name for the flyout: drop a trailing driver group only when the
    name has two top-level groups ("Speakers (USB C hub) (USB Audio Device)"),
    and never cut inside nested ones ("Microphone Array (Realtek(R) Audio)")."""
    groups, depth, start = [], 0, 0
    for i, ch in enumerate(name):
        if ch == "(":
            if depth == 0:
                start = i
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0:
                groups.append((start, i))
    if len(groups) >= 2 and groups[-1][1] == len(name) - 1:
        name = name[:groups[-1][0]].rstrip()
    return name if len(name) <= limit else name[:limit - 1] + "…"


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def shade(rgb, d):
    return tuple(max(0, min(255, c + d)) for c in rgb)


def grab(left, top, right, bottom):
    """One BitBlt of a screen rectangle (desktop coordinates), as RGB."""
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")


def median_rgb(pixels):
    pixels = [p[:3] for p in pixels]
    if not pixels:
        return None
    chans = [sorted(p[i] for p in pixels) for i in range(3)]
    return tuple(c[len(c) // 2] for c in chans)


def sample_taskbar(tray, x_from, x_to):
    """Median colour of the taskbar between x_from and x_to - stopping short of
    the Start button, away from the bright top edge - or None when the taskbar
    is hidden. One screen grab."""
    r = wt.RECT()
    if not tray or not u32.IsWindowVisible(tray) or not u32.GetWindowRect(tray, ctypes.byref(r)):
        return None
    start = u32.FindWindowExW(tray, None, "Start", None)
    sr = wt.RECT()
    if start and u32.GetWindowRect(start, ctypes.byref(sr)) and sr.left > x_from:
        x_to = min(x_to, sr.left - 20)
    x_from, x_to = max(x_from, r.left), min(x_to, r.right)
    if x_to - x_from < 4:
        return None
    try:
        img = grab(x_from, r.top, x_to, r.bottom)
    except Exception:
        return None
    h = img.height
    return median_rgb([img.getpixel((x, y)) for x in range(0, img.width, 6) for y in (h // 2, h - 8)])


class Palette:
    def __init__(self, base, avoid=None):
        self.base = tuple(base)
        self.key = hexc(base)                    # never drawn: the taskbar shows through, clicks fall through
        avoid = {self.key, avoid}
        for d in (1, -1, 2, -2, 3):              # one step off: looks the same, but is drawn and catches the mouse
            self.solid = hexc((base[0], max(0, min(255, base[1] + d)), base[2]))
            if self.solid not in avoid:
                break
        self.hover = hexc(shade(base, 26))
        self.press = hexc(shade(base, 42))
        self.track = hexc(shade(base, 70))

    def differs(self, base, tol=6):
        return any(abs(a - b) > tol for a, b in zip(self.base, base))


# -- widgets ---------------------------------------------------------------------------
class MiniSlider(tk.Canvas):
    """A thin Windows 11 style slider on an opaque, taskbar-coloured canvas.
    on_change(value) while dragging, on_commit(value) when the drag ends (and
    shortly after the last wheel notch). steps=True snaps to whole numbers."""

    def __init__(self, master, pal, width, height, lo, hi, value, steps=False, on_change=None, on_commit=None,
                 wheel_step=5):
        super().__init__(master, width=width, height=height, bg=pal.solid, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.pal, self.w, self.h = pal, width, height
        self.lo, self.hi, self.steps = lo, hi, steps
        self.value = value
        self.on_change, self.on_commit = on_change, on_commit
        self.wheel_step = 1 if steps else wheel_step
        self.dragging = False
        self.r = 6                                  # thumb radius
        self._wheel_job = None
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)
        self._draw()

    def _x(self, v):
        span = self.w - 2 * (self.r + 2)
        return self.r + 2 + (v - self.lo) / float(self.hi - self.lo) * span

    def _v(self, x):
        span = self.w - 2 * (self.r + 2)
        frac = min(1.0, max(0.0, (x - self.r - 2) / float(span)))
        v = self.lo + frac * (self.hi - self.lo)
        return int(round(v))

    def _draw(self):
        self.delete("all")
        cy, x0, x1 = self.h // 2, self.r + 2, self.w - self.r - 2
        x = self._x(self.value)
        self.create_line(x0, cy, x1, cy, fill=self.pal.track, width=4, capstyle="round")
        if self.steps:
            for i in range(self.lo, self.hi + 1):
                xi = self._x(i)
                self.create_oval(xi - 2, cy - 2, xi + 2, cy + 2, fill=self.pal.track, outline="")
        self.create_line(x0, cy, x, cy, fill=ACCENT, width=4, capstyle="round")
        self.create_oval(x - self.r, cy - self.r, x + self.r, cy + self.r, fill=FG, outline="")

    def set(self, v):
        """Programmatic update; ignored while the user is dragging."""
        v = int(max(self.lo, min(self.hi, round(v))))
        if not self.dragging and v != self.value:
            self.value = v
            self._draw()

    def recolor(self, pal):
        self.pal = pal
        self.config(bg=pal.solid)
        self._draw()

    def _update(self, v):
        v = int(max(self.lo, min(self.hi, v)))
        if v != self.value:
            self.value = v
            self._draw()
            if self.on_change:
                self.on_change(v)

    def _press(self, e):
        self.dragging = True
        self._update(self._v(e.x))

    def _move(self, e):
        if self.dragging:
            self._update(self._v(e.x))

    def _release(self, e):
        if self.dragging:
            self._update(self._v(e.x))
            self.dragging = False
            if self.on_commit:
                self.on_commit(self.value)

    def _wheel(self, e):
        self._update(self.value + (self.wheel_step if e.delta > 0 else -self.wheel_step))
        if self._wheel_job:
            self.after_cancel(self._wheel_job)
        self._wheel_job = self.after(350, lambda: self.on_commit and self.on_commit(self.value))


class HoverButton(tk.Frame):
    """A flat button: icon (and text) on the opaque taskbar colour, lit on hover
    like the taskbar's own buttons. command() runs on a click released inside."""

    def __init__(self, master, pal, command, icon=None, icon_font=None, text=None, font=None, padx=8):
        super().__init__(master, bg=pal.solid, cursor="hand2")
        self.pal, self.command = pal, command
        self.parts = []
        if icon is not None:
            self.parts.append(tk.Label(self, text=icon, font=icon_font, bg=pal.solid, fg=FG, bd=0))
        if text is not None:
            self.parts.append(tk.Label(self, text=text, font=font, bg=pal.solid, fg=FG, bd=0))
        for i, p in enumerate(self.parts):
            p.pack(side="left", pady=4, padx=(padx if i == 0 else 4, padx if i == len(self.parts) - 1 else 0))
        for w in [self] + self.parts:
            w.bind("<Enter>", lambda e: self._paint(self.pal.hover))
            w.bind("<Leave>", self._leave)
            w.bind("<ButtonPress-1>", lambda e: self._paint(self.pal.press))
            w.bind("<ButtonRelease-1>", self._release)

    def _inside(self):
        x, y = self.winfo_pointerxy()
        w = self.winfo_containing(x, y)
        return w is self or w in self.parts

    def _paint(self, c):
        self.config(bg=c)
        for p in self.parts:
            p.config(bg=c)

    def _leave(self, e):
        if not self._inside():                 # moving between the button's own labels is not leaving
            self._paint(self.pal.solid)

    def _release(self, e):
        inside = self._inside()
        self._paint(self.pal.hover if inside else self.pal.solid)
        if inside:
            self.command()

    def set_icon(self, icon):
        if self.parts and self.parts[0].cget("text") != icon:
            self.parts[0].config(text=icon)

    def recolor(self, pal):
        self.pal = pal
        self._paint(pal.solid)


class AudioMenu:
    """The audio button's flyout: pick the output and the input device. A muted mic is
    marked (it records silence), and picking one unmutes it."""

    def __init__(self, strip):
        self.strip, self.app = strip, strip.app
        self.top = None
        self.closed_at = 0.0

    def toggle(self, anchor):
        if self.top is not None:
            self.close()
            return
        if time.monotonic() - self.closed_at < 0.4:
            return                              # the click on the button is what just closed it (focus loss)
        self.open(anchor)

    def open(self, anchor):
        audio = getattr(self.app, "audio", None)
        snap = audio.devices() if audio is not None else {}
        outs = [d for d in snap.get("outputs", []) if d.get("state") == 1]
        ins = [d for d in snap.get("inputs", []) if d.get("state") == 1]
        top = tk.Toplevel(self.app.root, bg=POP_BORDER)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        body = tk.Frame(top, bg=POP_BG)
        body.pack(padx=1, pady=1)
        self.rows = []
        self._section(body, "Output", outs, snap.get("out_default"), "default")
        self._section(body, "Input", ins, snap.get("in_default"), "default_in")
        if not outs and not ins:
            tk.Label(body, text="No audio devices found", bg=POP_BG, fg=POP_SUB, font=self.strip.fonts["text"],
                     padx=14, pady=10).pack()
        tk.Frame(body, bg=POP_BG, height=6).pack()
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = max(0, min(anchor.winfo_rootx() - 6, top.winfo_screenwidth() - w))
        y = self.strip.win.winfo_rooty() - h - 6
        top.geometry("+%d+%d" % (x, y))
        top.bind("<FocusOut>", lambda e: top.after(80, self._maybe_close))
        top.bind("<Escape>", lambda e: self.close())
        self.top = top
        top.after(20, top.focus_force)

    def _section(self, body, title, devs, current, action):
        F = self.strip.fonts
        tk.Label(body, text=title, bg=POP_BG, fg=POP_SUB, font=F["small"], anchor="w").pack(
            fill="x", padx=14, pady=(10, 3))
        for d in devs:
            row = tk.Frame(body, bg=POP_BG, cursor="hand2")
            row.pack(fill="x", padx=4)
            mark = tk.Label(row, text=self.strip.icons["check"] if d.get("id") == current else "", width=2,
                            font=F["icon_small"], fg=ACCENT, bg=POP_BG)
            name = tk.Label(row, text=tidy(d.get("name", "?")), font=F["text"], fg=FG, bg=POP_BG, anchor="w")
            parts = [row, mark, name]
            mark.pack(side="left", padx=(6, 4), pady=5)
            if action == "default_in" and d.get("muted"):
                tag = tk.Label(row, text="Muted", font=F["small"], fg=MUTED_FG, bg=POP_BG)
                tag.pack(side="right", padx=(0, 14), pady=5)
                parts.append(tag)
            name.pack(side="left", padx=(0, 16), pady=5, fill="x", expand=True)
            for wdg in parts:
                wdg.bind("<Enter>", lambda e, r=row: self._hover(r, True))
                wdg.bind("<Leave>", lambda e, r=row: self._hover(r, False))
                wdg.bind("<ButtonRelease-1>", lambda e, a=action, i=d.get("id"): self._pick(a, i))
            self.rows.append((action, d.get("id"), row))

    @staticmethod
    def _hover(row, on):
        c = POP_HOVER if on else POP_BG
        row.config(bg=c)
        for ch in row.winfo_children():
            ch.config(bg=c)

    def _pick(self, action, did):
        try:
            self.app.audio.post(action, did)
            log.info("audio %s -> %s", "output" if action == "default" else "input", did)
        except Exception:
            log.exception("audio pick")
        self.close()
        self.strip.win.after(900, self.strip.refresh_audio_icon)

    def _maybe_close(self):
        if self.top is None:
            return
        try:
            f = self.top.focus_get()
        except Exception:
            f = None
        if f is None or not str(f).startswith(str(self.top)):
            self.close()

    def close(self):
        if self.top is not None:
            try:
                self.top.destroy()
            except Exception:
                pass
            self.top = None
            self.closed_at = time.monotonic()


# -- the strip ---------------------------------------------------------------------------
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
        self._ticks = 0
        tray = self._tray()
        r = wt.RECT()
        x0 = (r.left if tray and u32.GetWindowRect(tray, ctypes.byref(r)) else 0) + int(self.cfg.get("x_offset", 0))
        base = sample_taskbar(tray, x0 + 10, x0 + int(self.cfg.get("width", 660)))
        self.sampled = base is not None
        self._matched = False
        self.pal = Palette(base or DEFAULT_TASKBAR)
        fams = set(tkfont.families(app.root))
        ifont = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
        self.icons = GLYPH if ifont else PLAIN
        self.fonts = {"text": ("Segoe UI", 9), "small": ("Segoe UI", 8), "mono": ("Consolas", 9),
                      "icon": (ifont or "Segoe UI Symbol", 11), "icon_small": (ifont or "Segoe UI Symbol", 8)}
        self.win = tk.Toplevel(app.root, bg=self.pal.key)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", self.pal.key)
        self.audio_menu = AudioMenu(self)
        self._build()
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
    def _lab(self, parent, text, font, **kw):
        lab = tk.Label(parent, text=text, font=font, bg=self.pal.key, fg=FG, bd=0, **kw)
        self._keyed.append(lab)
        return lab

    def _build(self):
        P, F, I = self.pal, self.fonts, self.icons
        f = tk.Frame(self.win, bg=P.key)
        f.pack(fill="both", expand=True, padx=4)
        self._keyed = [f]
        self.sleep_btn = HoverButton(f, P, lambda: self.app.post(self.app.sleep), icon=I["sleep"],
                                     icon_font=F["icon"], text="Sleep", font=F["text"])
        self.sleep_btn.pack(side="left", padx=(0, 12))
        self._lab(f, I["sun"], F["icon"]).pack(side="left", padx=(0, 4))
        self.bright = MiniSlider(f, P, 112, 28, 0, 100, 50, on_change=self._on_bright, on_commit=self._commit_bright)
        self.bright.pack(side="left")
        self.bright_lab = self._lab(f, "--%", F["text"], width=4)
        self.bright_lab.pack(side="left", padx=(2, 12))
        self._lab(f, I["bolt"], F["icon"]).pack(side="left", padx=(0, 4))
        self.power = MiniSlider(f, P, 96, 28, 0, 4, 0, steps=True, on_change=self._on_power,
                                on_commit=self._commit_power)
        self.power.pack(side="left")
        self.power_lab = self._lab(f, "Auto", F["text"], width=10, anchor="w")
        self.power_lab.pack(side="left", padx=(4, 8))
        self.audio_btn = HoverButton(f, P, lambda: self.audio_menu.toggle(self.audio_btn), icon=I["speaker"],
                                     icon_font=F["icon"], text=I["down"], font=F["icon_small"])
        self.audio_btn.pack(side="left", padx=(0, 12))
        self.stats = self._lab(f, "", F["mono"], anchor="w")
        self.stats.pack(side="left", fill="x", expand=True)
        self._solids = [self.sleep_btn, self.bright, self.power, self.audio_btn]
        self.refresh_brightness()
        self.refresh_audio_icon()

    def recolor(self, pal):
        self.pal = pal
        self.win.config(bg=pal.key)
        self.win.attributes("-transparentcolor", pal.key)
        for w in self._keyed:
            w.config(bg=pal.key)
        for w in (self.sleep_btn, self.audio_btn, self.bright, self.power):
            w.recolor(Palette(pal.base, avoid=pal.key))

    # brightness: the label follows the drag, the monitors are written once on release
    def _on_bright(self, v):
        self.bright_lab.config(text="%d%%" % v)

    def _commit_bright(self, v):
        self.app.post(lambda: self.app.set_brightness_all(int(v)))

    def refresh_brightness(self):
        """Show the level the app last set (sun sync and hotkeys move it too)."""
        try:
            lvl = self.app.brightness_level()
        except Exception:
            vals = [v for v in list(self.app.last.values()) if isinstance(v, (int, float))]
            lvl = round(sum(vals) / len(vals)) if vals else None
        if lvl is not None and not self.bright.dragging:
            self.bright.set(lvl)
            self.bright_lab.config(text="%d%%" % self.bright.value)

    # power: snaps to a detent, applied on release
    def _on_power(self, v):
        self.power_lab.config(text=self.MODES[int(v)].capitalize())

    def _commit_power(self, v):
        self.app.post(lambda m=self.MODES[int(v)]: self.app.set_power_mode(m))

    # audio: the button shows what is playing to (headset / speakers / muted)
    def refresh_audio_icon(self):
        try:
            snap = self.app.audio.devices()
        except Exception:
            return
        name = next((d.get("name", "") for d in snap.get("outputs", []) if d.get("id") == snap.get("out_default")), "")
        if snap.get("muted"):
            icon = self.icons["mute"]
        elif any(w in name.lower() for w in HEADSET_WORDS):
            icon = self.icons["headphone"]
        else:
            icon = self.icons["speaker"]
        self.audio_btn.set_icon(icon)

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
            geom = "%dx%d+%d+%d" % (max(int(self.cfg.get("width", 660)), self.win.winfo_reqwidth()), r.bottom - r.top,
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
        self._ticks += 1
        try:
            if self.shown:
                st = self.app.engine.snapshot()
                mode = self.app.cfg["power"].get("mode", "auto")
                i = self.MODES.index(mode) if mode in self.MODES else 0
                if not self.power.dragging:
                    self.power.set(i)
                    prof = st.get("profile") or "-"
                    self.power_lab.config(text=("Auto·%s" % prof[:4]) if mode == "auto" else mode.capitalize())
                bits = []
                if st.get("cpu") is not None:
                    bits.append("CPU%3.0f%%" % st["cpu"] + (" %2.0f°" % st["cpu_temp"] if st.get("cpu_temp") else ""))
                if st.get("mem") is not None:
                    bits.append("RAM%3.0f%%" % st["mem"])
                if st.get("gpu_util") is not None:
                    bits.append("GPU%3.0f%%" % st["gpu_util"])
                if st.get("agent_n") and (st.get("agent_cpu") or 0) >= 2:       # AI agents' share; the arrow = held back
                    bits.append("AI%3.0f%%" % st["agent_cpu"] + ("↓" if st.get("agent_cap") else ""))
                if st.get("thermal"):
                    bits.append("⚠")
                self.stats.config(text="  ".join(bits))
                self.refresh_audio_icon()
                if self._ticks % 5 == 0:
                    self.refresh_brightness()
                if not self._matched or self._ticks % 30 == 0:
                    self._match_local()
        except Exception:
            log.exception("strip stats")
        self.win.after(2000, self._stats_tick)

    def _match_local(self):
        """Give each solid control the colour of the taskbar right beside it. An
        acrylic taskbar is not one colour - it shows the wallpaper; here it runs
        from #2c2c2c under the strip to #212120 by the Start button - and a
        control on the wrong shade shows as a box. Read from the see-through gap
        just left of each control, all in one screen grab; the colour key itself
        follows when the taskbar as a whole changes (theme, wallpaper)."""
        r = wt.RECT()
        if not self.shown or not u32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            return
        try:
            img = grab(r.left, r.top, r.right, r.bottom)
        except Exception:
            return
        self._matched = True
        local = []
        for c in self._solids:
            x = c.winfo_rootx() - r.left - 3
            top, h = c.winfo_rooty() - r.top, c.winfo_height()
            ys = [top + 2 + i * max(1, h - 5) // 4 for i in range(5)]     # the taskbar shades top to bottom too:
            pts = [img.getpixel((x, y)) for y in ys if 0 <= x < img.width and 0 <= y < img.height]
            local.append(tuple(round(sum(p[i] for p in pts) / len(pts)) for i in range(3)) if pts else None)  # its mean
        seen = [l for l in local if l]
        overall = median_rgb(seen)
        if overall and self.pal.differs(overall, tol=10):
            log.info("taskbar colour %s -> %s: strip recoloured", hexc(self.pal.base), hexc(overall))
            self.recolor(Palette(overall))
        for c, l in zip(self._solids, local):
            if l and c.pal.differs(l, tol=1):
                c.recolor(Palette(l, avoid=self.pal.key))

    def alive(self):
        try:
            return not self._closed and bool(self.win.winfo_exists())
        except Exception:
            return False

    def destroy(self):
        self._closed = True
        self.audio_menu.close()
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
