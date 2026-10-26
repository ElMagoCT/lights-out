"""Headless screenshots for the README: the panel and every settings tab.
    python panel_shot.py [out_dir]      (default docs\\)
Stop the tray instance first; this creates its own App."""
import os
import sys
import time

from PIL import ImageGrab

import lightsout

out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
os.makedirs(out_dir, exist_ok=True)
lightsout.setup_logging()
app = lightsout.App()
app.cfg["sun"]["label"] = ""        # keep the detected city out of published screenshots


def grab(win, name):
    win.update_idletasks()
    win.update()
    x, y, w, h = win.winfo_rootx(), win.winfo_rooty(), win.winfo_width(), win.winfo_height()
    path = os.path.join(out_dir, name)
    ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)
    print("saved", path, (w, h))


def shot_panel():
    p = app.panel
    p.geometry("+600+300")           # away from the notification corner
    grab(p, "panel.png")
    app.show_settings()
    app.root.after(1200, lambda: shot_tab(0))


def shot_tab(i):
    nb = app.settings_nb
    tabs = nb.tabs()
    if i >= len(tabs):
        app.quit()
        return
    nb.select(tabs[i])
    app.settings_win.update()
    time.sleep(0.3)
    grab(app.settings_win, f"settings-{nb.tab(tabs[i], 'text').lower()}.png")
    app.root.after(400, lambda: shot_tab(i + 1))


app.root.after(6000, app.show_panel)      # let the engine tick a few times so the panel shows live numbers
app.root.after(7500, shot_panel)
app.run()
