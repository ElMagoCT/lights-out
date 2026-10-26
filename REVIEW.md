# Lights Out: engineering review

*Reviewed 2026-10-25 against the code as published. The review was done by
reading every module, running the self-test and component benchmarks on the
development machine (ROG Zephyrus G14 GA401QM, Windows 11 26200), and fixing
what was worth fixing before publishing. Findings that were fixed are marked.*

## Verdict

A 4,100-line Python tray app that does four unrelated jobs (brightness, a
mouse-proof sleep mode, an automatic power engine, a security assessment)
without a framework, a service bus or a database, and still idles at under
one percent of a single core. The architecture is simple on purpose: one
process for everything that needs the desktop, one tiny elevated helper for
everything that needs admin, JSON on loopback between them. The parts that
touch hardware are the parts most likely to break on another machine, and
they all degrade to "feature missing" rather than crashing.

## Measured

| | tray app | helper |
|---|---|---|
| CPU, 60 s idle average | **0.04 %** of all cores (0.7 % of one core) | 0.00 % |
| resident memory | 74 MB | 19 MB |
| threads | 10 | 1 |

Before this review the tray app sat at 0.55-0.77 % of all cores. Almost all
of it was the per-process CPU scan: psutil opens every process to read its
times and, for the ones it cannot open, falls back to a full system query per
process. 300 processes cost ~850 ms per scan. **Fixed:** one
`NtQuerySystemInformation(SystemProcessInformation)` call now returns all of
it in ~5 ms (`power/procsnap.py`). Process-to-user lookups in the throttler
are cached per pid. The sensor scan runs every 10 s, the re-throttle pass
every 20 s.

## What was verified

- Self-test, run three times in a row and once from the packaged exe:
  hotkey opens the panel; engine has chosen a profile; sleep hides the
  taskbar, arms the fingerprint reader and dims both monitors; two injected
  mouse clicks wake it; the reader disarms; sleep again; two key taps wake it;
  brightness and SDR white level come back to the exact values.
- Engine profile changes watched in the log: Windows power overlay, plan
  boost mode and minimum processor state, Efficiency mode on 25 (balanced)
  and 88 (performance) background processes, firmware mode via the helper,
  and the power plan restored from its backup on stop.
- Security collectors run in 10 s and produce findings; the Claude write-up
  path returns the expected "not signed in" error when the CLI has no token.
- Fingerprint: session opens unelevated, `WinBioIdentify` blocks,
  `WinBioCancel` returns 0x80098004. A real touch was not simulated.

## Findings

### Correctness
1. **dxva2 physical-monitor handle 0 is a valid handle.** The first version
   tested it for truthiness and the second monitor randomly lost brightness
   control. *Fixed.* Worth a comment wherever handles are compared.
2. **HDR pins the monitor's backlight.** DDC brightness writes to the
   GM34-CWQ2 succeed and change nothing while HDR is on. The slider now drives
   Windows' SDR white level in that state (undocumented setter, type
   `0xFFFFFFEE`). The readback agrees, but there is no automated check that
   the picture actually changes; that stays a manual check.
3. **Low-level hooks are blind on the lock screen**, so the two-press gate
   only works while the desktop is unlocked. Ctrl+Alt+Del cannot be blocked.
   The fallback (counting `GetLastInputInfo` bursts, waking on session unlock)
   is reasonable but heuristic. Locking happens on wake, not on sleep, which
   is a deliberate trade.
4. **The helper's "full fans" hold** used to be silently undone by the next
   mode write. *Fixed:* a mode write during a hold re-applies the full curve.
5. **`schtasks /create` with `/ru` and no `/rp`** would have prompted for a
   password in the installer. *Fixed:* the task is created for the installing
   user without `/ru`, the way PowerDeck did it.
6. **Thread-safety of config writes.** The Claude classification worker and
   the settings dialog can both write `config.json`. Writes are atomic
   (temp file + replace) so nothing corrupts, but a settings save can drop a
   classification result made at the same moment. Low impact; noted.
7. **Eight swallowed exceptions** remain, all in cleanup paths (removing a
   file that is already gone, destroying a window twice). Acceptable, but
   each is a place where a future bug would hide.

### Security
- The helper listens on 127.0.0.1 only and requires a random per-run token
  stored in the user's profile. Its `/security` endpoint runs a fixed
  PowerShell script; no caller-supplied strings reach a shell anywhere.
- The Anthropic API key is DPAPI-encrypted at rest, so only the same Windows
  user can decrypt it. The CLI route sends nothing but the prompt.
- Geolocation is a single request (HTTPS ipinfo.io, HTTP ip-api.com as a
  fallback) whose answer is cached locally; *changed in this review* from
  HTTP-only. Type a latitude/longitude in Settings to skip it entirely.
- Security reports contain machine details and stay in
  `%LOCALAPPDATA%\LightsOut\reports`. Screenshots in this repo were taken
  with the location label blanked.
- Fingerprint wake matches the touch against the signed-in user's SID
  through the Windows Biometric Framework before skipping the lock screen.
  It is the same matching engine Windows Hello uses, but the decision is
  made by this app, not by Windows, so treat "fingerprint goes straight to
  the desktop" as a convenience switch, not a security boundary.

### Portability
- Works on any Windows 10/11 PC: brightness (DDC/CI or WMI panel), sleep
  mode, hotkeys, sun sync, Windows-level power levers, Efficiency mode,
  security checks. NVIDIA sensors appear when `nvml.dll` exists.
- ASUS firmware control (modes, PPT, fan curves, CPU temperature) only on
  ROG laptops exposing `\\.\ATKACPI`. On this unit it even works unelevated.
- Without an ASUS or RyzenAdj path there is **no CPU temperature source**,
  so the thermal guard only sees the GPU. Windows has no generic CPU
  temperature API without a kernel driver; this is a known gap.
- GPU overclocking: NVML refuses clock offsets on this laptop GPU
  (`NVML_ERROR_NOT_SUPPORTED`), so the only route is MSI Afterburner
  profiles, wired in as an optional hook and untested here.

### Design notes
- Hysteresis everywhere: dwell times on profile changes (fast up, slow
  down), temperature hysteresis plus a hold timer, latest-value-wins writers
  for sliders. The engine has not flapped in any test run.
- The "max" tier is the only part that deserves the word overclock, and it
  is bounded by the firmware's own PPT control and the thermal guard; the
  machine's own hardware limits (105 °C Tctl, GPU slowdown at 105 °C) sit
  well above the guard.
- Tk is single-threaded; every cross-thread call goes through a queue pumped
  on the UI thread. The one exception is pystray's icon title, which is
  thread-safe by design.
- `claude -p` is driven with a cleaned environment so it never inherits a
  parent Claude Code session's variables.

## Open items

- Confirm the fingerprint wake with a real touch on another unit.
- A CPU temperature source for non-ASUS machines (LibreHardwareMonitor's
  driver is the usual answer; it is a dependency and a kernel driver, so it
  was left out).
- Per-app resource history (what AppControl's proprietary database stores)
  could be kept by the engine cheaply now that a scan costs 5 ms.
- The security report would benefit from Windows Update's real pending-list
  (slow COM call, so it uses hotfix age and the reboot-pending keys instead).
