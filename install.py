"""Install / uninstall the pieces that need registering (run elevated by
"Install Lights Out.bat", or `LightsOut.exe --install` in a packaged build):

  * scheduled task "LightsOutHelper": the elevated helper, at logon, highest
    privileges, started right away
  * Startup shortcut so the tray app comes up at login, Desktop shortcut
  * starts the tray app

Works from a python checkout (pythonw + .pyw) or the PyInstaller build
(LightsOut.exe / LightsOutHelper.exe in the same folder)."""
import os
import subprocess
import sys

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
TASK = "LightsOutHelper"
NOWIN = 0x08000000


def targets():
    if FROZEN or os.path.exists(os.path.join(APP_DIR, "LightsOut.exe")):
        return (os.path.join(APP_DIR, "LightsOut.exe"), "",
                os.path.join(APP_DIR, "LightsOutHelper.exe"), "")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return (pythonw, f'"{os.path.join(APP_DIR, "lightsout.pyw")}"',
            pythonw, f'"{os.path.join(APP_DIR, "helper.pyw")}"')


def run(args):
    r = subprocess.run(args, capture_output=True, text=True, creationflags=NOWIN)
    return r.returncode, (r.stdout + r.stderr).strip()


def make_lnk(folder, name, target, args, icon):
    import win32com.client
    sh = win32com.client.Dispatch("WScript.Shell")
    lnk = sh.CreateShortcut(os.path.join(folder, f"{name}.lnk"))
    lnk.TargetPath = target
    lnk.Arguments = args
    lnk.WorkingDirectory = APP_DIR
    if os.path.exists(icon):
        lnk.IconLocation = icon
    lnk.Description = "Lights Out - brightness, sleep, power"
    lnk.Save()


def shortcut_folders():
    desktop = os.path.join(os.environ["USERPROFILE"], "OneDrive", "Desktop")
    if not os.path.isdir(desktop):
        desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
    return desktop, startup


def install():
    app, app_args, helper, helper_args = targets()
    icon = os.path.join(APP_DIR, "lightsout.ico")
    out = []
    cmd = f'"{helper}" {helper_args}'.strip()
    run(["schtasks", "/end", "/tn", TASK])
    # no /ru: the task runs as the installing user without asking for a password
    rc, msg = run(["schtasks", "/create", "/f", "/tn", TASK, "/sc", "onlogon", "/rl", "highest", "/tr", cmd])
    out.append(f"helper task: {'ok' if rc == 0 else msg}")
    rc, msg = run(["schtasks", "/run", "/tn", TASK])
    out.append(f"helper start: {'ok' if rc == 0 else msg}")
    desktop, startup = shortcut_folders()
    try:
        for folder in (desktop, startup):
            make_lnk(folder, "Lights Out", app, app_args, icon)
        out.append("shortcuts: Desktop + Startup")
    except Exception as e:
        out.append(f"shortcuts failed: {e}")
    try:
        subprocess.Popen([app] + ([app_args.strip('"')] if app_args else []), cwd=APP_DIR, creationflags=NOWIN)
        out.append("tray app started")
    except Exception as e:
        out.append(f"start failed: {e}")
    return out


def uninstall():
    out = []
    rc, msg = run(["schtasks", "/end", "/tn", TASK])
    rc, msg = run(["schtasks", "/delete", "/f", "/tn", TASK])
    out.append(f"helper task removed: {'ok' if rc == 0 else msg}")
    desktop, startup = shortcut_folders()
    for folder in (desktop, startup):
        p = os.path.join(folder, "Lights Out.lnk")
        if os.path.exists(p):
            os.remove(p)
            out.append(f"removed {p}")
    out.append("Runtime data left in %LOCALAPPDATA%\\LightsOut (delete it by hand if you want a clean slate).")
    return out


if __name__ == "__main__":
    lines = uninstall() if "--uninstall" in sys.argv else install()
    print("\n".join(lines))
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "\n".join(lines), "Lights Out", 0x40)
    except Exception:
        pass
