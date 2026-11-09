"""Start the app automatically when you log in to your computer (Windows / macOS / Linux)."""
from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

ENTRY_SCRIPT = Path(__file__).resolve().parent.parent / "fb_birthday_bot.py"
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WIN_VALUE = "FBBirthdayBot"
MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.fbbirthdaybot.app.plist"
LINUX_DESKTOP = Path.home() / ".config" / "autostart" / "fb-birthday-bot.desktop"


def _command() -> list[str]:
    if getattr(sys, "frozen", False):  # packaged with PyInstaller
        return [sys.executable, "--minimized"]
    exe = Path(sys.executable)
    if sys.platform.startswith("win"):
        pyw = exe.with_name("pythonw.exe")  # no console window
        if pyw.exists():
            exe = pyw
    return [str(exe), str(ENTRY_SCRIPT), "--minimized"]


def is_enabled() -> bool:
    if sys.platform.startswith("win"):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as k:
                winreg.QueryValueEx(k, WIN_VALUE)
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return MAC_PLIST.exists()
    return LINUX_DESKTOP.exists()


def set_enabled(enabled: bool) -> None:
    cmd = _command()
    if sys.platform.startswith("win"):
        import subprocess
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY) as k:
            if enabled:
                winreg.SetValueEx(k, WIN_VALUE, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, WIN_VALUE)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        if enabled:
            MAC_PLIST.parent.mkdir(parents=True, exist_ok=True)
            with open(MAC_PLIST, "wb") as f:
                plistlib.dump({"Label": "com.fbbirthdaybot.app", "ProgramArguments": cmd,
                               "RunAtLoad": True}, f)
        else:
            MAC_PLIST.unlink(missing_ok=True)
    else:
        if enabled:
            LINUX_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
            LINUX_DESKTOP.write_text(
                "[Desktop Entry]\nType=Application\nName=FB Birthday Bot\n"
                f"Exec={' '.join(shlex.quote(c) for c in cmd)}\n"
                "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
        else:
            LINUX_DESKTOP.unlink(missing_ok=True)
