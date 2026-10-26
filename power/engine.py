"""The power brain. Every couple of seconds it looks at what the machine is
doing (sensors), decides a profile, and applies it through whatever levers
this PC has:

  profile      Windows power mode   plan boost   background apps   ASUS firmware (helper)
  quiet        best efficiency      efficient    hogs slowed       silent
  balanced     balanced             efficient    hogs slowed       balanced
  performance  best performance     aggressive   all slowed        turbo
  max          best performance     agg@guar.    all slowed        turbo + raised PPT + fan curve (+ GPU OC profile)

"max" is the overclock tier. It is only ever entered when temperatures are
under the limits in config, and a thermal guard drops it the moment they are
not (and backs off further to balanced + full fans in a panic). The guard
runs on every sample, with hysteresis so it never flaps.

Manual override: mode = quiet/balanced/performance/max pins the profile; the
thermal guard still applies on top of it. mode = auto lets decide() choose.
"""
import collections
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request

from . import sensors as S
from . import throttle as T
from . import windows_power as W

log = logging.getLogger("lightsout.engine")
PROFILES = ("quiet", "balanced", "performance", "max")
ASUS_MODE = {"quiet": "silent", "balanced": "balanced", "performance": "turbo", "max": "boost"}
THROTTLE_LEVEL = {"quiet": "hogs", "balanced": "hogs", "performance": "all", "max": "all"}

DEFAULTS = {
    "enabled": True,
    "mode": "auto",                 # auto | quiet | balanced | performance | max
    "interval_s": 2,
    "idle_minutes": 5,              # no input this long -> quiet (unless a heavy job is running)
    "load_low": 18, "load_high": 55, "max_load": 85,
    "gpu_high": 45, "gpu_max": 85,
    "dwell_up_s": 6, "dwell_down_s": 45,
    "allow_max": True,
    "throttle_background": True,
    "heavy_apps": ["resolve.exe", "blender.exe", "unrealengine", "cura.exe", "prusa-slicer.exe",
                   "handbrake.exe", "ffmpeg.exe", "premiere.exe", "afterfx.exe", "fusion360.exe",
                   "solidworks", "capcut.exe", "obs64.exe"],
    "light_apps": [],
    "lists": {"protected": [], "hogs": []},
    "thermal": {"cpu_max": 92, "cpu_panic": 97, "gpu_max": 86, "hysteresis": 6, "hold_s": 60},
    "boost": {"ppt_total_w": 80, "ppt_apu_w": 65,
              "fan_temps": [20, 45, 55, 62, 68, 74, 80, 88], "fan_speeds": [10, 30, 45, 60, 75, 90, 100, 100]},
    "ryzenadj": {"path": "", "performance": "", "max": "--tctl-temp=95", "quiet": "", "balanced": ""},
    "afterburner": {"path": r"C:\Program Files (x86)\MSI Afterburner\MSIAfterburner.exe", "oc_profile": 0, "stock_profile": 0},
    "helper_port": 5178,
}


class HelperClient:
    """Talks to the elevated helper (helper.pyw) on 127.0.0.1. Starts it via
    its scheduled task when it is down, at most once every few minutes."""

    def __init__(self, port, token_path, task_name="LightsOutHelper"):
        self.base = f"http://127.0.0.1:{port}"
        self.token_path = token_path
        self.task = task_name
        self.token = None
        self.ok = False
        self.last_start_try = 0
        self.status = {}

    def _token(self):
        if not self.token:
            try:
                with open(self.token_path, encoding="utf-8") as f:
                    self.token = f.read().strip()
            except OSError:
                return None
        return self.token

    def call(self, path, data=None, timeout=3):
        tok = self._token()
        if not tok:
            self.ok = False
            return None
        req = urllib.request.Request(self.base + path, method="POST" if data is not None else "GET",
                                     headers={"X-Token": tok, "Content-Type": "application/json"},
                                     data=json.dumps(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self.ok = True
                return json.loads(r.read().decode())
        except Exception as e:
            if self.ok:
                log.warning("helper call %s failed: %s", path, e)
            self.ok = False
            return None

    def ensure_running(self):
        """Start the helper: through its elevated scheduled task when installed,
        otherwise directly (unelevated: firmware control still works on this
        ASUS; only RyzenAdj and the privileged security checks need admin)."""
        if self.ok or time.time() - self.last_start_try < 300:
            return
        self.last_start_try = time.time()
        try:
            r = subprocess.run(["schtasks", "/run", "/tn", self.task], capture_output=True, timeout=15,
                               creationflags=0x08000000)
            if r.returncode == 0:
                log.info("asked Task Scheduler to start %s", self.task)
                return
        except Exception as e:
            log.debug("schtasks: %s", e)
        try:
            frozen = getattr(sys, "frozen", False)
            app_dir = os.path.dirname(os.path.abspath(sys.executable if frozen else sys.argv[0]))
            exe = os.path.join(app_dir, "LightsOutHelper.exe")
            if os.path.exists(exe):
                cmd = [exe]
            else:
                pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                cmd = [pythonw if os.path.exists(pythonw) else sys.executable, os.path.join(app_dir, "helper.pyw")]
            subprocess.Popen(cmd, cwd=app_dir, creationflags=0x08000000)
            log.info("started helper directly (unelevated): %s", cmd[0])
        except Exception:
            log.exception("helper start")


class Engine(threading.Thread):
    def __init__(self, cfg, app_dir, data_dir, on_change=None):
        super().__init__(name="power-engine", daemon=True)
        self.cfg = cfg                          # the "power" dict of the app config (live reference)
        self.on_change = on_change
        self.sens = S.Sensors(process_scan_every=5)      # per-process scan every 10 s
        self.thr = T.Throttler(cfg.get("lists", {}))
        self.plan = W.PlanTuner(os.path.join(data_dir, "plan_backup.json"))
        self.helper = HelperClient(cfg.get("helper_port", 5178), os.path.join(data_dir, "helper.token"))
        self.hist = collections.deque(maxlen=60)
        self.profile = None
        self.reason = "starting"
        self.last_switch = 0
        self.locked = False
        self.asleep = False
        self.thermal_cap = None                 # profile ceiling imposed by temperature
        self.thermal_until = 0
        self.thermal_note = ""
        self.state = {}
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._oc_on = False

    # -- context from the app -----------------------------------------------
    def set_context(self, locked=None, asleep=None):
        if locked is not None:
            self.locked = locked
        if asleep is not None:
            self.asleep = asleep

    def set_mode(self, mode):
        self.cfg["mode"] = mode
        self.last_switch = 0                    # a manual choice applies right away

    def stop(self):
        self._halt.set()

    # -- the loop ------------------------------------------------------------------
    def run(self):
        log.info("engine up (mode=%s)", self.cfg.get("mode"))
        tick = 0
        while not self._halt.is_set():
            try:
                self.tick(tick)
            except Exception:
                log.exception("engine tick")
            tick += 1
            self._halt.wait(float(self.cfg.get("interval_s", 2)))
        self.shutdown()

    def shutdown(self):
        try:
            self.thr.release_all()
            self.plan.restore()
            if self._oc_on:
                self._afterburner(False)
            self.helper.call("/mode", {"name": "balanced"})
        except Exception:
            log.exception("engine shutdown")
        log.info("engine down")

    def tick(self, n):
        s = self.sens.sample()
        if n % 2 == 0:
            self.helper.ensure_running()
            st = self.helper.call("/status") or {}
            self.helper.status = st
        st = self.helper.status
        s["cpu_temp"] = st.get("cpu_temp")
        s["asus"] = st.get("asus", False)
        s["fans"] = st.get("fans")
        if s.get("gpu_temp") is None and st.get("gpu_temp"):
            s["gpu_temp"] = st["gpu_temp"]
        self.hist.append(s)
        enabled = self.cfg.get("enabled", True)
        if enabled:
            mode = self.cfg.get("mode", "auto")
            if mode in PROFILES:
                want, why = mode, "manual"
            else:
                want, why = self.decide(s)
            want, why = self.thermal_guard(s, want, why)
            self.maybe_switch(want, why, s)
        elif self.profile is not None:
            self.shutdown()
            self.profile, self.reason = None, "engine off"
        self.publish(s)

    # -- deciding ------------------------------------------------------------------
    def _avg(self, key, seconds):
        now = time.time()
        vals = [h[key] for h in self.hist if h.get(key) is not None and now - h["t"] <= seconds]
        return sum(vals) / len(vals) if vals else 0

    def decide(self, s):
        c = self.cfg
        cpu30, gpu30 = self._avg("cpu", 30), self._avg("gpu_util", 30)
        cpu10, gpu10 = self._avg("cpu", 10), self._avg("gpu_util", 10)
        fg = s.get("fg") or ""
        heavy_fg = any(h and h.lower() in fg for h in c.get("heavy_apps", []))
        light_fg = any(l and l.lower() in fg for l in c.get("light_apps", []))
        if self.asleep or self.locked:
            if cpu30 >= c["load_high"]:
                return "balanced", f"locked, but a job is running ({cpu30:.0f}% CPU)"
            return "quiet", "locked / asleep"
        if not s.get("on_ac", True):
            if cpu10 >= c["load_high"] or gpu10 >= c["gpu_high"]:
                return "balanced", f"on battery, busy ({cpu10:.0f}% CPU / {gpu10:.0f}% GPU)"
            return "quiet", "on battery"
        if s["idle_s"] >= c["idle_minutes"] * 60:
            if cpu30 >= c["load_high"] or gpu30 >= c["gpu_high"]:
                return "performance", f"unattended job ({cpu30:.0f}% CPU / {gpu30:.0f}% GPU)"
            return "quiet", f"idle {s['idle_s'] / 60:.0f} min"
        pressure = max(cpu30, gpu30)
        if c.get("allow_max") and pressure >= c["max_load"] and max(cpu10, gpu10) >= c["max_load"]:
            return "max", f"near the limit: {cpu30:.0f}% CPU / {gpu30:.0f}% GPU sustained"
        if heavy_fg:
            return "performance", f"{fg} in front"
        if s.get("fullscreen") and s.get("fg_on_gpu") and (gpu10 >= 30 or cpu10 >= 30):
            return "performance", f"fullscreen {fg} on the GPU"
        if cpu10 >= c["load_high"] or gpu10 >= c["gpu_high"]:
            return "performance", f"busy: {cpu10:.0f}% CPU / {gpu10:.0f}% GPU"
        if light_fg or cpu30 < c["load_low"] and gpu30 < 15:
            return "quiet", f"light use ({cpu30:.0f}% CPU)"
        return "balanced", f"normal use ({cpu30:.0f}% CPU / {gpu30:.0f}% GPU)"

    def thermal_guard(self, s, want, why):
        t = self.cfg["thermal"]
        cpu, gpu = s.get("cpu_temp") or 0, s.get("gpu_temp") or 0
        now = time.time()
        if cpu >= t["cpu_panic"]:
            self.thermal_cap, self.thermal_until = "balanced", now + t["hold_s"]
            self.thermal_note = f"CPU {cpu}°C: panic, cooling down"
            self.helper.call("/fans", {"full": True})
        elif cpu >= t["cpu_max"] or gpu >= t["gpu_max"]:
            if self.thermal_cap != "balanced" or now >= self.thermal_until:
                self.thermal_cap, self.thermal_until = "performance", now + t["hold_s"]
                self.thermal_note = f"{'CPU' if cpu >= t['cpu_max'] else 'GPU'} {max(cpu, gpu)}°C: no overclock"
        elif self.thermal_cap and now >= self.thermal_until and \
                cpu < t["cpu_max"] - t["hysteresis"] and gpu < t["gpu_max"] - t["hysteresis"]:
            self.thermal_cap, self.thermal_note = None, ""
        if self.thermal_cap and PROFILES.index(want) > PROFILES.index(self.thermal_cap):
            return self.thermal_cap, f"{self.thermal_note} (wanted {want}: {why})"
        return want, why

    def maybe_switch(self, want, why, s):
        now = time.time()
        if want == self.profile:
            self.reason = why
            return
        up = self.profile is None or PROFILES.index(want) > PROFILES.index(self.profile)
        dwell = self.cfg["dwell_up_s"] if up else self.cfg["dwell_down_s"]
        if self.thermal_cap and not up:
            dwell = 0                                            # cooling down never waits
        if now - self.last_switch < dwell:
            return
        self.apply(want, why, s)

    # -- applying ------------------------------------------------------------------
    def apply(self, profile, why, s):
        log.info("profile %s -> %s: %s", self.profile, profile, why)
        prev = self.profile
        self.profile, self.reason, self.last_switch = profile, why, time.time()
        try:
            W.set_overlay({"quiet": "battery", "balanced": "balanced"}.get(profile, "performance"))
        except Exception:
            log.exception("overlay")
        try:
            self.plan.apply(profile)
        except Exception:
            log.exception("plan")
        self._throttle(s)
        b = self.cfg.get("boost", {})
        self.helper.call("/mode", {"name": ASUS_MODE[profile], "boost": b,
                                   "ryzenadj": self.cfg.get("ryzenadj", {}).get(profile, "")})
        if profile == "max" and not self._oc_on:
            self._afterburner(True)
        elif profile != "max" and self._oc_on:
            self._afterburner(False)
        if self.on_change and prev != profile:
            try:
                self.on_change(profile, why)
            except Exception:
                log.exception("on_change")

    def _throttle(self, s):
        if not self.profile:
            return
        level = THROTTLE_LEVEL[self.profile] if self.cfg.get("throttle_background", True) else "none"
        try:
            n = self.thr.apply(level, s.get("fg"), s.get("fg_pid"), s.get("top", []))
            self.state["throttled"] = n
        except Exception:
            log.exception("throttle")

    def _afterburner(self, oc):
        ab = self.cfg.get("afterburner", {})
        prof = ab.get("oc_profile" if oc else "stock_profile") or 0
        path = ab.get("path", "")
        if not prof or not os.path.exists(path):
            return
        try:
            subprocess.Popen([path, f"-Profile{int(prof)}"], creationflags=0x08000000)
            self._oc_on = bool(oc)
            log.info("afterburner profile %s (%s)", prof, "OC" if oc else "stock")
        except Exception:
            log.exception("afterburner")

    def publish(self, s):
        if self.profile and self.sens.n % 10 == 0:
            self._throttle(s)                 # keep up with new processes (every 20 s)
        with self._lock:
            self.state.update({
                "profile": self.profile, "reason": self.reason, "mode": self.cfg.get("mode", "auto"),
                "cpu": s.get("cpu"), "mem": s.get("mem"), "gpu_util": s.get("gpu_util"),
                "cpu_temp": s.get("cpu_temp"), "gpu_temp": s.get("gpu_temp"), "fans": s.get("fans"),
                "fg": s.get("fg"), "idle_s": s.get("idle_s"), "on_ac": s.get("on_ac"), "battery": s.get("battery"),
                "pressure": round(max(self._avg("cpu", 30), self._avg("gpu_util", 30))),
                "top": s.get("top", [])[:5], "helper": self.helper.ok, "asus": s.get("asus"),
                "thermal": self.thermal_note, "gpu_power_w": s.get("gpu_power_w"),
            })

    def snapshot(self):
        with self._lock:
            return dict(self.state)
