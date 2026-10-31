#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ulki-lekki, natywny interfejs okienkowy (GUI) dla Universal DLNA Streamer
=========================================================================
Zaprojektowany pod kątem:
- Zerowego obciążenia CPU/GPU przez samo okno (brak pętli odświeżania/timerów w tle)
- Błyskawicznej zmiany rozmiaru i 100% kompatybilności z PowerToys FancyZones (pełny responsywny grid)
- Natychmiastowego przełączania ekranów (Monitor 1 / Monitor 2 / Oba) i jakości jednym kliknięciem
"""

import ctypes
import json
import os
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

import imageio_ffmpeg
import mss

import netcast_hw_streamer as core

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamer_settings.json")


def hide_console_if_present():
    """Ukrywa ewentualne okno konsoli bez ingerencji w styl okna Win32 (pełna zgodność z FancyZones)."""
    try:
        hwnd_con = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd_con:
            ctypes.windll.user32.ShowWindow(hwnd_con, 0)
    except Exception:
        pass


def free_port_if_in_use(port=8000):
    """Zwalnia port 8000 po poprzedniej instancji bez otwierania okna konsoli."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    in_use = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    if not in_use:
        return
    try:
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        out = subprocess.check_output(
            f"netstat -ano | findstr :{port}", shell=True, creationflags=no_win
        ).decode("utf-8", errors="ignore")
        my_pid = os.getpid()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in line.upper():
                pid = int(parts[-1])
                if pid != my_pid and pid > 0:
                    subprocess.run(
                        f"taskkill /PID {pid} /F /T",
                        shell=True,
                        capture_output=True,
                        creationflags=no_win,
                    )
        time.sleep(0.3)
    except Exception:
        pass


class StreamerGUI:
    BG = "#18181b"
    BG_PANEL = "#27272a"
    FG = "#f4f4f5"
    FG_DIM = "#a1a1aa"
    ACCENT = "#2563eb"
    GREEN = "#16a34a"
    RED = "#dc2626"

    def __init__(self, root):
        self.root = root
        self.root.title("LG DLNA Streamer")
        self.root.geometry("460x310")
        # Mały minsize i pełna elastyczność (resizable True/True) dla PowerToys FancyZones
        self.root.minsize(240, 160)
        self.root.resizable(True, True)
        self.root.configure(bg=self.BG)
        hide_console_if_present()

        self.port = 8000
        self.renderers = []
        self.monitors_info = self._detect_monitors()

        saved = self._load_settings()
        self.tv_ip_var = tk.StringVar(value=saved.get("tv_ip", "192.168.0.114"))
        # The TV is remembered by its UPnP friendly name; tv_ip is only a discovery hint
        self.tv_name = saved.get("tv_name", "")
        self.monitor_var = tk.IntVar(value=saved.get("monitor", 1))
        self.height_var = tk.IntVar(value=saved.get("height", 1080))
        self.fps_var = tk.IntVar(value=saved.get("fps", 60))
        self.bitrate_var = tk.StringVar(value=saved.get("bitrate", "6M"))
        self.encoder_var = tk.StringVar(value=saved.get("encoder", "auto"))
        self.status_var = tk.StringVar(value="Inicjalizacja serwera...")

        valid_mons = [m[0] for m in self.monitors_info]
        if self.monitor_var.get() not in valid_mons and valid_mons:
            self.monitor_var.set(valid_mons[0])

        core.BROADCASTER.active_monitor = self.monitor_var.get()
        core.BROADCASTER.active_height = self.height_var.get()
        core.BROADCASTER.active_fps = self.fps_var.get()
        core.BROADCASTER.active_bitrate = self.bitrate_var.get()
        core.BROADCASTER.active_encoder = self.encoder_var.get()

        self.mon_buttons = {}
        self.res_buttons = {}
        self._build_lightweight_ui()

        self.root.bind("<F1>", lambda _e: self.select_monitor(1))
        self.root.bind("<F2>", lambda _e: self.select_monitor(2))
        self.root.bind("<F3>", lambda _e: self.select_monitor(0))

        core.BROADCASTER.status_callback = self.set_status
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._init_backend, daemon=True).start()

    def _load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "tv_ip": self.tv_ip_var.get().strip(),
                        "tv_name": self.tv_name,
                        "monitor": self.monitor_var.get(),
                        "height": self.height_var.get(),
                        "fps": self.fps_var.get(),
                        "bitrate": self.bitrate_var.get(),
                        "encoder": self.encoder_var.get(),
                    },
                    f,
                )
        except Exception:
            pass

    def _detect_monitors(self):
        result = []
        try:
            with mss.MSS() as sct:
                for i in range(1, len(sct.monitors)):
                    result.append((i, f"Ekran {i}"))
                if len(sct.monitors) > 2:
                    result.append((0, "Oba"))
        except Exception:
            result = [(1, "Ekran 1"), (2, "Ekran 2")]
        return result

    def _build_lightweight_ui(self):
        """Buduje responsywny układ oparty na pojedynczej siatce grid (maksymalna szybkość podczas zmiany rozmiaru)."""
        main = tk.Frame(self.root, bg=self.BG, padx=10, pady=8)
        main.pack(fill="both", expand=True)

        main.columnconfigure(1, weight=1)
        main.rowconfigure(4, weight=1)

        # Wiersz 0: Telewizor IP + Szukaj
        tk.Label(main, text="TV IP:", font=("Segoe UI", 9, "bold"), bg=self.BG, fg=self.FG_DIM).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        tv_frame = tk.Frame(main, bg=self.BG)
        tv_frame.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        tv_frame.columnconfigure(0, weight=1)

        self.tv_combo = ttk.Combobox(
            tv_frame,
            textvariable=self.tv_ip_var,
            font=("Segoe UI", 9),
        )
        self.tv_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.tv_combo.bind("<<ComboboxSelected>>", self._on_tv_selected)

        tk.Button(
            tv_frame,
            text="Szukaj",
            font=("Segoe UI", 8, "bold"),
            bg=self.BG_PANEL,
            fg=self.FG,
            relief="flat",
            padx=8,
            command=self.scan_tvs_async,
        ).grid(row=0, column=1)

        # Wiersz 1: Wybór Ekranu (szybkie natywne przyciski rozciągane w gridzie)
        tk.Label(main, text="Ekran:", font=("Segoe UI", 9, "bold"), bg=self.BG, fg=self.FG_DIM).grid(
            row=1, column=0, sticky="w", pady=4
        )
        mon_frame = tk.Frame(main, bg=self.BG)
        mon_frame.grid(row=1, column=1, sticky="ew", pady=4)

        for idx, (m_id, m_label) in enumerate(self.monitors_info):
            mon_frame.columnconfigure(idx, weight=1)
            btn = tk.Button(
                mon_frame,
                text=m_label,
                font=("Segoe UI", 9, "bold"),
                relief="flat",
                pady=4,
                command=lambda mid=m_id: self.select_monitor(mid),
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 4, 0))
            self.mon_buttons[m_id] = btn

        # Wiersz 2: Jakość / Rozdzielczość
        tk.Label(main, text="Jakość:", font=("Segoe UI", 9, "bold"), bg=self.BG, fg=self.FG_DIM).grid(
            row=2, column=0, sticky="w", pady=4
        )
        res_frame = tk.Frame(main, bg=self.BG)
        res_frame.grid(row=2, column=1, sticky="ew", pady=4)
        res_frame.columnconfigure(0, weight=1)
        res_frame.columnconfigure(1, weight=1)

        for idx, (h_val, h_label) in enumerate([(1080, "1080p (Full HD)"), (720, "720p (Szybki)")]):
            btn = tk.Button(
                res_frame,
                text=h_label,
                font=("Segoe UI", 9, "bold"),
                relief="flat",
                pady=4,
                command=lambda hv=h_val: self.select_resolution(hv),
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 4, 0))
            self.res_buttons[h_val] = btn

        # Wiersz 3: FPS + Bitrate + Koder (kompaktowe listy)
        tk.Label(main, text="Opcje:", font=("Segoe UI", 9, "bold"), bg=self.BG, fg=self.FG_DIM).grid(
            row=3, column=0, sticky="w", pady=4
        )
        opt_frame = tk.Frame(main, bg=self.BG)
        opt_frame.grid(row=3, column=1, sticky="ew", pady=4)
        opt_frame.columnconfigure(0, weight=1)
        opt_frame.columnconfigure(1, weight=1)
        opt_frame.columnconfigure(2, weight=1)

        fps_cb = ttk.Combobox(opt_frame, textvariable=self.fps_var, values=[60, 30], state="readonly", width=6)
        fps_cb.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        fps_cb.bind("<<ComboboxSelected>>", lambda _e: self.on_profile_change())

        br_cb = ttk.Combobox(
            opt_frame, textvariable=self.bitrate_var, values=["4500k", "6M", "8M", "10M"], state="readonly", width=7
        )
        br_cb.grid(row=0, column=1, sticky="ew", padx=2)
        br_cb.bind("<<ComboboxSelected>>", lambda _e: self.on_profile_change())

        enc_cb = ttk.Combobox(
            opt_frame,
            textvariable=self.encoder_var,
            values=["auto", "h264_nvenc", "h264_amf", "h264_qsv", "libx264"],
            state="readonly",
            width=10,
        )
        enc_cb.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        enc_cb.bind("<<ComboboxSelected>>", lambda _e: self.on_profile_change())

        # Wiersz 4: Główne przyciski akcji (rozciągają się proporcjonalnie w FancyZones)
        act_frame = tk.Frame(main, bg=self.BG)
        act_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(8, 4))
        act_frame.columnconfigure(0, weight=3)
        act_frame.columnconfigure(1, weight=1)
        act_frame.rowconfigure(0, weight=1)

        tk.Button(
            act_frame,
            text="▶ WŁĄCZ / PRZEŁĄCZ NA TV",
            font=("Segoe UI", 10, "bold"),
            bg=self.GREEN,
            fg="#ffffff",
            activebackground="#15803d",
            activeforeground="#ffffff",
            relief="flat",
            command=self.push_to_tv_async,
        ).grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        tk.Button(
            act_frame,
            text="⏹ STOP",
            font=("Segoe UI", 10, "bold"),
            bg=self.RED,
            fg="#ffffff",
            activebackground="#b91c1c",
            activeforeground="#ffffff",
            relief="flat",
            command=self.stop_on_tv_async,
        ).grid(row=0, column=1, sticky="nsew")

        # Wiersz 5: Lekki pasek statusu (aktualizowany wyłącznie zdarzeniowo - 0% CPU w spoczynku)
        tk.Label(
            main,
            textvariable=self.status_var,
            font=("Segoe UI", 8),
            bg=self.BG,
            fg=self.FG_DIM,
            anchor="w",
        ).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        self._refresh_button_colors()

    def _refresh_button_colors(self):
        curr_m = self.monitor_var.get()
        for mid, btn in self.mon_buttons.items():
            active = mid == curr_m
            btn.configure(bg=self.ACCENT if active else self.BG_PANEL, fg="#ffffff" if active else self.FG_DIM)

        curr_h = self.height_var.get()
        for hval, btn in self.res_buttons.items():
            active = hval == curr_h
            btn.configure(bg=self.ACCENT if active else self.BG_PANEL, fg="#ffffff" if active else self.FG_DIM)

    def select_monitor(self, mon_id):
        if mon_id not in self.mon_buttons or self.monitor_var.get() == mon_id:
            return
        self.monitor_var.set(mon_id)
        self._refresh_button_colors()
        self.on_profile_change()

    def select_resolution(self, h_val):
        if self.height_var.get() == h_val:
            return
        self.height_var.set(h_val)
        self._refresh_button_colors()
        self.on_profile_change()

    def set_status(self, text):
        clean = text.strip()
        if not clean:
            return
        try:
            self.root.after(0, lambda: self.status_var.set(clean))
        except Exception:
            pass

    def _init_backend(self):
        self.set_status("Sprawdzanie portu 8000 i kodera GPU...")
        free_port_if_in_use(self.port)

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        _enc_id, enc_label, _ = core.detect_best_h264_encoder(ffmpeg_exe, self.encoder_var.get())
        self.set_status(f"Koder: {enc_label}")

        self._scan_tvs_worker(initial_auto_push=True)

        threading.Thread(target=core.global_wasapi_capture_thread, daemon=True).start()
        threading.Thread(
            target=core.ffmpeg_producer_loop,
            args=(
                self.monitor_var.get(),
                self.fps_var.get(),
                self.height_var.get(),
                self.bitrate_var.get(),
                self.encoder_var.get(),
            ),
            daemon=True,
        ).start()

        try:
            self.http_server = core.ThreadingHTTPServer(("0.0.0.0", self.port), core.DLNAHandler)
            threading.Thread(target=self.http_server.serve_forever, daemon=True).start()
            self.set_status(f"Gotowy ({enc_label.split('(')[0].strip()}) • {core.BASE_HTTP_URL}")
        except Exception as exc:
            self.set_status(f"Błąd portu {self.port}: {exc}")

    def _scan_tvs_worker(self, initial_auto_push=False):
        raw = self.tv_ip_var.get().strip()
        # Wyciągnij sam adres IP jeśli w polu była nazwa z listy
        hint = raw.split()[-1].strip("()") if " " in raw else (raw or "192.168.0.114")
        self.set_status(f"Szukanie TV w sieci ({self.tv_name or hint})...")
        found = core.discover_all_dlna_renderers(hint_ip=hint, preferred_name=self.tv_name)
        self._set_renderers(found)
        target = self._find_remembered_tv(found)

        if target:
            name, _ctrl, ip = target
            self.tv_name = name
            local_ip = core.get_preferred_ip(ip)
            core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"
            self.root.after(0, lambda: self.tv_ip_var.set(ip))
            self.set_status(f"Połączono: {name} ({ip})")
            if initial_auto_push:
                time.sleep(1.5)
                self._push_worker()
        elif found:
            # Never fall back to another TV on its own; the user picks one from the list instead
            self.set_status(f"Nie znaleziono TV: {self.tv_name} • wybierz odbiornik z listy")
        else:
            local_ip = core.get_preferred_ip(hint)
            core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"
            self.set_status(f"Brak odpowiedzi UPnP z {hint} (sprawdź czy TV jest włączony)")

    def scan_tvs_async(self):
        threading.Thread(target=self._scan_tvs_worker, args=(False,), daemon=True).start()

    def _set_renderers(self, found):
        self.renderers = found
        values = [f"{ip} ({name})" for name, _ctrl, ip in found]
        self.root.after(0, lambda: self.tv_combo.configure(values=values))

    def _find_remembered_tv(self, renderers):
        """Zapamiętany TV (po nazwie) albo, jeśli żadnego nie zapamiętano, pierwszy z listy."""
        if not self.tv_name:
            return renderers[0] if renderers else None
        return next((r for r in renderers if r[0] == self.tv_name), None)

    def _on_tv_selected(self, _event=None):
        val = self.tv_ip_var.get().strip()
        ip, _sep, rest = val.partition(" (")
        if rest.endswith(")"):
            self.tv_name = rest[:-1]
            self.tv_ip_var.set(ip)
        self._save_settings()

    def _get_active_control_url(self, refresh=False):
        raw = self.tv_ip_var.get().strip()
        ip_only = raw.split()[0] if " " in raw else raw
        if not refresh:
            for name, ctrl, ip in self.renderers:
                if (name == self.tv_name) if self.tv_name else (ip == ip_only):
                    return ctrl, ip
        found = core.discover_all_dlna_renderers(hint_ip=ip_only, preferred_name=self.tv_name)
        self._set_renderers(found)
        target = self._find_remembered_tv(found)
        if target:
            name, ctrl, ip = target
            self.tv_name = name
            self.root.after(0, lambda: self.tv_ip_var.set(ip))
            return ctrl, ip
        return None, self.tv_name or ip_only

    def on_profile_change(self):
        mon = self.monitor_var.get()
        h = int(self.height_var.get())
        fps = int(self.fps_var.get())
        br = self.bitrate_var.get()
        enc = self.encoder_var.get()
        self._save_settings()
        core.BROADCASTER.set_stream_profile(mon, height=h, fps=fps, bitrate=br, encoder=enc)
        self.push_to_tv_async()

    def _push_worker(self):
        ctrl_url, tv_ip = self._get_active_control_url()
        if not ctrl_url:
            self.set_status(f"Brak odpowiedzi z TV ({tv_ip})")
            return
        self._save_settings()
        if self._push_stream(ctrl_url, tv_ip):
            return
        # The cached control URL goes stale once DHCP gives the TV a new IP: rediscover and retry once
        new_ctrl, new_ip = self._get_active_control_url(refresh=True)
        if new_ctrl and new_ctrl != ctrl_url:
            self._save_settings()
            self._push_stream(new_ctrl, new_ip)

    def _push_stream(self, ctrl_url, tv_ip):
        local_ip = core.get_preferred_ip(tv_ip)
        core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"
        mon = self.monitor_var.get()
        h = self.height_var.get()
        stream_url = f"{core.BASE_HTTP_URL}/live/mon{mon}_{h}.mpg?t={int(time.time())}"
        self.set_status(f"Przełączanie na TV ({tv_ip}) -> Ekran {mon}, {h}p @ {self.fps_var.get()} FPS...")
        return core.push_stream_to_tv_dlna(ctrl_url, stream_url)

    def push_to_tv_async(self):
        threading.Thread(target=self._push_worker, daemon=True).start()

    def stop_on_tv_async(self):
        def _stop():
            ctrl_url, _tv_ip = self._get_active_control_url()
            if ctrl_url:
                core.stop_tv_dlna(ctrl_url)

        threading.Thread(target=_stop, daemon=True).start()

    def on_close(self):
        self._save_settings()
        core.RUNNING = False
        if core.BROADCASTER.ffmpeg_proc:
            try:
                core.BROADCASTER.ffmpeg_proc.kill()
            except Exception:
                pass
        self.root.destroy()
        os._exit(0)


def main():
    root = tk.Tk()
    StreamerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
