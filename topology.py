"""Detach a monitor from the desktop and put it back (SetDisplayConfig).

Why: some monitors (the GM34-CWQ2 here) have no DDC/CI power command, so the
only way to get their backlight off is to stop the video signal, the way
Settings > Display > "Disconnect this display" does. The monitor then drops
into its own standby. On wake the saved configuration is applied again,
unchanged, so positions, HDR and the primary display come back as they were.
Windows' own display-off is never used: on this machine it is the entry to
Modern Standby.
"""
import base64
import ctypes
import ctypes.wintypes as wt
import logging
import struct
import time

log = logging.getLogger("lightsout.topology")
user32 = ctypes.windll.user32

QDC_ONLY_ACTIVE_PATHS = 2
SDC_TOPOLOGY_EXTEND = 0x4
SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x20
SDC_VALIDATE = 0x40
SDC_APPLY = 0x80
SDC_SAVE_TO_DATABASE = 0x200
SDC_ALLOW_CHANGES = 0x400
PATH_ACTIVE = 0x1
MODE_IDX_INVALID = 0xFFFFFFFF
PATH_SIZE, MODE_SIZE = 72, 64
# DISPLAYCONFIG_MODE_INFO: infoType u32, id u32, adapterId LUID(8), union at 16;
# DISPLAYCONFIG_SOURCE_MODE: width u32, height u32, pixelFormat u32, position POINTL
MODE_SOURCE = 1
POS_OFFSET = 16 + 12


class Snapshot:
    """The active display configuration as raw DISPLAYCONFIG arrays."""

    def __init__(self, paths, modes):
        self.paths = bytes(paths)
        self.modes = bytes(modes)

    @property
    def npaths(self):
        return len(self.paths) // PATH_SIZE

    @property
    def nmodes(self):
        return len(self.modes) // MODE_SIZE

    def targets(self):
        """[(adapter_lo, adapter_hi, target_id, active)] in path order."""
        out = []
        for i in range(self.npaths):
            p = self.paths[i * PATH_SIZE:(i + 1) * PATH_SIZE]
            lo, hi, tid = struct.unpack_from("<IiI", p, 20)
            flags, = struct.unpack_from("<I", p, 68)
            out.append((lo, hi, tid, bool(flags & PATH_ACTIVE)))
        return out

    def to_json(self):
        return {"paths": base64.b64encode(self.paths).decode(), "modes": base64.b64encode(self.modes).decode()}

    @classmethod
    def from_json(cls, d):
        if not d or "paths" not in d:
            return None
        return cls(base64.b64decode(d["paths"]), base64.b64decode(d["modes"]))


def snapshot():
    np_, nm = wt.UINT(), wt.UINT()
    if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(np_), ctypes.byref(nm)):
        return None
    paths = (ctypes.c_byte * (PATH_SIZE * np_.value))()
    modes = (ctypes.c_byte * (MODE_SIZE * nm.value))()
    if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None):
        return None
    return Snapshot(bytes(paths)[:PATH_SIZE * np_.value], bytes(modes)[:MODE_SIZE * nm.value])


def active_targets():
    s = snapshot()
    return {(lo, hi, tid) for lo, hi, tid, active in (s.targets() if s else []) if active}


def _apply(paths, modes, flags):
    pa = (ctypes.c_byte * len(paths)).from_buffer_copy(paths)
    ma = (ctypes.c_byte * len(modes)).from_buffer_copy(modes) if modes else None
    return user32.SetDisplayConfig(len(paths) // PATH_SIZE, pa, len(modes) // MODE_SIZE, ma, flags)


def detach(snap, targets):
    """Deactivate the paths whose (adapter_lo, adapter_hi, target_id) is in
    targets. The remaining desktop is shifted so one display sits at (0,0).
    Not saved to Windows' database: the normal layout stays the remembered one.
    Returns 0 on success (a Win32 error code otherwise)."""
    paths = bytearray(snap.paths)
    modes = bytearray(snap.modes)
    keep_src = []
    for i, (lo, hi, tid, active) in enumerate(snap.targets()):
        off = i * PATH_SIZE
        if (lo, hi, tid) in targets:
            struct.pack_into("<I", paths, off + 68, 0)                      # not active
            struct.pack_into("<I", paths, off + 12, MODE_IDX_INVALID)       # source mode idx
            struct.pack_into("<I", paths, off + 32, MODE_IDX_INVALID)       # target mode idx
        elif active:
            keep_src.append(struct.unpack_from("<I", paths, off + 12)[0])
    if not keep_src:
        return -1                                   # never detach the last display
    # shift every remaining source so the first one becomes the origin
    shift = None
    for idx in keep_src:
        if idx == MODE_IDX_INVALID or idx >= snap.nmodes:
            continue
        off = idx * MODE_SIZE
        if struct.unpack_from("<I", modes, off)[0] != MODE_SOURCE:
            continue
        x, y = struct.unpack_from("<ii", modes, off + POS_OFFSET)
        if shift is None:
            shift = (x, y)
        struct.pack_into("<ii", modes, off + POS_OFFSET, x - shift[0], y - shift[1])
    rc = _apply(bytes(paths), bytes(modes), SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES)
    log.info("detach %s -> rc %s", sorted(targets), rc)
    return rc


def restore(snap, wait=6.0):
    """Apply the saved configuration again. Falls back to Windows' remembered
    'extend' layout. Returns True once every saved target is active again."""
    want = {(lo, hi, tid) for lo, hi, tid, active in snap.targets() if active}
    if want <= active_targets():
        return True
    rc = _apply(snap.paths, snap.modes, SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES | SDC_SAVE_TO_DATABASE)
    log.info("restore display configuration -> rc %s", rc)
    if rc:
        rc2 = user32.SetDisplayConfig(0, None, 0, None, SDC_APPLY | SDC_TOPOLOGY_EXTEND)
        log.warning("restore fell back to the remembered extend layout -> rc %s", rc2)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if want <= active_targets():
            return True
        time.sleep(0.25)
    return want <= active_targets()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = snapshot()
    print("active:", s.targets())
