#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Okienkowy interfejs graficzny (GUI) dla Universal DLNA Screen + Audio Streamer
==============================================================================
Pozwala jednym kliknięciem:
- wybrać telewizor DLNA w sieci domowej (np. LG 55LM670S-ZA)
- przełączać monitor PC w locie (Monitor 1 / Monitor 2 / Wszystkie)
- zmieniać rozdzielczość (Full HD 1080p / HD 720p), płynność (60 / 30 FPS), bitrate i koder GPU/CPU
- włączać oraz zatrzymywać strumień bezpośrednio na ekranie telewizora
"""

import ctypes
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


def enable_dark_titlebar_and_dpi(root):
    """Ukrywa ewentualne okno konsoli, włącza High-DPI oraz ciemny pasek tytułowy w Windows 10/11."""
    try:
        hwnd_con = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd_con:
            ctypes.windll.user32.ShowWindow(hwnd_con, 0)  # SW_HIDE
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        root.update()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def free_port_if_in_use(port=8000):
    """Jeśli w tle działa poprzednia instancja konsolowa na porcie 8000, zwalnia port bez otwierania okna."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
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
        time.sleep(0.4)
    except Exception:
        pass


class StreamerGUI:
    BG_MAIN = "#0d1117"
    BG_CARD = "#161b22"
    BG_INPUT = "#21262d"
    FG_TEXT = "#f0f6fc"
    FG_MUTED = "#8b949e"
    ACCENT_BLUE = "#1f6feb"
    ACCENT_GREEN = "#238636"
    ACCENT_RED = "#da3633"
    BORDER_COL = "#30363d"

    def __init__(self, root):
        self.root = root
        self.root.title("LG NetCast & Universal DLNA Streamer (Full HD 60 FPS + Audio)")
        self.root.geometry("720x620")
        self.root.minsize(680, 580)
        self.root.configure(bg=self.BG_MAIN)
        enable_dark_titlebar_and_dpi(self.root)

        self.port = 8000
        self.renderers = []  # [(name, ctrl_url, ip), ...]
        self.monitors_info = self._detect_monitors()

        # Zmienne stanu GUI
        self.tv_choice_var = tk.StringVar(value="Szukanie telewizorów w sieci...")
        self.manual_ip_var = tk.StringVar(value="192.168.0.114")
        self.monitor_var = tk.IntVar(value=1)
        self.height_var = tk.IntVar(value=1080)
        self.fps_var = tk.IntVar(value=60)
        self.bitrate_var = tk.StringVar(value="6M")
        self.encoder_var = tk.StringVar(value="auto")
        self.status_badge_var = tk.StringVar(value="Inicjalizacja serwera...")

        self._build_ui()
        core.BROADCASTER.status_callback = self.append_log

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._init_backend, daemon=True).start()

    def _detect_monitors(self):
        result = []
        try:
            with mss.MSS() as sct:
                for i in range(1, len(sct.monitors)):
                    m = sct.monitors[i]
                    result.append((i, f"Monitor {i} ({m['width']}x{m['height']})"))
                if len(sct.monitors) > 2:
                    m0 = sct.monitors[0]
                    result.append((0, f"Oba monitory ({m0['width']}x{m0['height']})"))
        except Exception:
            result = [(1, "Monitor 1 (Główny)"), (2, "Monitor 2")]
        return result

    def _build_ui(self):
        # Nagłówek
        header = tk.Frame(self.root, bg=self.BG_MAIN, padx=20, pady=14)
        header.pack(fill="x")

        tk.Label(
            header,
            text="PC -> TV DLNA Live Streamer",
            font=("Segoe UI", 16, "bold"),
            bg=self.BG_MAIN,
            fg="#58a6ff",
        ).pack(side="left")

        self.status_lbl = tk.Label(
            header,
            textvariable=self.status_badge_var,
            font=("Segoe UI", 10, "bold"),
            bg="#1f2937",
            fg="#7ee787",
            padx=12,
            pady=5,
        )
        self.status_lbl.pack(side="right")

        # Karta 1: Wybór telewizora
        card_tv = tk.LabelFrame(
            self.root,
            text=" 1. Docelowy Telewizor (DLNA / UPnP) ",
            font=("Segoe UI", 10, "bold"),
            bg=self.BG_CARD,
            fg=self.FG_TEXT,
            bd=1,
            relief="solid",
            padx=16,
            pady=12,
        )
        card_tv.pack(fill="x", padx=20, pady=(0, 10))

        row_tv = tk.Frame(card_tv, bg=self.BG_CARD)
        row_tv.pack(fill="x")

        self.tv_combo = ttk.Combobox(
            row_tv,
            textvariable=self.tv_choice_var,
            state="readonly",
            font=("Segoe UI", 10),
            width=42,
        )
        self.tv_combo.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.tv_combo.bind("<<ComboboxSelected>>", self._on_tv_selected)

        btn_scan = tk.Button(
            row_tv,
            text="Odśwież listę TV",
            font=("Segoe UI", 9, "bold"),
            bg=self.BG_INPUT,
            fg=self.FG_TEXT,
            activebackground=self.ACCENT_BLUE,
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=5,
            cursor="hand2",
            command=self.scan_tvs_async,
        )
        btn_scan.pack(side="right")

        row_ip = tk.Frame(card_tv, bg=self.BG_CARD, pady=6)
        row_ip.pack(fill="x")
        tk.Label(
            row_ip,
            text="Adres IP telewizora (np. LG 55LM670S-ZA):",
            font=("Segoe UI", 9),
            bg=self.BG_CARD,
            fg=self.FG_MUTED,
        ).pack(side="left")
        tk.Entry(
            row_ip,
            textvariable=self.manual_ip_var,
            font=("Consolas", 10),
            bg=self.BG_INPUT,
            fg=self.FG_TEXT,
            insertbackground=self.FG_TEXT,
            relief="flat",
            width=18,
        ).pack(side="left", padx=10)
        tk.Label(
            row_ip,
            text="(Wskazówka: wyjdź z menu Smart/Przeglądarki przyciskiem EXIT na pilocie)",
            font=("Segoe UI", 8),
            bg=self.BG_CARD,
            fg=self.FG_MUTED,
        ).pack(side="left")

        # Karta 2: Monitor i Parametry Wideo/Audio
        card_cfg = tk.LabelFrame(
            self.root,
            text=" 2. Źródło Obrazu i Jakość Strumienia (Zmiana w locie) ",
            font=("Segoe UI", 10, "bold"),
            bg=self.BG_CARD,
            fg=self.FG_TEXT,
            bd=1,
            relief="solid",
            padx=16,
            pady=12,
        )
        card_cfg.pack(fill="x", padx=20, pady=(0, 10))

        # Wybór monitora
        row_mon = tk.Frame(card_cfg, bg=self.BG_CARD)
        row_mon.pack(fill="x", pady=(0, 10))
        tk.Label(
            row_mon,
            text="Przechwytywany ekran:",
            font=("Segoe UI", 10, "bold"),
            bg=self.BG_CARD,
            fg=self.FG_TEXT,
            width=20,
            anchor="w",
        ).pack(side="left")

        for m_idx, m_label in self.monitors_info:
            rb = tk.Radiobutton(
                row_mon,
                text=m_label,
                variable=self.monitor_var,
                value=m_idx,
                font=("Segoe UI", 9, "bold"),
                bg=self.BG_CARD,
                fg=self.FG_TEXT,
                selectcolor=self.BG_INPUT,
                activebackground=self.BG_CARD,
                activeforeground="#58a6ff",
                cursor="hand2",
                command=self.on_profile_change,
            )
            rb.pack(side="left", padx=(0, 14))

        # Rozdzielczość i FPS
        row_res = tk.Frame(card_cfg, bg=self.BG_CARD)
        row_res.pack(fill="x", pady=(0, 10))
        tk.Label(
            row_res,
            text="Rozdzielczość i FPS:",
            font=("Segoe UI", 10, "bold"),
            bg=self.BG_CARD,
            fg=self.FG_TEXT,
            width=20,
            anchor="w",
        ).pack(side="left")

        for h_val, h_txt in [(1080, "Full HD 1080p"), (720, "HD 720p (Ultra-Low-Lag)")]:
            tk.Radiobutton(
                row_res,
                text=h_txt,
                variable=self.height_var,
                value=h_val,
                font=("Segoe UI", 9, "bold"),
                bg=self.BG_CARD,
                fg=self.FG_TEXT,
                selectcolor=self.BG_INPUT,
                activebackground=self.BG_CARD,
                activeforeground="#58a6ff",
                cursor="hand2",
                command=self.on_profile_change,
            ).pack(side="left", padx=(0, 14))

        for fps_val in (60, 30):
            tk.Radiobutton(
                row_res,
                text=f"{fps_val} FPS",
                variable=self.fps_var,
                value=fps_val,
                font=("Segoe UI", 9),
                bg=self.BG_CARD,
                fg="#a5d6ff",
                selectcolor=self.BG_INPUT,
                activebackground=self.BG_CARD,
                cursor="hand2",
                command=self.on_profile_change,
            ).pack(side="left", padx=(6, 6))

        # Bitrate i Koder
        row_enc = tk.Frame(card_cfg, bg=self.BG_CARD)
        row_enc.pack(fill="x")
        tk.Label(
            row_enc,
            text="Bitrate / Koder GPU:",
            font=("Segoe UI", 10, "bold"),
            bg=self.BG_CARD,
            fg=self.FG_TEXT,
            width=20,
            anchor="w",
        ).pack(side="left")

        br_combo = ttk.Combobox(
            row_enc,
            textvariable=self.bitrate_var,
            values=["4500k", "6M", "8M", "10M"],
            state="readonly",
            font=("Segoe UI", 9),
            width=10,
        )
        br_combo.pack(side="left", padx=(0, 16))
        br_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_profile_change())

        enc_combo = ttk.Combobox(
            row_enc,
            textvariable=self.encoder_var,
            values=["auto", "h264_nvenc", "h264_amf", "h264_qsv", "libx264"],
            state="readonly",
            font=("Segoe UI", 9),
            width=16,
        )
        enc_combo.pack(side="left")
        enc_combo.bind("<<ComboboxSelected>>", lambda _e: self.on_profile_change())

        self.enc_detected_lbl = tk.Label(
            row_enc,
            text="Koder: wykrywanie...",
            font=("Segoe UI", 9),
            bg=self.BG_CARD,
            fg="#7ee787",
        )
        self.enc_detected_lbl.pack(side="left", padx=12)

        # Przyciski Akcji (Start / Push oraz Stop)
        btn_frame = tk.Frame(self.root, bg=self.BG_MAIN, padx=20, pady=6)
        btn_frame.pack(fill="x")

        self.btn_push = tk.Button(
            btn_frame,
            text="▶  WŁĄCZ / PRZEŁĄCZ STREAM NA TELEWIZORZE",
            font=("Segoe UI", 12, "bold"),
            bg=self.ACCENT_GREEN,
            fg="#ffffff",
            activebackground="#2ea043",
            activeforeground="#ffffff",
            relief="flat",
            pady=10,
            cursor="hand2",
            command=self.push_to_tv_async,
        )
        self.btn_push.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.btn_stop = tk.Button(
            btn_frame,
            text="⏹  ZATRZYMAJ NA TV",
            font=("Segoe UI", 11, "bold"),
            bg=self.ACCENT_RED,
            fg="#ffffff",
            activebackground="#f85149",
            activeforeground="#ffffff",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
            command=self.stop_on_tv_async,
        )
        self.btn_stop.pack(side="right")

        # Konsola logów na dole
        log_frame = tk.LabelFrame(
            self.root,
            text=" Logi strumienia i diagnostyka DLNA ",
            font=("Segoe UI", 9, "bold"),
            bg=self.BG_CARD,
            fg=self.FG_MUTED,
            bd=1,
            relief="solid",
            padx=10,
            pady=8,
        )
        log_frame.pack(fill="both", expand=True, padx=20, pady=(4, 16))

        self.log_text = tk.Text(
            log_frame,
            height=8,
            bg="#090d11",
            fg="#c9d1d9",
            font=("Consolas", 9),
            relief="flat",
            state="disabled",
            wrap="word",
        )
        self.log_text.pack(fill="both", expand=True)

    def append_log(self, text):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {text}\n"

        def _update():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        try:
            self.root.after(0, _update)
        except Exception:
            pass

    def _init_backend(self):
        self.append_log("Sprawdzanie portu 8000 i inicjalizacja kodera...")
        free_port_if_in_use(self.port)

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        enc_id, enc_label, _ = core.detect_best_h264_encoder(ffmpeg_exe, "auto")
        self.root.after(0, lambda: self.enc_detected_lbl.configure(text=f"Aktywny: {enc_label}"))
        self.append_log(f"Wykryto koder wideo: {enc_label} ({enc_id})")

        # Wykryj telewizory w sieci
        self._scan_tvs_worker(initial_auto_push=True)

        # Uruchom wątek dźwięku WASAPI Loopback
        threading.Thread(target=core.global_wasapi_capture_thread, daemon=True).start()

        # Uruchom wątek kodera FFmpeg
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

        # Uruchom serwer HTTP/DLNA
        try:
            self.http_server = core.ThreadingHTTPServer(("0.0.0.0", self.port), core.DLNAHandler)
            threading.Thread(target=self.http_server.serve_forever, daemon=True).start()
            self.root.after(0, lambda: self.status_badge_var.set(f"● Serwer Aktywny (:{self.port})"))
            self.append_log(f"Serwer DLNA/HTTP nasłuchuje na: {core.BASE_HTTP_URL}")
        except Exception as exc:
            self.root.after(0, lambda: self.status_badge_var.set("Błąd portu 8000"))
            self.append_log(f"Błąd bindowania portu {self.port}: {exc}")

    def _scan_tvs_worker(self, initial_auto_push=False):
        hint = self.manual_ip_var.get().strip() or "192.168.0.114"
        self.append_log(f"Skanowanie sieci LAN w poszukiwaniu telewizorów DLNA (priorytet: {hint})...")
        found = core.discover_all_dlna_renderers(hint_ip=hint)
        self.renderers = found

        if found:
            labels = [f"{name} ({ip})" for name, _ctrl, ip in found]
            first_ip = found[0][2]
            local_ip = core.get_preferred_ip(first_ip)
            core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"

            def _upd():
                self.tv_combo["values"] = labels
                self.tv_combo.current(0)
                self.manual_ip_var.set(first_ip)

            self.root.after(0, _upd)
            self.append_log(f"Wykryto telewizor: {labels[0]}")
            if initial_auto_push:
                time.sleep(1.8)
                self._push_worker()
        else:
            local_ip = core.get_preferred_ip(hint)
            core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"

            def _upd_none():
                self.tv_combo["values"] = [f"Brak odpowiedzi SSDP (użyj IP: {hint})"]
                self.tv_combo.current(0)

            self.root.after(0, _upd_none)
            self.append_log("Nie wykryto odpowiedzi UPnP (upewnij się, że TV jest włączony).")

    def scan_tvs_async(self):
        threading.Thread(target=self._scan_tvs_worker, args=(False,), daemon=True).start()

    def _on_tv_selected(self, _event=None):
        idx = self.tv_combo.current()
        if 0 <= idx < len(self.renderers):
            self.manual_ip_var.set(self.renderers[idx][2])

    def _get_active_control_url(self):
        idx = self.tv_combo.current()
        if 0 <= idx < len(self.renderers):
            return self.renderers[idx][1], self.renderers[idx][2]
        hint = self.manual_ip_var.get().strip()
        found = core.discover_all_dlna_renderers(hint_ip=hint)
        if found:
            self.renderers = found
            return found[0][1], found[0][2]
        return None, hint

    def on_profile_change(self):
        mon = self.monitor_var.get()
        h = self.height_var.get()
        fps = self.fps_var.get()
        br = self.bitrate_var.get()
        enc = self.encoder_var.get()
        core.BROADCASTER.set_stream_profile(mon, height=h, fps=fps, bitrate=br, encoder=enc)
        # Automatycznie przeładuj strumień na TV po zmianie profilu
        self.push_to_tv_async()

    def _push_worker(self):
        ctrl_url, tv_ip = self._get_active_control_url()
        if not ctrl_url:
            self.append_log(f"Nie znaleziono usługi AVTransport na TV ({tv_ip}). Sprawdź czy TV jest włączony.")
            return
        local_ip = core.get_preferred_ip(tv_ip)
        core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"
        mon = self.monitor_var.get()
        h = self.height_var.get()
        stream_url = f"{core.BASE_HTTP_URL}/live/mon{mon}_{h}.mpg?t={int(time.time())}"
        self.append_log(f"Wysyłanie strumienia do TV ({tv_ip}) -> Monitor {mon}, {h}p @ {self.fps_var.get()} FPS...")
        core.push_stream_to_tv_dlna(ctrl_url, stream_url)

    def push_to_tv_async(self):
        threading.Thread(target=self._push_worker, daemon=True).start()

    def stop_on_tv_async(self):
        def _stop():
            ctrl_url, _tv_ip = self._get_active_control_url()
            if ctrl_url:
                core.stop_tv_dlna(ctrl_url)

        threading.Thread(target=_stop, daemon=True).start()

    def on_close(self):
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
