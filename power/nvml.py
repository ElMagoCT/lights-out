"""NVIDIA sensors straight from nvml.dll via ctypes: no pip package, no
nvidia-smi process spawns. Read-only unless running elevated (clock offsets
are refused on this laptop's 3060 anyway: NVML_ERROR_NOT_SUPPORTED)."""
import ctypes
import logging
import os

log = logging.getLogger("lightsout.nvml")

_nv = None
_dev = None


class _Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("mem", ctypes.c_uint)]


class _ProcInfo(ctypes.Structure):
    _fields_ = [("pid", ctypes.c_uint), ("usedGpuMemory", ctypes.c_ulonglong),
                ("gpuInstanceId", ctypes.c_uint), ("computeInstanceId", ctypes.c_uint)]


def available():
    global _nv, _dev
    if _nv is not None:
        return _dev is not None
    try:
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvml.dll")
        _nv = ctypes.WinDLL(path)
        if _nv.nvmlInit_v2() != 0:
            _nv = False
            return False
        h = ctypes.c_void_p()
        if _nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
            _nv = False
            return False
        _dev = h
        return True
    except Exception:
        _nv = False
        return False


def name():
    if not available():
        return None
    buf = ctypes.create_string_buffer(96)
    _nv.nvmlDeviceGetName(_dev, buf, 96)
    return buf.value.decode(errors="replace")


def read():
    """{'temp': C, 'util': %, 'mem_util': %, 'clock': MHz, 'power_w': W} or None."""
    if not available():
        return None
    out = {}
    t = ctypes.c_uint()
    if _nv.nvmlDeviceGetTemperature(_dev, 0, ctypes.byref(t)) == 0:
        out["temp"] = t.value
    u = _Util()
    if _nv.nvmlDeviceGetUtilizationRates(_dev, ctypes.byref(u)) == 0:
        out["util"], out["mem_util"] = u.gpu, u.mem
    c = ctypes.c_uint()
    if _nv.nvmlDeviceGetClockInfo(_dev, 0, ctypes.byref(c)) == 0:
        out["clock"] = c.value
    p = ctypes.c_uint()
    if _nv.nvmlDeviceGetPowerUsage(_dev, ctypes.byref(p)) == 0:
        out["power_w"] = round(p.value / 1000, 1)
    return out


def gpu_pids():
    """PIDs with a graphics or compute context on the GPU (games, CAD, video)."""
    if not available():
        return set()
    pids = set()
    for fn in ("nvmlDeviceGetGraphicsRunningProcesses_v3", "nvmlDeviceGetGraphicsRunningProcesses_v2",
               "nvmlDeviceGetComputeRunningProcesses_v3", "nvmlDeviceGetComputeRunningProcesses_v2"):
        f = getattr(_nv, fn, None)
        if f is None:
            continue
        n = ctypes.c_uint(0)
        rc = f(_dev, ctypes.byref(n), None)
        if rc not in (0, 7) or n.value == 0:     # 7 = INSUFFICIENT_SIZE with the count filled in
            continue
        arr = (_ProcInfo * (n.value + 8))()
        n = ctypes.c_uint(n.value + 8)
        if f(_dev, ctypes.byref(n), arr) == 0:
            pids.update(arr[i].pid for i in range(n.value))
    return pids


def set_clock_offsets(gpc_mhz, mem_mhz):
    """Core/memory clock offsets (needs admin). Returns (rc_gpc, rc_mem); 3 = not supported."""
    if not available():
        return (-1, -1)
    g = getattr(_nv, "nvmlDeviceSetGpcClkVfOffset", None)
    m = getattr(_nv, "nvmlDeviceSetMemClkVfOffset", None)
    return (g(_dev, int(gpc_mhz)) if g else -1, m(_dev, int(mem_mhz)) if m else -1)


if __name__ == "__main__":
    print(name(), read(), "gpu pids:", sorted(gpu_pids())[:10])
