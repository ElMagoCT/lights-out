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
    "dim_on_sleep": True, "ddc_standby": True, "hide_taskbar_asleep": True,
    "hotkeys": {"sleep": "ctrl+alt+s", "panel": "ctrl+alt+b",
                "brightness_up": "ctrl+alt+up", "brightness_down": "ctrl+alt+down"},
    "brightness_step": 10,
    "sun": {"enabled": False, "strength": 60, "transition_min": 45, "lat": None, "lon": None, "label": ""},
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
    if src:
        try:
            with open(src, encoding="utf-8") as f:
                _merge(cfg, json.load(f))
        except Exception:
            log.exception("config unreadable, using defaults")
    cfg.pop("schedule", None)                      # replaced by sun sync
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
from gate import WakeGate, session_locked  # noqa: E402

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
        self.hidden_taskbars = []
        self.writer = LatestWriter()
        self.gate = WakeGate(int(self.cfg["presses"]), float(self.cfg["window_seconds"]), clicks=bool(self.cfg.get("wake_on_click", True)))
        self.fp = fingerprint.FingerprintWatcher()
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
        self.security_busy = False
        self._style()
        self._recover_from_crash()            # a previous instance died while asleep?
        self._hb = time.monotonic()           # UI-thread heartbeat for the in-process hang detector
        threading.Thread(target=self._hang_detector, name="hang-detector", daemon=True).start()
        self.hotkeys = Hotkeys(lambda name: self.post(self.on_hotkey, name))
        self.hotkeys.apply(self.cfg["hotkeys"])
        self.engine.start()
        self.root.after(100, self._pump)
        self.root.after(500, self._poll_signal)
        self.root.after(3000, self._sun_tick)
        self.root.after(30_000, self._context_tick)
        self.root.after(90_000, self._security_tick)
        self.root.after(600_000, self._classify_tick)
        self._start_tray()

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

    def _context_tick(self):
        try:
            self.engine.set_context(locked=bool(session_locked()))
        except Exception:
            pass
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
        self.refresh_monitors()
        for m in self.monitors:
            cur = level_get(m)
            cur = self.last.get(m.device, 50) if cur is None else cur
            new = int(max(0, min(100, cur + step)))
            self._set_level(m, new)
        self._save_state()

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
    def refresh_monitors(self):
        for m in self.monitors:
            m.close()
        self.monitors = mon.enumerate_monitors()

    def show_panel(self):
        if self.asleep:
            return
        if self.panel and self.panel.winfo_exists():
            self.panel.destroy()
        self.refresh_monitors()
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
                ttk.Label(head, text="not answering, slider still sent", style="Dim.TLabel").pack(side="left", padx=(8, 0))
            elif level is not None:
                self.last[m.device] = level
            ttk.Label(f, text=level_kind(m), style="Dim.TLabel").grid(row=r + 1, column=0, columnspan=3, sticky="w")
            val = tk.IntVar(value=level if level is not None else 0)
            lab = ttk.Label(f, text=f"{val.get()}%", width=5, anchor="e")
            lab.grid(row=r + 2, column=2, sticky="e")
            sc = ttk.Scale(f, from_=0, to=100, orient="horizontal", length=300, variable=val)
            sc.grid(row=r + 2, column=0, columnspan=2, sticky="we", pady=(2, 0))
            for ev in ("<Button-1>", "<B1-Motion>"):
                sc.bind(ev, lambda e, m=m, val=val, lab=lab, sc=sc: self._scale_jump(e, m, val, lab, sc))
            sc.bind("<MouseWheel>", lambda e, m=m, val=val, lab=lab: self._scale_wheel(e, m, val, lab))
            if not controllable(m):
                sc.state(["disabled"])
            self.rows.append((m, val, lab, sc))
            r += 3
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
        hk = self.cfg["hotkeys"]
        sec = self.cfg["security"]
        inputs = "key presses or clicks" if self.cfg.get("wake_on_click", True) else "key presses"
        if self.cfg.get("lock_on_sleep", True):
            fp = " or a Windows Hello fingerprint" if self.fp.available() else ""
            foot = (f"Locks Windows, then wakes the screens on {self.cfg['presses']} {inputs} within "
                    f"{self.cfg['window_seconds']:g} s{fp}; your password or fingerprint gets you back in. ")
        else:
            fp = (", or a fingerprint" + (" (straight to the desktop)" if self.cfg.get("fingerprint_unlocks", False) else "")
                  if self.cfg.get("fingerprint_wake", True) and self.fp.available() else "")
            foot = f"Wakes on {self.cfg['presses']} {inputs} within {self.cfg['window_seconds']:g} s{fp}, to the lock screen. "
        foot += (f"Mouse movement never wakes it.  Hotkeys: {hk.get('sleep')} sleep, {hk.get('panel')} panel, "
                 f"{hk.get('brightness_up')} / {hk.get('brightness_down')} brightness.")
        if sec.get("last_summary"):
            foot += f"\nSecurity ({sec.get('last_run', '')[:10]}): {sec['last_summary']}."
        ttk.Label(f, text=foot, style="Dim.TLabel", wraplength=360).grid(row=r, column=0, columnspan=3, sticky="w")
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
        self.root.after(2000, self._panel_refresh)

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
        if st.get("throttled"):
            bits.append(f"{st['throttled']} apps slowed")
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
        self.sun_lab.config(text=f"night {max(0, 100 - s)}%" + ("" if f >= 0.999 else f" · now x{f:.2f}"))
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
        tabs = {name: ttk.Frame(nb, padding=14) for name in ("Sleep", "Power", "Display", "Claude", "Security")}
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
        ttk.Checkbutton(f, text="Lock Windows the moment it sleeps (password or Windows Hello to get back in, "
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

        # Claude tab
        f = tabs["Claude"]
        cl = cfg["claude"]
        r = 0
        ttk.Label(f, text="Claude", style="Section.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        ttk.Label(f, text="Used for two things only: a one-time cheap classification of unknown background apps, and the "
                          "write-up of the security assessment. Everything is cached and capped per day.",
                  style="Dim.TLabel", wraplength=560).grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        v["c_mode"] = tk.StringVar(value=cl.get("mode", "cli"))
        mf = ttk.Frame(f)
        ttk.Radiobutton(mf, text="Claude Code CLI (your account)", variable=v["c_mode"], value="cli").pack(side="left")
        ttk.Radiobutton(mf, text="API key", variable=v["c_mode"], value="api").pack(side="left", padx=(12, 0))
        r = row(f, r, "Use", mf)
        v["c_key"] = tk.StringVar(value="")
        r = row(f, r, "API key (saved encrypted; leave blank to keep)", ttk.Entry(f, textvariable=v["c_key"], width=46, show="•"))
        v["c_model"] = tk.StringVar(value=cl.get("api_model", CB.GOOD_MODEL)); v["c_cap"] = tk.IntVar(value=cl.get("daily_call_cap", 20))
        r = row(f, r, "API model for reports", ttk.Entry(f, textvariable=v["c_model"], width=30))
        r = row(f, r, "Daily call cap", ttk.Spinbox(f, from_=1, to=500, width=5, textvariable=v["c_cap"]))
        u = self.claude.usage.get(dt.date.today().isoformat(), {})
        ttk.Label(f, text=f"Status: {self.claude.status()}\nToday: {u.get('calls', 0)} calls, {u.get('in', 0)} in / {u.get('out', 0)} out tokens"
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
            cl["mode"] = v["c_mode"].get(); cl["api_model"] = v["c_model"].get().strip() or CB.GOOD_MODEL; cl["daily_call_cap"] = int(v["c_cap"].get())
            if v["c_key"].get().strip():
                cl["api_key_enc"] = CB.encrypt_key(v["c_key"].get().strip())
            cfg["security"]["weekly"] = bool(v["sec_weekly"].get())
        except Exception as e:
            self.settings_msg.config(text=f"Could not read a value: {e}")
            return
        save_config(cfg)
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
        log.info("=== sleep ===")
        self.engine.set_context(asleep=True)
        if self.icon:
            self.icon.icon = make_icon_image(asleep=True)
            self.icon.title = "Lights Out - asleep (press any key twice to wake)"
        self.refresh_monitors()
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        lock_now = self.cfg.get("lock_on_sleep", True) and not self.selftest
        if lock_now:
            # Lock first: a password (or Windows Hello) is required to get back in no
            # matter what happens to this process afterwards.
            if not user32.LockWorkStation():
                log.warning("LockWorkStation failed: %s", ctypes.get_last_error())
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
        # Our own fingerprint session only when the desktop stays unlocked; on the
        # lock screen Windows Hello owns the sensor and the unlock itself wakes us.
        if not lock_now and self.cfg.get("fingerprint_wake", True) and self.fp.available():
            if not self.fp.start(self.wake):
                log.warning("fingerprint wake not armed (sensor busy?)")
        self.saved = {}
        for m in self.monitors:
            entry = {}
            if m.brightness is not None:
                entry["backlight"] = m.brightness
            if m.hdr:
                entry["sdr_nits"] = m.sdr_white_nits()
            self.saved[m.device] = entry
        self._touch_heartbeat()
        self._write_sentinel()
        self.engine.helper.call("/asleep", {"pid": os.getpid(), "heartbeat": HEARTBEAT_PATH, "saved": self.saved})
        threading.Thread(target=self._lights_off, args=(list(self.monitors),), name="lights-off", daemon=True).start()
        threading.Thread(target=self._asleep_loop, name="asleep-loop", daemon=True).start()
        log.info("asleep (locked=%s); saved %s", lock_now, self.saved)

    def _lights_off(self, mons):
        try:
            for m in mons:
                if self.cfg["dim_on_sleep"]:
                    if m.ddc or m.wmi:
                        m.set(m.min)
                    if m.hdr:
                        m.set_sdr_white_nits(mon.SDR_MIN_NITS)
            if self.cfg["ddc_standby"]:
                time.sleep(0.2)
                for m in mons:
                    m.power(False)
        except Exception:
            log.exception("lights off")

    def _touch_heartbeat(self):
        try:
            with open(HEARTBEAT_PATH, "a"):
                pass
            os.utime(HEARTBEAT_PATH, None)
        except OSError:
            pass

    def _write_sentinel(self):
        try:
            with open(SENTINEL_PATH, "w", encoding="utf-8") as f:
                json.dump({"since": dt.datetime.now().isoformat(timespec="seconds"), "pid": os.getpid(),
                           "saved": self.saved}, f)
        except OSError:
            log.exception("sentinel")

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
        mons, saved = self.monitors, dict(self.saved)
        self.monitors = []
        threading.Thread(target=self._lights_on, args=(mons, saved), name="restore", daemon=True).start()
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

    def _lights_on(self, mons, saved):
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
        deadline = time.monotonic() + 15
        todo = {m.device: m for m in mons if saved.get(m.device)}
        for m in mons:
            nits = saved.get(m.device, {}).get("sdr_nits")
            if nits:
                m.set_sdr_white_nits(nits)
        while todo and time.monotonic() < deadline:
            for dev, m in list(todo.items()):
                want = saved[dev].get("backlight")
                if want is None or not (m.ddc or m.wmi):
                    del todo[dev]
                elif m.set(want) and (m.wmi or m.read() == want):
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
        self.engine.stop()
        self.engine.join(8)
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

    def step1():                                           # sleep #1: two mouse clicks must wake it
        app.sleep()
        app.root.after(1500, step2)

    def step2():
        result["asleep"] = app.asleep
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
        app.root.after(1500, step6)

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
        checks = ("awake_after_two_clicks", "awake_after_two_taps", "restored", "panel_via_hotkey", "taskbar_hidden", "taskbar_back",
                  "sentinel_written", "sentinel_cleared", "heartbeat_fresh", "watchdog_armed")
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
