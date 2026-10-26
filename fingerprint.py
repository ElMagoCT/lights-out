"""Fingerprint wake via the Windows Biometric Framework (winbio.dll).

While the app is asleep (desktop unlocked, screens dark) it holds a session on
the system fingerprint pool and waits in WinBioIdentify. A touch that matches
one of the current user's enrolled fingers wakes the machine; an unknown
finger is ignored. The session is closed again on wake so Windows Hello has
the sensor to itself the rest of the time.

Verified on the GA401QM (Goodix sensor in the power button): the session
opens unelevated, Identify blocks until a touch, WinBioCancel unblocks it.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

log = logging.getLogger("lightsout.fingerprint")

WINBIO_TYPE_FINGERPRINT = 0x8
WINBIO_POOL_SYSTEM = 1
WINBIO_ID_TYPE_SID = 4
E_CANCELED = 0x80098004
RETRY_OK = {0x80098003, 0x80098005, 0x80098008, 0x80098006}   # unknown id, no match, bad capture, capture aborted


class IDENTITY(ctypes.Structure):
    _fields_ = [("Type", wt.ULONG), ("Size", wt.ULONG), ("Data", ctypes.c_ubyte * 68)]


class SCHEMA(ctypes.Structure):
    _fields_ = [("UnitId", wt.ULONG), ("PoolType", wt.ULONG), ("BiometricFactor", wt.ULONG), ("SensorSubType", wt.ULONG),
                ("Capabilities", wt.ULONG), ("DeviceInstanceId", ctypes.c_wchar * 256), ("Description", ctypes.c_wchar * 256),
                ("Manufacturer", ctypes.c_wchar * 256), ("Model", ctypes.c_wchar * 256), ("SerialNumber", ctypes.c_wchar * 256),
                ("FirmwareVersion", wt.USHORT * 2)]


def _my_sid():
    import win32api
    import win32security
    sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
    return win32security.ConvertSidToStringSid(sid)


def _sid_string(identity):
    adv = ctypes.windll.advapi32
    out = ctypes.c_wchar_p()
    if not adv.ConvertSidToStringSidW(ctypes.byref(identity.Data), ctypes.byref(out)):
        return None
    try:
        return out.value
    finally:
        ctypes.windll.kernel32.LocalFree(out)


class FingerprintWatcher:
    def __init__(self):
        self._wb = None
        self._avail = None
        self.description = ""
        self._sess = None
        self._thread = None
        self.active = False
        self._on_wake = None

    def _lib(self):
        if self._wb is None:
            try:
                self._wb = ctypes.WinDLL("winbio")
            except OSError:
                self._wb = False
        return self._wb

    def available(self):
        """True if a system-pool fingerprint unit exists. Cached after the first call."""
        if self._avail is not None:
            return self._avail
        wb = self._lib()
        self._avail = False
        if not wb:
            return False
        arr, n = ctypes.POINTER(SCHEMA)(), ctypes.c_size_t()
        if wb.WinBioEnumBiometricUnits(WINBIO_TYPE_FINGERPRINT, ctypes.byref(arr), ctypes.byref(n)) == 0:
            for i in range(n.value):
                if arr[i].PoolType == WINBIO_POOL_SYSTEM:
                    self._avail = True
                    self.description = arr[i].Description or arr[i].Model or "fingerprint reader"
            if n.value:
                wb.WinBioFree(arr)
        log.info("fingerprint reader: %s", self.description if self._avail else "none")
        return self._avail

    def start(self, on_wake):
        if self.active or not self.available():
            return False
        wb = self._lib()
        sess = ctypes.c_size_t()
        hr = wb.WinBioOpenSession(WINBIO_TYPE_FINGERPRINT, WINBIO_POOL_SYSTEM, 0, None, 0, None, ctypes.byref(sess)) & 0xFFFFFFFF
        if hr:
            log.warning("fingerprint session failed: %#x", hr)
            return False
        self._sess, self._on_wake, self.active = sess, on_wake, True
        self._thread = threading.Thread(target=self._run, name="fingerprint", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        wb = self._lib()
        try:
            me = _my_sid()
        except Exception:
            me = None
        failures = 0
        while self.active:
            unit, ident, sub, rej = wt.ULONG(), IDENTITY(), ctypes.c_ubyte(), wt.ULONG()
            hr = wb.WinBioIdentify(self._sess, ctypes.byref(unit), ctypes.byref(ident), ctypes.byref(sub), ctypes.byref(rej)) & 0xFFFFFFFF
            if not self.active or hr == E_CANCELED:
                break
            if hr == 0:
                sid = _sid_string(ident) if ident.Type == WINBIO_ID_TYPE_SID else None
                if me is None or sid == me:
                    log.info("fingerprint matched the signed-in user")
                    self.active = False
                    try:
                        self._on_wake("fingerprint")
                    except Exception:
                        log.exception("fingerprint on_wake")
                    break
                log.info("fingerprint matched a different account (%s); ignored", sid)
                continue
            if hr in RETRY_OK:
                log.debug("fingerprint: no match (%#x), waiting again", hr)
                time.sleep(0.3)
                continue
            failures += 1
            log.warning("fingerprint identify error %#x (%d)", hr, failures)
            if failures >= 5:
                break
            time.sleep(2)
        self.active = False

    def stop(self):
        wb = self._lib()
        self.active = False
        if self._sess is not None and wb:
            wb.WinBioCancel(self._sess)
            if self._thread:
                self._thread.join(3)
            wb.WinBioCloseSession(self._sess)
            self._sess = None
        self._thread = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    w = FingerprintWatcher()
    print("available:", w.available(), w.description)
    if w.available():
        print("started:", w.start(lambda why: print("WAKE", why)))
        print("touch the sensor within 8 s to test, or wait for cancel")
        time.sleep(8)
        w.stop()
        print("stopped, active:", w.active)
