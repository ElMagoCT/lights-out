"""The wake gate: while asleep, swallow all keyboard and mouse input and wake
only on N distinct key presses within a window (default 2 within 5 s).

Low-level hooks live on a dedicated thread with its own message loop. They
only see input on the interactive desktop; if the session gets locked while
asleep (Ctrl+Alt+Del, which no hook can block) the gate falls back to
GetLastInputInfo bursts and to the session-unlock event.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

log = logging.getLogger("lightsout.gate")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
wtsapi = ctypes.windll.wtsapi32

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_QUIT = 0x12
HC_ACTION = 0

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class WTSINFOEX_LEVEL1(ctypes.Structure):
    _fields_ = [("SessionId", wt.ULONG), ("State", ctypes.c_int), ("SessionFlags", wt.LONG),
                ("WinStationName", ctypes.c_wchar * 33), ("UserName", ctypes.c_wchar * 21),
                ("DomainName", ctypes.c_wchar * 18), ("LogonTime", ctypes.c_longlong),
                ("ConnectTime", ctypes.c_longlong), ("DisconnectTime", ctypes.c_longlong),
                ("LastInputTime", ctypes.c_longlong), ("CurrentTime", ctypes.c_longlong),
                ("IncomingBytes", wt.DWORD), ("OutgoingBytes", wt.DWORD),
                ("IncomingFrames", wt.DWORD), ("OutgoingFrames", wt.DWORD),
                ("IncomingCompressedBytes", wt.DWORD), ("OutgoingCompressedBytes", wt.DWORD)]


class WTSINFOEX(ctypes.Structure):
    _fields_ = [("Level", wt.DWORD), ("Data", WTSINFOEX_LEVEL1)]


def session_locked():
    """True if the console session is on the lock screen. None if unknown."""
    try:
        buf = ctypes.c_void_p()
        n = wt.DWORD()
        # WTS_CURRENT_SERVER_HANDLE = 0, WTS_CURRENT_SESSION = -1, WTSSessionInfoEx = 25
        if not wtsapi.WTSQuerySessionInformationW(None, wt.DWORD(0xFFFFFFFF), 25,
                                                  ctypes.byref(buf), ctypes.byref(n)):
            return None
        try:
            info = ctypes.cast(buf, ctypes.POINTER(WTSINFOEX)).contents
            flags = info.Data.SessionFlags
        finally:
            wtsapi.WTSFreeMemory(buf)
        # WTS_SESSIONSTATE_LOCK = 0, UNLOCK = 1 (yes, the documented values look backwards)
        return flags == 0
    except Exception:
        log.exception("session_locked")
        return None


def last_input_tick():
    li = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(li))
    return li.dwTime


def wts_last_input():
    """The session's LastInputTime from Terminal Services (FILETIME), or None.
    Polled alongside GetLastInputInfo on the lock screen: whichever of the two
    the secure desktop still updates is enough to notice a key press."""
    try:
        buf, n = ctypes.c_void_p(), wt.DWORD()
        if not wtsapi.WTSQuerySessionInformationW(None, wt.DWORD(0xFFFFFFFF), 25, ctypes.byref(buf), ctypes.byref(n)):
            return None
        try:
            return ctypes.cast(buf, ctypes.POINTER(WTSINFOEX)).contents.Data.LastInputTime
        finally:
            wtsapi.WTSFreeMemory(buf)
    except Exception:
        return None


def input_stamp():
    """Combined 'something was pressed' stamp: changes when either source changes."""
    return (last_input_tick(), wts_last_input())


class WakeGate:
    """Install with start(on_wake); stop() tears the hooks down."""

    def __init__(self, presses=2, window=5.0, clicks=True):
        self.presses = presses
        self.window = window
        self.clicks = clicks            # mouse button presses count as taps too (movement never does)
        self._thread = None
        self._tid = None
        self._on_wake = None
        self._fired = False
        self._lock = threading.Lock()
        self._down = set()
        self._taps = []
        self._kb = None   # keep callback refs alive
        self._ms = None
        self.active = False

    # -- public ---------------------------------------------------------------
    def start(self, on_wake):
        self._on_wake = on_wake
        self._fired = False
        self._down.clear()
        self._taps.clear()
        self.active = True
        self._thread = threading.Thread(target=self._run, name="wake-gate", daemon=True)
        self._thread.start()
        threading.Thread(target=self._fallback_loop, name="wake-fallback", daemon=True).start()

    def stop(self):
        self.active = False
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._tid = None

    # -- internals ------------------------------------------------------------
    def _fire(self, why):
        with self._lock:
            if self._fired or not self.active:
                return
            self._fired = True
        log.info("wake: %s", why)
        self.active = False
        try:
            self._on_wake(why)
        except Exception:
            log.exception("on_wake")

    def _tap(self, source):
        now = time.monotonic()
        self._taps = [t for t in self._taps if now - t <= self.window]
        self._taps.append(now)
        log.debug("tap via %s (%d/%d)", source, len(self._taps), self.presses)
        if len(self._taps) >= self.presses:
            self._fire(f"{self.presses} presses within {self.window:g}s (last: {source})")

    def _kb_proc(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and self.active:
            vk = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
            if wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                if vk not in self._down:       # ignore auto-repeat
                    self._down.add(vk)
                    self._tap("keyboard")
            elif wParam in (WM_KEYUP, WM_SYSKEYUP):
                self._down.discard(vk)
            return 1                           # swallow everything while asleep
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _ms_proc(self, nCode, wParam, lParam):
        if nCode == HC_ACTION and self.active:
            # WM_LBUTTONDOWN 0x201, WM_RBUTTONDOWN 0x204, WM_MBUTTONDOWN 0x207, WM_XBUTTONDOWN 0x20B
            if self.clicks and wParam in (0x201, 0x204, 0x207, 0x20B):
                self._tap("mouse click")
            return 1                           # movement never wakes; nothing reaches apps
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _run(self):
        self._tid = kernel32.GetCurrentThreadId()
        self._kb = HOOKPROC(self._kb_proc)
        self._ms = HOOKPROC(self._ms_proc)
        hmod = kernel32.GetModuleHandleW(None)
        hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb, hmod, 0)
        hm = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms, hmod, 0)
        log.info("hooks installed kb=%s mouse=%s", bool(hk), bool(hm))
        if not hk:
            # Without a keyboard hook there is no gate at all: wake immediately
            # rather than leave the screens dark with no way back.
            self._fire("keyboard hook failed")
        msg = wt.MSG()
        while self.active and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if hk:
            user32.UnhookWindowsHookEx(hk)
        if hm:
            user32.UnhookWindowsHookEx(hm)
        log.info("hooks removed")

    def _fallback_loop(self):
        """Hooks go blind on the lock screen. If the session is locked while
        asleep, count bursts of input via GetLastInputInfo instead (a key tap
        is one short burst; a mouse wiggle is one long burst), and always wake
        when the session unlocks."""
        was_locked = False
        last = input_stamp()
        bursts = []
        burst_open = None
        burst_last = 0.0
        while self.active:
            time.sleep(0.05)
            locked = session_locked()
            if locked and not was_locked:
                log.info("session locked while asleep: counting input bursts on the lock screen")
                last = input_stamp()
                bursts, burst_open = [], None
            if was_locked and locked is False:
                self._fire("session unlocked")
                return
            was_locked = bool(locked)
            if not locked:
                continue
            tick = input_stamp()
            now = time.monotonic()
            if tick != last:
                last = tick
                if burst_open is None:
                    burst_open = now
                burst_last = now
            if burst_open is not None and now - burst_last > 0.3:
                bursts = [b for b in bursts if now - b <= self.window] + [burst_open]
                burst_open = None
                log.debug("lock-screen input burst (%d/%d)", len(bursts), self.presses)
                if len(bursts) >= self.presses:
                    self._fire(f"{self.presses} input bursts on the lock screen")
                    return
