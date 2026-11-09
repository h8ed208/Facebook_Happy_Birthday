"""Tkinter GUI for the Facebook Birthday Bot."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import queue
import socket
import subprocess
import sys
import threading
from datetime import date, datetime

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import autostart
from . import settings as S
from .fb_client import Friend
from .runner import BirthdayRunner, poke_rule_key, render_message
from .scheduler import PokeScheduler, Scheduler, normalize_time, parse_times

log = logging.getLogger("birthday_bot")
SINGLE_INSTANCE_PORT = 47613
STATUS_LABELS = {"sent": "✔ Sent", "preview": "👁 Preview", "failed": "✘ Failed", "excluded": "— Excluded"}


# ─────────────────────────── helpers ───────────────────────────
class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


def open_path(path):
    path = str(path)
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class TextDialog(tk.Toplevel):
    """Modal multi-line editor. Result in .result (None if cancelled)."""

    def __init__(self, parent, title, text="", hint="", two_fields=False, first_label="", first_value=""):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.result = None
        self.resizable(True, True)
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        self.entry = None
        if two_fields:
            ttk.Label(frm, text=first_label).pack(anchor="w")
            self.entry = ttk.Entry(frm, width=50)
            self.entry.insert(0, first_value)
            self.entry.pack(fill="x", pady=(0, 8))
        if hint:
            ttk.Label(frm, text=hint, foreground="#555", wraplength=460, justify="left").pack(anchor="w", pady=(0, 6))
        self.text = tk.Text(frm, width=60, height=8, wrap="word", font=("Segoe UI", 10))
        self.text.insert("1.0", text)
        self.text.pack(fill="both", expand=True)
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="OK", command=self._ok).pack(side="right", padx=6)
        (self.entry or self.text).focus_set()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window(self)

    def _ok(self):
        body = self.text.get("1.0", "end").strip()
        if self.entry is not None:
            self.result = (self.entry.get().strip(), body)
        else:
            self.result = body
        self.destroy()


# ─────────────────────────── main app ───────────────────────────
class App:
    def __init__(self, root: tk.Tk, start_minimized: bool, server: socket.socket | None):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.settings = S.Settings()
        self.state = S.State()
        self.logged_in: bool | None = None

        self._setup_logging()
        self.runner = BirthdayRunner(self.settings, self.state, on_event=self.emit)
        self.scheduler = Scheduler(self.settings, self.state, self.runner, on_event=self.emit)
        self.poke_scheduler = PokeScheduler(self.settings, self.runner, on_event=self.emit)

        root.title("Facebook Birthday Bot")
        root.geometry("860x640")
        root.minsize(720, 520)
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        if server is not None:
            threading.Thread(target=self._listen, args=(server,), daemon=True).start()

        self.scheduler.start()
        self.poke_scheduler.start()
        self._bg(self.runner.check_login)
        root.after(150, self._pump)
        root.after(500, self._tick_status)
        if start_minimized or self.settings.get("start_minimized"):
            root.after(100, root.iconify)
        log.info("Ready. Data folder: %s", S.data_dir())

    # --- plumbing --------------------------------------------------------
    def _setup_logging(self):
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
        fh = logging.handlers.RotatingFileHandler(S.data_dir() / "bot.log", maxBytes=1_000_000,
                                                  backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
        qh = QueueLogHandler(self.q)
        qh.setFormatter(fmt)
        log.addHandler(qh)

    def emit(self, kind, payload=None):
        self.q.put((kind, payload))

    def _bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _listen(self, server):
        while True:
            try:
                conn, _ = server.accept()
                conn.close()
                self.emit("show")
            except OSError:
                return

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                elif kind == "login":
                    self.logged_in = payload
                    self._refresh_status()
                elif kind in ("run_start",):
                    self.run_btn.state(["disabled"])
                    self.preview_btn.state(["disabled"])
                    self.status_var.set("Running…")
                elif kind in ("run_done", "schedule", "progress"):
                    if kind == "run_done":
                        self.run_btn.state(["!disabled"])
                        self.preview_btn.state(["!disabled"])
                    self._refresh_today()
                    self._refresh_status()
                elif kind in ("poke_done", "poke_schedule"):
                    self._refresh_pokes()
                elif kind == "show":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _style(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Big.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def bind_var(self, var, key, cast=None, on_change=None):
        var.set(self.settings.get(key))

        def cb(*_):
            try:
                val = var.get()
            except (tk.TclError, ValueError):
                return
            if cast:
                val = cast(val)
            self.settings.set(key, val)
            if on_change:
                on_change(val)
            self.scheduler.poke()
            self.poke_scheduler.poke()
            self.root.after(300, self._refresh_status)

        var.trace_add("write", cb)
        return var

    # --- layout ---------------------------------------------------------
    def _build(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_dashboard(ttk.Frame(nb, padding=10), nb)
        self._build_messages(ttk.Frame(nb, padding=10), nb)
        self._build_schedule(ttk.Frame(nb, padding=10), nb)
        self._build_pokes(ttk.Frame(nb, padding=10), nb)
        self._build_settings(ttk.Frame(nb, padding=10), nb)

    def _build_dashboard(self, f, nb):
        nb.add(f, text="  Dashboard  ")
        top = ttk.Frame(f)
        top.pack(fill="x")

        acct = ttk.LabelFrame(top, text="Facebook account", padding=10)
        acct.pack(side="left", fill="both", expand=True)
        self.login_var = tk.StringVar(value="Checking…")
        ttk.Label(acct, textvariable=self.login_var, style="Big.TLabel").pack(anchor="w")
        row = ttk.Frame(acct)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Button(row, text="Log in to Facebook…", command=self.do_login).pack(side="left")
        ttk.Button(row, text="Check", command=lambda: self._bg(self.runner.check_login)).pack(side="left", padx=6)

        sched = ttk.LabelFrame(top, text="Schedule", padding=10)
        sched.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.next_var = tk.StringVar()
        self.last_var = tk.StringVar()
        self.mode_var = tk.StringVar()
        ttk.Label(sched, textvariable=self.next_var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(sched, textvariable=self.last_var, wraplength=360).pack(anchor="w", pady=(4, 0))
        ttk.Label(sched, textvariable=self.mode_var, foreground="#b45309").pack(anchor="w", pady=(4, 0))

        actions = ttk.Frame(f)
        actions.pack(fill="x", pady=10)
        self.run_btn = ttk.Button(actions, text="▶ Check & send now", style="Accent.TButton",
                                  command=lambda: self.do_run(None))
        self.run_btn.pack(side="left")
        self.preview_btn = ttk.Button(actions, text="👁 Preview only", command=lambda: self.do_run(True))
        self.preview_btn.pack(side="left", padx=6)
        ttk.Button(actions, text="■ Stop", command=self.runner.stop_event.set).pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(actions, textvariable=self.status_var).pack(side="left", padx=12)

        tf = ttk.LabelFrame(f, text="Today", padding=6)
        tf.pack(fill="both", expand=True)
        cols = ("name", "status", "time", "detail")
        self.today_tree = ttk.Treeview(tf, columns=cols, show="headings", height=6)
        for c, w, label in (("name", 180, "Friend"), ("status", 100, "Status"), ("time", 70, "Time"),
                            ("detail", 400, "Message / error")):
            self.today_tree.heading(c, text=label)
            self.today_tree.column(c, width=w, anchor="w", stretch=(c == "detail"))
        self.today_tree.pack(fill="both", expand=True)

        lf = ttk.LabelFrame(f, text="Activity log", padding=6)
        lf.pack(fill="both", expand=True, pady=(8, 0))
        self.log_box = ScrolledText(lf, height=8, state="disabled", font=("Consolas", 9), wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self._refresh_today()

    def _build_messages(self, f, nb):
        nb.add(f, text="  Messages  ")
        hint = ("Placeholders: {first_name}, {last_name}, {name}. With several templates, one is picked at "
                "random for each friend. Press Enter in the editor for a new line.")
        ttk.Label(f, text=hint, wraplength=780, foreground="#555").pack(anchor="w", pady=(0, 6))

        tf = ttk.LabelFrame(f, text="Message templates", padding=6)
        tf.pack(fill="both", expand=True)
        self.tpl_list = tk.Listbox(tf, height=5, activestyle="none")
        self.tpl_list.pack(side="left", fill="both", expand=True)
        self.tpl_list.bind("<Double-Button-1>", lambda e: self.edit_template())
        self.tpl_list.bind("<<ListboxSelect>>", lambda e: self._preview_template())
        b = ttk.Frame(tf)
        b.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(b, text="Add…", command=self.add_template).pack(fill="x")
        ttk.Button(b, text="Edit…", command=self.edit_template).pack(fill="x", pady=4)
        ttk.Button(b, text="Remove", command=self.remove_template).pack(fill="x")
        self.tpl_preview = tk.StringVar()
        ttk.Label(f, textvariable=self.tpl_preview, wraplength=780, foreground="#1d4ed8").pack(anchor="w", pady=4)

        cf = ttk.LabelFrame(f, text="Personal messages for specific friends (name must match Facebook exactly)",
                            padding=6)
        cf.pack(fill="both", expand=True, pady=(6, 0))
        self.custom_tree = ttk.Treeview(cf, columns=("name", "msg"), show="headings", height=4)
        self.custom_tree.heading("name", text="Friend")
        self.custom_tree.heading("msg", text="Message")
        self.custom_tree.column("name", width=180, stretch=False)
        self.custom_tree.column("msg", width=500)
        self.custom_tree.pack(side="left", fill="both", expand=True)
        self.custom_tree.bind("<Double-Button-1>", lambda e: self.edit_custom())
        b = ttk.Frame(cf)
        b.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(b, text="Add…", command=self.add_custom).pack(fill="x")
        ttk.Button(b, text="Edit…", command=self.edit_custom).pack(fill="x", pady=4)
        ttk.Button(b, text="Remove", command=self.remove_custom).pack(fill="x")

        ef = ttk.LabelFrame(f, text="Never message (names or profile URL bits)", padding=6)
        ef.pack(fill="both", expand=True, pady=(6, 0))
        self.ex_list = tk.Listbox(ef, height=3)
        self.ex_list.pack(side="left", fill="both", expand=True)
        b = ttk.Frame(ef)
        b.pack(side="left", fill="y", padx=(6, 0))
        self.ex_entry = ttk.Entry(b, width=22)
        self.ex_entry.pack(fill="x")
        ttk.Button(b, text="Add", command=self.add_excluded).pack(fill="x", pady=4)
        ttk.Button(b, text="Remove", command=self.remove_excluded).pack(fill="x")
        self._load_message_lists()

    def _build_schedule(self, f, nb):
        nb.add(f, text="  Schedule  ")
        en = tk.BooleanVar()
        self.bind_var(en, "schedule_enabled")
        ttk.Checkbutton(f, text="Check for birthdays automatically", variable=en).pack(anchor="w")

        tf = ttk.LabelFrame(f, text="Daily check times (24-hour, e.g. 09:00 or 18:30)", padding=8)
        tf.pack(fill="x", pady=8)
        self.times_list = tk.Listbox(tf, height=5, width=12)
        self.times_list.pack(side="left")
        b = ttk.Frame(tf)
        b.pack(side="left", padx=8, anchor="n")
        self.time_entry = ttk.Entry(b, width=10)
        self.time_entry.insert(0, "18:00")
        self.time_entry.pack(fill="x")
        self.time_entry.bind("<Return>", lambda e: self.add_time())
        ttk.Button(b, text="Add time", command=self.add_time).pack(fill="x", pady=4)
        ttk.Button(b, text="Remove", command=self.remove_time).pack(fill="x")
        ttk.Label(tf, wraplength=440, foreground="#555", justify="left",
                  text="Extra times act as retries – friends already messaged today are never "
                       "messaged twice.").pack(side="left", padx=10, anchor="n")

        cu = tk.BooleanVar()
        self.bind_var(cu, "catch_up")
        ttk.Checkbutton(f, variable=cu, text="Catch up on missed times – if the computer was off or asleep, "
                                             "run as soon as it's back on (same day)").pack(anchor="w")

        rf = ttk.Frame(f)
        rf.pack(anchor="w", pady=8)
        ttk.Label(rf, text="If a run fails, retry every").pack(side="left")
        rm = tk.IntVar()
        self.bind_var(rm, "retry_minutes", int)
        ttk.Spinbox(rf, from_=5, to=240, width=5, textvariable=rm).pack(side="left", padx=4)
        ttk.Label(rf, text="minutes, up to").pack(side="left")
        mr = tk.IntVar()
        self.bind_var(mr, "max_retries", int)
        ttk.Spinbox(rf, from_=0, to=20, width=4, textvariable=mr).pack(side="left", padx=4)
        ttk.Label(rf, text="times").pack(side="left")

        sf = ttk.LabelFrame(f, text="Keep it running", padding=8)
        sf.pack(fill="x", pady=8)
        auto = tk.BooleanVar(value=autostart.is_enabled())
        self.settings.set("autostart", auto.get())

        def on_auto(*_):
            want = auto.get()
            try:
                autostart.set_enabled(want)
                self.settings.set("autostart", want)
                log.info("Start with computer: %s", "on" if want else "off")
            except Exception as e:
                messagebox.showerror("Autostart", f"Couldn't change autostart: {e}")

        auto.trace_add("write", on_auto)
        ttk.Checkbutton(sf, variable=auto, text="Start automatically when I log in to this computer "
                                               "(starts minimized)").pack(anchor="w")
        sm = tk.BooleanVar()
        self.bind_var(sm, "start_minimized")
        ttk.Checkbutton(sf, variable=sm, text="Always start minimized").pack(anchor="w")
        mc = tk.BooleanVar()
        self.bind_var(mc, "minimize_on_close")
        ttk.Checkbutton(sf, variable=mc, text="Closing the window minimizes it instead of quitting "
                                             "(the schedule only works while the app is running)").pack(anchor="w")
        self._load_times()

    def _build_settings(self, f, nb):
        nb.add(f, text="  Settings  ")
        pf = ttk.LabelFrame(f, text="Safety", padding=8)
        pf.pack(fill="x")
        dr = tk.BooleanVar()
        self.bind_var(dr, "dry_run")
        ttk.Checkbutton(pf, variable=dr, text="Preview mode – find birthdays and show the messages, but don't "
                                             "send anything").pack(anchor="w")
        ttk.Label(pf, foreground="#555", wraplength=760,
                  text="Leave this on until a preview run lists the right friends, then switch it off.").pack(anchor="w")

        mf = ttk.LabelFrame(f, text="How to send", padding=8)
        mf.pack(fill="x", pady=8)
        mode = tk.StringVar()
        self.bind_var(mode, "send_mode")
        ttk.Radiobutton(mf, text="Private Messenger message", value="messenger", variable=mode).pack(anchor="w")
        ttk.Radiobutton(mf, text="Post on their timeline (only if Facebook offers a box on the birthdays page)",
                        value="timeline", variable=mode).pack(anchor="w")
        fb_ = tk.BooleanVar()
        self.bind_var(fb_, "fallback_to_messenger")
        ttk.Checkbutton(mf, variable=fb_, text="If timeline posting isn't possible, send a Messenger message "
                                              "instead").pack(anchor="w", padx=(20, 0))

        pacing = ttk.LabelFrame(f, text="Pacing (looks less robotic to Facebook)", padding=8)
        pacing.pack(fill="x")
        r = ttk.Frame(pacing)
        r.pack(anchor="w")
        ttk.Label(r, text="Wait between messages:").pack(side="left")
        dmin, dmax = tk.IntVar(), tk.IntVar()
        self.bind_var(dmin, "delay_min_sec", int)
        self.bind_var(dmax, "delay_max_sec", int)
        ttk.Spinbox(r, from_=5, to=600, width=5, textvariable=dmin).pack(side="left", padx=4)
        ttk.Label(r, text="to").pack(side="left")
        ttk.Spinbox(r, from_=5, to=900, width=5, textvariable=dmax).pack(side="left", padx=4)
        ttk.Label(r, text="seconds    Max messages per run:").pack(side="left")
        mx = tk.IntVar()
        self.bind_var(mx, "max_per_run", int)
        ttk.Spinbox(r, from_=1, to=200, width=5, textvariable=mx).pack(side="left", padx=4)

        bf = ttk.LabelFrame(f, text="Browser", padding=8)
        bf.pack(fill="x", pady=8)
        r = ttk.Frame(bf)
        r.pack(anchor="w")
        ttk.Label(r, text="Browser engine:").pack(side="left")
        ch = tk.StringVar()
        self.bind_var(ch, "browser_channel")
        ttk.Combobox(r, textvariable=ch, values=["chromium", "msedge", "chrome"], state="readonly",
                     width=12).pack(side="left", padx=6)
        ttk.Label(r, foreground="#555", text="(msedge / chrome use the browser already installed on "
                                             "your PC)").pack(side="left")
        hl = tk.BooleanVar()
        self.bind_var(hl, "headless")
        ttk.Checkbutton(bf, variable=hl, text="Run hidden (no browser window). If Facebook starts showing "
                                             "security checks, turn this off.").pack(anchor="w", pady=(6, 0))

        tools = ttk.LabelFrame(f, text="Troubleshooting", padding=8)
        tools.pack(fill="x")
        r = ttk.Frame(tools)
        r.pack(anchor="w")
        ttk.Button(r, text="Open data folder", command=lambda: open_path(S.data_dir())).pack(side="left")
        ttk.Button(r, text="Save birthdays-page snapshot",
                   command=lambda: self._bg(self._snapshot_and_open)).pack(side="left", padx=6)
        ttk.Button(r, text="Edit selectors.json",
                   command=lambda: open_path(S.data_dir() / "selectors.json")).pack(side="left")
        ttk.Button(r, text="Log out (forget session)", command=self.do_logout).pack(side="left", padx=6)

        ttk.Button(f, text="Quit app", command=self.quit).pack(anchor="e", pady=10)

    # --- pokes tab --------------------------------------------------------
    POKE_RULE_LABELS = {"auto": "👉 Auto-poke back", "ignore": "🚫 Ignore (never poke)", None: "Default"}

    def _build_pokes(self, f, nb):
        nb.add(f, text="  Pokes  ")
        top = ttk.Frame(f)
        top.pack(fill="x")
        en = tk.BooleanVar()
        self.bind_var(en, "poke_enabled", on_change=lambda v: log.info("Auto-poker %s.", "on" if v else "off"))
        ttk.Checkbutton(top, variable=en, text="Automatically poke people back").pack(side="left")
        ttk.Label(top, text="   Check every").pack(side="left")
        iv = tk.IntVar()
        self.bind_var(iv, "poke_interval_minutes", int)
        ttk.Spinbox(top, from_=2, to=240, width=5, textvariable=iv).pack(side="left", padx=4)
        ttk.Label(top, text="minutes").pack(side="left")

        mf = ttk.Frame(f)
        mf.pack(fill="x", pady=(6, 0))
        mode = tk.StringVar()
        self.bind_var(mode, "poke_mode", on_change=lambda v: self._refresh_pokes())
        ttk.Radiobutton(mf, text="Poke back everyone, except people set to Ignore", value="everyone",
                        variable=mode).pack(anchor="w")
        ttk.Radiobutton(mf, text="Only poke back people I set to Auto-poke back", value="selected",
                        variable=mode).pack(anchor="w")
        hd = tk.BooleanVar()
        self.bind_var(hd, "poke_headless")
        ttk.Checkbutton(mf, variable=hd, text="Run poke checks hidden (no browser window popping up)").pack(anchor="w")

        st = ttk.Frame(f)
        st.pack(fill="x", pady=8)
        ttk.Button(st, text="🔍 Scan pokes (don't poke)",
                   command=lambda: self._poke_now(scan_only=True)).pack(side="left")
        ttk.Button(st, text="👉 Poke back now", command=lambda: self._poke_now(scan_only=False)).pack(side="left", padx=6)
        self.poke_status_var = tk.StringVar()
        ttk.Label(st, textvariable=self.poke_status_var, wraplength=480).pack(side="left", padx=8)

        pf = ttk.LabelFrame(f, text="People who've poked you (Scan pokes to fill this list)", padding=6)
        pf.pack(fill="both", expand=True)
        cols = ("name", "rule", "result", "last", "count")
        self.poke_tree = ttk.Treeview(pf, columns=cols, show="headings", height=8, selectmode="extended")
        for c, w, label in (("name", 180, "Person"), ("rule", 170, "Setting"), ("result", 150, "What happens"),
                            ("last", 120, "Last seen poking"), ("count", 90, "Poked back")):
            self.poke_tree.heading(c, text=label)
            self.poke_tree.column(c, width=w, anchor="w", stretch=(c == "name"))
        self.poke_tree.pack(side="left", fill="both", expand=True)
        b = ttk.Frame(pf)
        b.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(b, text="Auto-poke back", command=lambda: self._set_poke_rule("auto")).pack(fill="x")
        ttk.Button(b, text="Ignore", command=lambda: self._set_poke_rule("ignore")).pack(fill="x", pady=4)
        ttk.Button(b, text="Default", command=lambda: self._set_poke_rule(None)).pack(fill="x")
        ttk.Button(b, text="Remove from list", command=self._forget_pokers).pack(fill="x", pady=(16, 0))

        af = ttk.Frame(f)
        af.pack(fill="x", pady=(6, 0))
        ttk.Label(af, text="Add someone by name or profile link:").pack(side="left")
        self.poke_add_entry = ttk.Entry(af, width=40)
        self.poke_add_entry.pack(side="left", padx=6)
        ttk.Button(af, text="Ignore", command=lambda: self._add_poke_rule("ignore")).pack(side="left")
        ttk.Button(af, text="Auto-poke back", command=lambda: self._add_poke_rule("auto")).pack(side="left", padx=4)
        ttk.Label(f, foreground="#555", wraplength=780, text="Ignored people are never poked back, so their poke "
                  "stays where it is. Only “Poke back” buttons are ever clicked – the bot never starts a poke "
                  "with anyone.").pack(anchor="w", pady=(6, 0))
        self._refresh_pokes()

    def _rule_for(self, rules, key, name):
        return rules.get(key) or rules.get((name or "").lower())

    def _refresh_pokes(self):
        if not hasattr(self, "poke_tree"):
            return
        rules = self.settings.get("poke_rules") or {}
        everyone = self.settings.get("poke_mode") == "everyone"
        sel = set(self.poke_tree.selection())
        self.poke_tree.delete(*self.poke_tree.get_children())
        rows = []
        for key, p in self.state.pokers().items():
            rows.append((key, p.get("name", key), self._rule_for(rules, key, p.get("name")), p.get("last_seen"),
                         p.get("poked_back", 0)))
        known = {r[0] for r in rows} | {r[1].lower() for r in rows}
        for key, rule in rules.items():  # people added by hand who haven't shown up yet
            if key not in known:
                rows.append((key, key, rule, None, 0))
        for key, name, rule, last, count in sorted(rows, key=lambda r: r[1].lower()):
            if rule == "ignore":
                result = "Never poked"
            elif rule == "auto" or everyone:
                result = "Poked back"
            else:
                result = "Left alone"
            last_txt = datetime.fromisoformat(last).strftime("%b %d %H:%M") if last else "—"
            self.poke_tree.insert("", "end", iid=key, values=(name, self.POKE_RULE_LABELS.get(rule, rule),
                                                             result, last_txt, count))
        for iid in sel:
            if self.poke_tree.exists(iid):
                self.poke_tree.selection_add(iid)
        self._refresh_poke_status()

    def _refresh_poke_status(self):
        if not hasattr(self, "poke_status_var"):
            return
        parts = []
        if not self.settings.get("poke_enabled"):
            parts.append("Auto-poker is off.")
        elif self.poke_scheduler.next_at:
            parts.append(f"Next check {self.poke_scheduler.next_at:%H:%M}.")
        else:
            parts.append("First check starting…")
        last = self.state.data.get("last_poke_check")
        if last:
            parts.append(f"Last check {datetime.fromisoformat(last):%H:%M}: {self.state.data.get('last_poke_result', '')}")
        self.poke_status_var.set("  ".join(parts))

    def _set_poke_rule(self, rule):
        keys = self.poke_tree.selection()
        if not keys:
            messagebox.showinfo("Pokes", "Select one or more people in the list first.")
            return
        rules = self.settings.get("poke_rules") or {}
        pokers = self.state.pokers()
        for key in keys:
            name = (pokers.get(key) or {}).get("name", "")
            rules.pop(name.lower(), None)  # one rule per person, stored by profile link
            if rule is None:
                rules.pop(key, None)
            else:
                rules[key] = rule
        self.settings.set("poke_rules", rules)
        self._refresh_pokes()

    def _add_poke_rule(self, rule):
        text = self.poke_add_entry.get().strip()
        if not text:
            return
        rules = self.settings.get("poke_rules") or {}
        rules[poke_rule_key(text)] = rule
        self.settings.set("poke_rules", rules)
        self.poke_add_entry.delete(0, "end")
        self._refresh_pokes()

    def _forget_pokers(self):
        keys = self.poke_tree.selection()
        if not keys:
            return
        pokers = self.state.pokers()
        if any((self.settings.get("poke_rules") or {}).get(k) == "ignore" for k in keys):
            if not messagebox.askyesno("Remove", "Removing someone also removes their Ignore setting, so they "
                                       "could be poked back. Remove anyway?"):
                return
        rules = self.settings.get("poke_rules") or {}
        for key in keys:
            self.state.forget_poker(key)
            rules.pop(key, None)
            rules.pop((pokers.get(key) or {}).get("name", "").lower(), None)
        self.settings.set("poke_rules", rules)
        self._refresh_pokes()

    def _poke_now(self, scan_only):
        if self.runner.busy:
            messagebox.showinfo("Busy", "Another task is running – try again in a moment.")
            return
        self.poke_status_var.set("Scanning…" if scan_only else "Checking pokes…")
        self._bg(self.runner.poke, scan_only, "manual")

    # --- status ----------------------------------------------------------
    def _refresh_status(self):
        self.login_var.set({True: "✔ Logged in", False: "✘ Not logged in", None: "Checking…",
                            "error": "Unknown – see the activity log"}.get(self.logged_in, "?"))
        nxt = self.scheduler.next_run()
        if not self.settings.get("schedule_enabled"):
            self.next_var.set("Automatic checks are off")
        elif nxt is None:
            self.next_var.set("No check times set")
        else:
            when = "today" if nxt.date() == date.today() else nxt.strftime("%a %b %d")
            prefix = "Retry" if self.scheduler.retry_at == nxt else "Next check"
            self.next_var.set(f"{prefix}: {when} at {nxt:%H:%M}")
        last = self.state.data.get("last_run")
        if last:
            dt = datetime.fromisoformat(last)
            self.last_var.set(f"Last run {dt:%b %d %H:%M}: {self.state.data.get('last_result', '')}")
        else:
            self.last_var.set("Hasn't run yet")
        self.mode_var.set("Preview mode is ON – nothing will actually be sent" if self.settings.get("dry_run") else "")
        if not self.runner.busy:
            self.status_var.set("")

    def _tick_status(self):
        self._refresh_status()
        self._refresh_poke_status()
        self.root.after(30_000, self._tick_status)

    def _refresh_today(self):
        self.today_tree.delete(*self.today_tree.get_children())
        for key, e in self.state.day_entries(date.today().isoformat()).items():
            detail = e.get("error") or (e.get("message") or "").replace("\n", " ⏎ ")
            self.today_tree.insert("", "end", values=(e.get("name", key), STATUS_LABELS.get(e.get("status"), e.get("status")),
                                                    e.get("time", ""), detail))

    # --- actions ----------------------------------------------------------
    def do_run(self, dry):
        if self.runner.busy:
            messagebox.showinfo("Busy", "A run is already in progress.")
            return
        if dry is None and not self.settings.get("dry_run"):
            if not messagebox.askyesno("Send messages?", "This will send birthday messages to "
                                       "anyone with a birthday today (who hasn't been messaged yet). Continue?"):
                return
        self._bg(self.runner.run, dry, "manual" if not dry else "manual preview")

    def do_login(self):
        if self.runner.busy:
            messagebox.showinfo("Busy", "Wait for the current run to finish first.")
            return
        self._bg(self.runner.login)

    def do_logout(self):
        if self.runner.busy:
            messagebox.showinfo("Busy", "Wait for the current run to finish first.")
            return
        if not messagebox.askyesno("Log out", "Delete the saved Facebook session from this app?"):
            return
        import shutil
        shutil.rmtree(S.browser_profile_dir(), ignore_errors=True)
        S.browser_profile_dir()
        self.logged_in = False
        self._refresh_status()
        log.info("Saved session removed.")

    def _snapshot_and_open(self):
        path = self.runner.debug_snapshot()
        if path:
            open_path(S.debug_dir())

    # templates
    def _load_message_lists(self):
        self.tpl_list.delete(0, "end")
        for t in self.settings.get("templates"):
            self.tpl_list.insert("end", t.replace("\n", " ⏎ "))
        self.custom_tree.delete(*self.custom_tree.get_children())
        for name, msg in sorted(self.settings.get("custom_messages").items()):
            self.custom_tree.insert("", "end", iid=name, values=(name, msg.replace("\n", " ⏎ ")))
        self.ex_list.delete(0, "end")
        for x in self.settings.get("excluded"):
            self.ex_list.insert("end", x)
        self._preview_template()

    def _preview_template(self):
        tpls = self.settings.get("templates")
        sel = self.tpl_list.curselection()
        t = tpls[sel[0]] if sel and sel[0] < len(tpls) else (tpls[0] if tpls else "")
        sample = render_message(t, Friend("Jane Doe", "")) if t else "(no templates – a default message will be used)"
        self.tpl_preview.set("Preview for “Jane Doe”:  " + sample.replace("\n", " ⏎ "))

    def add_template(self):
        d = TextDialog(self.root, "New message template", "Happy birthday, {first_name}! 🎂",
                       hint="Use {first_name}, {last_name} or {name}.")
        if d.result:
            tpls = self.settings.get("templates") + [d.result]
            self.settings.set("templates", tpls)
            self._load_message_lists()

    def edit_template(self):
        sel = self.tpl_list.curselection()
        if not sel:
            return
        tpls = self.settings.get("templates")
        d = TextDialog(self.root, "Edit template", tpls[sel[0]], hint="Use {first_name}, {last_name} or {name}.")
        if d.result:
            tpls[sel[0]] = d.result
            self.settings.set("templates", tpls)
            self._load_message_lists()

    def remove_template(self):
        sel = self.tpl_list.curselection()
        if not sel:
            return
        tpls = self.settings.get("templates")
        del tpls[sel[0]]
        self.settings.set("templates", tpls)
        self._load_message_lists()

    def add_custom(self, name="", msg=""):
        d = TextDialog(self.root, "Personal message", msg, two_fields=True,
                       first_label="Friend's name exactly as on Facebook:", first_value=name,
                       hint="Message for this friend only (placeholders work here too):")
        if d.result and d.result[0] and d.result[1]:
            custom = self.settings.get("custom_messages")
            if name and name != d.result[0]:
                custom.pop(name, None)
            custom[d.result[0]] = d.result[1]
            self.settings.set("custom_messages", custom)
            self._load_message_lists()

    def edit_custom(self):
        sel = self.custom_tree.selection()
        if sel:
            self.add_custom(sel[0], self.settings.get("custom_messages").get(sel[0], ""))

    def remove_custom(self):
        sel = self.custom_tree.selection()
        if sel:
            custom = self.settings.get("custom_messages")
            custom.pop(sel[0], None)
            self.settings.set("custom_messages", custom)
            self._load_message_lists()

    def add_excluded(self):
        v = self.ex_entry.get().strip()
        if v:
            self.settings.set("excluded", self.settings.get("excluded") + [v])
            self.ex_entry.delete(0, "end")
            self._load_message_lists()

    def remove_excluded(self):
        sel = self.ex_list.curselection()
        if sel:
            ex = self.settings.get("excluded")
            del ex[sel[0]]
            self.settings.set("excluded", ex)
            self._load_message_lists()

    # times
    def _load_times(self):
        self.times_list.delete(0, "end")
        for t in parse_times(self.settings.get("schedule_times")):
            self.times_list.insert("end", t)

    def add_time(self):
        t = normalize_time(self.time_entry.get())
        if not t:
            messagebox.showerror("Invalid time", "Use 24-hour HH:MM, e.g. 09:00 or 18:30.")
            return
        self.settings.set("schedule_times", parse_times(self.settings.get("schedule_times") + [t]))
        self._load_times()
        self.scheduler.poke()
        self._refresh_status()

    def remove_time(self):
        sel = self.times_list.curselection()
        if sel:
            t = self.times_list.get(sel[0])
            self.settings.set("schedule_times", [x for x in parse_times(self.settings.get("schedule_times")) if x != t])
            self._load_times()
            self._refresh_status()

    # window
    def on_close(self):
        if self.settings.get("minimize_on_close"):
            self.root.iconify()
        else:
            self.quit()

    def quit(self):
        if self.runner.busy and not messagebox.askyesno("Quit", "A run is in progress. Quit anyway?"):
            return
        self.runner.stop_event.set()
        self.scheduler.stop()
        self.poke_scheduler.stop()
        self.root.destroy()


def _single_instance() -> socket.socket | None | bool:
    """Returns a listening socket if we're the first instance, False if another is running."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2):
                pass  # tells the running copy to show its window
            return False
        except OSError:
            return None  # port taken by something else; run anyway


def main():
    p = argparse.ArgumentParser(description="Facebook Birthday Bot")
    p.add_argument("--minimized", action="store_true", help="start minimized (used by autostart)")
    args = p.parse_args()

    server = _single_instance()
    if server is False:
        return
    root = tk.Tk()
    App(root, args.minimized, server or None)
    root.mainloop()


if __name__ == "__main__":
    main()
