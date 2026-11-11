"""Lights Out - tray app for the desk.

* Brightness sliders per monitor (real backlight; SDR white level while HDR is on)
  with sun-synced dimming (sunrise/sunset from your location, strength knob).
* Sleep mode that only wakes on two key presses within five seconds and
  comes back to the lock screen. Never Windows sleep.
* Power engine: watches what you are doing and picks quiet / balanced /
  performance / max (overclock tier with a thermal guard), slows background
  apps while something heavy is in front, and talks to the elevated helper
  for firmware controls (ASUS ROG today, RyzenAdj / Afterburner optional).
* Security assessment from local tools + AppControl's catalogue, written up
  by Claude (CLI account or API key) when configured.

Runs windowless via pythonw or as LightsOut.exe. Runtime data (config, logs,
reports) lives in %LOCALAPPDATA%\\LightsOut so the install folder stays clean.
"""
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import logging
import logging.handlers
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
sys.path.insert(0, APP_DIR)
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "LightsOut")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "lightsout.log")
SIGNAL_PATH = os.path.join(DATA_DIR, ".open-panel")
SLEEP_SIGNAL_PATH = os.path.join(DATA_DIR, ".sleep-now")   # drop this file to put the screens to sleep
STATE_PATH = os.path.join(DATA_DIR, "state.json")
GEO_PATH = os.path.join(DATA_DIR, "location.json")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
CRASH_PATH = os.path.join(DATA_DIR, "crash.log")        # faulthandler: hard crashes + hang dumps
HEARTBEAT_PATH = os.path.join(DATA_DIR, "heartbeat")    # touched by the UI thread every 2 s while asleep
SENTINEL_PATH = os.path.join(DATA_DIR, "asleep.json")   # exists only while asleep: what to put back after a crash
_crash_file = None

def _trace(msg):
    """Startup breadcrumbs that need nothing but the data folder: if the app
    dies before logging is up (it vanished at one logon), this still shows how far it got."""
    try:
        with open(os.path.join(DATA_DIR, "startup.log"), "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} pid {os.getpid()} {msg}\n")
    except OSError:
        pass


_trace(f"launch argv={sys.argv[1:]} frozen={FROZEN}")
try:
    from power import engine as E          # noqa: E402
    import claude_bridge as CB             # noqa: E402
except Exception:
    import traceback
    _trace("import failed:\n" + traceback.format_exc())
    raise

DEFAULTS = {
    "presses": 2, "window_seconds": 5, "lock_on_sleep": True, "lock_on_wake": True, "relock_minutes": 3,
    "wake_on_click": True, "fingerprint_wake": True, "fingerprint_unlocks": False,
    "dim_on_sleep": True, "ddc_standby": True, "hide_taskbar_asleep": True, "detach_no_standby": True,
    "hotkeys": {"sleep": "ctrl+alt+s", "panel": "ctrl+alt+b",
                "brightness_up": "ctrl+alt+up", "brightness_down": "ctrl+alt+down"},
    "brightness_step": 10,
    "sun": {"enabled": False, "strength": 60, "transition_min": 45, "lat": None, "lon": None, "label": ""},
    "audio": {"show_in_panel": True, "auto_switch": True, "headphone_id": "", "speaker_id": "",
              "switch_mic": False, "mic_on_headphones_id": "", "mic_on_speakers_id": "", "poll_seconds": 3},
    "remote": {"enabled": True, "wide_to_16x9": True, "width": 0, "height": 0, "lock_on_physical": True},
    "taskbar_strip": {"enabled": True, "width": 660, "x_offset": 0},
    "power": json.loads(json.dumps(E.DEFAULTS)),
    "claude": dict(CB.DEFAULTS),
    "security": {"weekly": True, "last_run": "", "last_summary": ""},
}
HDR_CALIBRATION = r"shell:AppsFolder\MicrosoftCorporationII.WindowsHDRCalibration_8wekyb3d8bbwe!App"

log = logging.getLogger("lightsout")


def setup_logging():
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=600_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("lightsout").setLevel(logging.DEBUG)
    if sys.stdout is not None and sys.stdout.isatty():
        logging.getLogger().addHandler(logging.StreamHandler())
    # hard crashes (access violations) and hang dumps go here with every thread's stack
    try:
        import faulthandler
        global _crash_file
        _crash_file = open(CRASH_PATH, "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception:
        log.exception("faulthandler")


def _merge(base, override):
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    old = os.path.join(APP_DIR, "config.json")
    src = CONFIG_PATH if os.path.exists(CONFIG_PATH) else (old if os.path.exists(old) else None)
    saved = {}
    if src:
        try:
            with open(src, encoding="utf-8") as f:
                saved = json.load(f)
            _merge(cfg, saved)
        except Exception:
            log.exception("config unreadable, using defaults")
    cfg.pop("schedule", None)                      # replaced by sun sync
    # Migration: a config written before the adaptive budget stored a low flat cap
    # (old default 20). Left as-is it would throttle the new scaling budget, so lift
    # it to the new default once, when upgrading.
    cl = cfg.get("claude", {})
    if "rate_adaptive" not in saved.get("claude", {}) and int(cl.get("daily_call_cap", 0)) <= 20:
        cl["daily_call_cap"] = CB.DEFAULTS["daily_call_cap"]
    save_config(cfg)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

import monitors as mon         # noqa: E402
import sun                     # noqa: E402
import security                # noqa: E402
import fingerprint             # noqa: E402
import topology                # noqa: E402
import audio                   # noqa: E402
import remote                  # noqa: E402
from gate import WakeGate, PhysicalKeyWatcher, session_locked  # noqa: E402

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
BG, PANEL, FG, DIM, ACCENT = "#101114", "#1a1c21", "#e8e8ea", "#8a8d96", "#f2c14e"
PROFILE_LABEL = {"auto": "Auto", "quiet": "Quiet", "balanced": "Balanced", "performance": "Perf", "max": "Max"}
SUN_DAY_FLOOR = 10      # the sun curve never anchors a "daytime" level below this


def make_icon_image(asleep=False):
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, s - 3, s - 3], radius=14, fill="#2b2d33" if asleep else "#f2c14e")
    col = "#8a8d96" if asleep else "#101114"
    d.ellipse([18, 10, 46, 38], outline=col, width=5)
    d.rectangle([24, 40, 40, 46], fill=col)
    d.rectangle([27, 48, 37, 53], fill=col)
    return img


# -- brightness abstraction ---------------------------------------------------------
def level_get(m):
    if m.hdr:
        nits = m.sdr_white_nits()
        if nits is not None:
            return round((nits - mon.SDR_MIN_NITS) * 100 / (mon.SDR_MAX_NITS - mon.SDR_MIN_NITS))
    return m.brightness


def level_set(m, pct):
    pct = int(max(0, min(100, pct)))
    if m.hdr:
        return m.set_sdr_white_nits(mon.SDR_MIN_NITS + pct * (mon.SDR_MAX_NITS - mon.SDR_MIN_NITS) / 100)
    if m.ddc or m.wmi:
        return m.set(m.min + pct * (m.max - m.min) / 100)
    return False


def level_kind(m):
    return "SDR content brightness (HDR on)" if m.hdr else ("brightness" if (m.ddc or m.wmi) else "no brightness control")


def controllable(m):
    return m.ddc or m.wmi or m.hdr


class LatestWriter(threading.Thread):
    """Latest-value-wins writer so slider drags never queue up DDC calls."""

    def __init__(self):
        super().__init__(name="writer", daemon=True)
        self.pending, self.ev, self.lock = {}, threading.Event(), threading.Lock()
        self.start()

    def submit(self, key, fn):
        with self.lock:
            self.pending[key] = fn
        self.ev.set()

    def run(self):
        while True:
            self.ev.wait()
            self.ev.clear()
            while True:
                with self.lock:
                    if not self.pending:
                        break
                    _, fn = self.pending.popitem()
                try:
                    fn()
                except Exception:
                    log.exception("writer")


# -- global hotkeys ---------------------------------------------------------------------
VK = {"up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "space": 0x20, "pause": 0x13, "home": 0x24,
      "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "delete": 0x2E, "esc": 0x1B, "tab": 0x09,
      "enter": 0x0D, "backspace": 0x08, "scrolllock": 0x91, "printscreen": 0x2C}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i
MODS = {"alt": 1, "ctrl": 2, "shift": 4, "win": 8}


def parse_hotkey(text):
    parts = [p.strip().lower() for p in (text or "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    key = parts[-1]
    vk = VK.get(key) if key in VK else (ord(key.upper()) if len(key) == 1 and key.isalnum() else None)
    return (mods | 0x4000, vk) if vk else None


class Hotkeys(threading.Thread):
    def __init__(self, on_hotkey):
        super().__init__(name="hotkeys", daemon=True)
        self.on_hotkey, self.bindings, self.tid, self.failed, self._pending = on_hotkey, {}, None, [], None
        self._ready = threading.Event()
        self.start()
        self._ready.wait(2)

    def apply(self, mapping):
        self._pending = dict(mapping)
        if self.tid:
            user32.PostThreadMessageW(self.tid, 0x0400, 0, 0)

    def _register(self):
        for i in list(self.bindings):
            user32.UnregisterHotKey(None, i)
        self.bindings, self.failed = {}, []
        for i, (name, text) in enumerate(self._pending.items(), start=1):
            hk = parse_hotkey(text)
            if not hk:
                if text:
                    self.failed.append(f"{name}: '{text}' not understood")
                continue
            if user32.RegisterHotKey(None, i, hk[0], hk[1]):
                self.bindings[i] = name
            else:
                self.failed.append(f"{name}: '{text}' is taken by another app")
        log.info("hotkeys: %s%s", {v: self._pending[v] for v in self.bindings.values()},
                 f" failed={self.failed}" if self.failed else "")

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0x0400, 0x0400, 0)   # create the queue first
        self._ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:
                name = self.bindings.get(msg.wParam)
                if name:
                    self.on_hotkey(name)
            elif msg.message == 0x0400 and self._pending is not None:
                self._register()


# -- taskbar hide while asleep ----------------------------------------------------------
def taskbar_windows():
    out = []
    buf = ctypes.create_unicode_buffer(64)

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            out.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return out


class TaskbarStrip:
    """A thin always-on-top control bar docked over the left end of the taskbar:
    sleep button, one brightness slider for every monitor, a power-mode slider
    and live stats. Windows 11 has no API to put controls inside the taskbar, so
    this is a borderless, no-activate tool window kept at the taskbar's left edge
    (empty on a centred taskbar) and held just above it. Lightweight: it reuses
    the engine's 2 s snapshot and writes brightness only when a drag ends."""

    _MODES = ["auto", "quiet", "balanced", "performance", "max"]
    GA_ROOT = 2
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
    HWND_TOPMOST = -1
    SWP = 0x1 | 0x2 | 0x10        # NOSIZE | NOMOVE | NOACTIVATE

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg.get("taskbar_strip", {})
        self._hwnd = None
        self._last_geom = None
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
        self._style_window()
        self._reposition()
        self._tick()

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
        self.bright_scale = ttk.Scale(f, from_=0, to=100, variable=self.bright, length=110,
                                      command=self._on_bright)
        self.bright_scale.pack(side="left", padx=(3, 3))
        self.bright_scale.bind("<ButtonRelease-1>", self._commit_bright)
        self.bright_lab = tk.Label(f, text="--%", bg=BG, fg=FG, width=4, font=("Segoe UI", 9))
        self.bright_lab.pack(side="left", padx=(0, 10))
        tk.Label(f, text="⚡", bg=BG, fg=DIM, font=("Segoe UI", 11)).pack(side="left")
        self.power = tk.DoubleVar(value=0)
        self.power_scale = ttk.Scale(f, from_=0, to=4, variable=self.power, length=96,
                                     command=self._on_power)
        self.power_scale.pack(side="left", padx=(3, 3))
        self.power_scale.bind("<ButtonRelease-1>", self._commit_power)
        self.power_lab = tk.Label(f, text="Auto", bg=BG, fg=FG, width=10, anchor="w", font=("Segoe UI", 9))
        self.power_lab.pack(side="left", padx=(0, 10))
        self.stats = tk.Label(f, text="", bg=BG, fg="#9aa0ad", anchor="w", font=("Consolas", 9))
        self.stats.pack(side="left", fill="x", expand=True)

    # -- brightness (write only on release; label tracks the drag) --------------
    def _on_bright(self, _=None):
        self.bright_lab.config(text=f"{int(float(self.bright.get()))}%")

    def _commit_bright(self, _=None):
        v = int(float(self.bright.get()))
        self.app.post(lambda: self.app.set_brightness_all(v))

    # -- power mode (snap to a detent; apply on release) ------------------------
    def _on_power(self, _=None):
        i = max(0, min(4, int(round(float(self.power.get())))))
        self.power_lab.config(text=self._MODES[i].capitalize())

    def _commit_power(self, _=None):
        i = max(0, min(4, int(round(float(self.power.get())))))
        self.power.set(i)
        self.app.post(lambda m=self._MODES[i]: self.app.set_power_mode(m))

    # -- window placement -------------------------------------------------------
    def _style_window(self):
        try:
            self._hwnd = user32.GetAncestor(self.win.winfo_id(), self.GA_ROOT)
            ex = user32.GetWindowLongW(self._hwnd, self.GWL_EXSTYLE)
            user32.SetWindowLongW(self._hwnd, self.GWL_EXSTYLE,
                                  ex | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE)
            user32.SetWindowPos(self._hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, self.SWP)
        except Exception:
            log.exception("strip style")

    def _reposition(self):
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        r = wt.RECT()
        if tray and user32.GetWindowRect(tray, ctypes.byref(r)):
            h = r.bottom - r.top
            w = int(self.cfg.get("width", 520))
            x = r.left + int(self.cfg.get("x_offset", 0))
            geom = f"{w}x{h}+{x}+{r.top}"
            if geom != self._last_geom:
                self.win.geometry(geom)
                self._last_geom = geom

    def _tick(self):
        if not self.win.winfo_exists():
            return
        self._reposition()
        if self._hwnd:
            try:
                user32.SetWindowPos(self._hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, self.SWP)
            except Exception:
                pass
        try:
            st = self.app.engine.snapshot()
            mode = self.app.cfg["power"].get("mode", "auto")
            i = self._MODES.index(mode) if mode in self._MODES else 0
            if int(round(float(self.power.get()))) != i:
                self.power.set(i)
            prof = (st.get("profile") or "-")
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
            pass
        self.win.after(2000, self._tick)

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class App:
    def __init__(self, selftest=False):
        self.cfg = load_config()
        self.selftest = selftest
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Lights Out")
        self.q = queue.Queue()
        self.monitors, self.rows, self.overlays = [], [], []
        self.panel = self.settings_win = self.report_win = None
        self.asleep = False
        self.saved = {}
        self.topo = None            # display layout at sleep, put back on wake
        self.detached = set()       # (adapter lo, hi, target) disconnected to turn a monitor off
        self.standby_targets = set()  # (adapter lo, hi, target) put into DDC/CI standby (cycled on wake)
        self.dark_plan = {}
        self._dark = False
        self._restore_thread = None
        self.hidden_taskbars = []
        self.writer = LatestWriter()
        self.gate = WakeGate(int(self.cfg["presses"]), float(self.cfg["window_seconds"]), clicks=bool(self.cfg.get("wake_on_click", True)))
        self.fp = fingerprint.FingerprintWatcher()
        self.crd = remote.CrdWatcher()            # Chrome Remote Desktop connect/disconnect while asleep
        self.pkw = PhysicalKeyWatcher()           # a real keystroke taking over a remote session
        self.remote_active = False                # a remote-desktop session is driving the main monitor
        self._remote_main = None                  # device name of the monitor brought up for it
        self._remote_resized = False              # its resolution was switched to 16:9
        self._remote_lock = threading.Lock()      # serialise enter/exit display work
        self._wake_lock = threading.Lock()
        self._wake_done = False
        self.last_wake_why = ""
        self.icon = None
        self.link = tk.BooleanVar(value=True)
        self.last = self._load_json(STATE_PATH, {})       # device -> last slider level
        self.sun_day = self.last.get("_sun_day", {})      # device -> day level the sun curve scales
        self.sun_info = ""
        self.claude = CB.Claude(self.cfg["claude"], DATA_DIR)
        self.engine = E.Engine(self.cfg["power"], APP_DIR, DATA_DIR, on_change=self._on_profile)
        self.audio = audio.AudioHub(self.cfg["audio"])
        self._audio_dragging = False
        self._audio_sig = None            # last device set drawn in the panel, to avoid needless rebuilds
        self.security_busy = False
        self.strip = None
        self._style()
        self._recover_from_crash()            # a previous instance died while asleep?
        self._hb = time.monotonic()           # UI-thread heartbeat for the in-process hang detector
        threading.Thread(target=self._hang_detector, name="hang-detector", daemon=True).start()
        self.hotkeys = Hotkeys(lambda name: self.post(self.on_hotkey, name))
        self.hotkeys.apply(self.cfg["hotkeys"])
        self.engine.start()
        self.audio.start()
        self.root.after(100, self._pump)
        self.root.after(500, self._poll_signal)
        self.root.after(2500, self._audio_prefill)
        self.root.after(3000, self._sun_tick)
        self.root.after(30_000, self._context_tick)
        self.root.after(90_000, self._security_tick)
        self.root.after(600_000, self._classify_tick)
        self._start_tray()
        self.root.after(1500, self._strip_watchdog)

    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    # -- plumbing ---------------------------------------------------------------
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=PANEL, foreground=FG, font=("Segoe UI", 10), fieldbackground="#2a2d35",
                     insertcolor=FG, selectbackground="#3b4252", bordercolor="#2a2d35", lightcolor=PANEL, darkcolor=PANEL)
        for name, kw in {
            "TFrame": dict(background=PANEL), "TLabel": dict(background=PANEL, foreground=FG),
            "Dim.TLabel": dict(foreground=DIM, font=("Segoe UI", 9)),
            "Title.TLabel": dict(font=("Segoe UI Semibold", 12)),
            "Section.TLabel": dict(foreground=ACCENT, font=("Segoe UI Semibold", 10)),
            "Tag.TLabel": dict(background="#2d3a4a", foreground="#9ecbff", font=("Segoe UI", 8, "bold")),
            "Warn.TLabel": dict(foreground="#ffb86b"),
            "Horizontal.TScale": dict(troughcolor="#2a2d35", sliderlength=22),
            "TCheckbutton": dict(background=PANEL, foreground=FG),
            "Sleep.TButton": dict(background=ACCENT, foreground="#101114", font=("Segoe UI Semibold", 11), padding=(14, 8), borderwidth=0),
            "Flat.TButton": dict(background="#2a2d35", foreground=FG, padding=(10, 5), borderwidth=0),
            "Mode.TButton": dict(background="#2a2d35", foreground=FG, padding=(8, 4), borderwidth=0, font=("Segoe UI", 9)),
            "ModeOn.TButton": dict(background=ACCENT, foreground="#101114", padding=(8, 4), borderwidth=0, font=("Segoe UI Semibold", 9)),
            "TEntry": dict(foreground=FG), "TSpinbox": dict(foreground=FG, arrowcolor=FG),
            "TCombobox": dict(foreground=FG, arrowcolor=FG),
            "TNotebook": dict(background=PANEL, borderwidth=0), "TNotebook.Tab": dict(background="#2a2d35", foreground=FG, padding=(12, 6)),
            "Horizontal.TProgressbar": dict(troughcolor="#2a2d35", background=ACCENT, borderwidth=0),
        }.items():
            st.configure(name, **kw)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.map("Sleep.TButton", background=[("active", "#ffd36b")])
        st.map("Flat.TButton", background=[("active", "#363a44")])
        st.map("Mode.TButton", background=[("active", "#363a44")])
        st.map("TNotebook.Tab", background=[("selected", PANEL)])
        st.map("TCombobox", fieldbackground=[("readonly", "#2a2d35")])
        self.root.option_add("*TCombobox*Listbox.background", "#2a2d35")
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

    def post(self, fn, *a):
        self.q.put((fn, a))

    def _pump(self):
        self._hb = time.monotonic()
        # faulthandler's own C watchdog: if this thread stops pumping for 40 s it
        # dumps every thread's stack without needing the GIL (the Python-level
        # hang detector could not run during the second hang)
        if time.monotonic() - getattr(self, "_fh_armed", 0) > 10:
            self._fh_armed = time.monotonic()
            try:
                import faulthandler
                faulthandler.dump_traceback_later(40, repeat=False, file=_crash_file)
            except Exception:
                pass
        try:
            while True:
                fn, a = self.q.get_nowait()
                try:
                    fn(*a)
                except Exception:
                    log.exception("ui task")
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _hang_detector(self):
        """If the UI thread stops pumping for 20 s, dump every thread's stack to
        crash.log so the next hang is diagnosable, and undo sleep's side
        effects from here (the helper's watchdog does the same from outside)."""
        import faulthandler
        dumped = False
        while True:
            time.sleep(5)
            stale = time.monotonic() - self._hb
            if stale < 20:
                dumped = False
                continue
            if not dumped:
                log.error("UI thread unresponsive for %.0f s: dumping stacks to crash.log", stale)
                try:
                    _crash_file.write(f"\n=== UI thread hang {dt.datetime.now():%Y-%m-%d %H:%M:%S}, {stale:.0f}s ===\n")
                    _crash_file.flush()
                    faulthandler.dump_traceback(file=_crash_file, all_threads=True)
                    _crash_file.flush()
                except Exception:
                    pass
                dumped = True
                if self.asleep:
                    user32.LockWorkStation()          # the gate can no longer be trusted: Windows takes over
                    self.wake("ui thread hung")       # the whole wake path runs on worker threads

    def _recover_from_crash(self):
        """Runs at startup. If asleep.json is there, the last instance died in
        sleep mode: show the taskbar, wake the monitors, put brightness back."""
        for h in taskbar_windows():
            if not user32.IsWindowVisible(h):
                user32.ShowWindow(h, 5)
                log.warning("taskbar was hidden at startup: shown")
        if not os.path.exists(SENTINEL_PATH):
            return
        try:
            with open(SENTINEL_PATH, encoding="utf-8") as f:
                st = json.load(f)
            os.remove(SENTINEL_PATH)
            log.warning("previous instance died while asleep (since %s): restoring monitors", st.get("since"))
            topo = topology.Snapshot.from_json(st.get("topology"))
            if topo is not None:
                standby = st.get("standby") or []
                ok = topology.wake_standby(topo, standby) if standby else topology.restore(topo)
                log.warning("displays restored (standby %s): %s", standby, ok)
            mons = mon.enumerate_monitors(probe_wmi=False)
            for m in mons:
                m.power(True)
            threading.Thread(target=self._restore, args=(mons, st.get("saved", {})), daemon=True).start()
        except Exception:
            log.exception("crash recovery")

    def _poll_signal(self):
        if os.path.exists(SIGNAL_PATH):
            try:
                os.remove(SIGNAL_PATH)
            except OSError:
                pass
            if not self.asleep:
                self.show_panel()
        if os.path.exists(SLEEP_SIGNAL_PATH):
            try:
                os.remove(SLEEP_SIGNAL_PATH)
            except OSError:
                pass
            if not self.asleep:
                log.info("sleep requested via signal file")
                self.sleep()
        self.root.after(500, self._poll_signal)

    def _start_tray(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem("Open", lambda: self.post(self.show_panel), default=True),
            pystray.MenuItem("Sleep now", lambda: self.post(self.sleep)),
            pystray.MenuItem("Power", pystray.Menu(
                *[pystray.MenuItem(PROFILE_LABEL[m], (lambda m=m: lambda: self.post(self.set_power_mode, m))(),
                                   checked=(lambda m=m: lambda item: self.cfg["power"].get("mode") == m)(), radio=True)
                  for m in ("auto", "quiet", "balanced", "performance", "max")])),
            pystray.MenuItem("Security check now", lambda: self.post(self.run_security)),
            pystray.MenuItem("Taskbar controls", lambda: self.post(self.toggle_strip),
                             checked=lambda item: self.strip is not None),
            pystray.MenuItem("Settings", lambda: self.post(self.show_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log", lambda: os.startfile(LOG_PATH)),
            pystray.MenuItem("Quit", lambda: self.post(self.quit)),
        )
        self.icon = pystray.Icon("LightsOut", make_icon_image(), "Lights Out", menu)

        def start():
            # at logon the taskbar can appear after us; an icon added before it exists is lost
            for _ in range(120):
                if user32.FindWindowW("Shell_TrayWnd", None):
                    break
                time.sleep(1)
            self.icon.run_detached()
            _trace("tray icon added")
        threading.Thread(target=start, name="tray-start", daemon=True).start()

    def _save_state(self):
        self.last["_sun_day"] = self.sun_day
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.last, f)
        except Exception:
            log.exception("state save")

    # -- hotkeys / power / periodic ----------------------------------------------
    def on_hotkey(self, name):
        log.info("hotkey %s", name)
        if name == "sleep":
            self.sleep()
        elif name == "panel":
            self.hide_panel() if (self.panel and self.panel.winfo_exists()) else self.show_panel()
        elif name in ("brightness_up", "brightness_down"):
            self.nudge_all(int(self.cfg.get("brightness_step", 10)) * (1 if name == "brightness_up" else -1))

    def set_power_mode(self, mode):
        self.engine.set_mode(mode)
        save_config(self.cfg)
        self._refresh_power_ui()
        log.info("power mode -> %s", mode)

    def _on_profile(self, profile, why):
        if self.icon:
            self.icon.title = f"Lights Out - {profile}: {why}"[:120]
        self._update_claude_load()

    # how busy the machine is, 0..1, so the Claude call budget can scale with it:
    # idle sits at the floor rate, a gaming / max session climbs to the ceiling.
    _PROFILE_LOAD = {"quiet": 0.0, "balanced": 0.2, "performance": 0.7, "max": 1.0}

    def _update_claude_load(self):
        try:
            st = self.engine.snapshot()
            load = max(self._PROFILE_LOAD.get(st.get("profile"), 0.0), (st.get("pressure") or 0) / 100.0)
            self.claude.set_load(load)
        except Exception:
            pass

    def _context_tick(self):
        try:
            self.engine.set_context(locked=bool(session_locked()))
        except Exception:
            pass
        self._update_claude_load()
        self.root.after(30_000, self._context_tick)

    def _classify_tick(self):
        """Ask Claude (cheaply, cached) about unknown heavy background apps so
        the throttler never slows something that matters."""
        self.root.after(600_000, self._classify_tick)
        if not self.cfg["power"].get("throttle_background"):
            return
        st = self.engine.snapshot()
        lists = self.cfg["power"].setdefault("lists", {"protected": [], "hogs": []})
        known = set(E.T.PROTECTED) | set(E.T.HOGS) | {n.lower() for n in lists.get("protected", []) + lists.get("hogs", [])}
        names = sorted({t[0] for t in st.get("top", []) if t[2] >= 3 and t[0] not in known and t[0] != st.get("fg")})[:8]
        if not names:
            return

        def work():
            res = self.claude.classify_processes(names)
            prot = [n for n, v in res.items() if v == "protect"]
            if prot:
                lists["protected"] = sorted(set(lists.get("protected", [])) | set(prot))
                save_config(self.cfg)
                log.info("Claude marked as protected: %s", prot)
        threading.Thread(target=work, daemon=True).start()

    def nudge_all(self, step):
        if self.asleep:
            return
        if self.panel and self.panel.winfo_exists():
            for m, val, lab, sc in self.rows:
                val.set(int(val.get()) + step)
                self._slide(m, val, lab, 1)
            return
        self.refresh_monitors(retries=1)
        for m in self.monitors:
            cur = level_get(m)
            cur = self.last.get(m.device, 50) if cur is None else cur
            new = int(max(0, min(100, cur + step)))
            self._set_level(m, new)
        self._save_state()

    def set_brightness_all(self, v):
        """Absolute brightness on every controllable monitor (the taskbar strip
        slider). Uses its own enumeration so it never needs the panel open."""
        v = int(max(0, min(100, v)))

        def work():
            try:
                mons = mon.enumerate_monitors(probe_wmi=False, retries=1)
            except Exception:
                log.exception("brightness-all enumerate")
                return
            fac = self._sun_factor() if self.cfg["sun"].get("enabled") else None
            for m in mons:
                try:
                    if m.ddc or m.wmi:
                        m.set(v)
                        self.last[m.device] = v
                        if fac is not None:
                            self.sun_day[m.device] = self._anchor_day(v, fac)
                finally:
                    m.close()
            self._save_state()
        threading.Thread(target=work, name="bright-all", daemon=True).start()

    def show_strip(self):
        if getattr(self, "strip", None) is not None and self.strip.win.winfo_exists():
            return
        log.info("show_strip: creating taskbar strip")
        try:
            self.strip = TaskbarStrip(self)
            log.info("taskbar strip shown")
        except Exception:
            log.exception("taskbar strip create failed")
            self.strip = None

    def _strip_watchdog(self):
        """Ensure the strip exists whenever it is enabled. The one-shot startup
        schedule can be starved by a busy UI thread at launch (monitor
        enumeration can stall it), so this re-creates the strip and heals it if
        it ever dies. Runs every 5 s; a no-op once the strip is up."""
        try:
            want = self.cfg.get("taskbar_strip", {}).get("enabled", True)
            alive = getattr(self, "strip", None) is not None and self.strip.win.winfo_exists()
            if want and not alive:
                self.show_strip()
            elif not want and alive:
                self.hide_strip()
        except Exception:
            log.exception("strip watchdog")
        self.root.after(5000, self._strip_watchdog)

    def hide_strip(self):
        if self.strip is not None:
            self.strip.destroy()
            self.strip = None

    def toggle_strip(self):
        on = not (self.strip is not None and self.strip.win.winfo_exists())
        self.hide_strip()
        if on:
            self.show_strip()
        self.cfg.setdefault("taskbar_strip", {})["enabled"] = on
        save_config(self.cfg)

    # -- sun sync -------------------------------------------------------------------
    def _sun_location(self):
        s = self.cfg["sun"]
        if s.get("lat") is None or s.get("lon") is None:
            lat, lon, label = sun.locate(GEO_PATH)
            if lat is not None:
                s["lat"], s["lon"], s["label"] = lat, lon, label
                save_config(self.cfg)
        return s.get("lat"), s.get("lon")

    def _sun_factor(self, now=None):
        s = self.cfg["sun"]
        lat, lon = self._sun_location()
        if lat is None:
            return 1.0
        return sun.brightness_factor(now or dt.datetime.now(), lat, lon, s.get("transition_min", 45), s.get("strength", 60) / 100)

    def _sun_tick(self):
        self.root.after(60_000, self._sun_tick)
        s = self.cfg["sun"]
        try:
            lat, lon = self._sun_location()
            if lat is not None:
                rise, sett = sun.local_sun_times(dt.date.today(), lat, lon)
                self.sun_info = (f"sunrise {rise:%H:%M}, sunset {sett:%H:%M}" if rise else "polar day/night") + \
                                (f" ({s.get('label')})" if s.get("label") else "")
            if not s.get("enabled") or self.asleep:
                return
            f = self._sun_factor()
            mons = mon.enumerate_monitors()
            for m in mons:
                if not controllable(m):
                    continue
                day = self.sun_day.get(m.device)
                if not day or day < SUN_DAY_FLOOR:
                    # unset, or anchored while the slider sat near zero: a day level that low
                    # would pin the monitor at the floor forever (it happened once)
                    cur = level_get(m)
                    day = self.sun_day[m.device] = self._anchor_day(cur if cur is not None else 60, f)
                target = max(2, round(day * f))
                if self.last.get(m.device) != target:
                    level_set(m, target)
                    self.last[m.device] = target
                    log.info("sun: %s -> %d%% (day %d x %.2f)", m.name, target, day, f)
            for m in mons:
                m.close()
            self._save_state()
        except Exception:
            log.exception("sun tick")

    # -- security -------------------------------------------------------------------
    def _security_tick(self):
        self.root.after(3600_000, self._security_tick)
        sec = self.cfg["security"]
        if not sec.get("weekly"):
            return
        last = sec.get("last_run", "")
        if not last or (dt.datetime.now() - dt.datetime.fromisoformat(last)).days >= 7:
            self.run_security(quiet=True)

    def run_security(self, quiet=False):
        if self.security_busy:
            return
        self.security_busy = True
        if not quiet:
            self._show_report("Running the security assessment... this takes about 15 seconds.")

        def work():
            try:
                r = security.assess(helper_call=self.engine.helper.call, claude=self.claude, report_dir=REPORT_DIR)
                self.cfg["security"]["last_run"] = dt.datetime.now().isoformat(timespec="minutes")
                self.cfg["security"]["last_summary"] = r["summary"]
                save_config(self.cfg)
                text = r["report"] + (f"\n\n(Claude write-up skipped: {r['claude_error']})" if r["claude_error"] else "")
                self.post(self._show_report, text, r["path"]) if not quiet else None
                if self.icon and quiet:
                    self.icon.title = f"Lights Out - security: {r['summary']}"
            except Exception as e:
                log.exception("security")
                if not quiet:
                    self.post(self._show_report, f"Assessment failed: {e}")
            finally:
                self.security_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _show_report(self, text, path=None):
        if not (self.report_win and self.report_win.winfo_exists()):
            w = tk.Toplevel(self.root, bg=PANEL)
            w.title("Lights Out - security assessment")
            w.geometry("760x640")
            self.report_win = w
            f = ttk.Frame(w, padding=10)
            f.pack(fill="both", expand=True)
            self.report_text = tk.Text(f, bg="#121318", fg=FG, insertbackground=FG, wrap="word", relief="flat",
                                       font=("Segoe UI", 10), padx=12, pady=10)
            self.report_text.pack(fill="both", expand=True)
            b = ttk.Frame(f)
            b.pack(fill="x", pady=(8, 0))
            ttk.Button(b, text="Run again", style="Flat.TButton", command=self.run_security).pack(side="left")
            self.report_open_btn = ttk.Button(b, text="Open report file", style="Flat.TButton")
            self.report_open_btn.pack(side="left", padx=8)
            ttk.Button(b, text="Close", style="Flat.TButton", command=w.destroy).pack(side="right")
        self.report_text.config(state="normal")
        self.report_text.delete("1.0", "end")
        self.report_text.insert("1.0", text)
        self.report_text.config(state="disabled")
        self.report_open_btn.config(command=(lambda p=path: os.startfile(p)) if path else lambda: None)
        self.report_win.lift()

    # -- brightness panel --------------------------------------------------------
    def refresh_monitors(self, retries=6, probe_wmi=True):
        for m in self.monitors:
            m.close()
        self.monitors = mon.enumerate_monitors(probe_wmi=probe_wmi, retries=retries)

    def show_panel(self):
        if self.asleep:
            return
        if self.panel and self.panel.winfo_exists():
            self.panel.destroy()
        self.refresh_monitors(retries=1, probe_wmi=False)   # DDC only: the dead laptop DDC + its 15 s WMI call must never stall the pop-up (that is the freeze in crash.log)
        p = tk.Toplevel(self.root, bg=PANEL)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        self.panel = p
        self.rows = []
        f = ttk.Frame(p, padding=(16, 12, 16, 14))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Lights Out", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(f, text="⚙", style="Flat.TButton", width=3, command=self.show_settings).grid(row=0, column=1, sticky="e")
        ttk.Button(f, text="✕", style="Flat.TButton", width=3, command=self.hide_panel).grid(row=0, column=2, sticky="e", padx=(6, 0))
        r = 1
        # -- power --
        ttk.Label(f, text="Power", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(10, 2))
        r += 1
        bar = ttk.Frame(f)
        bar.grid(row=r, column=0, columnspan=3, sticky="we")
        self.mode_btns = {}
        for m in ("auto", "quiet", "balanced", "performance", "max"):
            b = ttk.Button(bar, text=PROFILE_LABEL[m], style="Mode.TButton", command=lambda m=m: self.set_power_mode(m))
            b.pack(side="left", padx=(0, 4), fill="x", expand=True)
            self.mode_btns[m] = b
        r += 1
        self.power_line = ttk.Label(f, text="", style="Dim.TLabel", wraplength=360)
        self.power_line.grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        r += 1
        self.sensor_line = ttk.Label(f, text="", style="Dim.TLabel", wraplength=360)
        self.sensor_line.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        pf = ttk.Frame(f)
        pf.grid(row=r, column=0, columnspan=3, sticky="we", pady=(4, 0))
        ttk.Label(pf, text="Compute", style="Dim.TLabel").pack(side="left")
        self.pressure_bar = ttk.Progressbar(pf, length=200, maximum=100)
        self.pressure_bar.pack(side="left", padx=8, fill="x", expand=True)
        self.pressure_lab = ttk.Label(pf, text="", style="Dim.TLabel", width=12)
        self.pressure_lab.pack(side="left")
        r += 1
        # -- monitors --
        ttk.Label(f, text="Brightness", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 0))
        r += 1
        for m in self.monitors:
            head = ttk.Frame(f)
            head.grid(row=r, column=0, columnspan=3, sticky="we", pady=(6, 0))
            ttk.Label(head, text=m.name).pack(side="left")
            if m.hdr_supported:
                ttk.Label(head, text=" HDR ON " if m.hdr else " HDR OFF ", style="Tag.TLabel").pack(side="left", padx=(8, 0))
                ttk.Button(head, text="turn off" if m.hdr else "turn on", style="Flat.TButton",
                           command=lambda m=m: self.toggle_hdr(m)).pack(side="right")
            level = level_get(m)
            if level is None and m.flaky:
                level = self.last.get(m.device)
                ttk.Label(head, text="not answering", style="Dim.TLabel").pack(side="left", padx=(8, 0))
            elif level is not None:
                self.last[m.device] = level
            val = tk.IntVar(value=level if level is not None else 0)
            lab = ttk.Label(f, text=f"{val.get()}%", width=5, anchor="e")
            lab.grid(row=r + 1, column=2, sticky="e")
            sc = ttk.Scale(f, from_=0, to=100, orient="horizontal", length=300, variable=val)
            sc.grid(row=r + 1, column=0, columnspan=2, sticky="we", pady=(2, 0))
            for ev in ("<Button-1>", "<B1-Motion>"):
                sc.bind(ev, lambda e, m=m, val=val, lab=lab, sc=sc: self._scale_jump(e, m, val, lab, sc))
            sc.bind("<MouseWheel>", lambda e, m=m, val=val, lab=lab: self._scale_wheel(e, m, val, lab))
            if not controllable(m):
                sc.state(["disabled"])
            self.rows.append((m, val, lab, sc))
            r += 2
        # -- audio --
        if self.cfg["audio"].get("show_in_panel", True):
            r = self._build_audio_section(f, r)
        opts = ttk.Frame(f)
        opts.grid(row=r, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Checkbutton(opts, text="Move together", variable=self.link).pack(side="left")
        self.sun_var = tk.BooleanVar(value=bool(self.cfg["sun"].get("enabled")))
        ttk.Checkbutton(opts, text="Sun-synced", variable=self.sun_var, command=self._toggle_sun).pack(side="left", padx=(14, 0))
        self.sun_strength = tk.IntVar(value=int(self.cfg["sun"].get("strength", 60)))
        ss = ttk.Scale(opts, from_=0, to=100, orient="horizontal", length=90, variable=self.sun_strength,
                       command=lambda v: self._sun_strength_changed(v))
        ss.pack(side="left", padx=(8, 4))
        self.sun_lab = ttk.Label(opts, text="", style="Dim.TLabel")
        self.sun_lab.pack(side="left")
        r += 1
        self.sun_line = ttk.Label(f, text=self.sun_info, style="Dim.TLabel")
        self.sun_line.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        ttk.Button(f, text="Sleep now", style="Sleep.TButton", command=self.sleep).grid(
            row=r, column=0, columnspan=3, sticky="we", pady=(12, 4))
        r += 1
        sec = self.cfg["security"]
        if sec.get("last_summary"):
            ttk.Label(f, text=f"Security: {sec['last_summary']}", style="Dim.TLabel", wraplength=360).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
            r += 1
        f.columnconfigure(0, weight=1)
        self._refresh_power_ui()
        self._sun_strength_changed(None)
        p.update_idletasks()
        self._place_near_tray(p)
        p.bind("<Escape>", lambda e: self.hide_panel())
        p.bind("<FocusOut>", self._maybe_hide)
        p.deiconify()
        p.lift()
        p.focus_force()
        self._panel_refresh()

    def _panel_refresh(self):
        if not (self.panel and self.panel.winfo_exists()):
            return
        self._refresh_power_ui()
        self._refresh_audio_ui()
        self.root.after(2000, self._panel_refresh)

    # -- audio ----------------------------------------------------------------------
    @staticmethod
    def _short_audio(name):
        s = name
        if s.count("(") >= 2 and s.endswith(")"):      # drop a trailing "(driver)" group
            s = s[:s.rfind("(")].strip()
        return s if len(s) <= 40 else s[:39] + "…"

    def _build_audio_section(self, f, r):
        snap = self.audio.devices()
        ttk.Label(f, text="Audio", style="Section.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(12, 2)); r += 1
        orow = ttk.Frame(f); orow.grid(row=r, column=0, columnspan=3, sticky="we"); r += 1
        ttk.Label(orow, text="Out", style="Dim.TLabel", width=4).pack(side="left")
        self.out_combo = ttk.Combobox(orow, state="readonly", width=32)
        self.out_combo.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.out_combo.bind("<<ComboboxSelected>>", self._audio_pick_output)
        self.mute_btn = ttk.Button(orow, text="\U0001F50A", style="Flat.TButton", width=3, command=self._audio_toggle_mute)
        self.mute_btn.pack(side="left")
        vrow = ttk.Frame(f); vrow.grid(row=r, column=0, columnspan=3, sticky="we", pady=(2, 0)); r += 1
        ttk.Label(vrow, text="Vol", style="Dim.TLabel", width=4).pack(side="left")
        self.vol_val = tk.IntVar(value=snap.get("volume") or 0)
        self.vol_scale = ttk.Scale(vrow, from_=0, to=100, orient="horizontal", variable=self.vol_val)
        self.vol_scale.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.vol_lab = ttk.Label(vrow, text="", style="Dim.TLabel", width=5, anchor="e"); self.vol_lab.pack(side="left")
        self.vol_scale.bind("<Button-1>", self._audio_vol_press)
        self.vol_scale.bind("<B1-Motion>", self._audio_vol_drag)
        self.vol_scale.bind("<ButtonRelease-1>", self._audio_vol_release)
        self.vol_scale.bind("<MouseWheel>", self._audio_vol_wheel)
        irow = ttk.Frame(f); irow.grid(row=r, column=0, columnspan=3, sticky="we", pady=(2, 0)); r += 1
        ttk.Label(irow, text="Mic", style="Dim.TLabel", width=4).pack(side="left")
        self.in_combo = ttk.Combobox(irow, state="readonly", width=32)
        self.in_combo.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.in_combo.bind("<<ComboboxSelected>>", self._audio_pick_input)
        self._audio_sig = None
        self._refresh_audio_ui(force=True)
        return r

    def _refresh_audio_ui(self, force=False):
        if not (self.panel and self.panel.winfo_exists() and getattr(self, "out_combo", None)):
            return
        try:
            if not self.out_combo.winfo_exists():
                return
        except Exception:
            return
        snap = self.audio.devices()
        outs = [d for d in snap.get("outputs", []) if d["state"] == audio.STATE_ACTIVE]
        ins = [d for d in snap.get("inputs", []) if d["state"] == audio.STATE_ACTIVE]
        sig = (tuple(d["id"] for d in outs), tuple(d["id"] for d in ins))
        if force or sig != self._audio_sig:
            self._out_map = {self._short_audio(d["name"]): d["id"] for d in outs}
            self._in_map = {self._short_audio(d["name"]): d["id"] for d in ins}
            self.out_combo["values"] = list(self._out_map)
            self.in_combo["values"] = list(self._in_map)
            self._audio_sig = sig
        focused = self.panel.focus_get()
        cur_out = next((n for n, i in getattr(self, "_out_map", {}).items() if i == snap.get("out_default")), None)
        if cur_out and self.out_combo.get() != cur_out and focused is not self.out_combo:
            self.out_combo.set(cur_out)
        cur_in = next((n for n, i in getattr(self, "_in_map", {}).items() if i == snap.get("in_default")), None)
        if cur_in and self.in_combo.get() != cur_in and focused is not self.in_combo:
            self.in_combo.set(cur_in)
        if not self._audio_dragging:
            v = snap.get("volume")
            if v is not None:
                self.vol_val.set(v)
                self.vol_lab.config(text=f"{v}%")
        self.mute_btn.config(text="\U0001F507" if snap.get("muted") else "\U0001F50A")

    def _audio_pick_output(self, e=None):
        did = getattr(self, "_out_map", {}).get(self.out_combo.get())
        if did:
            self.audio.post("default", did)

    def _audio_pick_input(self, e=None):
        did = getattr(self, "_in_map", {}).get(self.in_combo.get())
        if did:
            self.audio.post("default_in", did)

    def _audio_toggle_mute(self):
        snap = self.audio.devices()
        if snap.get("out_default"):
            self.audio.post("mute", snap["out_default"], not bool(snap.get("muted")))

    def _audio_vol_from_event(self, e):
        w = self.vol_scale.winfo_width() or 1
        return max(0, min(100, round(e.x / w * 100)))

    def _audio_vol_apply(self, pct):
        self.vol_val.set(pct)
        if getattr(self, "vol_lab", None):
            self.vol_lab.config(text=f"{pct}%")
        od = self.audio.devices().get("out_default")
        if od:
            self.audio.post("volume", od, pct)

    def _audio_vol_press(self, e):
        self._audio_dragging = True
        self._audio_vol_apply(self._audio_vol_from_event(e))

    def _audio_vol_drag(self, e):
        self._audio_vol_apply(self._audio_vol_from_event(e))

    def _audio_vol_release(self, e):
        self._audio_vol_apply(self._audio_vol_from_event(e))
        self._audio_dragging = False

    def _audio_vol_wheel(self, e):
        self._audio_vol_apply(max(0, min(100, int(self.vol_val.get()) + (5 if e.delta > 0 else -5))))

    def _audio_prefill(self):
        a = self.cfg["audio"]
        snap = self.audio.devices()
        if not snap.get("outputs"):
            self.root.after(2000, self._audio_prefill)         # hub still warming up
            return
        changed = False
        if not a.get("headphone_id") and not a.get("speaker_id"):
            hp, sp = audio.guess_pair(snap["outputs"])
            if hp and sp:
                a["headphone_id"], a["speaker_id"] = hp, sp
                changed = True
                log.info("audio pair guessed: headphones=%r speakers=%r",
                         self.audio._devname(hp), self.audio._devname(sp))
        if not a.get("mic_on_headphones_id") and not a.get("mic_on_speakers_id") and snap.get("inputs"):
            mh, ms = audio.guess_mic_pair(snap["inputs"])
            if mh or ms:
                a["mic_on_headphones_id"], a["mic_on_speakers_id"] = mh, ms
                changed = True
        if changed:
            save_config(self.cfg)

    def _refresh_power_ui(self):
        if not (self.panel and self.panel.winfo_exists()):
            return
        st = self.engine.snapshot()
        mode = self.cfg["power"].get("mode", "auto")
        for m, b in self.mode_btns.items():
            b.configure(style="ModeOn.TButton" if m == mode else "Mode.TButton")
        prof = st.get("profile") or "-"
        line = f"{PROFILE_LABEL.get(mode, mode)} → {prof}: {st.get('reason', '')}"
        if not self.cfg["power"].get("enabled", True):
            line = "Power engine is off (Settings > Power)"
        self.power_line.config(text=line)
        bits = []
        if st.get("cpu") is not None:
            bits.append(f"CPU {st['cpu']:.0f}%" + (f" {st['cpu_temp']}°C" if st.get("cpu_temp") else ""))
        if st.get("gpu_util") is not None:
            bits.append(f"GPU {st['gpu_util']}%" + (f" {st['gpu_temp']}°C" if st.get("gpu_temp") else "") +
                        (f" {st['gpu_power_w']:.0f} W" if st.get("gpu_power_w") else ""))
        if st.get("fans"):
            bits.append(f"fans {st['fans'][0]}/{st['fans'][1]} rpm")
        if st.get("battery") is not None and not st.get("on_ac"):
            bits.append(f"battery {st['battery']:.0f}%")
        if st.get("thermal"):
            bits.append("⚠ " + st["thermal"])
        if not st.get("helper"):
            bits.append("helper not running")
        self.sensor_line.config(text=" · ".join(bits))
        pr = st.get("pressure") or 0
        self.pressure_bar["value"] = pr
        self.pressure_lab.config(text=f"{pr}%" + (" (near limit)" if pr >= self.cfg["power"].get("max_load", 85) else ""))

    @staticmethod
    def _anchor_day(level, factor):
        """Day level that makes the curve pass through `level` right now, never
        below SUN_DAY_FLOOR so the monitors can always come back up."""
        return max(SUN_DAY_FLOOR, min(100, round(level / max(factor, 0.05))))

    def _toggle_sun(self):
        self.cfg["sun"]["enabled"] = bool(self.sun_var.get())
        if self.cfg["sun"]["enabled"]:
            self.sun_day = {}
            f = self._sun_factor()
            for m, val, lab, sc in self.rows:
                self.sun_day[m.device] = self._anchor_day(int(val.get()), f)
        save_config(self.cfg)
        self._save_state()
        self.root.after(200, self._sun_tick_now)

    def _sun_tick_now(self):
        # run one sun pass right away (the periodic one re-arms itself)
        s = self.cfg["sun"]
        if s.get("enabled") and not self.asleep:
            f = self._sun_factor()
            for m, val, lab, sc in self.rows:
                day = self.sun_day.get(m.device)
                if day is not None:
                    t = max(2, round(day * f))
                    val.set(t)
                    lab.config(text=f"{t}%")
                    self.writer.submit((m.device, "level"), lambda m=m, v=t: level_set(m, v))
                    self.last[m.device] = t
            self._save_state()

    def _sun_strength_changed(self, _v):
        s = int(self.sun_strength.get())
        self.cfg["sun"]["strength"] = s
        f = self._sun_factor()
        self.sun_lab.config(text=f"night {max(0, 100 - s)}%")
        if _v is not None:
            save_config(self.cfg)
            if self.cfg["sun"].get("enabled"):
                self._sun_tick_now()

    def toggle_hdr(self, m):
        self.hide_panel()
        m.set_hdr(not m.hdr)
        self.root.after(1500, self.show_panel)

    def _scale_jump(self, e, m, val, lab, sc):
        w, pad = sc.winfo_width(), 11
        frac = (e.x - pad) / max(1, w - 2 * pad)
        val.set(int(round(max(0, min(1, frac)) * 100)))
        self._slide(m, val, lab)
        return "break"

    def _scale_wheel(self, e, m, val, lab):
        val.set(int(max(0, min(100, int(val.get()) + (5 if e.delta > 0 else -5)))))
        self._slide(m, val, lab)
        return "break"

    def _maybe_hide(self, _e):
        self.root.after(120, lambda: self.hide_panel()
                        if self.panel and self.panel.winfo_exists() and self.panel.focus_get() is None else None)

    def hide_panel(self):
        if self.panel and self.panel.winfo_exists():
            self.panel.destroy()
        self.panel = None

    def _place_near_tray(self, p):
        w, h = p.winfo_reqwidth(), p.winfo_reqheight()
        prim = next((m for m in self.monitors if m.primary), None)
        if prim:
            mi = mon.MONITORINFOEX(cb=ctypes.sizeof(mon.MONITORINFOEX))
            hmon = user32.MonitorFromPoint(wt.POINT(prim.rect[0], prim.rect[1]), 1)
            user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
            x, y = mi.work.right - w - 12, mi.work.bottom - h - 12
        else:
            x, y = p.winfo_screenwidth() - w - 12, p.winfo_screenheight() - h - 60
        p.geometry(f"{w}x{h}+{x}+{y}")

    def _set_level(self, m, v):
        self.writer.submit((m.device, "level"), lambda m=m, v=v: level_set(m, v))
        self.last[m.device] = v
        if self.cfg["sun"].get("enabled"):
            self.sun_day[m.device] = self._anchor_day(v, self._sun_factor())

    def _slide(self, m, val, lab, _depth=0):
        v = int(max(0, min(100, int(float(val.get())))))
        lab.config(text=f"{v}%")
        self._set_level(m, v)
        self._save_state()
        if self.link.get() and _depth == 0:
            for om, oval, olab, osc in self.rows:
                if om is not m and controllable(om):
                    oval.set(v)
                    self._slide(om, oval, olab, 1)

    # -- settings window -----------------------------------------------------------
    def show_settings(self):
        self.hide_panel()
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        w = tk.Toplevel(self.root, bg=PANEL)
        w.title("Lights Out settings")
        w.attributes("-topmost", True)
        self.settings_win = w
        outer = ttk.Frame(w, padding=14)
        outer.pack(fill="both", expand=True)
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        self.settings_nb = nb
        v = {}
        cfg = self.cfg
        tabs = {name: ttk.Frame(nb, padding=14) for name in ("Sleep", "Power", "Display", "Audio", "Remote", "Claude", "Security")}
        for name, fr in tabs.items():
            nb.add(fr, text=name)

        def row(fr, r, label, widget):
            ttk.Label(fr, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="w", padx=(10, 0), pady=2)
            return r + 1

        # Sleep tab
        f = tabs["Sleep"]
        r = 0
        ttk.Label(f, text="Sleep and wake", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["presses"] = tk.IntVar(value=cfg["presses"]); v["window_seconds"] = tk.DoubleVar(value=cfg["window_seconds"])
        r = row(f, r, "Wake after this many key presses", ttk.Spinbox(f, from_=1, to=5, width=5, textvariable=v["presses"]))
        r = row(f, r, "...within this many seconds", ttk.Spinbox(f, from_=1, to=30, width=5, textvariable=v["window_seconds"]))
        v["lock_on_wake"] = tk.BooleanVar(value=cfg["lock_on_wake"]); v["hide_taskbar_asleep"] = tk.BooleanVar(value=cfg.get("hide_taskbar_asleep", True))
        v["lock_on_sleep"] = tk.BooleanVar(value=cfg.get("lock_on_sleep", True))
        v["relock_minutes"] = tk.IntVar(value=int(cfg.get("relock_minutes", 3)))
        ttk.Checkbutton(f, text="Lock Windows at sleep, once the screens are dark (password or Windows Hello to get back in, "
                                "even if this app dies)", variable=v["lock_on_sleep"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        r = row(f, r, "If woken onto the lock screen and nobody signs in, go dark again after (minutes, 0 = never)",
                ttk.Spinbox(f, from_=0, to=60, width=5, textvariable=v["relock_minutes"]))
        ttk.Checkbutton(f, text="Otherwise lock Windows when waking (opens to the lock screen)", variable=v["lock_on_wake"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["wake_on_click"] = tk.BooleanVar(value=cfg.get("wake_on_click", True))
        v["fingerprint_wake"] = tk.BooleanVar(value=cfg.get("fingerprint_wake", True))
        v["fingerprint_unlocks"] = tk.BooleanVar(value=cfg.get("fingerprint_unlocks", True))
        ttk.Checkbutton(f, text="Mouse clicks count as presses too (movement never wakes)", variable=v["wake_on_click"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        fp_note = f" ({self.fp.description})" if self.fp.available() else " (no fingerprint reader found)"
        ttk.Checkbutton(f, text="Fingerprint wakes" + fp_note + "  (when locked at sleep, Windows Hello on the lock screen does this)",
                        variable=v["fingerprint_wake"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Checkbutton(f, text="...and a matched fingerprint on an unlocked desktop skips the lock screen",
                        variable=v["fingerprint_unlocks"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Checkbutton(f, text="Hide the taskbar while asleep", variable=v["hide_taskbar_asleep"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["detach_no_standby"] = tk.BooleanVar(value=cfg.get("detach_no_standby", True))
        ttk.Checkbutton(f, text="A monitor with no DDC/CI standby command is disconnected while asleep (signal off, so its "
                                "backlight turns off; reconnected on wake)", variable=v["detach_no_standby"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(f, text="Hotkeys  (e.g. ctrl+alt+s, ctrl+shift+f9, win+pause)", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=(14, 2)); r += 1
        v["hotkeys"] = {}
        for key, label in (("sleep", "Sleep now"), ("panel", "Open panel"), ("brightness_up", "Brightness up"), ("brightness_down", "Brightness down")):
            v["hotkeys"][key] = tk.StringVar(value=cfg["hotkeys"].get(key, ""))
            r = row(f, r, label, ttk.Entry(f, textvariable=v["hotkeys"][key], width=18))
        v["brightness_step"] = tk.IntVar(value=cfg.get("brightness_step", 10))
        r = row(f, r, "Brightness step per press (%)", ttk.Spinbox(f, from_=1, to=50, width=5, textvariable=v["brightness_step"]))

        # Power tab
        f = tabs["Power"]
        pw = cfg["power"]
        r = 0
        ttk.Label(f, text="Power engine", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["p_enabled"] = tk.BooleanVar(value=pw.get("enabled", True)); v["p_throttle"] = tk.BooleanVar(value=pw.get("throttle_background", True))
        v["p_allow_max"] = tk.BooleanVar(value=pw.get("allow_max", True))
        ttk.Checkbutton(f, text="Engine on (auto-pick quiet / balanced / performance / max)", variable=v["p_enabled"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Checkbutton(f, text="Slow background apps (Efficiency mode) while something heavy is in front", variable=v["p_throttle"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Checkbutton(f, text="Allow Max (overclock tier: raised power limits, aggressive fans, GPU OC profile)", variable=v["p_allow_max"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["p_idle"] = tk.IntVar(value=pw.get("idle_minutes", 5)); v["p_load_high"] = tk.IntVar(value=pw.get("load_high", 55)); v["p_max_load"] = tk.IntVar(value=pw.get("max_load", 85))
        r = row(f, r, "Quiet after idle (minutes)", ttk.Spinbox(f, from_=1, to=120, width=5, textvariable=v["p_idle"]))
        r = row(f, r, "Performance above CPU/GPU load (%)", ttk.Spinbox(f, from_=20, to=95, width=5, textvariable=v["p_load_high"]))
        r = row(f, r, "Max above sustained load (%)", ttk.Spinbox(f, from_=50, to=100, width=5, textvariable=v["p_max_load"]))
        ttk.Label(f, text="Thermal guard (°C) - Max is dropped above these, panic = balanced + full fans", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=(12, 2)); r += 1
        th = pw.get("thermal", {})
        v["t_cpu_max"] = tk.IntVar(value=th.get("cpu_max", 92)); v["t_cpu_panic"] = tk.IntVar(value=th.get("cpu_panic", 97)); v["t_gpu_max"] = tk.IntVar(value=th.get("gpu_max", 86))
        r = row(f, r, "CPU: no overclock above", ttk.Spinbox(f, from_=70, to=100, width=5, textvariable=v["t_cpu_max"]))
        r = row(f, r, "CPU: panic above", ttk.Spinbox(f, from_=80, to=105, width=5, textvariable=v["t_cpu_panic"]))
        r = row(f, r, "GPU: no overclock above", ttk.Spinbox(f, from_=60, to=95, width=5, textvariable=v["t_gpu_max"]))
        ttk.Label(f, text="Max tier (ASUS firmware)", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=(12, 2)); r += 1
        b = pw.get("boost", {})
        v["b_total"] = tk.IntVar(value=b.get("ppt_total_w", 80)); v["b_apu"] = tk.IntVar(value=b.get("ppt_apu_w", 65))
        r = row(f, r, "Package power limit (W)", ttk.Spinbox(f, from_=35, to=120, width=5, textvariable=v["b_total"]))
        r = row(f, r, "APU power limit (W)", ttk.Spinbox(f, from_=25, to=100, width=5, textvariable=v["b_apu"]))
        ttk.Label(f, text="Apps and tools", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=(12, 2)); r += 1
        v["heavy"] = tk.StringVar(value=", ".join(pw.get("heavy_apps", [])))
        v["protected"] = tk.StringVar(value=", ".join(pw.get("lists", {}).get("protected", [])))
        v["hogs"] = tk.StringVar(value=", ".join(pw.get("lists", {}).get("hogs", [])))
        r = row(f, r, "Heavy apps (performance when in front)", ttk.Entry(f, textvariable=v["heavy"], width=52))
        r = row(f, r, "Never slow down", ttk.Entry(f, textvariable=v["protected"], width=52))
        r = row(f, r, "Always slow in background", ttk.Entry(f, textvariable=v["hogs"], width=52))
        ab = pw.get("afterburner", {})
        v["ab_oc"] = tk.IntVar(value=ab.get("oc_profile", 0)); v["ab_stock"] = tk.IntVar(value=ab.get("stock_profile", 0))
        r = row(f, r, "MSI Afterburner OC profile for Max (0 = off)", ttk.Spinbox(f, from_=0, to=5, width=5, textvariable=v["ab_oc"]))
        r = row(f, r, "Afterburner stock profile", ttk.Spinbox(f, from_=0, to=5, width=5, textvariable=v["ab_stock"]))
        helper_note = "helper running" if self.engine.helper.ok else "helper not running (run Install Lights Out.bat once for firmware control)"
        ttk.Label(f, text=f"{helper_note}. Optional: drop ryzenadj.exe into tools\\ for CPU power limits and a hardware "
                          f"temperature cap on AMD laptops.", style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0)); r += 1

        # Display tab
        f = tabs["Display"]
        r = 0
        ttk.Label(f, text="Sun-synced brightness", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        s = cfg["sun"]
        v["s_enabled"] = tk.BooleanVar(value=s.get("enabled", False)); v["s_strength"] = tk.IntVar(value=s.get("strength", 60)); v["s_trans"] = tk.IntVar(value=s.get("transition_min", 45))
        v["s_lat"] = tk.StringVar(value="" if s.get("lat") is None else str(s["lat"])); v["s_lon"] = tk.StringVar(value="" if s.get("lon") is None else str(s["lon"]))
        ttk.Checkbutton(f, text="Dim at night, full by day (follows local sunrise and sunset)", variable=v["s_enabled"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        r = row(f, r, "Strength (% dimmer at night)", ttk.Spinbox(f, from_=0, to=100, width=5, textvariable=v["s_strength"]))
        r = row(f, r, "Transition length (minutes)", ttk.Spinbox(f, from_=5, to=180, width=5, textvariable=v["s_trans"]))
        r = row(f, r, "Latitude (blank = detect)", ttk.Entry(f, textvariable=v["s_lat"], width=12))
        r = row(f, r, "Longitude", ttk.Entry(f, textvariable=v["s_lon"], width=12))
        ttk.Label(f, text=self.sun_info or "location not detected yet", style="Dim.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(f, text="Colour", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w", pady=(12, 2)); r += 1
        self.refresh_monitors()
        for m in self.monitors:
            c = m.contrast()
            if c is None:
                continue
            cv = tk.IntVar(value=c)
            fr = ttk.Frame(f)
            cl = ttk.Label(fr, text=f"{c}%", width=5, anchor="e")
            cs = ttk.Scale(fr, from_=0, to=100, orient="horizontal", length=220, variable=cv,
                           command=lambda val, m=m, cl=cl: (cl.config(text=f"{int(float(val))}%"),
                                                            self.writer.submit((m.device, "contrast"), lambda: m.set_contrast(int(float(val))))))
            cs.pack(side="left"); cl.pack(side="left", padx=(6, 0))
            r = row(f, r, f"{m.name} contrast", fr)
        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0)); r += 1
        for text, target in (("Windows HDR Calibration", HDR_CALIBRATION), ("HDR settings", "ms-settings:display-hdr"),
                             ("Night light", "ms-settings:nightlight"), ("Auto HDR / graphics", "ms-settings:display-advancedgraphics")):
            ttk.Button(btns, text=text, style="Flat.TButton", command=lambda t=target: self.open_external(t)).pack(side="left", padx=(0, 6))

        # Audio tab
        f = tabs["Audio"]
        au = cfg["audio"]
        asnap = self.audio.devices()
        a_outs = [d for d in asnap.get("outputs", []) if d["state"] == audio.STATE_ACTIVE] or asnap.get("outputs", [])
        a_ins = [d for d in asnap.get("inputs", []) if d["state"] == audio.STATE_ACTIVE] or asnap.get("inputs", [])
        out_map = {self._short_audio(d["name"]): d["id"] for d in a_outs}
        in_map = {self._short_audio(d["name"]): d["id"] for d in a_ins}
        # keep the configured device selectable even if it is unplugged right now
        for did, lbl in ((au.get("headphone_id"), "headphones"), (au.get("speaker_id"), "speakers")):
            if did and did not in out_map.values():
                nm = next((self._short_audio(d["name"]) for d in asnap.get("outputs", []) if d["id"] == did), f"(saved {lbl})")
                out_map[nm] = did
        for did in (au.get("mic_on_headphones_id"), au.get("mic_on_speakers_id")):
            if did and did not in in_map.values():
                nm = next((self._short_audio(d["name"]) for d in asnap.get("inputs", []) if d["id"] == did), "(saved mic)")
                in_map[nm] = did
        self._settings_out_map, self._settings_in_map = out_map, in_map
        id2out = {i: n for n, i in out_map.items()}
        id2in = {i: n for n, i in in_map.items()}
        r = 0
        ttk.Label(f, text="Audio", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(f, text="Switch the output and input in the panel. Here you set the automatic switch between your "
                          "headphones and speakers: when the headphones turn on (plugged in, or a wireless set powered "
                          "on) the sound moves to them, and back to the speakers when they turn off.",
                  style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["a_panel"] = tk.BooleanVar(value=au.get("show_in_panel", True))
        ttk.Checkbutton(f, text="Show audio controls in the panel", variable=v["a_panel"]).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0)); r += 1
        v["a_auto"] = tk.BooleanVar(value=au.get("auto_switch", True))
        ttk.Checkbutton(f, text="Automatically switch when my headphones turn on or off", variable=v["a_auto"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["a_hp"] = tk.StringVar(value=id2out.get(au.get("headphone_id"), ""))
        v["a_sp"] = tk.StringVar(value=id2out.get(au.get("speaker_id"), ""))
        r = row(f, r, "Headphones", ttk.Combobox(f, state="readonly", width=42, textvariable=v["a_hp"], values=list(out_map)))
        r = row(f, r, "Speakers", ttk.Combobox(f, state="readonly", width=42, textvariable=v["a_sp"], values=list(out_map)))
        v["a_mic"] = tk.BooleanVar(value=au.get("switch_mic", False))
        ttk.Checkbutton(f, text="Also switch the microphone to match", variable=v["a_mic"]).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0)); r += 1
        v["a_mh"] = tk.StringVar(value=id2in.get(au.get("mic_on_headphones_id"), ""))
        v["a_ms"] = tk.StringVar(value=id2in.get(au.get("mic_on_speakers_id"), ""))
        r = row(f, r, "Mic with headphones", ttk.Combobox(f, state="readonly", width=42, textvariable=v["a_mh"], values=list(in_map)))
        r = row(f, r, "Mic with speakers", ttk.Combobox(f, state="readonly", width=42, textvariable=v["a_ms"], values=list(in_map)))
        cur = next((self._short_audio(d["name"]) for d in asnap.get("outputs", []) if d["id"] == asnap.get("out_default")), "?")
        ttk.Label(f, text=f"Default output now: {cur}", style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0)); r += 1
        ttk.Label(f, text="Note: if you route sound through Voicemeeter, automatic switching changes the Windows default "
                          "device and may fight that setup — turn it off here if so.",
                  style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1

        # Remote tab
        f = tabs["Remote"]
        rc_ = cfg.get("remote", {})
        r = 0
        ttk.Label(f, text="Remote desktop (Chrome Remote Desktop)", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(f, text="When a Chrome Remote Desktop client connects while asleep, the main monitor comes on at 16:9 for "
                          "the session and the others stay off. Windows stays locked — you sign in once through the remote "
                          "session (this never stores or types your password). On disconnect, or a real keystroke at the "
                          "desk, it locks and goes back to the dark sleep state.",
                  style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["r_enabled"] = tk.BooleanVar(value=rc_.get("enabled", True))
        ttk.Checkbutton(f, text="Bring the main monitor up for a remote-desktop session", variable=v["r_enabled"]).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0)); r += 1
        v["r_16x9"] = tk.BooleanVar(value=rc_.get("wide_to_16x9", True))
        ttk.Checkbutton(f, text="Switch the main monitor to 16:9 for the session (an ultrawide is awkward to remote into)", variable=v["r_16x9"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["r_w"] = tk.IntVar(value=int(rc_.get("width") or 0)); v["r_h"] = tk.IntVar(value=int(rc_.get("height") or 0))
        r = row(f, r, "16:9 width (0 = pick the best one)", ttk.Spinbox(f, from_=0, to=5120, increment=10, width=7, textvariable=v["r_w"]))
        r = row(f, r, "16:9 height (0 = auto)", ttk.Spinbox(f, from_=0, to=2880, increment=10, width=7, textvariable=v["r_h"]))
        v["r_lock_phys"] = tk.BooleanVar(value=rc_.get("lock_on_physical", True))
        ttk.Checkbutton(f, text='A real keystroke at the desk ends the session and locks ("switched to manual")', variable=v["r_lock_phys"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        crd_note = "Chrome Remote Desktop host detected." if remote.available() else "event-log API unavailable."
        ttk.Label(f, text=crd_note + " Detection reads the host's connect/disconnect events from the Windows log; it never "
                          "unlocks Windows for you.", style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0)); r += 1

        # Claude tab
        f = tabs["Claude"]
        cl = cfg["claude"]
        r = 0
        ttk.Label(f, text="Claude", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(f, text="Used for two things only: a one-time cheap classification of unknown background apps, and the "
                          "write-up of the security assessment. Everything is cached; the call budget scales with how "
                          "busy the machine is.",
                  style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["c_mode"] = tk.StringVar(value=cl.get("mode", "cli"))
        mf = ttk.Frame(f)
        ttk.Radiobutton(mf, text="Claude Code CLI (your account)", variable=v["c_mode"], value="cli").pack(side="left")
        ttk.Radiobutton(mf, text="API key", variable=v["c_mode"], value="api").pack(side="left", padx=(12, 0))
        r = row(f, r, "Use", mf)
        v["c_key"] = tk.StringVar(value="")
        r = row(f, r, "API key (saved encrypted; leave blank to keep)", ttk.Entry(f, textvariable=v["c_key"], width=46, show="•"))
        v["c_model"] = tk.StringVar(value=cl.get("api_model", CB.GOOD_MODEL))
        r = row(f, r, "API model for reports", ttk.Entry(f, textvariable=v["c_model"], width=30))
        v["c_adaptive"] = tk.BooleanVar(value=cl.get("rate_adaptive", True))
        ttk.Checkbutton(f, text="Scale the call budget with how busy the machine is (gaming-aware)",
                        variable=v["c_adaptive"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["c_rmin"] = tk.DoubleVar(value=cl.get("rate_min_per_hour", 0.5))
        v["c_rmax"] = tk.DoubleVar(value=cl.get("rate_max_per_hour", 15.0))
        v["c_reserve"] = tk.IntVar(value=cl.get("daily_reserve", 3))
        v["c_cap"] = tk.IntVar(value=cl.get("daily_call_cap", 20))
        rf = ttk.Frame(f)
        ttk.Label(rf, text="idle").pack(side="left")
        ttk.Spinbox(rf, from_=0, to=60, increment=0.5, width=5, textvariable=v["c_rmin"]).pack(side="left", padx=(4, 2))
        ttk.Label(rf, text="to busy").pack(side="left")
        ttk.Spinbox(rf, from_=1, to=120, increment=1, width=5, textvariable=v["c_rmax"]).pack(side="left", padx=(4, 2))
        ttk.Label(rf, text="calls/hour,  + reserve/day").pack(side="left")
        ttk.Spinbox(rf, from_=0, to=50, width=4, textvariable=v["c_reserve"]).pack(side="left", padx=(4, 0))
        r = row(f, r, "Rate scales", rf)
        r = row(f, r, "Absolute daily backstop", ttk.Spinbox(f, from_=1, to=1000, width=6, textvariable=v["c_cap"]))
        u = self.claude.usage.get(dt.date.today().isoformat(), {})
        ttk.Label(f, text=f"Status: {self.claude.status()}\n{self.claude.budget()}\n"
                          f"Today: {u.get('calls', 0)} calls, {u.get('in', 0)} in / {u.get('out', 0)} out tokens"
                          + (f", ${u.get('usd', 0):.3f}" if u.get("usd") else ""), style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0)); r += 1
        ttk.Label(f, text="CLI sign-in: open a terminal, type `claude`, then `/login` once. No key needed after that.",
                  style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1

        # Security tab
        f = tabs["Security"]
        se = cfg["security"]
        r = 0
        ttk.Label(f, text="Security assessment", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["sec_weekly"] = tk.BooleanVar(value=se.get("weekly", True))
        ttk.Checkbutton(f, text="Run automatically once a week (quietly, result shows on the panel)", variable=v["sec_weekly"]).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(f, text=f"Last run: {se.get('last_run') or 'never'}  {se.get('last_summary', '')}", style="Dim.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        bf = ttk.Frame(f)
        bf.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 0)); r += 1
        ttk.Button(bf, text="Run assessment now", style="Flat.TButton", command=self.run_security).pack(side="left")
        ttk.Button(bf, text="Open reports folder", style="Flat.TButton",
                   command=lambda: (os.makedirs(REPORT_DIR, exist_ok=True), os.startfile(REPORT_DIR))).pack(side="left", padx=8)
        ttk.Label(f, text="Checks Defender, firewall, UAC, SmartScreen, Smart App Control, updates, autoruns, scheduled tasks, "
                          "listening ports, local admins, RDP, shares, BitLocker / Secure Boot / SMB1 / LSA (via the helper), and "
                          "AppControl's catalogue of new or unsigned programs. Claude writes the plain-English report when set up.",
                  style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0)); r += 1

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(10, 0))
        self.settings_msg = ttk.Label(bottom, text="", style="Dim.TLabel")
        self.settings_msg.pack(side="left")
        ttk.Button(bottom, text="Save", style="Sleep.TButton", command=lambda: self.save_settings(v)).pack(side="right")
        w.bind("<Escape>", lambda e: w.destroy())
        w.update_idletasks()
        w.geometry(f"+{max(0, (w.winfo_screenwidth() - w.winfo_reqwidth()) // 2)}+100")

    def open_external(self, target):
        try:
            subprocess.Popen(["explorer.exe", target]) if target.startswith("shell:") else os.startfile(target)
        except Exception:
            log.exception("open %s", target)

    def save_settings(self, v):
        cfg = self.cfg
        try:
            cfg["presses"] = max(1, int(v["presses"].get())); cfg["window_seconds"] = max(1.0, float(v["window_seconds"].get()))
            cfg["lock_on_wake"] = bool(v["lock_on_wake"].get()); cfg["hide_taskbar_asleep"] = bool(v["hide_taskbar_asleep"].get())
            cfg["wake_on_click"] = bool(v["wake_on_click"].get()); cfg["fingerprint_wake"] = bool(v["fingerprint_wake"].get())
            cfg["fingerprint_unlocks"] = bool(v["fingerprint_unlocks"].get())
            cfg["lock_on_sleep"] = bool(v["lock_on_sleep"].get()); cfg["relock_minutes"] = max(0, int(v["relock_minutes"].get()))
            cfg["detach_no_standby"] = bool(v["detach_no_standby"].get())
            cfg["brightness_step"] = max(1, int(v["brightness_step"].get()))
            cfg["hotkeys"] = {k: var.get().strip() for k, var in v["hotkeys"].items()}
            pw = cfg["power"]
            pw["enabled"] = bool(v["p_enabled"].get()); pw["throttle_background"] = bool(v["p_throttle"].get()); pw["allow_max"] = bool(v["p_allow_max"].get())
            pw["idle_minutes"] = int(v["p_idle"].get()); pw["load_high"] = int(v["p_load_high"].get()); pw["max_load"] = int(v["p_max_load"].get())
            pw.setdefault("thermal", {}).update(cpu_max=int(v["t_cpu_max"].get()), cpu_panic=int(v["t_cpu_panic"].get()), gpu_max=int(v["t_gpu_max"].get()))
            pw.setdefault("boost", {}).update(ppt_total_w=int(v["b_total"].get()), ppt_apu_w=int(v["b_apu"].get()))
            split = lambda s: [x.strip().lower() for x in s.get().split(",") if x.strip()]
            pw["heavy_apps"] = split(v["heavy"])
            pw.setdefault("lists", {})["protected"] = split(v["protected"]); pw["lists"]["hogs"] = split(v["hogs"])
            pw.setdefault("afterburner", {}).update(oc_profile=int(v["ab_oc"].get()), stock_profile=int(v["ab_stock"].get()))
            s = cfg["sun"]
            s["enabled"] = bool(v["s_enabled"].get()); s["strength"] = int(v["s_strength"].get()); s["transition_min"] = int(v["s_trans"].get())
            lat, lon = v["s_lat"].get().strip(), v["s_lon"].get().strip()
            s["lat"], s["lon"] = (float(lat), float(lon)) if lat and lon else (None, None)
            if s["lat"] is None and os.path.exists(GEO_PATH):
                os.remove(GEO_PATH)
            cl = cfg["claude"]
            cl["mode"] = v["c_mode"].get(); cl["api_model"] = v["c_model"].get().strip() or CB.GOOD_MODEL
            cl["rate_adaptive"] = bool(v["c_adaptive"].get())
            cl["rate_min_per_hour"] = max(0.0, float(v["c_rmin"].get()))
            cl["rate_max_per_hour"] = max(cl["rate_min_per_hour"], float(v["c_rmax"].get()))
            cl["daily_reserve"] = max(0, int(v["c_reserve"].get()))
            cl["daily_call_cap"] = max(1, int(v["c_cap"].get()))
            if v["c_key"].get().strip():
                cl["api_key_enc"] = CB.encrypt_key(v["c_key"].get().strip())
            cfg["security"]["weekly"] = bool(v["sec_weekly"].get())
            au = cfg["audio"]
            au["show_in_panel"] = bool(v["a_panel"].get())
            au["auto_switch"] = bool(v["a_auto"].get())
            au["headphone_id"] = self._settings_out_map.get(v["a_hp"].get(), au.get("headphone_id", ""))
            au["speaker_id"] = self._settings_out_map.get(v["a_sp"].get(), au.get("speaker_id", ""))
            au["switch_mic"] = bool(v["a_mic"].get())
            au["mic_on_headphones_id"] = self._settings_in_map.get(v["a_mh"].get(), au.get("mic_on_headphones_id", ""))
            au["mic_on_speakers_id"] = self._settings_in_map.get(v["a_ms"].get(), au.get("mic_on_speakers_id", ""))
            rem = cfg.setdefault("remote", {})
            rem["enabled"] = bool(v["r_enabled"].get())
            rem["wide_to_16x9"] = bool(v["r_16x9"].get())
            rem["width"] = max(0, int(v["r_w"].get()))
            rem["height"] = max(0, int(v["r_h"].get()))
            rem["lock_on_physical"] = bool(v["r_lock_phys"].get())
        except Exception as e:
            self.settings_msg.config(text=f"Could not read a value: {e}")
            return
        save_config(cfg)
        self.audio._last_hp = None            # re-evaluate the auto-switch against the new pair
        self.gate.presses, self.gate.window, self.gate.clicks = cfg["presses"], cfg["window_seconds"], cfg["wake_on_click"]
        self.hotkeys.apply(cfg["hotkeys"])
        self.engine.thr.cfg = cfg["power"].get("lists", {})
        if cfg["sun"]["enabled"]:
            self.sun_day = {}
        self.root.after(300, lambda: self.settings_msg.config(
            text="Saved. " + ("; ".join(self.hotkeys.failed) if self.hotkeys.failed else "Hotkeys active.")))
        log.info("settings saved")

    # -- sleep / wake --------------------------------------------------------------
    # The wake path must not depend on the Tk thread: twice now the UI thread stopped
    # responding during a long sleep. sleep() is entered on the UI thread (it creates
    # windows); everything that gets the user back (locking, taskbar, monitors,
    # brightness, helper) runs on worker threads, and Tk is only asked, best-effort,
    # to tear its overlay windows down.
    def sleep(self):
        if self.asleep:
            return
        if threading.current_thread() is not threading.main_thread():
            self.post(self.sleep)
            return
        self.hide_panel()
        self.asleep = True
        self._wake_done = False
        self._dark = False
        self.dark_plan = {}
        self.remote_active = False
        self._remote_main = None
        self._remote_resized = False
        log.info("=== sleep ===")
        self.engine.set_context(asleep=True)
        if self.icon:
            self.icon.icon = make_icon_image(asleep=True)
            self.icon.title = "Lights Out - asleep (press any key twice to wake)"
        self.refresh_monitors()
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        # Locking happens on the lights-off worker once every screen is dark, so the
        # lock screen is never drawn on a lit monitor; the helper's watchdog locks
        # for us if this process dies in between.
        lock_now = self.cfg.get("lock_on_sleep", True) and not self.selftest
        if self.cfg.get("hide_taskbar_asleep", True):
            self.hidden_taskbars = taskbar_windows()
            for h in self.hidden_taskbars:
                user32.ShowWindow(h, 0)
        if not lock_now:
            # black windows only matter while the desktop stays unlocked; on the lock
            # screen there is nothing to cover, and fewer windows means less to hang
            for m in self.monitors:
                o = tk.Toplevel(self.root, bg="black", cursor="none")
                o.overrideredirect(True)
                o.attributes("-topmost", True)
                l, t, r, b = m.rect
                o.geometry(f"{r - l}x{b - t}+{l}+{t}")
                o.deiconify()
                o.lift()
                self.overlays.append(o)
            self.root.update()
        self.gate.start(self.wake)
        if self.cfg.get("remote", {}).get("enabled", True):
            try:
                self.crd.start(on_connect=lambda: self.post(self._remote_connected),
                               on_disconnect=lambda: self.post(self._remote_disconnected))
            except Exception:
                log.exception("crd watcher start")
        # Our own fingerprint session only when the desktop stays unlocked; on the
        # lock screen Windows Hello owns the sensor and the unlock itself wakes us.
        if not lock_now and self.cfg.get("fingerprint_wake", True) and self.fp.available():
            if not self.fp.start(self.wake):
                log.warning("fingerprint wake not armed (sensor busy?)")
        self.saved = {}
        for m in self.monitors:
            entry = {"name": m.name}
            if m.brightness is not None:
                entry["backlight"] = m.brightness
            if m.hdr:
                entry["sdr_nits"] = m.sdr_white_nits()
            self.saved[m.device] = entry
        self.topo = topology.snapshot()          # the layout to put back if a monitor gets disconnected
        self.detached = set()
        self.standby_targets = set()
        self._touch_heartbeat()
        self._write_sentinel()
        self._notify_helper_asleep()
        threading.Thread(target=self._lights_off, args=(list(self.monitors), lock_now), name="lights-off", daemon=True).start()
        threading.Thread(target=self._asleep_loop, name="asleep-loop", daemon=True).start()
        log.info("asleep (locking=%s); saved %s", lock_now, self.saved)

    def _darken_core(self, mons, exclude=frozenset()):
        """Take each monitor dark: DDC/CI standby where the monitor implements it,
        otherwise disconnect its video signal. Sets self.standby_targets and
        self.detached; returns the plan dict. Monitors whose target is in
        `exclude` are left on (the remote-desktop mode keeps the main one lit).
        Does not dim or lock - the caller does that."""
        plan = {"standby": [], "disconnected": [], "dimmed": []}
        to_detach = {}
        standby = set()
        for m in mons:
            tgt = (m.adapter[0], m.adapter[1], m.target) if m.adapter is not None else None
            if tgt is not None and tgt in exclude:
                continue
            if self.cfg["ddc_standby"] and m.standby_capable():
                m.power(False)
                plan["standby"].append(m.name)
                if tgt is not None:
                    standby.add(tgt)
            elif self.cfg.get("detach_no_standby", True) and self.topo is not None and tgt is not None:
                to_detach[tgt] = m.name
            else:
                plan["dimmed"].append(m.name)
        self.standby_targets = standby
        if to_detach:
            rc = topology.detach(self.topo, set(to_detach))
            if rc == 0:
                self.detached = set(to_detach)
                plan["disconnected"] = list(to_detach.values())
                log.info("disconnected to turn off (no DDC/CI standby): %s", plan["disconnected"])
            else:
                plan["dimmed"] += list(to_detach.values())
                log.warning("could not disconnect %s (rc %s): left dimmed", list(to_detach.values()), rc)
        return plan

    # -- remote desktop (Chrome Remote Desktop) ------------------------------------
    # When a CRD client connects while asleep, bring the main monitor up at 16:9
    # for the session and leave the others off. Windows stays locked: the remote
    # user signs in once through CRD (this never stores or types a password, never
    # bypasses the lock). On disconnect, or a real keystroke at the desk ("switched
    # to manual"), it locks and goes back to the dark sleep state.
    def _remote_connected(self):
        """A CRD client connected (runs on the UI thread). Hand the main monitor
        to the session and stop treating the sign-in as a wake."""
        if not self.asleep or self.remote_active:
            return
        if not self.cfg.get("remote", {}).get("enabled", True):
            return
        log.info("=== remote desktop connected ===")
        self.remote_active = True
        self.gate.stop()                 # the CRD sign-in must not fire the wake gate
        if self.fp.active or self.fp._sess is not None:
            threading.Thread(target=self.fp.stop, name="fp-stop", daemon=True).start()
        self.engine.set_context(asleep=False)    # a remote session may game: let power scale
        if self.cfg.get("remote", {}).get("lock_on_physical", True):
            self.pkw.start(lambda why: self.post(self._remote_manual, why))
        threading.Thread(target=self._remote_enter, name="remote-enter", daemon=True).start()

    def _remote_disconnected(self):
        """The CRD client went away: back to dark sleep, still locked."""
        if not self.remote_active:
            return
        log.info("=== remote desktop disconnected ===")
        self.remote_active = False
        self.pkw.stop()
        self.engine.set_context(asleep=True)
        threading.Thread(target=self._remote_leave, args=("disconnected", False),
                         name="remote-exit", daemon=True).start()

    def _remote_manual(self, why):
        """A real keystroke arrived at the desk during a remote session: lock at
        once (a walk-up must never land in the remote user's session) and revert."""
        if not self.remote_active:
            return
        log.info("=== remote: manual takeover (%s) -> lock ===", why)
        self.remote_active = False
        self.pkw.stop()
        self.engine.set_context(asleep=True)
        user32.LockWorkStation()
        threading.Thread(target=self._remote_leave, args=("manual input", True),
                         name="remote-manual", daemon=True).start()

    def _remote_enter(self):
        """Worker: make the desktop just the main monitor, at 16:9."""
        with self._remote_lock:
            try:
                if not self.remote_active or not self.asleep:
                    return
                if self.topo is not None:
                    topology.restore(self.topo)          # clean base: everything back
                for m in self.monitors:
                    m.close()
                mons = mon.enumerate_monitors(probe_wmi=False, retries=2)
                self.monitors = mons
                main = next((m for m in mons if m.primary), mons[0] if mons else None)
                if main is None or main.adapter is None:
                    log.warning("remote: no main monitor to bring up")
                    return
                others = {(m.adapter[0], m.adapter[1], m.target) for m in mons
                          if m is not main and m.adapter is not None}
                if others and self.topo is not None:
                    rc = topology.detach(self.topo, others)
                    self.detached = set(others) if rc == 0 else set()
                    log.info("remote: kept main %s, disconnected %d other(s) rc=%s",
                             main.device, len(others), rc)
                self.standby_targets = set()
                self._remote_main = main.device
                self._remote_resized = False
                if self.cfg.get("remote", {}).get("wide_to_16x9", True):
                    w = int(self.cfg["remote"].get("width") or 0)
                    h = int(self.cfg["remote"].get("height") or 0)
                    if not (w and h):
                        best = mon.best_16x9_mode(main.device)
                        if best:
                            w, h = best
                    if w and h and mon.set_resolution(main.device, w, h):
                        self._remote_resized = True
                        log.info("remote: main set to %dx%d (16:9)", w, h)
                    else:
                        log.info("remote: left main at native (no 16:9 mode applied)")
                try:
                    main.power(True)
                except Exception:
                    pass
                self._dark = False
                self._write_sentinel()
                log.info("remote: desktop is the main monitor only; sign in via CRD")
            except Exception:
                log.exception("remote enter")

    def _remote_leave(self, reason, locked_already):
        """Worker: resolution back to native, every monitor dark again, locked."""
        with self._remote_lock:
            try:
                log.info("remote: leaving (%s)", reason)
                if self._remote_resized and self._remote_main:
                    mon.reset_resolution(self._remote_main)
                self._remote_resized = False
                self._remote_main = None
                if self.topo is not None:
                    topology.restore(self.topo)
                for m in self.monitors:
                    m.close()
                mons = mon.enumerate_monitors(probe_wmi=False, retries=2)
                self.monitors = mons
                self.detached = set()
                self.standby_targets = set()
                for m in mons:
                    if self.cfg["dim_on_sleep"]:
                        if m.ddc or m.wmi:
                            m.set(m.min)
                        if m.hdr:
                            m.set_sdr_white_nits(mon.SDR_MIN_NITS)
                self.dark_plan = self._darken_core(mons)
                self._dark = True
                self._write_sentinel()
                self._notify_helper_asleep()
                if not locked_already and (self.cfg.get("lock_on_sleep", True) or reason == "manual input"):
                    if not user32.LockWorkStation():
                        log.warning("remote leave: LockWorkStation failed %s", ctypes.get_last_error())
            except Exception:
                log.exception("remote leave")
            finally:
                if self.asleep and not self.remote_active:
                    self.gate.start(self.wake)    # resume the normal two-tap / sign-in wake

    def _lights_off(self, mons, lock_now):
        """Worker: dim, take every monitor dark, then lock. DDC/CI standby where
        the monitor implements it; otherwise its video signal is cut (the way
        Settings > Disconnect this display does), which is the only thing that
        turns such a backlight off. The lock comes last so the lock screen is
        never drawn on a lit screen."""
        plan = {"standby": [], "disconnected": [], "dimmed": []}
        try:
            for m in mons:
                if self.cfg["dim_on_sleep"]:
                    if m.ddc or m.wmi:
                        m.set(m.min)
                    if m.hdr:
                        m.set_sdr_white_nits(mon.SDR_MIN_NITS)
            time.sleep(0.2)
            if self._wake_done:                  # woken already (an early key): leave the screens alone
                return
            plan = self._darken_core(mons)
        except Exception:
            log.exception("lights off")
        finally:
            self.dark_plan = plan
            # persist the final plan (standby + detached) so the helper watchdog and
            # a fresh start can cycle the right monitors if this process dies asleep
            if self.asleep and not self._wake_done:
                self._write_sentinel()
                self._notify_helper_asleep()
            self._dark = True
            if lock_now:
                time.sleep(1.2)                  # let the panels blank before the lock screen exists
                if self.asleep and not self._wake_done:
                    if not user32.LockWorkStation():
                        log.warning("LockWorkStation failed: %s", ctypes.get_last_error())
                    else:
                        log.info("Windows locked (screens dark: %s)", plan)

    def _touch_heartbeat(self):
        try:
            with open(HEARTBEAT_PATH, "a"):
                pass
            os.utime(HEARTBEAT_PATH, None)
        except OSError:
            pass

    def _sleep_state(self):
        """What the helper watchdog and a fresh start need to put the desk back:
        who is asleep, what to restore, the display layout, and which monitors
        were parked in DDC/CI standby so they can be signal-cycled awake."""
        return {"pid": os.getpid(), "heartbeat": HEARTBEAT_PATH, "saved": self.saved,
                "topology": self.topo.to_json() if self.topo else None,
                "standby": [list(t) for t in self.standby_targets]}

    def _write_sentinel(self):
        try:
            with open(SENTINEL_PATH, "w", encoding="utf-8") as f:
                json.dump(dict(self._sleep_state(), since=dt.datetime.now().isoformat(timespec="seconds")), f)
        except OSError:
            log.exception("sentinel")

    def _notify_helper_asleep(self):
        self.engine.helper.call("/asleep", self._sleep_state())

    def _asleep_loop(self):
        """Worker thread while asleep: proves the process is alive to the helper's
        watchdog and keeps the overlays (unlocked mode) on top, asking Tk for the
        latter without ever waiting on it."""
        while self.asleep:
            if time.monotonic() - self._hb < 10:
                self._touch_heartbeat()          # only while the UI thread is pumping too
            if self.overlays:
                try:
                    self.root.after(0, self._lift_overlays)
                except Exception:
                    pass
            time.sleep(2)

    def _lift_overlays(self):
        for o in self.overlays:
            try:
                o.lift()
                o.attributes("-topmost", True)
            except Exception:
                pass

    def wake(self, why="manual", wait=False):
        """Thread-safe, idempotent. Called straight from the gate and fingerprint
        threads; runs the work on its own thread so no caller is ever blocked."""
        if not self.asleep:
            return
        t = threading.Thread(target=self._wake_worker, args=(why,), name="wake", daemon=True)
        t.start()
        if wait:
            t.join(20)

    def _wake_worker(self, why):
        with self._wake_lock:
            if self._wake_done or not self.asleep:
                return
            self._wake_done = True
        log.info("=== wake (%s) ===", why)
        self.last_wake_why = why
        self.asleep = False                      # new sleep requests are allowed from here on
        self.gate.stop()
        try:
            self.crd.stop()
        except Exception:
            pass
        if self.remote_active:
            self.remote_active = False
            try:
                self.pkw.stop()
            except Exception:
                pass
            if self._remote_resized and self._remote_main:
                try:
                    mon.reset_resolution(self._remote_main)
                except Exception:
                    pass
            self._remote_resized = False
        if self.fp.active or self.fp._sess is not None:
            threading.Thread(target=self.fp.stop, name="fp-stop", daemon=True).start()
        locked_now = bool(session_locked())
        # the user just authenticated (sign-in, or a matched finger on an open desktop)
        authenticated = why == "session unlocked" or (why == "fingerprint" and self.cfg.get("fingerprint_unlocks", False))
        if self.cfg["lock_on_wake"] and not authenticated and not locked_now and not self.selftest:
            if not user32.LockWorkStation():
                log.warning("LockWorkStation failed: %s", ctypes.get_last_error())
        for h in self.hidden_taskbars or taskbar_windows():
            user32.ShowWindow(h, 5)
        self.hidden_taskbars = []
        mons, saved, topo = self.monitors, dict(self.saved), self.topo
        self.monitors = []
        self._restore_thread = threading.Thread(target=self._lights_on, args=(mons, saved, topo), name="restore", daemon=True)
        self._restore_thread.start()
        try:
            os.remove(SENTINEL_PATH)
        except OSError:
            pass
        self.engine.helper.call("/awake", {})
        self.engine.set_context(asleep=False)
        if self.icon:
            self.icon.icon = make_icon_image()
            self.icon.title = "Lights Out"
        try:
            self.root.after(0, self._after_wake_ui)      # overlays, execution state: UI thread, best-effort
        except Exception:
            pass
        if locked_now and why != "session unlocked":
            # woke onto the lock screen (a key, a click): if nobody signs in, go dark again
            mins = float(self.cfg.get("relock_minutes", 3) or 0)
            if mins > 0:
                threading.Timer(mins * 60, self._resleep_if_still_locked).start()
        log.info("awake")

    def _after_wake_ui(self):
        for o in self.overlays:
            try:
                o.destroy()
            except Exception:
                pass
        self.overlays = []
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)

    def _lights_on(self, mons, saved, topo):
        deadline = time.monotonic() + 20
        while not self._dark and time.monotonic() < deadline:
            time.sleep(0.1)                      # the lights-off worker may still be at it (an early wake)
        standby = set(self.standby_targets)
        if topo is not None and (self.detached or standby):
            if standby:
                # signal-cycle the standby monitors (they ignore D6=1) and, in the
                # same pass, bring any disconnected monitor back
                ok = topology.wake_standby(topo, standby)
                log.info("woke standby %s + reconnected %s -> %s", sorted(standby), sorted(self.detached), ok)
            else:
                ok = topology.restore(topo)
                log.info("reconnected %s -> %s", sorted(self.detached), ok)
            self.detached = set()
            self.standby_targets = set()
            for m in mons:
                m.close()
            mons = mon.enumerate_monitors(probe_wmi=False)   # fresh handles after the layout change
        for m in mons:
            try:
                m.power(True)
            except Exception:
                log.exception("power on")
        self._restore(mons, saved)

    def _resleep_if_still_locked(self):
        if not self.asleep and session_locked():
            log.info("still on the lock screen after the wake: going dark again")
            self.sleep()                         # posts itself to the UI thread

    def _restore(self, mons, saved):
        def entry(m):
            e = saved.get(m.device)
            if e is None:       # a reconnected monitor can come back under another device name
                e = next((v for v in saved.values() if v.get("name") == m.name), None)
            return e or {}
        deadline = time.monotonic() + 15
        todo = {m.device: m for m in mons if entry(m).get("backlight") is not None and (m.ddc or m.wmi)}
        for m in mons:
            nits = entry(m).get("sdr_nits")
            if nits:
                m.set_sdr_white_nits(nits)
        while todo and time.monotonic() < deadline:
            for dev, m in list(todo.items()):
                want = entry(m)["backlight"]
                if m.set(want) and (m.wmi or m.read() == want):
                    del todo[dev]
            if todo:
                time.sleep(0.5)
        if todo:
            log.warning("brightness not confirmed on %s", [m.name for m in todo.values()])
        log.info("brightness restored: %s", saved)
        for m in mons:
            m.close()

    def quit(self):
        if self.asleep:
            self.wake("quit", wait=True)
        if self._restore_thread is not None:
            self._restore_thread.join(15)        # a disconnected monitor must be back before we exit
        self.engine.stop()
        self.engine.join(8)
        try:
            self.audio.stop()
        except Exception:
            pass
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.after(100, self.root.destroy)

    def run(self):
        self.root.mainloop()


# -- selftest -----------------------------------------------------------------------------
def inject_key_tap(vk=0x10, mods=()):
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT), ("pad", ctypes.c_ulong * 2)]

    def send(key, up):
        inp = INPUT(type=1)
        inp.ki.wVk, inp.ki.dwFlags = key, (2 if up else 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.04)

    for mk in mods:
        send(mk, False)
    send(vk, False)
    send(vk, True)
    for mk in reversed(mods):
        send(mk, True)


def inject_click():
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("mi", MOUSEINPUT)]

    for flags in (0x2, 0x4):                      # left button down, up
        inp = INPUT(type=0)
        inp.mi.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.04)


def selftest(app):
    before = {m.device: (m.brightness, m.sdr_white_nits() if m.hdr else None) for m in mon.enumerate_monitors(probe_wmi=False)}
    displays_before = len(topology.active_targets())
    result = {"ok": False}

    def step0():
        inject_key_tap(ord("B"), mods=(0x11, 0x12))        # ctrl+alt+b opens the panel
        app.root.after(1500, step0b)

    def step0b():
        result["panel_via_hotkey"] = bool(app.panel and app.panel.winfo_exists())
        st = app.engine.snapshot()
        result["engine_profile"] = st.get("profile")
        result["engine_reason"] = st.get("reason")
        result["helper"] = st.get("helper")
        result["sun_factor_now"] = round(app._sun_factor(), 2)
        result["fingerprint_reader"] = app.fp.description if app.fp.available() else None
        app.hide_panel()
        app.root.after(300, step1)

    def wait_dark(nxt, secs=15.0):                         # the lights-off worker probes and disconnects: give it time
        if app._dark or secs <= 0:
            app.root.after(400, nxt)
        else:
            app.root.after(250, lambda: wait_dark(nxt, secs - 0.25))

    def step1():                                           # sleep #1: two mouse clicks must wake it
        app.sleep()
        wait_dark(step2)

    def step2():
        result["asleep"] = app.asleep
        result["dark_plan"] = app.dark_plan
        result["displays_while_asleep"] = len(topology.active_targets())
        result["fingerprint_armed"] = app.fp.active if app.fp.available() else "n/a"
        result["taskbar_hidden"] = all(not user32.IsWindowVisible(h) for h in app.hidden_taskbars) and bool(app.hidden_taskbars)
        result["sentinel_written"] = os.path.exists(SENTINEL_PATH)
        result["heartbeat_fresh"] = os.path.exists(HEARTBEAT_PATH) and time.time() - os.path.getmtime(HEARTBEAT_PATH) < 5
        result["watchdog_armed"] = bool((app.engine.helper.call("/status") or {}).get("watchdog"))
        result["dimmed"] = {m.device: (m.brightness, m.sdr_white_nits() if m.hdr else None) for m in app.monitors}
        inject_click()
        app.root.after(800, step3)

    def step3():
        inject_click()
        app.root.after(1500, step4)

    def step4():
        result["awake_after_two_clicks"] = not app.asleep
        result["click_wake_reason"] = app.last_wake_why
        result["fingerprint_disarmed"] = (not app.fp.active) if app.fp.available() else "n/a"
        result["sentinel_cleared"] = not os.path.exists(SENTINEL_PATH)
        app.root.after(2500, step5)

    def step5():                                           # sleep #2: two key taps must wake it
        app.sleep()
        wait_dark(step6)

    def step6():
        inject_key_tap()
        app.root.after(800, step7)

    def step7():
        inject_key_tap()
        app.root.after(1500, step8)

    def step8():
        result["awake_after_two_taps"] = not app.asleep
        result["taskbar_back"] = all(user32.IsWindowVisible(h) for h in taskbar_windows())
        for _ in range(30):
            after = {m.device: (m.brightness, m.sdr_white_nits() if m.hdr else None) for m in mon.enumerate_monitors(probe_wmi=False)}
            if after == before:
                break
            time.sleep(0.5)
        result["restored"] = after == before
        result["before"], result["after"] = before, after
        result["displays_awake"] = len(topology.active_targets())
        result["reconnected"] = result["displays_awake"] == displays_before
        checks = ("awake_after_two_clicks", "awake_after_two_taps", "restored", "reconnected", "panel_via_hotkey", "taskbar_hidden",
                  "taskbar_back", "sentinel_written", "sentinel_cleared", "heartbeat_fresh", "watchdog_armed")
        result["ok"] = all(result.get(k) for k in checks) and result.get("fingerprint_armed") in (True, "n/a") \
            and result.get("fingerprint_disarmed") in (True, "n/a")
        print(json.dumps(result, indent=2))
        app.quit()

    app.root.after(2500, step0)
    app.run()
    sys.exit(0 if result["ok"] else 1)


def main():
    setup_logging()
    if "--install" in sys.argv or "--uninstall" in sys.argv:
        import install
        lines = install.uninstall() if "--uninstall" in sys.argv else install.install()
        log.info("install: %s", lines)
        user32.MessageBoxW(None, "\n".join(lines), "Lights Out", 0x40)
        return
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)      # the error code must be read the safe way
    mutex = k32.CreateMutexW(None, False, "Local\\LightsOutTray")
    if ctypes.get_last_error() == 183:                          # ERROR_ALREADY_EXISTS: another instance is up
        open(SIGNAL_PATH, "w").close()
        log.info("another instance is running: asked it to open the panel")
        return
    log.info("starting (pid %s, frozen=%s, data=%s)", os.getpid(), FROZEN, DATA_DIR)
    _trace("logging up, mutex taken")
    try:
        app = App(selftest="--selftest" in sys.argv)
        _trace("app ready")
        selftest(app) if app.selftest else app.run()
        _trace("mainloop ended")
    except SystemExit:
        raise
    except Exception:
        log.exception("fatal")
        raise
    finally:
        kernel32.ReleaseMutex(mutex)


if __name__ == "__main__":
    main()
