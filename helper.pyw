"""Lights Out helper: the one elevated piece. Installed as the scheduled task
"LightsOutHelper" (run with highest privileges at logon) by "Install Lights
Out.bat"; the tray app starts it through that task when it is not running.

It serves a tiny JSON API on 127.0.0.1 only, protected by a random token
written to %LOCALAPPDATA%\\LightsOut\\helper.token (same user, so the tray
app can read it). What needs elevation lives here and nowhere else:

  * ASUS ROG firmware (ATKACPI): performance mode, PPT limits, fan curves,
    CPU/GPU temperatures, fan RPM  -- only on ASUS laptops that expose it
  * RyzenAdj (optional, if the user drops ryzenadj.exe into tools\\)
  * the privileged half of the security assessment (BitLocker, Secure Boot,
    SMB1, LSA protection)

Nothing here loops or polls: it sleeps on the socket until asked.
"""
import ctypes
import ctypes.wintypes
import json
import logging
import logging.handlers
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "LightsOut")
os.makedirs(DATA_DIR, exist_ok=True)
TOKEN_PATH = os.path.join(DATA_DIR, "helper.token")
LOG_PATH = os.path.join(DATA_DIR, "helper.log")
PORT = 5178
NOWIN = 0x08000000

log = logging.getLogger("helper")
h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=300_000, backupCount=1, encoding="utf-8")
h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(h)
log.setLevel(logging.INFO)

FULL_BLAST = ([20, 30, 40, 50, 60, 70, 80, 90], [100] * 8)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class Hardware:
    def __init__(self):
        self.asus = None
        self.mode = None
        self.full_fans_until = 0
        self.lock = threading.Lock()
        try:
            from power.asus_acpi import AsusAcpi
            self.asus = AsusAcpi()
            log.info("ASUS ATKACPI open; firmware mode read: %s", self.asus.get_mode())
        except Exception as e:
            log.info("no ASUS firmware control: %s", e)
        self.ryzenadj = next((p for p in (os.path.join(APP_DIR, "tools", "ryzenadj.exe"),
                                          os.path.join(APP_DIR, "tools", "ryzenadj", "ryzenadj.exe"))
                              if os.path.exists(p)), None)

    def status(self):
        out = {"admin": is_admin(), "asus": self.asus is not None, "mode": self.mode,
               "ryzenadj": bool(self.ryzenadj), "full_fans": time.time() < self.full_fans_until}
        if self.asus:
            with self.lock:
                try:
                    out["cpu_temp"] = self.asus.get_cpu_temp()
                    out["gpu_temp"] = self.asus.get_gpu_temp() or None
                    out["fans"] = self.asus.get_fan_rpm()
                except Exception as e:
                    out["error"] = str(e)
        return out

    def set_mode(self, name, boost=None, ryzenadj_args=""):
        """silent / balanced / turbo / boost (= turbo + PPT + fan curve)."""
        res = {"asus": None, "ryzenadj": None}
        if self.asus and name in ("silent", "balanced", "turbo", "boost"):
            with self.lock:
                base = "turbo" if name == "boost" else name
                ok = self.asus.set_mode(base)
                if time.time() < self.full_fans_until:
                    time.sleep(0.3)           # a mode write resets the curve: put full fans back
                    self.asus.set_fan_curves(*FULL_BLAST)
                if name == "boost" and boost:
                    time.sleep(0.3)           # mode writes reset the firmware curves
                    ppt = self.asus.set_ppt(boost.get("ppt_total_w"), boost.get("ppt_apu_w"))
                    fans = self.asus.set_fan_curves(boost.get("fan_temps"), boost.get("fan_speeds"))
                    res["boost"] = {"ppt": ppt, "fans": fans}
                res["asus"] = ok
                self.mode = name
        if self.ryzenadj and ryzenadj_args:
            try:
                r = subprocess.run([self.ryzenadj] + ryzenadj_args.split(), capture_output=True, text=True,
                                   timeout=10, creationflags=NOWIN)
                res["ryzenadj"] = r.returncode == 0
            except Exception as e:
                res["ryzenadj"] = str(e)
        log.info("mode %s -> %s", name, res)
        return res

    def fans(self, full=None, temps=None, speeds=None, minutes=3):
        if not self.asus:
            return {"asus": False}
        with self.lock:
            if full:
                ok = self.asus.set_fan_curves(*FULL_BLAST)
                self.full_fans_until = time.time() + minutes * 60
                threading.Timer(minutes * 60 + 1, self._end_full_fans).start()
            elif temps and speeds:
                ok = self.asus.set_fan_curves(temps, speeds)
            else:
                ok = False
        log.info("fans full=%s -> %s", full, ok)
        return {"ok": ok}

    def _end_full_fans(self):
        if time.time() >= self.full_fans_until and self.mode:
            self.full_fans_until = 0
            self.set_mode(self.mode)            # a mode write puts the firmware curve back


class Watchdog:
    """While the tray app is asleep it registers here with its pid, a heartbeat
    file its UI thread touches every 2 s, the taskbar windows it hid and the
    brightness it saved. If the tray dies or stops heartbeating (it hung once
    for 42 hours and left the taskbar hidden), this thread puts everything
    back: taskbar shown, monitors out of standby, brightness restored, and the
    hung process ended so its hooks and black overlays go away."""

    STILL_ACTIVE = 259

    def __init__(self):
        self.state = None
        self.lock = threading.Lock()
        threading.Thread(target=self._run, name="watchdog", daemon=True).start()

    def asleep(self, data):
        with self.lock:
            self.state = dict(data, since=time.time())
        log.info("watchdog armed for pid %s", data.get("pid"))

    def awake(self):
        with self.lock:
            self.state = None
        log.info("watchdog disarmed")

    @staticmethod
    def _alive(pid):
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == Watchdog.STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)

    def _run(self):
        while True:
            time.sleep(5)
            with self.lock:
                st = self.state
            if not st:
                continue
            pid = st.get("pid")
            hb = st.get("heartbeat", "")
            try:
                stale = (time.time() - os.path.getmtime(hb)) if hb and os.path.exists(hb) else 0
            except OSError:
                stale = 0
            dead = not self._alive(pid)
            if not dead and stale < 20:
                continue
            log.warning("tray app %s while asleep (pid %s, heartbeat %.0fs old): restoring",
                        "died" if dead else "stopped responding", pid, stale)
            self.restore(st, kill=not dead)
            with self.lock:
                self.state = None

    def restore(self, st, kill=False):
        if kill:
            h = ctypes.windll.kernel32.OpenProcess(0x0001, False, int(st["pid"]))   # PROCESS_TERMINATE
            if h:
                ctypes.windll.kernel32.TerminateProcess(h, 1)
                ctypes.windll.kernel32.CloseHandle(h)
                log.info("terminated hung tray app %s", st["pid"])
        # the tray app can no longer guard the desktop: make Windows ask for the password
        log.info("LockWorkStation -> %s", ctypes.windll.user32.LockWorkStation())
        show_taskbars()
        try:
            import monitors as mon
            saved = st.get("saved", {})
            for m in mon.enumerate_monitors(probe_wmi=False):
                m.power(True)
            time.sleep(1.5)
            for m in mon.enumerate_monitors(probe_wmi=False):
                entry = saved.get(m.device, {})
                if entry.get("sdr_nits"):
                    m.set_sdr_white_nits(entry["sdr_nits"])
                if entry.get("backlight") is not None and (m.ddc or m.wmi):
                    m.set(entry["backlight"])
                m.close()
            log.info("monitors restored: %s", saved)
        except Exception:
            log.exception("monitor restore")


def show_taskbars():
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(64)
    n = [0]

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, _):
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            user32.ShowWindow(hwnd, 5)
            n[0] += 1
        return True

    user32.EnumWindows(cb, 0)
    log.info("taskbars shown: %d", n[0])
    return n[0]


PRIV_PS = r"""
$o = [ordered]@{}
try { $o.bitlocker = @(Get-BitLockerVolume -ErrorAction Stop | % { [pscustomobject]@{ mount=$_.MountPoint; status=[string]$_.ProtectionStatus; pct=$_.EncryptionPercentage } }) } catch { $o.bitlocker = "unavailable: $($_.Exception.Message)" }
try { $o.secure_boot = Confirm-SecureBootUEFI -ErrorAction Stop } catch { $o.secure_boot = "unavailable" }
try { $o.smb1 = (Get-SmbServerConfiguration -ErrorAction Stop).EnableSMB1Protocol } catch { $o.smb1 = "unavailable" }
try { $o.lsa_ppl = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa' -ErrorAction Stop).RunAsPPL } catch { $o.lsa_ppl = $null }
try { $o.tpm = (Get-Tpm -ErrorAction Stop | select TpmPresent,TpmReady,TpmEnabled) } catch { $o.tpm = "unavailable" }
try { $o.defender_tamper = (Get-MpComputerStatus -ErrorAction Stop).IsTamperProtected } catch { $o.defender_tamper = $null }
$o | ConvertTo-Json -Compress -Depth 4
"""


def privileged_security():
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PRIV_PS],
                           capture_output=True, text=True, timeout=90, creationflags=NOWIN)
        return json.loads(r.stdout.strip() or "{}")
    except Exception as e:
        return {"error": str(e)}


class Handler(BaseHTTPRequestHandler):
    hw = None
    token = None
    watchdog = None

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self.headers.get("X-Token") != self.token:
            self._send(403, {"error": "bad token"})
            return False
        return True

    def do_GET(self):
        if self.path == "/ping":
            return self._send(200, {"ok": True})
        if not self._auth():
            return
        if self.path == "/status":
            st = self.hw.status()
            st["watchdog"] = bool(self.watchdog and self.watchdog.state)
            return self._send(200, st)
        if self.path == "/security":
            return self._send(200, privileged_security())
        self._send(404, {"error": "no such path"})

    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            data = {}
        if self.path == "/mode":
            return self._send(200, self.hw.set_mode(data.get("name"), data.get("boost"), data.get("ryzenadj", "")))
        if self.path == "/fans":
            return self._send(200, self.hw.fans(data.get("full"), data.get("temps"), data.get("speeds"),
                                                data.get("minutes", 3)))
        if self.path == "/asleep":
            self.watchdog.asleep(data)
            return self._send(200, {"ok": True})
        if self.path == "/awake":
            self.watchdog.awake()
            return self._send(200, {"ok": True})
        if self.path == "/show_taskbars":
            return self._send(200, {"shown": show_taskbars()})
        if self.path == "/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._send(404, {"error": "no such path"})


def main():
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\LightsOutHelper")
    if ctypes.windll.kernel32.GetLastError() == 183:
        return
    token = secrets.token_hex(24)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(token)
    Handler.hw = Hardware()
    Handler.token = token
    Handler.watchdog = Watchdog()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("helper listening on 127.0.0.1:%s admin=%s asus=%s", PORT, is_admin(), Handler.hw.asus is not None)
    try:
        srv.serve_forever()
    finally:
        try:
            os.remove(TOKEN_PATH)
        except OSError:
            pass
        ctypes.windll.kernel32.ReleaseMutex(mutex)


if __name__ == "__main__":
    main()
