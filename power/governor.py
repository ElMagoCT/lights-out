"""Keep AI coding agents' background work from starving what you are doing.

The complaint this answers: a few agent sessions (Claude Code here) each run
builds, test suites and scans, the machine sits at 100 %, and the browser
becomes unusable. Measured on this machine, the mechanics are:

* the agents' child processes run at NORMAL priority, while a browser lowers
  its own renderers to BELOW_NORMAL / IDLE - so under load the background work
  outranks the thing in front of you;
* their disk scans compete with the browser's I/O at the same I/O priority;
* nothing bounds them: N sessions x M workers will take every core, the CPU
  hits its thermal limit and then everything slows down, front included.

The agents cannot coordinate among themselves (each is its own process tree
and knows nothing of the others), so the coordination happens here, from
outside, on the trees as a whole:

always, at no cost when the machine is otherwise idle
    every process descended from an agent runs at BELOW_NORMAL priority with
    low I/O and memory priority: it yields to anything else, instantly.
while you are using the PC and the agent is not what you are looking at
    the trees are confined to a share of the logical CPUs (the rest stay free
    for the foreground whatever the agents do) and, when their load is high,
    marked EcoQoS so they stop driving the CPU to its thermal limit.
when you are away, the screen is asleep, or the agent app is in front
    no ceiling: unattended work gets the whole machine.

An "agent" is a claude.exe that is not the desktop app's own UI (the Code
sessions it spawns, or the standalone CLI) or a node.exe running the CLI. What
gets governed is their descendants, found through the live parent chain with
creation times checked (PIDs are reused within minutes here). The agent
process itself keeps normal priority - it is light, and it is the part you are
waiting on - but shares the CPU confinement so new children inherit it.
Everything is put back by release_all().
"""
import ctypes
import ctypes.wintypes as wt
import logging
import os
import time

from . import procsnap

log = logging.getLogger("lightsout.governor")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
HANDLE, DWORD, BOOL = wt.HANDLE, wt.DWORD, wt.BOOL
PTR = ctypes.c_size_t
for _name, _res, _args in (
        ("OpenProcess", HANDLE, [DWORD, BOOL, DWORD]),
        ("CloseHandle", BOOL, [HANDLE]),
        ("GetPriorityClass", DWORD, [HANDLE]),
        ("SetPriorityClass", BOOL, [HANDLE, DWORD]),
        ("GetProcessAffinityMask", BOOL, [HANDLE, ctypes.POINTER(PTR), ctypes.POINTER(PTR)]),
        ("SetProcessAffinityMask", BOOL, [HANDLE, PTR]),
        ("SetProcessInformation", BOOL, [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD]),
        ("QueryFullProcessImageNameW", BOOL, [HANDLE, DWORD, wt.LPWSTR, ctypes.POINTER(DWORD)]),
        ("GetCurrentProcess", HANDLE, [])):
    _f = getattr(k32, _name)
    _f.restype, _f.argtypes = _res, _args
ntdll.NtSetInformationProcess.restype = ctypes.c_long
ntdll.NtSetInformationProcess.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG]

PROCESS_SET_INFORMATION, PROCESS_QUERY_LIMITED = 0x0200, 0x1000
IDLE, BELOW_NORMAL, NORMAL, ABOVE_NORMAL = 0x40, 0x4000, 0x20, 0x8000
ProcessPowerThrottling, EXECUTION_SPEED = 4, 0x1
ProcessIoPriority, ProcessPagePriority = 33, 39
IO_LOW, IO_NORMAL = 1, 2
PAGE_LOW, PAGE_NORMAL = 2, 5

DEFAULTS = {
    "enabled": True,
    "reserve_pct": 35,          # share of the logical CPUs kept free of agent work while you are active
    "away_s": 120,              # no input this long = away: the ceiling comes off
    "eco_above": 30,            # agents' share of the whole CPU (%) above which they are also marked EcoQoS...
    "eco_below": 12,            # ...until it has stayed below this for eco_hold_s
    "eco_hold_s": 20,
    "agents": [],               # extra image names to treat as agents (e.g. "codex.exe")
}
# console plumbing: never worth touching, and a slow conhost makes a terminal feel broken
NEVER = {"conhost.exe", "openconsole.exe", "lightsout.exe", "lightsouthelper.exe"}


class _PPT(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]


def image_path(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = DWORD(1024)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        k32.CloseHandle(h)


def classify(pid, name, extra=()):
    """'agent' (a session whose descendants we govern), 'ui' (the desktop app
    itself: never touched, and when it is in front the ceiling comes off) or None."""
    if name == "claude.exe":
        path = image_path(pid).lower()
        if not path:
            return None
        if "claude-code" in path or "\\.local\\bin\\" in path or "\\npm\\" in path:
            return "agent"
        return "ui"
    if name == "node.exe":
        try:
            import psutil
            cmd = " ".join(psutil.Process(pid).cmdline()).lower()
        except Exception:
            return None
        return "agent" if ("claude-code" in cmd or "@anthropic-ai" in cmd) else None
    if name in extra:
        return "agent"
    return None


def cap_mask(system_mask, reserve):
    """The affinity mask for agent trees: every allowed CPU except the lowest `reserve`."""
    cpus = [i for i in range(system_mask.bit_length()) if system_mask >> i & 1]
    keep = cpus[reserve:] if 0 < reserve < len(cpus) else cpus
    m = 0
    for i in keep:
        m |= 1 << i
    return m


class Governor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kinds = {}             # pid -> (create, 'agent' | 'ui' | None): classification cache for candidate roots
        self.gov = {}               # pid -> {"create", "orig", "eco", "cap"}: descendants we have touched
        self.root_cap = {}          # agent pid -> (create, confined?) once its affinity has been set by us
        self.failed = {}            # pid -> create: could not open (elevated, gone): do not retry every pass
        self.prev = {}              # pid -> cpu time at the last pass
        self.prev_t = None
        self.cpu = 0.0              # agents' share of the whole CPU, smoothed
        self.eco_on = False
        self.eco_low_since = None
        self.capping = False
        self.ncpu = max(1, os.cpu_count() or 1)
        pm, sm = PTR(), PTR()
        ok = k32.GetProcessAffinityMask(k32.GetCurrentProcess(), ctypes.byref(pm), ctypes.byref(sm))
        self.sys_mask = sm.value if ok and sm.value else (1 << min(self.ncpu, 64)) - 1
        self.me = os.getpid()
        self.info = {"agent_n": 0, "agent_roots": 0, "agent_cpu": 0.0, "agent_cap": False, "agent_eco": False}

    # -- low-level actions -------------------------------------------------------
    @staticmethod
    def _open(pid):
        return k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED, False, pid)

    @staticmethod
    def _eco(h, on):
        flag = EXECUTION_SPEED if on else 0             # off = Windows decides again
        s = _PPT(1, flag, flag)
        return bool(k32.SetProcessInformation(h, ProcessPowerThrottling, ctypes.byref(s), ctypes.sizeof(s)))

    @staticmethod
    def _hints(h, io, page):
        v = wt.ULONG(io)
        ntdll.NtSetInformationProcess(h, ProcessIoPriority, ctypes.byref(v), ctypes.sizeof(v))
        p = wt.ULONG(page)
        ntdll.NtSetInformationProcess(h, ProcessPagePriority, ctypes.byref(p), ctypes.sizeof(p))

    def _reserve(self):
        n = max(2, round(self.ncpu * float(self.cfg.get("reserve_pct", 35)) / 100))
        n += n % 2                                  # whole SMT cores
        return min(n, max(0, self.ncpu - 2))

    # -- one pass ----------------------------------------------------------------------
    def tick(self, s, asleep=False, locked=False):
        """s: the sensors sample (idle_s, fg_pid, fg, on_ac). Returns the info
        dict (also kept as self.info) for the engine to publish."""
        if not self.cfg.get("enabled", True):
            if self.gov or self.root_cap:
                self.release_all()
            self.info = dict(self.info, agent_n=0, agent_roots=0, agent_cpu=0.0, agent_cap=False, agent_eco=False)
            return self.info
        now = time.perf_counter()
        snap = procsnap.snapshot_tree()             # [(pid, name, cpu, ppid, create)]
        procs = {p[0]: p for p in snap}
        extra = {a.lower() for a in self.cfg.get("agents", [])}
        # 1. which processes are agents / the agent UI (classified once per process)
        roots, ui = set(), set()
        for pid, name, cpu, ppid, create in snap:
            if name in ("claude.exe", "node.exe") or name in extra:
                k = self.kinds.get(pid)
                if k is None or k[0] != create:
                    k = self.kinds[pid] = (create, classify(pid, name, extra))
                if k[1] == "agent":
                    roots.add(pid)
                elif k[1] == "ui":
                    ui.add(pid)
        # 2. their descendants, through the live parent chain (a child is never older than its parent)
        kids = {}
        for pid, name, cpu, ppid, create in snap:
            par = procs.get(ppid)
            if par is not None and par[4] <= create and pid != ppid:
                kids.setdefault(ppid, []).append(pid)
        governed, stack = set(), list(roots)
        while stack:
            for c in kids.get(stack.pop(), ()):
                if c in governed or c in roots:
                    if c in roots:
                        stack.append(c)
                    continue
                governed.add(c)
                stack.append(c)
        fg_pid = s.get("fg_pid")
        governed = {p for p in governed if p != self.me and p != fg_pid and procs[p][1] not in NEVER}
        # 3. how much of the machine they are using
        if self.prev_t is not None:
            dt = max(1e-3, now - self.prev_t) * 1e7
            used = sum(max(0, procs[p][2] - self.prev.get(p, procs[p][2])) for p in governed | roots)
            self.cpu = 0.5 * self.cpu + 0.5 * (used / dt * 100 / self.ncpu)
        self.prev = {p[0]: p[2] for p in snap}
        self.prev_t = now
        # 4. should they be held back right now?
        here = not asleep and not locked and (s.get("idle_s") or 0) < float(self.cfg.get("away_s", 120))
        fg_is_agent = fg_pid in roots or fg_pid in ui or (s.get("fg") == "claude.exe")
        cap = here and not fg_is_agent
        if cap and (self.cpu >= self.cfg.get("eco_above", 30) or not s.get("on_ac", True)):
            self.eco_on, self.eco_low_since = True, None
        elif self.eco_on:
            if not cap:
                self.eco_on = False
            elif self.cpu < self.cfg.get("eco_below", 12):
                self.eco_low_since = self.eco_low_since or now
                if now - self.eco_low_since >= float(self.cfg.get("eco_hold_s", 20)):
                    self.eco_on = False
            else:
                self.eco_low_since = None
        eco = cap and self.eco_on
        mask = cap_mask(self.sys_mask, self._reserve()) if cap else self.sys_mask
        if cap != self.capping:
            # every time you step away and come back: only worth the log when they are busy
            (log.info if self.cpu >= 5 else log.debug)(
                "agents %s: %d processes under %d sessions, %.0f%% CPU%s", "held back" if cap else "released",
                     len(governed), len(roots), self.cpu,
                     (" (confined to %d of %d logical CPUs)" % (bin(mask).count("1"), self.ncpu)) if cap else "")
            self.capping = cap
        # 5. apply to descendants
        for pid in governed:
            create = procs[pid][4]
            st = self.gov.get(pid)
            if st is not None and st["create"] != create:
                st = None
            if st is None:
                if self.failed.get(pid) == create:
                    continue
                h = self._open(pid)
                if not h:
                    self.failed[pid] = create
                    continue
                try:
                    orig = k32.GetPriorityClass(h)
                    if orig in (NORMAL, ABOVE_NORMAL):
                        k32.SetPriorityClass(h, BELOW_NORMAL)
                    self._hints(h, IO_LOW, PAGE_LOW)
                    st = self.gov[pid] = {"create": create, "orig": orig, "eco": False, "cap": False}
                    # Set both, whatever we find: an instance that was killed while holding the
                    # agents back left them confined and in Efficiency mode, and their children
                    # inherited the confinement.
                    if self._eco(h, eco):
                        st["eco"] = eco
                    if k32.SetProcessAffinityMask(h, mask):
                        st["cap"] = cap
                finally:
                    k32.CloseHandle(h)
                continue
            if st["eco"] != eco or st["cap"] != cap:
                h = self._open(pid)
                if not h:
                    continue
                try:
                    if st["eco"] != eco and self._eco(h, eco):
                        st["eco"] = eco
                    if st["cap"] != cap and k32.SetProcessAffinityMask(h, mask):
                        st["cap"] = cap
                finally:
                    k32.CloseHandle(h)
        # the agents themselves: confinement only, so their new children are born inside it
        for pid in roots:
            create = procs[pid][4]
            seen = self.root_cap.get(pid)               # (create, confined?) - set on first sight too, see above
            if seen is None or seen[0] != create or seen[1] != cap:
                h = self._open(pid)
                if h:
                    try:
                        if k32.SetProcessAffinityMask(h, mask):
                            self.root_cap[pid] = (create, cap)
                    finally:
                        k32.CloseHandle(h)
                elif seen is None:
                    self.root_cap[pid] = (create, cap)  # cannot open it: do not retry every pass
        # 6. forget what is gone
        for d in (self.gov, self.failed, self.root_cap, self.kinds):
            for pid in [p for p in d if p not in procs]:
                del d[pid]
        for pid in [p for p in self.gov if p not in governed]:
            self._restore(pid, procs)               # no longer under an agent (or now in the foreground): hands off
        self.info = {"agent_n": len(governed), "agent_roots": len(roots), "agent_cpu": round(self.cpu, 1),
                     "agent_cap": cap, "agent_eco": eco}
        return self.info

    def pids(self):
        return set(self.gov) | set(self.root_cap)

    def _restore(self, pid, procs=None):
        st = self.gov.pop(pid, None)
        if st is None:
            return
        if procs is not None and (pid not in procs or procs[pid][4] != st["create"]):
            return
        h = self._open(pid)
        if not h:
            return
        try:
            if st["eco"]:
                self._eco(h, False)
            if st["cap"]:
                k32.SetProcessAffinityMask(h, self.sys_mask)
            self._hints(h, IO_NORMAL, PAGE_NORMAL)
            if st["orig"] in (NORMAL, ABOVE_NORMAL):
                k32.SetPriorityClass(h, st["orig"])
        finally:
            k32.CloseHandle(h)

    def release_all(self):
        """Put every process we touched back the way we found it."""
        procs = {p[0]: p for p in procsnap.snapshot_tree()}
        n = len(self.gov)
        for pid in list(self.gov):
            self._restore(pid, procs)
        for pid, (create, confined) in list(self.root_cap.items()):
            if confined and pid in procs and procs[pid][4] == create:
                h = self._open(pid)
                if h:
                    k32.SetProcessAffinityMask(h, self.sys_mask)
                    k32.CloseHandle(h)
        self.root_cap.clear()
        self.eco_on, self.capping = False, False
        if n:
            log.info("agents released: %d processes restored", n)
