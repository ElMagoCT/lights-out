# PyInstaller spec: two windowed executables sharing one _internal folder.
#   LightsOut.exe        tray app (lightsout.pyw)
#   LightsOutHelper.exe  elevated helper (helper.pyw)
# Build with: python build.py
import os

block_cipher = None
HERE = os.path.abspath(".")
ICON = os.path.join(HERE, "lightsout.ico")
hidden = ["pystray._win32", "PIL.Image", "PIL.ImageDraw", "win32com.client", "pythoncom", "pywintypes",
          "psutil", "power.engine", "power.sensors", "power.throttle", "power.windows_power", "power.nvml",
          "power.asus_acpi", "monitors", "gate", "sun", "security", "claude_bridge", "install"]

a_app = Analysis(["lightsout.pyw"], pathex=[HERE], hiddenimports=hidden, excludes=["tkinter.test", "unittest"],
                 noarchive=False, cipher=block_cipher)
a_helper = Analysis(["helper.pyw"], pathex=[HERE], hiddenimports=["power.asus_acpi"], excludes=["tkinter", "PIL", "pystray"],
                    noarchive=False, cipher=block_cipher)

pyz_app = PYZ(a_app.pure, a_app.zipped_data, cipher=block_cipher)
pyz_helper = PYZ(a_helper.pure, a_helper.zipped_data, cipher=block_cipher)

exe_app = EXE(pyz_app, a_app.scripts, [], exclude_binaries=True, name="LightsOut", debug=False,
              bootloader_ignore_signals=False, strip=False, upx=False, console=False, icon=ICON)
exe_helper = EXE(pyz_helper, a_helper.scripts, [], exclude_binaries=True, name="LightsOutHelper", debug=False,
                 bootloader_ignore_signals=False, strip=False, upx=False, console=False, icon=ICON)

coll = COLLECT(exe_app, a_app.binaries, a_app.zipfiles, a_app.datas,
               exe_helper, a_helper.binaries, a_helper.zipfiles, a_helper.datas,
               strip=False, upx=False, name="LightsOut")
