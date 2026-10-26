# Copied from Micah's PowerDeck (power-deck/powerdeck/acpi.py), proven on the GA401QM 2026-09-01.
r"""Direct hardware control for ASUS ROG laptops via the \\.\ATKACPI device.

Same channel Armoury Crate and G-Helper use. Verified working on this
machine (GA401QM) by scratchpad/probe.py on 2026-09-01: DSTS reads and a
DEVS write both succeeded elevated.

Raw DSTS values carry a "supported" flag at 0x10000; the real value is
raw - 0x10000. A raw of None or an error means the endpoint is absent.
"""
import ctypes
import struct
import threading
from ctypes import wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value

ATK_IOCTL = 0x0022240C
DSTS = 0x53545344  # "DSTS" get
DEVS = 0x53564544  # "DEVS" set

# Device IDs (from the ASUS WMI interface, cross-checked against G-Helper)
THROTTLE_POLICY = 0x00120075   # 0=balanced 1=turbo 2=silent
BATTERY_LIMIT = 0x00120057     # charge limit percent (write-only in practice)
CPU_TEMP = 0x00120094          # deg C
GPU_TEMP = 0x00120097          # deg C
CPU_FAN_RPM = 0x00110013       # value*100 = RPM
GPU_FAN_RPM = 0x00110014       # value*100 = RPM
CPU_FAN_CURVE = 0x00110024     # 16-byte curve payload
GPU_FAN_CURVE = 0x00110025     # 16-byte curve payload
PPT_TOTAL_A0 = 0x001200A0      # total package power (W) — slow limit
PPT_APU_A3 = 0x001200A3        # APU power (W)
GPU_ECO = 0x00090020           # 1 = dGPU disabled (eco)

MODE_NAMES = {0: "balanced", 1: "turbo", 2: "silent"}
MODE_VALUES = {v: k for k, v in MODE_NAMES.items()}

kernel32 = ctypes.windll.kernel32


class AcpiError(Exception):
    pass


class AsusAcpi:
    """Thread-safe wrapper around the ATKACPI device."""

    def __init__(self):
        self._lock = threading.Lock()
        self._handle = None
        self.open()

    def open(self):
        h = kernel32.CreateFileW(
            r"\\.\ATKACPI", GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
        if h == INVALID_HANDLE or h is None:
            raise AcpiError(f"Cannot open ATKACPI device (error {kernel32.GetLastError()}). "
                            "PowerDeck must run elevated.")
        self._handle = h

    def close(self):
        if self._handle:
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def _call(self, method, args_bytes):
        inbuf = struct.pack("<II", method, len(args_bytes)) + args_bytes
        outbuf = ctypes.create_string_buffer(20)
        returned = wintypes.DWORD(0)
        with self._lock:
            ok = kernel32.DeviceIoControl(
                self._handle, ATK_IOCTL, inbuf, len(inbuf),
                outbuf, len(outbuf), ctypes.byref(returned), None)
        if not ok:
            return None
        return struct.unpack("<I", outbuf.raw[:4])[0]

    # ---- primitives ----

    def get_raw(self, device_id):
        return self._call(DSTS, struct.pack("<II", device_id, 0))

    def get(self, device_id):
        """Return the device value, or None when unsupported/unreadable."""
        raw = self.get_raw(device_id)
        if raw is None or raw < 0x10000:
            return None
        return raw - 0x10000

    def set(self, device_id, value):
        res = self._call(DEVS, struct.pack("<II", device_id, value))
        return res is not None and res != 0

    def set_bytes(self, device_id, payload):
        res = self._call(DEVS, struct.pack("<I", device_id) + payload)
        return res is not None and res != 0

    # ---- high-level ----

    def get_mode(self):
        v = self.get(THROTTLE_POLICY)
        return MODE_NAMES.get(v, f"custom({v})") if v is not None else None

    def set_mode(self, name):
        if name not in MODE_VALUES:
            raise ValueError(f"unknown mode {name}")
        return self.set(THROTTLE_POLICY, MODE_VALUES[name])

    def get_cpu_temp(self):
        return self.get(CPU_TEMP)

    def get_gpu_temp(self):
        # reads 0 when the dGPU is asleep
        return self.get(GPU_TEMP)

    def get_fan_rpm(self):
        cpu = self.get(CPU_FAN_RPM)
        gpu = self.get(GPU_FAN_RPM)
        return ((cpu or 0) * 100, (gpu or 0) * 100)

    def set_battery_limit(self, percent):
        percent = max(40, min(100, int(percent)))
        return self.set(BATTERY_LIMIT, percent)

    def set_ppt(self, total_w=None, apu_w=None):
        """Set package power limits in watts. Returns dict of what stuck."""
        out = {}
        if total_w is not None:
            out["ppt_total"] = self.set(PPT_TOTAL_A0, int(total_w))
        if apu_w is not None:
            out["ppt_apu"] = self.set(PPT_APU_A3, int(apu_w))
        return out

    def set_fan_curve(self, device_id, temps, speeds):
        """temps: 8 ascending deg C points; speeds: 8 percents 0-100."""
        if len(temps) != 8 or len(speeds) != 8:
            raise ValueError("fan curve needs exactly 8 points")
        payload = bytes(int(t) & 0xFF for t in temps) + \
                  bytes(min(100, max(0, int(s))) & 0xFF for s in speeds)
        return self.set_bytes(device_id, payload)

    def set_fan_curves(self, temps, speeds):
        a = self.set_fan_curve(CPU_FAN_CURVE, temps, speeds)
        b = self.set_fan_curve(GPU_FAN_CURVE, temps, speeds)
        return a and b

    def get_gpu_eco(self):
        return self.get(GPU_ECO)

    def set_gpu_eco(self, enabled):
        return self.set(GPU_ECO, 1 if enabled else 0)
