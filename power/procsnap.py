"""One-call process snapshot via NtQuerySystemInformation(SystemProcessInformation).

psutil's per-process cpu_percent opens every process (and falls back to a
full system query for each one it cannot open), which costs ~850 ms for 300
processes on this machine. The kernel can hand over everything in one buffer
in a couple of milliseconds: pid, image name, user + kernel time, working set.
"""
import ctypes
import ctypes.wintypes as wt
import struct
import time

ntdll = ctypes.WinDLL("ntdll")
SystemProcessInformation = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

# x64 offsets inside SYSTEM_PROCESS_INFORMATION (ntexapi.h)
_OFF_NEXT, _OFF_THREADS, _OFF_USER, _OFF_KERNEL, _OFF_NAME, _OFF_PID, _OFF_PPID, _OFF_SESSION, _OFF_WS = \
    0, 4, 40, 48, 56, 80, 88, 100, 144

_buf = ctypes.create_string_buffer(1 << 20)


def snapshot():
    """[(pid, name_lower, cpu_100ns, rss_bytes, ppid, session)] for every process."""
    global _buf
    needed = wt.ULONG(0)
    while True:
        st = ntdll.NtQuerySystemInformation(SystemProcessInformation, _buf, len(_buf), ctypes.byref(needed)) & 0xFFFFFFFF
        if st == STATUS_INFO_LENGTH_MISMATCH:
            _buf = ctypes.create_string_buffer(max(needed.value + 65536, len(_buf) * 2))
            continue
        if st != 0:
            return []
        break
    raw = _buf.raw
    out = []
    off = 0
    while True:
        nxt = struct.unpack_from("<I", raw, off + _OFF_NEXT)[0]
        user = struct.unpack_from("<q", raw, off + _OFF_USER)[0]
        kern = struct.unpack_from("<q", raw, off + _OFF_KERNEL)[0]
        nlen, _, nptr = struct.unpack_from("<HHxxxxQ", raw, off + _OFF_NAME)
        pid = struct.unpack_from("<Q", raw, off + _OFF_PID)[0]
        ppid = struct.unpack_from("<Q", raw, off + _OFF_PPID)[0]
        session = struct.unpack_from("<I", raw, off + _OFF_SESSION)[0]
        ws = struct.unpack_from("<Q", raw, off + _OFF_WS)[0]
        name = ctypes.wstring_at(nptr, nlen // 2).lower() if nptr and nlen else ("system" if pid == 4 else "idle" if pid == 0 else "?")
        out.append((pid, name, user + kern, ws, ppid, session))
        if not nxt:
            break
        off += nxt
    return out


class CpuTracker:
    """Per-process CPU % between two snapshots, as a share of all logical CPUs."""

    def __init__(self):
        self.prev = {}
        self.t = None
        self.ncpu = max(1, __import__("os").cpu_count() or 1)

    def update(self):
        """Returns ({pid: (name, cpu%, rss_bytes)}, {name: summed cpu%})."""
        now = time.perf_counter()
        snap = snapshot()
        cur = {p[0]: p for p in snap}
        per_pid, by_name = {}, {}
        if self.t is not None:
            dt = max(1e-6, now - self.t) * 1e7          # wall time in 100 ns units
            for pid, (p_pid, name, cpu, ws, ppid, sess) in cur.items():
                old = self.prev.get(pid)
                if old is None:
                    continue
                pct = (cpu - old[2]) / dt * 100 / self.ncpu
                if pct < 0:
                    pct = 0.0
                per_pid[pid] = (name, pct, ws)
                by_name[name] = by_name.get(name, 0.0) + pct
        self.prev, self.t = cur, now
        return per_pid, by_name


if __name__ == "__main__":
    t = time.perf_counter()
    s = snapshot()
    print(f"{len(s)} processes in {(time.perf_counter() - t) * 1000:.1f} ms")
    tr = CpuTracker()
    tr.update()
    time.sleep(1.0)
    per, by = tr.update()
    top = sorted(per.items(), key=lambda kv: -kv[1][1])[:6]
    for pid, (name, pct, ws) in top:
        print(f"  {name:30s} pid {pid:6d} {pct:5.1f}%  {ws / 1048576:6.0f} MB")
