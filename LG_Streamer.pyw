#!/usr/bin/env pythonw
# -*- coding: utf-8 -*-
"""
Natywny skrypt okienkowy Windows (.pyw) - uruchamia się przez pythonw.exe
bez otwierania jakiegokolwiek okna konsoli cmd.exe w tle.
"""
import os
import subprocess
import sys

# Automatycznie doinstaluj zależności w tle, jeśli uruchamiane na nowym PC
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
