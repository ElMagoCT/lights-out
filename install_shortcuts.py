"""Make the icon and the two shortcuts: Desktop (open it) and Startup (so the
tray icon is there after every login). Safe to re-run."""
import os
import sys

from PIL import Image, ImageDraw

APP = os.path.dirname(os.path.abspath(__file__))
ICO = os.path.join(APP, "lightsout.ico")
TARGET = os.path.join(APP, "lightsout.pyw")


def make_ico():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = []
    for s in sizes:
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=max(2, s // 5), fill="#f2c14e")
        k = s / 64
        d.ellipse([18 * k, 10 * k, 46 * k, 38 * k], outline="#101114", width=max(1, int(5 * k)))
        d.rectangle([24 * k, 40 * k, 40 * k, 46 * k], fill="#101114")
        d.rectangle([27 * k, 48 * k, 37 * k, 53 * k], fill="#101114")
        imgs.append(img)
    imgs[-1].save(ICO, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])


def make_lnk(folder, name):
    import win32com.client
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    sh = win32com.client.Dispatch("WScript.Shell")
    path = os.path.join(folder, f"{name}.lnk")
    lnk = sh.CreateShortcut(path)
    lnk.TargetPath = pythonw
    lnk.Arguments = f'"{TARGET}"'
    lnk.WorkingDirectory = APP
    lnk.IconLocation = ICO
    lnk.Description = "Lights Out - monitor brightness and sleep"
    lnk.Save()
    return path


if __name__ == "__main__":
    make_ico()
    desktop = os.path.join(os.environ["USERPROFILE"], "OneDrive", "Desktop")
    if not os.path.isdir(desktop):
        desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
    for folder in (desktop, startup):
        print("wrote", make_lnk(folder, "Lights Out"))
