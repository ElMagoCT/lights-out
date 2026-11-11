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

QDC_ALL_PATHS = 1
QDC_ONLY_ACTIVE_PATHS = 2
SDC_TOPOLOGY_INTERNAL = 0x1
SDC_TOPOLOGY_EXTEND = 0x4
SDC_USE_DATABASE_CURRENT = 0xF          # internal | clone | extend | external: 'whatever Windows remembers'
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


def detach(snap, targets, save=False):
    """Deactivate the paths whose (adapter_lo, adapter_hi, target_id) is in
    targets. The remaining desktop is shifted so one display sits at (0,0).
    Not saved to Windows' database unless save=True: for sleep the normal layout
    must stay the remembered one; for a display the user wants kept off, the
    layout without it is exactly what Windows should remember.
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
    rc = _apply(bytes(paths), bytes(modes), SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES
                | (SDC_SAVE_TO_DATABASE if save else 0))
    log.info("detach %s -> rc %s", sorted(targets), rc)
    return rc


# -- which monitors exist, and which the user has switched off -------------------------
INTERNAL_TECH = (0x80000000, 11, 13)        # INTERNAL, DISPLAYPORT_EMBEDDED, UDI_EMBEDDED: a laptop's own panel


def connected():
    """Every connected monitor, on the desktop or not:
    [{"target": (adapter_lo, adapter_hi, id), "name": str, "internal": bool, "active": bool}]."""
    from monitors import TARGET_NAME
    np_, nm = wt.UINT(), wt.UINT()
    if user32.GetDisplayConfigBufferSizes(QDC_ALL_PATHS, ctypes.byref(np_), ctypes.byref(nm)):
        return []
    paths = (ctypes.c_byte * (PATH_SIZE * np_.value))()
    modes = (ctypes.c_byte * (MODE_SIZE * nm.value))()
    if user32.QueryDisplayConfig(QDC_ALL_PATHS, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None):
        return []
    raw, seen = bytes(paths), {}
    for i in range(np_.value):
        off = i * PATH_SIZE
        lo, hi, tid = struct.unpack_from("<IiI", raw, off + 20)
        tech, = struct.unpack_from("<I", raw, off + 36)
        available, = struct.unpack_from("<I", raw, off + 60)
        flags, = struct.unpack_from("<I", raw, off + 68)
        if not available:
            continue
        e = seen.setdefault((lo, hi, tid), {"target": (lo, hi, tid), "internal": tech in INTERNAL_TECH, "active": False})
        e["active"] = e["active"] or bool(flags & PATH_ACTIVE)
    for e in seen.values():
        t = TARGET_NAME(type=2, size=ctypes.sizeof(TARGET_NAME), id=e["target"][2])
        t.adapter.lo, t.adapter.hi = e["target"][0], e["target"][1]
        e["name"] = (t.name or "") if user32.DisplayConfigGetDeviceInfo(ctypes.byref(t)) == 0 else ""
    return list(seen.values())


def off_names(conn=None):
    """Names of the monitors that are connected but not part of the desktop: the
    ones the user has switched off in Settings. Recorded when going to sleep so
    that waking never turns one of them on."""
    return sorted({c["name"] for c in (connected() if conn is None else conn) if not c["active"] and c["name"]})


def unwanted(conn, off=(), keep_off=(), keep_off_internal=False):
    """Targets that are on although they should be off: switched off by the user
    before sleep (off), listed to be kept off (keep_off), or the laptop's own
    panel when that is to stay off. Never every display: a laptop on its own
    keeps its screen."""
    act = [c for c in conn if c["active"]]
    bad = [c for c in act if c["name"] in off or c["name"] in keep_off or (keep_off_internal and c["internal"])]
    if not bad or len(bad) >= len(act):
        return set()
    return {c["target"] for c in bad}


def enforce_off(off=(), keep_off=(), keep_off_internal=False):
    """Switch off again any display that should not be on. Saved to Windows'
    database, so Windows remembers it off for this set of monitors too.
    Returns the names switched off."""
    conn = connected()
    bad = unwanted(conn, off, keep_off, keep_off_internal)
    if not bad:
        return []
    snap = snapshot()
    names = sorted(c["name"] for c in conn if c["target"] in bad)
    rc = detach(snap, bad, save=True) if snap else -1
    log.info("switched off again (kept off by preference): %s -> rc %s", names, rc)
    return names if rc == 0 else []


def internal_only(validate=False):
    """Show the desktop on the laptop's own panel only ('PC screen only'): every
    external display loses its signal. Used as the dark idle surface when no
    external monitor can stay connected dark. validate=True only asks whether
    it would work. Returns 0 on success."""
    return user32.SetDisplayConfig(0, None, 0, None, (SDC_VALIDATE if validate else SDC_APPLY) | SDC_TOPOLOGY_INTERNAL)


def restore(snap, wait=6.0):
    """Apply the saved configuration again. Falls back to Windows' remembered
    'extend' layout. Returns True once every saved target is active again."""
    want = {(lo, hi, tid) for lo, hi, tid, active in snap.targets() if active}
    if want == active_targets():
        return True                              # exactly the saved set: an extra display would have to go too
    rc = _apply(snap.paths, snap.modes, SDC_APPLY | SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES | SDC_SAVE_TO_DATABASE)
    log.info("restore display configuration -> rc %s", rc)
    if rc:
        # The saved layout was rejected (an adapter re-enumerated: its id changes). Ask for
        # the layout Windows itself remembers for the monitors now connected - that keeps a
        # display the user switched off, off. 'Extend' turns every connected display on, so
        # it is only the last resort, and the caller prunes afterwards (enforce_off).
        rc2 = user32.SetDisplayConfig(0, None, 0, None, SDC_APPLY | SDC_USE_DATABASE_CURRENT)
        log.warning("saved layout rejected (rc %s): Windows' remembered layout -> rc %s", rc, rc2)
        if rc2:
            rc3 = user32.SetDisplayConfig(0, None, 0, None, SDC_APPLY | SDC_TOPOLOGY_EXTEND)
            log.warning("fell back to 'extend' -> rc %s", rc3)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if want <= active_targets():
            return True
        time.sleep(0.25)
    return want <= active_targets()


def wake_standby(snap, standby_targets, settle=1.0):
    """Wake monitors that were put into DDC/CI standby but do not come back from a
    power-on (D6=1) write (the ES-24X5AL here acknowledges D6=1 and stays asleep
    until power-cycled). Momentarily drop each from the desktop and re-apply the
    full configuration: losing and regaining the video signal makes the monitor
    power its backlight on, the same as unplugging and replugging it. At least one
    display stays active throughout. Also brings back anything that was detached.
    Returns True once the whole saved configuration is active again."""
    want = {(lo, hi, tid) for lo, hi, tid, active in snap.targets() if active}
    standby = {tuple(t) for t in standby_targets} & want
    if not standby:
        return restore(snap)
    others = want - standby
    if others:
        # drop every standby target at once; the others keep a display active
        detach(snap, standby)
        time.sleep(settle)
        return restore(snap)
    # nothing else to hold the desktop up: cycle the standby targets one at a time
    ok = True
    for t in standby:
        if len(want) <= 1:
            break                                # cannot drop the only display
        detach(snap, {t})
        time.sleep(settle)
        ok = restore(snap) and ok
    return ok


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = snapshot()
    print("active:", s.targets())
