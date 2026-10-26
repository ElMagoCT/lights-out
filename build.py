"""Package Lights Out for any Windows PC: python build.py
Produces dist\\LightsOut\\ (LightsOut.exe, LightsOutHelper.exe, the install
bats, README) and dist\\LightsOut.zip. Needs PyInstaller in this Python."""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist", "LightsOut")


def main():
    os.chdir(HERE)
    if not os.path.exists("lightsout.ico"):
        import install_shortcuts
        install_shortcuts.make_ico()
    for d in ("build", DIST):
        shutil.rmtree(d, ignore_errors=True)
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "LightsOut.spec"])
    if r.returncode:
        sys.exit(r.returncode)
    for f in ("Install Lights Out.bat", "Uninstall Lights Out.bat", "README.md", "lightsout.ico"):
        shutil.copy(f, DIST)
    os.makedirs(os.path.join(DIST, "tools"), exist_ok=True)
    with open(os.path.join(DIST, "tools", "README.txt"), "w", encoding="utf-8") as f:
        f.write("Optional add-ons picked up automatically:\n"
                "  ryzenadj.exe  (AMD laptops: power limits + temperature cap; from github.com/FlyGoat/RyzenAdj releases)\n"
                "  MSI Afterburner installed in its default folder for GPU OC profiles (set the profile numbers in Settings > Power)\n")
    zip_path = os.path.join(HERE, "dist", "LightsOut")
    shutil.make_archive(zip_path, "zip", os.path.join(HERE, "dist"), "LightsOut")
    size = os.path.getsize(zip_path + ".zip") / 1048576
    print(f"built {DIST}\nzip: {zip_path}.zip ({size:.1f} MB)")


if __name__ == "__main__":
    main()
