#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
Native Windows GUI script (.pyw) - runs via pythonw.exe
without opening any cmd.exe console window in the background.
"""
import os
import subprocess
import sys

# Install missing dependencies in the background when run on a new PC
try:
    import mss
    import PIL
    import imageio_ffmpeg
    import pyaudiowpatch
except ImportError:
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
    req_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path], creationflags=no_win)

import netcast_gui

if __name__ == "__main__":
    netcast_gui.main()
