#!/usr/bin/env python3
"""Graphical Linux/Windows frontend for the FIFA 19 FUT launcher and installer."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import uuid

import client_platform

HERE = Path(__file__).resolve().parent
TEXT_FIELDS = ('server', 'rpcs3', 'game', 'profile', 'update_source', 'trust_ca', 'instance')
BOOL_FIELDS = ('trace_network', 'background_input')


def settings_path():
    if client_platform.windows():
        base = Path(os.environ.get('APPDATA', str(Path.home()/'AppData/Roaming')))
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))
    return base/'fifa19-fut/launcher.json'


def load_settings(path):
    defaults = dict.fromkeys(TEXT_FIELDS, '')
    defaults.update(dict.fromkeys(BOOL_FIELDS, False))
    defaults['rpcs3'] = str(Path('rpcs3.exe') if client_platform.windows() else Path('/usr/bin/rpcs3'))
    # An operator may seed portable-package defaults; per-user choices win.
    for source in (HERE/'launcher-defaults.json', Path(path)):
        try: saved = json.loads(source.read_text(encoding='utf-8'))
        except FileNotFoundError: continue
        if not isinstance(saved, dict): raise ValueError('Settings must be an object')
        for key in TEXT_FIELDS:
            if isinstance(saved.get(key), str): defaults[key] = saved[key]
        for key in BOOL_FIELDS:
            if type(saved.get(key)) is bool: defaults[key] = saved[key]
    return defaults


def save_settings(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {key: values[key] for key in TEXT_FIELDS+BOOL_FIELDS}
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.launcher-', mode='w',
                                     encoding='utf-8', delete=False) as stream:
        stage = Path(stream.name)
        json.dump(safe, stream, indent=2)
        stream.write('\n')
    try: stage.replace(path)
    finally: stage.unlink(missing_ok=True)


def python_executable():
    executable = Path(sys.executable)
    if client_platform.windows() and executable.name.casefold() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    return str(executable)


def build_command(values, action, state_dir, refresh=False):
    """Arguments are a list so spaces, quotes and UNC paths never enter a shell."""
    if action not in ('play', 'check', 'configure', 'install'): raise ValueError('Unknown launcher action')
    if not values['rpcs3'].strip(): raise ValueError('Choose your RPCS3 executable')
    command = [python_executable(), '-u', str(HERE/('install_update.py' if action == 'install' else 'launch.py')),
               '--rpcs3', values['rpcs3'].strip()]
    instance = values['instance'].strip()
    if action == 'install':
        if instance: raise ValueError('Install the update in your main RPCS3 profile before preparing isolated clients')
        if not values['update_source'].strip(): raise ValueError('Choose your extracted BLES02258 v1.02 update folder')
        command += ['--source', values['update_source'].strip()]
    elif action == 'configure' and not instance:
        command += ['--open-rpcs3']
    else:
        if not values['server'].strip(): raise ValueError('Enter the FUT server IP address or hostname')
        command += ['--server', values['server'].strip(), '--cert-cache', str(Path(state_dir)/'server-certs')]
        if instance: command += ['--instance', instance]
        elif not values['game'].strip(): raise ValueError('Choose your FIFA 19 game folder or decrypted ISO')
        else: command += ['--game', values['game'].strip()]
        if values['trust_ca'].strip(): command += ['--trust-ca', values['trust_ca'].strip()]
        if refresh: command += ['--refresh-server-cert']
        if action == 'check': command += ['--prepare-only']
        if action == 'configure': command += ['--configure']
        if values['trace_network']: command += ['--trace-network']
        if values['background_input'] and instance: command += ['--background-input']
    if values['profile'].strip() and not instance: command += ['--profile', values['profile'].strip()]
    return command


class Launcher:
    def __init__(self, root, path, initial=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.path = Path(path)
        self.state_dir = self.path.parent
        self.messages = queue.Queue()
        self.process = None
        self.busy = False
        self.stop_file = None
        self.action = None
        self.buttons = []
        self.close_after_stop = False
        self.last_logs = None
        self.vars = {}
        values = load_settings(path)
        if initial is not None:
            for key in ('server', 'rpcs3', 'game', 'profile', 'trust_ca', 'instance'):
                value = getattr(initial, key, None)
                if value is not None:
                    # CLI defaults must not replace saved choices on --gui alone.
                    if key in ('rpcs3', 'game') and not any(arg == '--'+key or arg.startswith('--'+key+'=') for arg in sys.argv): continue
                    values[key] = str(value)
        for key in TEXT_FIELDS: self.vars[key] = tk.StringVar(value=values[key])
        for key in BOOL_FIELDS: self.vars[key] = tk.BooleanVar(value=values[key])
        self.refresh = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value='Ready — choose your files and server')
        self.certificate_status = tk.StringVar(value='The server certificate will be fetched automatically.')
        self._style()
        self._build()
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.after(100, self.drain)

    def _style(self):
        style = self.ttk.Style()
        style.theme_use('clam')
        self.root.title('FIFA 19 • FUT Launcher')
        self.root.geometry('920x790')
        self.root.minsize(780, 700)
        self.root.configure(bg='#edf2f7')
        self.root.option_add('*Font', 'TkDefaultFont')
        style.configure('.', font=('Segoe UI' if client_platform.windows() else 'DejaVu Sans', 10))
        style.configure('TFrame', background='#edf2f7')
        style.configure('Card.TFrame', background='#ffffff')
        style.configure('TLabel', background='#ffffff', foreground='#17243a')
        style.configure('Muted.TLabel', foreground='#5b6b80')
        style.configure('Title.TLabel', font=('Segoe UI', 15, 'bold'))
        style.configure('TButton', padding=(14, 9), background='#e8edf5', foreground='#17243a')
        style.map('TButton', background=[('active', '#dbe4ef')])
        style.configure('Primary.TButton', background='#125fcd', foreground='white', font=('Segoe UI', 11, 'bold'))
        style.map('Primary.TButton', background=[('active', '#074fab'), ('disabled', '#ced8e7')],
                  foreground=[('disabled', '#718097')])
        style.configure('TEntry', padding=8, fieldbackground='#f8fafc')
        style.configure('TNotebook', background='#edf2f7', borderwidth=0)
        style.configure('TNotebook.Tab', padding=(22, 10), background='#dfe7f1')
        style.map('TNotebook.Tab', background=[('selected', '#ffffff')])
        style.configure('TCheckbutton', background='#ffffff', foreground='#17243a', padding=4)

    def _card(self, parent, title, subtitle):
        frame = self.ttk.Frame(parent, style='Card.TFrame', padding=22)
        frame.pack(fill='both', expand=True, padx=14, pady=14)
        self.ttk.Label(frame, text=title, style='Title.TLabel').grid(row=0, column=0, columnspan=3, sticky='w')
        self.ttk.Label(frame, text=subtitle, style='Muted.TLabel', wraplength=730).grid(
            row=1, column=0, columnspan=3, sticky='w', pady=(7, 20))
        frame.columnconfigure(1, weight=1)
        return frame

    def _field(self, parent, row, title, key, chooser=None):
        self.ttk.Label(parent, text=title).grid(row=row, column=0, sticky='w', padx=(0, 18), pady=7)
        self.ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky='ew', pady=7)
        if chooser:
            self.ttk.Button(parent, text='Browse…', command=lambda: self.browse(key, chooser)).grid(
                row=row, column=2, padx=(10, 0), pady=7)

    def _button(self, parent, text, command, primary=False):
        button = self.ttk.Button(parent, text=text, command=command, style='Primary.TButton' if primary else 'TButton')
        self.buttons.append(button)
        return button

    def _build(self):
        tk, ttk = self.tk, self.ttk
        header = tk.Frame(self.root, bg='#10223c', padx=28, pady=23)
        header.pack(fill='x')
        tk.Label(header, text='FIFA 19', bg='#10223c', fg='white', font=('Segoe UI', 25, 'bold')).pack(side='left')
        tk.Label(header, text='  ULTIMATE TEAM  ', bg='#f3c75f', fg='#10223c', font=('Segoe UI', 10, 'bold'),
                 padx=8, pady=6).pack(side='right')
        tk.Label(self.root, text='Your game. Your club. Your server.', bg='#edf2f7', fg='#56677f',
                 anchor='w', padx=28, pady=12).pack(fill='x')
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=14)
        self.play_tab, self.setup_tab, self.advanced_tab, self.activity_tab = [ttk.Frame(self.notebook) for _ in range(4)]
        for tab, title in ((self.play_tab, 'Play'), (self.setup_tab, 'Setup'),
                           (self.advanced_tab, 'Advanced'), (self.activity_tab, 'Activity')):
            self.notebook.add(tab, text=title)
        play = self._card(self.play_tab, 'Connect to your FUT server',
                          'Choose your RPCS3 installation and FIFA 19, then enter the server address.')
        self._field(play, 2, 'Server IP or hostname', 'server')
        self._field(play, 3, 'RPCS3 executable', 'rpcs3', 'executable')
        self._field(play, 4, 'FIFA 19 game folder', 'game', 'folder')
        ttk.Button(play, text='Choose decrypted ISO instead…', command=lambda: self.browse('game', 'iso')).grid(
            row=5, column=1, sticky='w', pady=(0, 12))
        ttk.Label(play, text='BLES02258 • Update 1.02 • A configured RPCN account is required.',
                  style='Muted.TLabel', wraplength=700).grid(row=6, column=0, columnspan=3, sticky='w', pady=(8, 16))
        certificate = tk.Frame(play, bg='#eef6ff', padx=16, pady=14)
        certificate.grid(row=7, column=0, columnspan=3, sticky='ew')
        tk.Label(certificate, text='SERVER CERTIFICATE', bg='#eef6ff', fg='#125fcd',
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        tk.Label(certificate, textvariable=self.certificate_status, bg='#eef6ff', fg='#344a69',
                 wraplength=700, justify='left').pack(anchor='w', pady=(6, 0))
        controls = ttk.Frame(play, style='Card.TFrame')
        controls.grid(row=8, column=0, columnspan=3, sticky='ew', pady=(26, 0))
        self._button(controls, 'Launch FIFA', lambda: self.start('play'), True).pack(side='left')
        self._button(controls, 'Check setup', lambda: self.start('check')).pack(side='left', padx=10)
        self.stop_button = ttk.Button(controls, text='Stop game', command=self.stop, state='disabled')
        self.stop_button.pack(side='right')
        setup = self._card(self.setup_tab, 'Get ready to play',
                           'Configure RPCN and your controller in RPCS3. Install your own extracted game update if needed.')
        self._button(setup, 'Open RPCS3 settings', lambda: self.start('configure')).grid(row=2, column=0, columnspan=3, sticky='w', pady=(0, 22))
        self._field(setup, 3, 'Update 1.02 folder', 'update_source', 'folder')
        ttk.Label(setup, text='Select the extracted BLES02258 update folder containing PARAM.SFO and USRDIR.\n'
                  'For a .pkg update, use File → Install Packages in RPCS3.', style='Muted.TLabel',
                  wraplength=700).grid(row=4, column=0, columnspan=3, sticky='w', pady=(10, 22))
        self._button(setup, 'Install FIFA update', lambda: self.start('install')).grid(row=5, column=0, columnspan=3, sticky='w')
        ttk.Label(setup, text='An existing update is backed up. Close RPCS3 before installing or launching.',
                  style='Muted.TLabel', wraplength=700).grid(row=6, column=0, columnspan=3, sticky='w', pady=20)
        advanced = self._card(self.advanced_tab, 'Optional settings', 'Most players can leave these at their defaults.')
        self._field(advanced, 2, 'RPCS3 data folder', 'profile', 'folder')
        self._field(advanced, 3, 'Manual public CA file', 'trust_ca', 'certificate')
        ttk.Label(advanced, text='Leave the CA file empty to fetch it from the server.', style='Muted.TLabel').grid(row=4, column=1, sticky='w')
        ttk.Label(advanced, text='Prepared Linux instance').grid(row=5, column=0, sticky='w', pady=14)
        available = ['']
        if sys.platform == 'linux':
            import client_instances
            available = ['']+[name for name in ('player1', 'player2') if (client_instances.instance_root(name)/'instance.json').is_file()]
        ttk.Combobox(advanced, textvariable=self.vars['instance'], values=available, state='readonly').grid(row=5, column=1, sticky='ew')
        ttk.Checkbutton(advanced, text='Verbose network logging', variable=self.vars['trace_network']).grid(row=6, column=0, columnspan=3, sticky='w')
        ttk.Checkbutton(advanced, text='Background input for isolated clients', variable=self.vars['background_input']).grid(row=7, column=0, columnspan=3, sticky='w')
        ttk.Checkbutton(advanced, text='Replace a changed server certificate on the next check or launch', variable=self.refresh).grid(row=8, column=0, columnspan=3, sticky='w', pady=(10, 0))
        ttk.Label(advanced, text='Use certificate replacement when the server operator has deliberately changed its CA.',
                  style='Muted.TLabel', wraplength=700).grid(row=9, column=0, columnspan=3, sticky='w', pady=8)
        activity = self._card(self.activity_tab, 'Launcher activity', 'Connection checks, update installation and launch progress appear here.')
        activity.rowconfigure(2, weight=1)
        self.log = tk.Text(activity, bg='#10223c', fg='#dce8f8', insertbackground='white',
                           relief='flat', padx=16, pady=16, wrap='word', state='disabled', height=12,
                           font=('Consolas' if client_platform.windows() else 'DejaVu Sans Mono', 10))
        self.log.grid(row=2, column=0, columnspan=2, sticky='nsew')
        scroll = ttk.Scrollbar(activity, orient='vertical', command=self.log.yview)
        scroll.grid(row=2, column=2, sticky='ns')
        self.log.configure(yscrollcommand=scroll.set)
        ttk.Button(activity, text='Copy activity', command=self.copy_activity).grid(row=3, column=0, sticky='w', pady=(14, 0))
        ttk.Button(activity, text='Open logs folder', command=self.open_logs).grid(row=3, column=1, sticky='e', pady=(14, 0))
        footer = tk.Frame(self.root, bg='#edf2f7', padx=28, pady=14)
        footer.pack(fill='x')
        tk.Label(footer, textvariable=self.status, bg='#edf2f7', fg='#435977', anchor='w').pack(side='left')
        self.progress = ttk.Progressbar(footer, mode='indeterminate', length=130)
        self.progress.pack(side='right')

    def browse(self, key, kind):
        from tkinter import filedialog
        if kind == 'folder': value = filedialog.askdirectory(parent=self.root, title='Choose '+key.replace('_', ' '))
        else:
            types = {'executable': [('RPCS3', '*.exe *.AppImage'), ('All files', '*')],
                     'iso': [('Decrypted PS3 ISO', '*.iso *.ISO')],
                     'certificate': [('Public certificate', '*.pem *.crt'), ('All files', '*')]}
            value = filedialog.askopenfilename(parent=self.root, filetypes=types[kind])
        if value: self.vars[key].set(value)

    def values(self): return {key: variable.get() for key, variable in self.vars.items()}

    def start(self, action):
        from tkinter import messagebox
        if self.busy: return
        try:
            values = self.values()
            command = build_command(values, action, self.state_dir, self.refresh.get())
            save_settings(self.path, values)
            self.stop_file = None
            if action == 'play':
                directory = self.state_dir/'controls'
                directory.mkdir(parents=True, exist_ok=True)
                self.stop_file = directory/(uuid.uuid4().hex+'.stop')
                command += ['--stop-file', str(self.stop_file)]
        except (ValueError, OSError) as error:
            messagebox.showerror('Check your settings', str(error), parent=self.root)
            return
        self.busy, self.action = True, action
        self.close_after_stop = False
        for button in self.buttons: button.configure(state='disabled')
        self.stop_button.configure(state='normal' if action == 'play' else 'disabled')
        self.status.set({'play': 'Starting FIFA…', 'check': 'Checking files and server…',
                         'configure': 'RPCS3 settings are open', 'install': 'Installing update…'}[action])
        self.progress.start(12)
        self.append('\n'+{'play': 'Launching FIFA', 'check': 'Checking setup', 'configure': 'Opening RPCS3',
                           'install': 'Installing your update'}[action]+'\n')
        if action != 'play': self.notebook.select(self.activity_tab)
        threading.Thread(target=self.worker, args=(command,), daemon=True).start()

    def worker(self, command):
        try:
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
            process = subprocess.Popen(command, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace', env=env,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if client_platform.windows() else 0)
            self.messages.put(('process', process))
            with process.stdout:
                for line in process.stdout: self.messages.put(('line', line))
            self.messages.put(('done', process.wait()))
        except Exception as error: self.messages.put(('error', str(error)))

    def append(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text)
        # Keep the UI usable even with a long launch/diagnostic session.
        if int(self.log.index('end-1c').split('.')[0]) > 2000: self.log.delete('1.0', '200.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def drain(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == 'process': self.process = value
                elif kind == 'line':
                    self.append(value)
                    if value.startswith('Server certificate ready:'):
                        self.certificate_status.set(value.strip().replace('Server certificate ready: ', 'Verified: '))
                    if value.startswith('Logs: '): self.last_logs = Path(value[6:].strip())
                    if 'Try entering Ultimate Team' in value: self.status.set('FIFA is running — enter Ultimate Team')
                elif kind in ('done', 'error'):
                    self.busy = False
                    self.process = None
                    self.progress.stop()
                    self.stop_button.configure(state='disabled')
                    for button in self.buttons: button.configure(state='normal')
                    if self.stop_file: self.stop_file.unlink(missing_ok=True)
                    failed = kind == 'error' or value != 0
                    if kind == 'error': self.append(value+'\n')
                    self.status.set('Could not finish — see Activity' if failed else
                                    {'play': 'Game closed — temporary patches restored', 'check': 'Setup checked — ready to launch',
                                     'configure': 'RPCS3 closed — settings saved', 'install': 'Update installed — ready to check setup'}[self.action])
                    if failed: self.notebook.select(self.activity_tab)
                    else: self.refresh.set(False)
                    if self.close_after_stop:
                        self.root.destroy()
                        return
        except queue.Empty: pass
        self.root.after(100, self.drain)

    def stop(self):
        if self.busy and self.action == 'play' and self.stop_file:
            self.stop_file.touch()
            self.stop_button.configure(state='disabled')
            self.status.set('Stopping FIFA and restoring patches…')

    def close(self):
        from tkinter import messagebox
        if self.busy:
            if self.action == 'play':
                if messagebox.askyesno('Close FIFA?', 'Stop FIFA and restore its temporary patches before closing?', parent=self.root):
                    self.close_after_stop = True
                    self.stop()
            else: messagebox.showinfo('Operation in progress', 'Finish the current operation or close RPCS3 first.', parent=self.root)
            return
        try: save_settings(self.path, self.values())
        except OSError as error: messagebox.showerror('Settings could not be saved', str(error), parent=self.root)
        self.root.destroy()

    def copy_activity(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get('1.0', 'end-1c'))

    def open_logs(self):
        from tkinter import messagebox
        path = self.last_logs or HERE/'logs'
        if not path.is_dir():
            messagebox.showinfo('No logs yet', 'Launch FIFA first to create a log folder.', parent=self.root)
            return
        if client_platform.windows(): os.startfile(str(path))
        else: subprocess.Popen(['xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(initial=None, settings=None):
    try:
        import tkinter as tk
    except ImportError as error:
        raise RuntimeError('The GUI needs Tkinter. On Debian/Ubuntu install python3-tk; '
                           'on Windows use Python with Tcl/Tk support or the prepared GUI package.') from error
    path = settings or settings_path()
    root = tk.Tk()
    try: Launcher(root, path, initial)
    except Exception:
        root.destroy()
        raise
    root.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, help='Override the per-user launcher settings file')
    args = parser.parse_args()
    try: main(settings=args.settings)
    except (OSError, ValueError, RuntimeError) as error:
        print('FUT launcher: '+str(error), file=sys.stderr)
        raise SystemExit(1)
