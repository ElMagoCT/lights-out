"""Notice when a Chrome Remote Desktop session starts and ends.

The CRD host (Windows service 'chromoting') writes to the Application event
log every time a client connects or disconnects:

    EventID 1  "Client connected: <account>/..."
    EventID 2  "Client disconnected: <account>/..."
    EventID 3  "Access denied for client: ..."   (a failed attempt; ignored)
    EventID 4  "Channel IP for client: ..."       (noise; ignored)

We subscribe to *future* 1/2 events with EvtSubscribe in pull mode (a kernel
event is signalled when a new matching record arrives; no polling of CPU or
network, no log files to tail) and call on_connect / on_disconnect.

This is detection only. It reads the event log; it never touches Windows'
lock, never types a password, never bypasses a sign-in. What to *do* on a
connection is the app's decision, in lightsout.pyw.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import re
import threading

log = logging.getLogger("lightsout.remote")

try:
    wevtapi = ctypes.windll.wevtapi
    kernel32 = ctypes.windll.kernel32
except OSError:                       # pragma: no cover - every Windows has these
    wevtapi = kernel32 = None

EVT_HANDLE = ctypes.c_void_p
EvtSubscribeToFutureEvents = 1
EvtRenderEventXml = 1
ERROR_NO_MORE_ITEMS = 259
ERROR_INSUFFICIENT_BUFFER = 122
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

CHANNEL = "Application"
QUERY = "*[System[Provider[@Name='chromoting'] and (EventID=1 or EventID=2)]]"
_EID_RE = re.compile(r"<EventID[^>]*>\s*(\d+)\s*</EventID>")
# the connecting Google account, for the log only (never leaves the machine)
_ACCT_RE = re.compile(r"<Data[^>]*>([^<]*@[^<]*)</Data>")

if wevtapi is not None:
    wevtapi.EvtSubscribe.restype = EVT_HANDLE
    wevtapi.EvtSubscribe.argtypes = [EVT_HANDLE, wt.HANDLE, wt.LPCWSTR, wt.LPCWSTR,
                                     EVT_HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
    wevtapi.EvtNext.argtypes = [EVT_HANDLE, wt.DWORD, ctypes.POINTER(EVT_HANDLE),
                                wt.DWORD, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    wevtapi.EvtRender.argtypes = [EVT_HANDLE, EVT_HANDLE, wt.DWORD, wt.DWORD,
                                  ctypes.c_void_p, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD)]
    wevtapi.EvtClose.argtypes = [EVT_HANDLE]
    kernel32.CreateEventW.restype = wt.HANDLE
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
    kernel32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL, wt.DWORD]


def available():
    return wevtapi is not None


class CrdWatcher:
    """watcher.start(on_connect, on_disconnect) arms it; stop() disarms.

    Callbacks fire on the watcher thread; keep them short (post to the UI
    queue). on_connect fires once per session (a second connect with no
    disconnect in between is ignored), on_disconnect only after a connect.
    """

    def __init__(self):
        self._thread = None
        self._sub = None
        self._sig = None
        self._stop = None
        self._running = False
        self._connected = False
        self._on_connect = None
        self._on_disconnect = None

    def start(self, on_connect, on_disconnect):
        if not available():
            log.warning("no wevtapi: Chrome Remote Desktop watch unavailable")
            return False
        if self._running:
            return True
        self._on_connect, self._on_disconnect = on_connect, on_disconnect
        self._connected = False
        self._sig = kernel32.CreateEventW(None, True, False, None)       # manual-reset
        self._stop = kernel32.CreateEventW(None, True, False, None)
        self._sub = wevtapi.EvtSubscribe(None, self._sig, CHANNEL, QUERY, None, None,
                                         None, EvtSubscribeToFutureEvents)
        if not self._sub:
            err = ctypes.get_last_error()
            log.warning("EvtSubscribe failed (err %s): remote-desktop watch off", err)
            self._cleanup()
            return False
        self._running = True
        self._thread = threading.Thread(target=self._run, name="crd-watch", daemon=True)
        self._thread.start()
        log.info("watching for Chrome Remote Desktop connections")
        return True

    def stop(self):
        self._running = False
        if self._stop:
            kernel32.SetEvent(self._stop)
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._cleanup()

    # -- internals ------------------------------------------------------------
    def _cleanup(self):
        for h, closer in ((self._sub, wevtapi.EvtClose if wevtapi else None),
                          (self._sig, kernel32.CloseHandle if kernel32 else None),
                          (self._stop, kernel32.CloseHandle if kernel32 else None)):
            if h and closer:
                try:
                    closer(h)
                except Exception:
                    pass
        self._sub = self._sig = self._stop = None

    def _run(self):
        handles = (wt.HANDLE * 2)(self._sig, self._stop)
        while self._running:
            w = kernel32.WaitForMultipleObjects(2, handles, False, 1000)   # 1 s safety net
            if not self._running:
                break
            if w == WAIT_OBJECT_0 + 1:         # stop signalled
                break
            kernel32.ResetEvent(self._sig)     # reset before draining: a record that
            self._drain()                      # arrives mid-drain re-signals, caught next loop

    def _drain(self):
        while self._running:
            events = (EVT_HANDLE * 8)()
            returned = wt.DWORD(0)
            if not wevtapi.EvtNext(self._sub, 8, events, 0, 0, ctypes.byref(returned)):
                err = ctypes.get_last_error()
                if err != ERROR_NO_MORE_ITEMS:
                    log.debug("EvtNext err %s", err)
                return
            for i in range(returned.value):
                try:
                    self._handle(self._render(events[i]))
                finally:
                    wevtapi.EvtClose(events[i])

    @staticmethod
    def _render(evt):
        used = wt.DWORD(0)
        props = wt.DWORD(0)
        wevtapi.EvtRender(None, evt, EvtRenderEventXml, 0, None, ctypes.byref(used), ctypes.byref(props))
        if not used.value:
            return ""
        buf = (ctypes.c_wchar * (used.value // 2 + 1))()
        if wevtapi.EvtRender(None, evt, EvtRenderEventXml, used.value, buf,
                             ctypes.byref(used), ctypes.byref(props)):
            return buf.value
        return ""

    def _handle(self, xml):
        if not xml:
            return
        m = _EID_RE.search(xml)
        if not m:
            return
        eid = int(m.group(1))
        if eid == 1 and not self._connected:
            self._connected = True
            acct = _ACCT_RE.search(xml)
            log.info("Chrome Remote Desktop connected%s", f" ({acct.group(1)})" if acct else "")
            self._fire(self._on_connect)
        elif eid == 2 and self._connected:
            self._connected = False
            log.info("Chrome Remote Desktop disconnected")
            self._fire(self._on_disconnect)

    @staticmethod
    def _fire(cb):
        if cb is None:
            return
        try:
            cb()
        except Exception:
            log.exception("remote callback")


if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.DEBUG)
    w = CrdWatcher()
    w.start(lambda: print(">>> CONNECT"), lambda: print(">>> DISCONNECT"))
    print("watching 60 s - connect/disconnect a Chrome Remote Desktop session...")
    try:
        time.sleep(60)
    finally:
        w.stop()
