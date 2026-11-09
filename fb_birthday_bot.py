"""FB Birthday Bot – launch the GUI.  Usage:  python fb_birthday_bot.py [--minimized]"""
import sys

if sys.version_info < (3, 10):
    sys.exit("FB Birthday Bot needs Python 3.10 or newer.")

try:
    import tkinter  # noqa: F401
except ImportError:
    sys.exit("tkinter isn't installed. On Linux: sudo apt install python3-tk (or your distro's equivalent).")

from birthday_bot.app import main

if __name__ == "__main__":
    main()
