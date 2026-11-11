"""Background throttling = Windows 11 Task Manager's "Efficiency mode" applied
by rule: EcoQoS (PROCESS_POWER_THROTTLING_EXECUTION_SPEED) plus a lower
priority class on background processes while something heavy is in front.
Everything we touch is remembered and put back when the profile relaxes.
Only the user's own processes can be changed without elevation, which is
exactly the set we want (services and drivers are never touched)."""
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os

import psutil

log = logging.getLogger("lightsout.throttle")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_SET_INFORMATION, PROCESS_QUERY_INFORMATION, PROCESS_QUERY_LIMITED = 0x0200, 0x0400, 0x1000
ProcessPowerThrottling = 4
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
BELOW_NORMAL, NORMAL, IDLE = 0x4000, 0x20, 0x40

# Never slowed down, whatever is in front: audio, capture, input, shells, us.
PROTECTED = {
    "audiodg.exe", "voicemeeter.exe", "voicemeeterpro.exe", "voicemeeter8x64.exe", "voicemeeter8.exe",
    "obs64.exe", "obs32.exe", "mt.native.exe", "mt.studio.exe", "ffmpeg.exe", "explorer.exe", "dwm.exe",
    "csrss.exe", "winlogon.exe", "lsass.exe", "svchost.exe", "system", "registry", "smss.exe",
    "displaylinkmanager.exe", "displaylinkuserservice.exe", "pythonw.exe", "python.exe", "lightsout.exe",
    "lightsouthelper.exe", "powerdeck", "service.pyw", "appcservice.exe", "appchelper.exe",
    "ctfmime.exe", "textinputhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "searchhost.exe", "runtimebroker.exe", "sihost.exe", "taskhostw.exe", "fontdrvhost.exe",
    "nvcontainer.exe", "nvdisplay.container.exe", "rtkauduservice64.exe", "steamservice.exe",
}
# Known background hogs: slowed even in the balanced profile.
HOGS = {
    "onedrive.exe", "discord.exe", "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe",
    "googledrivefs.exe", "dropbox.exe", "ms-teams.exe", "teams.exe", "slack.exe", "spotify.exe",
    "curseforge.exe", "overwolf.exe", "armourycrate.exe", "armourycrate.service.exe", "asusoptimization.exe",
    "msedgewebview2.exe", "copilot.exe", "widgets.exe", "phoneexperiencehost.exe", "yourphone.exe",
    "searchindexer.exe", "wsappx", "msmpeng.exe",
}


class _PPT(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]


def _set_eco(h, on):
    # off = hand the decision back to Windows (ControlMask 0), not "never throttle this process"
    flag = PROCESS_POWER_THROTTLING_EXECUTION_SPEED if on else 0
    s = _PPT(1, flag, flag)
    return bool(k32.SetProcessInformation(h, ProcessPowerThrottling, ctypes.byref(s), ctypes.sizeof(s)))


class Throttler:
    def __init__(self, cfg_lists, state_path=None):
        self.cfg = cfg_lists            # {"protected": [...], "hogs": [...], "never": [...]}
        self.applied = {}               # pid -> (name, original priority)
        self.level = "none"
        self.state_path = state_path    # what is throttled right now, on disk: see recover()
        self._created = {}              # pid -> process creation time (tells a reused pid apart)
        self._saved = None
        self._users = {}                # pid -> username (token lookups are the expensive part of a scan)
        self.skip = set()               # pids the agent governor manages: hands off
        self.recover()

    def recover(self):
        """Everything we slow down is put back when the profile relaxes or the app
        quits - but not when the app is killed or crashes: the processes it had in
        Efficiency mode at below-normal priority then stay that way until they exit
        (found 98 of them on this machine, the browser included). So the list of what
        is throttled lives in a small state file, and a new instance starts by
        restoring whatever its predecessor left behind."""
        if not self.state_path or not os.path.exists(self.state_path):
            return 0
        n = 0
        try:
            with open(self.state_path, encoding="utf-8") as f:
                old = json.load(f)
            for pid_s, (created, name, orig) in old.items():
                pid = int(pid_s)
                try:
                    if abs(psutil.Process(pid).create_time() - created) > 1:
                        continue                    # that pid is somebody else now
                except Exception:
                    continue
                self.applied[pid] = (name, orig)
                self._restore(pid)
                n += 1
            os.remove(self.state_path)
        except Exception:
            log.exception("throttle recovery")
        if n:
            log.info("restored %d processes a previous instance left throttled", n)
        return n

    def _save(self):
        if not self.state_path:
            return
        cur = {str(pid): [self._created.get(pid, 0), name, orig] for pid, (name, orig) in self.applied.items()}
        if cur == self._saved:
            return
        try:
            if cur:
                tmp = self.state_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cur, f)
                os.replace(tmp, self.state_path)
            elif os.path.exists(self.state_path):
                os.remove(self.state_path)
            self._saved = cur
        except OSError:
            pass

    def _lists(self):
        prot = PROTECTED | {n.lower() for n in self.cfg.get("protected", [])}
        hogs = (HOGS | {n.lower() for n in self.cfg.get("hogs", [])}) - prot
        return prot, hogs

    def apply(self, level, fg_name, fg_pid, top):
        """level: 'none' | 'hogs' | 'all'. fg_* = foreground app (never throttled).
        top = [(name, pid, cpu, rss)] from Sensors, used to find busy processes."""
        prot, hogs = self._lists()
        my_user = psutil.Process().username()
        wanted = {}
        seen = set()
        if level != "none":
            for p in psutil.process_iter(["pid", "name"]):
                name = (p.info["name"] or "").lower()
                pid = p.info["pid"]
                seen.add(pid)
                if pid == fg_pid or name == fg_name or name in prot or pid in self.skip:
                    continue
                if level != "all" and name not in hogs:
                    continue
                user = self._users.get(pid)
                if user is None:
                    try:
                        user = p.username()
                    except Exception:
                        user = ""
                    self._users[pid] = user
                if user != my_user:
                    continue
                wanted[pid] = name
            for pid in [k for k in self._users if k not in seen]:
                del self._users[pid]
        # release what is no longer wanted
        for pid in list(self.applied):
            if pid not in wanted:
                self._restore(pid)
        # apply new ones
        for pid, name in wanted.items():
            if pid not in self.applied:
                self._throttle(pid, name, level)
        self.level = level
        self._save()
        return len(self.applied)

    def _throttle(self, pid, name, level):
        h = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            return
        try:
            orig = k32.GetPriorityClass(h)
            ok = _set_eco(h, True)
            if ok and level == "all" and orig in (NORMAL, 0x8000):   # normal / above normal only
                k32.SetPriorityClass(h, BELOW_NORMAL)
            if ok:
                self.applied[pid] = (name, orig)
                try:
                    self._created[pid] = psutil.Process(pid).create_time()
                except Exception:
                    self._created[pid] = 0
        finally:
            k32.CloseHandle(h)

    def _restore(self, pid):
        name, orig = self.applied.pop(pid)
        self._created.pop(pid, None)
        h = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            return
        try:
            _set_eco(h, False)
            if orig:
                k32.SetPriorityClass(h, orig)
        finally:
            k32.CloseHandle(h)

    def release_all(self):
        for pid in list(self.applied):
            self._restore(pid)
        self.level = "none"
        self._save()


if __name__ == "__main__":
    import time
    t = Throttler({})
    n = t.apply("hogs", None, None, [])
    print("throttled hogs:", n, sorted({v[0] for v in t.applied.values()}))
    time.sleep(1)
    t.release_all()
    print("released, left:", len(t.applied))
