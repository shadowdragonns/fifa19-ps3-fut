"""Render all launcher tabs and exercise its async progress UI without an emulator."""
import io
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher_gui


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = tk.Tk()
        try:
            app = launcher_gui.Launcher(root, Path(directory)/'launcher.json')
            for tab in app.notebook.tabs():
                app.notebook.select(tab)
                root.update()
            assert len(app.notebook.tabs()) == 4
            app.vars['server'].set('192.0.2.55')
            app.vars['rpcs3'].set('C:/Example RPCS3/rpcs3.exe')
            app.vars['game'].set('C:/Example Games/FIFA19')
            class Process:
                stdout = io.StringIO('Server certificate ready: 192.0.2.55 (SHA256 '+('a'*64)+').\nPrepared test config\n')
                def wait(self): return 0
            with patch.object(launcher_gui.subprocess, 'Popen', return_value=Process()):
                app.start('check')
                deadline = time.monotonic()+5
                while app.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
            assert not app.busy, 'GUI worker completion was not processed'
            assert app.status.get() == 'Setup checked — ready to launch'
            assert 'Verified:' in app.certificate_status.get()
            assert 'Prepared test config' in app.log.get('1.0', 'end')
            assert launcher_gui.load_settings(app.path)['server'] == '192.0.2.55'
            print('GUI rendered and async progress/settings checked on '+sys.platform)
        finally: root.destroy()


if __name__ == '__main__': main()
