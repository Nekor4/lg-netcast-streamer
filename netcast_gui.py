#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nowoczesny interfejs graficzny (GUI) dla Universal DLNA Screen + Audio Streamer
===============================================================================
Zaprojektowany pod kątem maksymalnej wygody (UX) i nowoczesnego wyglądu (Dark UI):
- Wizualne karty monitorów z podglądem proporcji i skrótami klawiszowymi (F1 / F2 / F3)
- Pasek telemetrii na żywo: status odbioru przez TV, bitrate (Mbps) oraz wskaźnik wysterowania dźwięku (VU Meter)
- 1-klikowe presety jakości (Gra / Kino / Ultra-Low-Lag) oraz nowoczesne przełączniki kafelkowe (Segmented Pills)
- Automatyczny zapis ostatnich ustawień użytkownika (streamer_settings.json)
- Zwijana szuflada logów diagnostycznych (czystość interfejsu na co dzień)
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
    """Jeśli w tle działa poprzednia instancja na porcie 8000, zwalnia port bez otwierania okna konsoli."""
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


class SegmentedPills(tk.Frame):
    """Nowoczesny przełącznik kafelkowy (Segmented Control) zastępujący stare Radiobuttony i Comboboxy."""

    def __init__(self, parent, options, variable, command=None, bg="#151b23"):
        super().__init__(parent, bg=bg)
        self.options = options  # [(val, label), ...]
        self.variable = variable
        self.command = command
        self.buttons = {}

        for val, label in options:
            btn = tk.Label(
                self,
                text=label,
                font=("Segoe UI", 9, "bold"),
                padx=12,
                pady=5,
                cursor="hand2",
                bd=0,
            )
            btn.pack(side="left", padx=(0, 6))
            btn.bind("<Button-1>", lambda _e, v=val: self._select(v))
            btn.bind("<Enter>", lambda _e, v=val, b=btn: self._on_hover(v, b, True))
            btn.bind("<Leave>", lambda _e, v=val, b=btn: self._on_hover(v, b, False))
            self.buttons[val] = btn

        self.refresh()

    def _select(self, val):
        if self.variable.get() == val:
            return
        self.variable.set(val)
        self.refresh()
        if self.command:
            self.command()

    def _on_hover(self, val, btn, entering):
        if self.variable.get() == val:
            return
        btn.configure(bg="#262f3d" if entering else "#1c232d", fg="#f0f6fc" if entering else "#9ca3af")

    def refresh(self):
        curr = self.variable.get()
        for val, btn in self.buttons.items():
            if val == curr:
                btn.configure(bg="#1f6feb", fg="#ffffff")
            else:
                btn.configure(bg="#1c232d", fg="#9ca3af")


class StreamerGUI:
    # Paleta kolorystyczna GitHub Dark / Modern Studio
    BG_MAIN = "#0b0f14"
    BG_CARD = "#151b23"
    BG_CARD_ALT = "#1c232d"
    BG_INPUT = "#0d1117"
    FG_TEXT = "#f0f6fc"
    FG_MUTED = "#8b949e"
    ACCENT_BLUE = "#1f6feb"
    ACCENT_BLUE_HOVER = "#388bfd"
    ACCENT_GREEN = "#238636"
    ACCENT_GREEN_HOVER = "#2ea043"
    ACCENT_RED = "#da3633"
    ACCENT_RED_HOVER = "#f85149"
    BORDER_SUBTLE = "#262c36"

    def __init__(self, root):
        self.root = root
        self.root.title("LG NetCast & Universal DLNA Streamer")
        self.root.geometry("780x635")
        self.root.minsize(740, 590)
        self.root.configure(bg=self.BG_MAIN)
        enable_dark_titlebar_and_dpi(self.root)
        self._setup_dark_ttk_styles()

        self.port = 8000
        self.renderers = []  # [(name, ctrl_url, ip), ...]
        self.monitors_info = self._detect_monitors()
        self.logs_visible = False

        # Wczytaj zapisane ustawienia lub domyślne
        saved = self._load_settings()

        self.tv_choice_var = tk.StringVar(value="Szukanie telewizorów w sieci LAN...")
        self.manual_ip_var = tk.StringVar(value=saved.get("tv_ip", "192.168.0.114"))
        self.monitor_var = tk.IntVar(value=saved.get("monitor", 1))
        self.height_var = tk.IntVar(value=saved.get("height", 1080))
        self.fps_var = tk.IntVar(value=saved.get("fps", 60))
        self.bitrate_var = tk.StringVar(value=saved.get("bitrate", "6M"))
        self.encoder_var = tk.StringVar(value=saved.get("encoder", "auto"))

        # Walidacja numeru monitora
        valid_mons = [m[0] for m in self.monitors_info]
        if self.monitor_var.get() not in valid_mons and valid_mons:
            self.monitor_var.set(valid_mons[0])

        # Synchronizacja początkowego profilu z backendem
        core.BROADCASTER.active_monitor = self.monitor_var.get()
        core.BROADCASTER.active_height = self.height_var.get()
        core.BROADCASTER.active_fps = self.fps_var.get()
        core.BROADCASTER.active_bitrate = self.bitrate_var.get()
        core.BROADCASTER.active_encoder = self.encoder_var.get()

        self.monitor_cards = {}
        self.pill_groups = []

        self._build_ui()
        self._bind_shortcuts()
        core.BROADCASTER.status_callback = self.append_log

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._init_backend, daemon=True).start()
        self._schedule_telemetry_update()

    def _setup_dark_ttk_styles(self):
        """Wymusza ciemny motyw dla listy rozwijanej ttk.Combobox (zero białych pól)."""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Dark.TCombobox",
            fieldbackground=self.BG_CARD_ALT,
            background=self.BG_CARD_ALT,
            foreground=self.FG_TEXT,
            arrowcolor="#58a6ff",
            bordercolor=self.BORDER_SUBTLE,
            lightcolor=self.BG_CARD_ALT,
            darkcolor=self.BG_CARD_ALT,
            padding=6,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", self.BG_CARD_ALT)],
            selectbackground=[("readonly", self.BG_CARD_ALT)],
            selectforeground=[("readonly", self.FG_TEXT)],
            background=[("active", "#262f3d"), ("readonly", self.BG_CARD_ALT)],
            foreground=[("readonly", self.FG_TEXT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", self.BG_CARD_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", self.FG_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", self.ACCENT_BLUE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))

    def _load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_settings(self):
        data = {
            "tv_ip": self.manual_ip_var.get().strip(),
            "monitor": self.monitor_var.get(),
            "height": self.height_var.get(),
            "fps": self.fps_var.get(),
            "bitrate": self.bitrate_var.get(),
            "encoder": self.encoder_var.get(),
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _detect_monitors(self):
        result = []
        try:
            with mss.MSS() as sct:
                for i in range(1, len(sct.monitors)):
                    m = sct.monitors[i]
                    result.append((i, f"EKRAN {i}", f"{m['width']} × {m['height']}", f"F{i}"))
                if len(sct.monitors) > 2:
                    m0 = sct.monitors[0]
                    result.append((0, "WSZYSTKIE", f"{m0['width']} × {m0['height']}", "F3"))
        except Exception:
            result = [
                (1, "EKRAN 1", "Główny", "F1"),
                (2, "EKRAN 2", "Dodatkowy", "F2"),
            ]
        return result

    def _bind_shortcuts(self):
        self.root.bind("<F1>", lambda _e: self._select_monitor_card(1))
        self.root.bind("<F2>", lambda _e: self._select_monitor_card(2))
        self.root.bind("<F3>", lambda _e: self._select_monitor_card(0))

    def _make_card(self, parent, title=None, subtitle=None, pady=(0, 10)):
        outer = tk.Frame(
            parent,
            bg=self.BG_CARD,
            highlightbackground=self.BORDER_SUBTLE,
            highlightthickness=1,
            padx=16,
            pady=12,
        )
        outer.pack(fill="x", padx=20, pady=pady)
        if title:
            hdr = tk.Frame(outer, bg=self.BG_CARD)
            hdr.pack(fill="x", pady=(0, 8))
            tk.Label(
                hdr,
                text=title,
                font=("Segoe UI", 10, "bold"),
                bg=self.BG_CARD,
                fg=self.FG_TEXT,
            ).pack(side="left")
            if subtitle:
                tk.Label(
                    hdr,
                    text=subtitle,
                    font=("Segoe UI", 8),
                    bg=self.BG_CARD,
                    fg=self.FG_MUTED,
                ).pack(side="right")
        return outer

    def _build_ui(self):
        # ======================================================================
        # 1. NAGŁÓWEK + PASEK TELEMETRII NA ŻYWO
        # ======================================================================
        top_bar = tk.Frame(self.root, bg=self.BG_MAIN, padx=20, pady=12)
        top_bar.pack(fill="x")

        title_col = tk.Frame(top_bar, bg=self.BG_MAIN)
        title_col.pack(side="left")

        tk.Label(
            title_col,
            text="PC ➔ TV Live Streamer",
            font=("Segoe UI", 15, "bold"),
            bg=self.BG_MAIN,
            fg="#ffffff",
        ).pack(anchor="w")

        tk.Label(
            title_col,
            text="Sprzętowy H.264 Low-Latency • DirectX 11 + Dźwięk WASAPI",
            font=("Segoe UI", 8),
            bg=self.BG_MAIN,
            fg=self.FG_MUTED,
        ).pack(anchor="w")

        # Pigułka statusu na żywo po prawej
        self.live_badge = tk.Label(
            top_bar,
            text="● INICJALIZACJA...",
            font=("Segoe UI", 9, "bold"),
            bg="#1c232d",
            fg="#58a6ff",
            padx=14,
            pady=6,
        )
        self.live_badge.pack(side="right")

        # Pasek 3 mini-kart telemetrii (Bitrate, Dźwięk VU, Koder GPU)
        tele_row = tk.Frame(self.root, bg=self.BG_MAIN, padx=20)
        tele_row.pack(fill="x", pady=(0, 10))
        tele_row.columnconfigure(0, weight=1)
        tele_row.columnconfigure(1, weight=1)
        tele_row.columnconfigure(2, weight=1)

        # Kafelek 1: Przepustowość Wideo
        t1 = tk.Frame(tele_row, bg=self.BG_CARD, highlightbackground=self.BORDER_SUBTLE, highlightthickness=1, padx=12, pady=7)
        t1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(t1, text="STRUMIEŃ WIDEO", font=("Segoe UI", 7, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED).pack(anchor="w")
        self.lbl_tele_video = tk.Label(t1, text="0.0 Mbps • 1080p60", font=("Segoe UI", 10, "bold"), bg=self.BG_CARD, fg="#58a6ff")
        self.lbl_tele_video.pack(anchor="w")

        # Kafelek 2: Poziom Dźwięku WASAPI (z paskiem VU Meter)
        t2 = tk.Frame(tele_row, bg=self.BG_CARD, highlightbackground=self.BORDER_SUBTLE, highlightthickness=1, padx=12, pady=7)
        t2.grid(row=0, column=1, sticky="nsew", padx=3)
        hdr_a = tk.Frame(t2, bg=self.BG_CARD)
        hdr_a.pack(fill="x")
        tk.Label(hdr_a, text="DŹWIĘK SYSTEMOWY", font=("Segoe UI", 7, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED).pack(side="left")
        self.lbl_audio_dev = tk.Label(hdr_a, text="WASAPI", font=("Segoe UI", 7), bg=self.BG_CARD, fg="#7ee787")
        self.lbl_audio_dev.pack(side="right")

        self.vu_canvas = tk.Canvas(t2, height=10, bg="#0d1117", highlightthickness=0)
        self.vu_canvas.pack(fill="x", pady=(5, 2))
        self.vu_rect = self.vu_canvas.create_rectangle(0, 0, 0, 10, fill="#3fb950", width=0)

        # Kafelek 3: Koder GPU
        t3 = tk.Frame(tele_row, bg=self.BG_CARD, highlightbackground=self.BORDER_SUBTLE, highlightthickness=1, padx=12, pady=7)
        t3.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        tk.Label(t3, text="AKTYWNY KODER", font=("Segoe UI", 7, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED).pack(anchor="w")
        self.lbl_tele_enc = tk.Label(t3, text="Wykrywanie GPU...", font=("Segoe UI", 10, "bold"), bg=self.BG_CARD, fg="#d2a8ff")
        self.lbl_tele_enc.pack(anchor="w")

        # ======================================================================
        # 2. KARTA TELEWIZORA DLNA
        # ======================================================================
        card_tv = self._make_card(
            self.root,
            title="1. Odbiornik TV w sieci domowej (DLNA / UPnP)",
            subtitle="Wskazówka: wyjdź z menu Smart na pilocie przyciskiem EXIT",
        )

        row_tv = tk.Frame(card_tv, bg=self.BG_CARD)
        row_tv.pack(fill="x")

        self.tv_combo = ttk.Combobox(
            row_tv,
            textvariable=self.tv_choice_var,
            state="readonly",
            style="Dark.TCombobox",
            font=("Segoe UI", 10),
        )
        self.tv_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.tv_combo.bind("<<ComboboxSelected>>", self._on_tv_selected)

        tk.Label(row_tv, text="IP:", font=("Segoe UI", 9, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED).pack(side="left", padx=(4, 4))
        ip_entry = tk.Entry(
            row_tv,
            textvariable=self.manual_ip_var,
            font=("Consolas", 10),
            bg=self.BG_CARD_ALT,
            fg=self.FG_TEXT,
            insertbackground=self.FG_TEXT,
            relief="flat",
            width=14,
            highlightbackground=self.BORDER_SUBTLE,
            highlightthickness=1,
        )
        ip_entry.pack(side="left", ipady=4, padx=(0, 8))

        btn_scan = tk.Label(
            row_tv,
            text="🔄 Szukaj TV",
            font=("Segoe UI", 9, "bold"),
            bg=self.BG_CARD_ALT,
            fg=self.FG_TEXT,
            padx=12,
            pady=6,
            cursor="hand2",
        )
        btn_scan.pack(side="right")
        btn_scan.bind("<Button-1>", lambda _e: self.scan_tvs_async())
        btn_scan.bind("<Enter>", lambda _e: btn_scan.configure(bg="#262f3d"))
        btn_scan.bind("<Leave>", lambda _e: btn_scan.configure(bg=self.BG_CARD_ALT))

        # ======================================================================
        # 3. KARTA WYBORU EKRANU (WIZUALNE KAFELKI MONITORÓW)
        # ======================================================================
        card_mon = self._make_card(
            self.root,
            title="2. Przechwytywany Ekran (Kliknij ekran lub użyj F1 / F2 / F3)",
            subtitle="Przełączanie w locie bez przerywania pracy",
        )

        mon_grid = tk.Frame(card_mon, bg=self.BG_CARD)
        mon_grid.pack(fill="x")

        for idx, (m_id, m_title, m_res, m_key) in enumerate(self.monitors_info):
            mon_grid.columnconfigure(idx, weight=1)
            mc = tk.Frame(
                mon_grid,
                bg=self.BG_CARD_ALT,
                highlightbackground=self.BORDER_SUBTLE,
                highlightthickness=2,
                padx=12,
                pady=8,
                cursor="hand2",
            )
            mc.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 6, 0))

            # Ikona ekranu na małym Canvasie
            cv = tk.Canvas(mc, width=42, height=28, bg=self.BG_CARD_ALT, highlightthickness=0)
            cv.pack(side="left", padx=(0, 10))
            rect_id = cv.create_rectangle(3, 2, 39, 22, outline="#58a6ff", width=2, fill="#0d1117")
            stand_id = cv.create_line(14, 26, 28, 26, fill="#8b949e", width=2)
            num_txt = "1+2" if m_id == 0 else str(m_id)
            txt_id = cv.create_text(21, 12, text=num_txt, fill="#f0f6fc", font=("Segoe UI", 8, "bold"))

            info_f = tk.Frame(mc, bg=self.BG_CARD_ALT)
            info_f.pack(side="left", fill="x", expand=True)

            lbl_t = tk.Label(info_f, text=f"{m_title}  [{m_key}]", font=("Segoe UI", 9, "bold"), bg=self.BG_CARD_ALT, fg=self.FG_TEXT)
            lbl_t.pack(anchor="w")
            lbl_r = tk.Label(info_f, text=m_res, font=("Segoe UI", 8), bg=self.BG_CARD_ALT, fg=self.FG_MUTED)
            lbl_r.pack(anchor="w")

            self.monitor_cards[m_id] = (mc, cv, info_f, lbl_t, lbl_r, rect_id, stand_id, txt_id)

            for widget in (mc, cv, info_f, lbl_t, lbl_r):
                widget.bind("<Button-1>", lambda _e, mid=m_id: self._select_monitor_card(mid))

        self._refresh_monitor_cards()

        # ======================================================================
        # 4. KARTA JAKOŚCI I PRESETÓW
        # ======================================================================
        card_q = self._make_card(
            self.root,
            title="3. Profil Jakości i Kodowania",
            subtitle="Każda zmiana automatycznie odświeża strumień na TV",
        )

        # Szybkie presety (1-Click)
        preset_row = tk.Frame(card_q, bg=self.BG_CARD)
        preset_row.pack(fill="x", pady=(0, 10))
        tk.Label(preset_row, text="Szybki profil:", font=("Segoe UI", 9, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED, width=14, anchor="w").pack(side="left")

        presets = [
            ("⚡ Gra / Balans (1080p60 • 6M)", 1080, 60, "6M"),
            ("🎬 Kino / Max (1080p60 • 10M)", 1080, 60, "10M"),
            ("🚀 Ultra-Low-Lag (720p60 • 4.5M)", 720, 60, "4500k"),
        ]
        for p_label, p_h, p_fps, p_br in presets:
            pb = tk.Label(
                preset_row,
                text=p_label,
                font=("Segoe UI", 8, "bold"),
                bg="#1c232d",
                fg="#a5d6ff",
                padx=10,
                pady=4,
                cursor="hand2",
            )
            pb.pack(side="left", padx=(0, 6))
            pb.bind("<Button-1>", lambda _e, h=p_h, f=p_fps, b=p_br: self.apply_preset(h, f, b))
            pb.bind("<Enter>", lambda _e, w=pb: w.configure(bg="#262f3d", fg="#ffffff"))
            pb.bind("<Leave>", lambda _e, w=pb: w.configure(bg="#1c232d", fg="#a5d6ff"))

        # Rozdzielczość + FPS w jednym wierszu
        row_rf = tk.Frame(card_q, bg=self.BG_CARD)
        row_rf.pack(fill="x", pady=(0, 8))
        tk.Label(row_rf, text="Rozdzielczość:", font=("Segoe UI", 9, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED, width=14, anchor="w").pack(side="left")

        pills_res = SegmentedPills(
            row_rf,
            [(1080, "Full HD 1080p"), (720, "HD 720p")],
            self.height_var,
            command=self.on_profile_change,
            bg=self.BG_CARD,
        )
        pills_res.pack(side="left", padx=(0, 18))

        tk.Label(row_rf, text="Płynność:", font=("Segoe UI", 9, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED).pack(side="left", padx=(0, 8))
        pills_fps = SegmentedPills(
            row_rf,
            [(60, "60 FPS"), (30, "30 FPS")],
            self.fps_var,
            command=self.on_profile_change,
            bg=self.BG_CARD,
        )
        pills_fps.pack(side="left")

        # Bitrate + Koder w drugim wierszu
        row_be = tk.Frame(card_q, bg=self.BG_CARD)
        row_be.pack(fill="x")
        tk.Label(row_be, text="Bitrate / GPU:", font=("Segoe UI", 9, "bold"), bg=self.BG_CARD, fg=self.FG_MUTED, width=14, anchor="w").pack(side="left")

        pills_br = SegmentedPills(
            row_be,
            [("4500k", "4.5 Mbps"), ("6M", "6 Mbps"), ("8M", "8 Mbps"), ("10M", "10 Mbps")],
            self.bitrate_var,
            command=self.on_profile_change,
            bg=self.BG_CARD,
        )
        pills_br.pack(side="left", padx=(0, 14))

        pills_enc = SegmentedPills(
            row_be,
            [("auto", "Auto GPU"), ("h264_nvenc", "NVENC"), ("h264_amf", "AMD"), ("h264_qsv", "Intel"), ("libx264", "CPU")],
            self.encoder_var,
            command=self.on_profile_change,
            bg=self.BG_CARD,
        )
        pills_enc.pack(side="left")

        self.pill_groups = [pills_res, pills_fps, pills_br, pills_enc]

        # ======================================================================
        # 5. GŁÓWNE PRZYCISKI STEROWANIA + STOPKA LOGÓW
        # ======================================================================
        action_bar = tk.Frame(self.root, bg=self.BG_MAIN, padx=20, pady=4)
        action_bar.pack(fill="x")

        self.btn_push = tk.Label(
            action_bar,
            text="▶  WŁĄCZ / PRZEŁĄCZ STREAM NA TELEWIZORZE",
            font=("Segoe UI", 11, "bold"),
            bg=self.ACCENT_GREEN,
            fg="#ffffff",
            pady=11,
            cursor="hand2",
        )
        self.btn_push.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_push.bind("<Button-1>", lambda _e: self.push_to_tv_async())
        self.btn_push.bind("<Enter>", lambda _e: self.btn_push.configure(bg=self.ACCENT_GREEN_HOVER))
        self.btn_push.bind("<Leave>", lambda _e: self.btn_push.configure(bg=self.ACCENT_GREEN))

        self.btn_stop = tk.Label(
            action_bar,
            text="⏹  ZATRZYMAJ",
            font=("Segoe UI", 10, "bold"),
            bg=self.ACCENT_RED,
            fg="#ffffff",
            padx=18,
            pady=11,
            cursor="hand2",
        )
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.btn_stop.bind("<Button-1>", lambda _e: self.stop_on_tv_async())
        self.btn_stop.bind("<Enter>", lambda _e: self.btn_stop.configure(bg=self.ACCENT_RED_HOVER))
        self.btn_stop.bind("<Leave>", lambda _e: self.btn_stop.configure(bg=self.ACCENT_RED))

        self.btn_toggle_logs = tk.Label(
            action_bar,
            text="📋 Logi ▾",
            font=("Segoe UI", 9, "bold"),
            bg=self.BG_CARD_ALT,
            fg=self.FG_MUTED,
            padx=14,
            pady=11,
            cursor="hand2",
        )
        self.btn_toggle_logs.pack(side="right")
        self.btn_toggle_logs.bind("<Button-1>", lambda _e: self.toggle_logs())
        self.btn_toggle_logs.bind("<Enter>", lambda _e: self.btn_toggle_logs.configure(bg="#262f3d", fg=self.FG_TEXT))
        self.btn_toggle_logs.bind("<Leave>", lambda _e: self.btn_toggle_logs.configure(bg=self.BG_CARD_ALT, fg=self.FG_MUTED))

        # Pasek ostatniego komunikatu na dole (zawsze widoczny)
        footer = tk.Frame(self.root, bg=self.BG_MAIN, padx=22, pady=4)
        footer.pack(fill="x")
        self.lbl_last_status = tk.Label(
            footer,
            text="Gotowy do uruchomienia strumienia.",
            font=("Segoe UI", 8),
            bg=self.BG_MAIN,
            fg=self.FG_MUTED,
            anchor="w",
        )
        self.lbl_last_status.pack(side="left", fill="x", expand=True)

        # Szuflada logów (domyślnie zwinięta dla czystości UI)
        self.log_drawer = tk.Frame(
            self.root,
            bg=self.BG_CARD,
            highlightbackground=self.BORDER_SUBTLE,
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        self.log_text = tk.Text(
            self.log_drawer,
            height=7,
            bg="#080b0f",
            fg="#c9d1d9",
            font=("Consolas", 8),
            relief="flat",
            state="disabled",
            wrap="word",
        )
        self.log_text.pack(fill="both", expand=True)

    def toggle_logs(self):
        self.logs_visible = not self.logs_visible
        if self.logs_visible:
            self.root.geometry("780x770")
            self.log_drawer.pack(fill="both", expand=True, padx=20, pady=(2, 14))
            self.btn_toggle_logs.configure(text="📋 Logi ▴")
        else:
            self.log_drawer.pack_forget()
            self.root.geometry("780x635")
            self.btn_toggle_logs.configure(text="📋 Logi ▾")

    def _select_monitor_card(self, mon_id):
        if mon_id not in self.monitor_cards:
            return
        if self.monitor_var.get() == mon_id:
            return
        self.monitor_var.set(mon_id)
        self._refresh_monitor_cards()
        self.on_profile_change()

    def _refresh_monitor_cards(self):
        curr = self.monitor_var.get()
        for m_id, (mc, cv, info_f, lbl_t, lbl_r, rect_id, _stand_id, txt_id) in self.monitor_cards.items():
            active = m_id == curr
            bg_col = "#16233a" if active else self.BG_CARD_ALT
            border_col = "#388bfd" if active else self.BORDER_SUBTLE
            mc.configure(bg=bg_col, highlightbackground=border_col)
            cv.configure(bg=bg_col)
            info_f.configure(bg=bg_col)
            lbl_t.configure(bg=bg_col, fg="#ffffff" if active else self.FG_TEXT)
            lbl_r.configure(bg=bg_col, fg="#79c0ff" if active else self.FG_MUTED)
            cv.itemconfig(rect_id, outline="#58a6ff" if active else "#6e7681", fill="#1f6feb" if active else "#0d1117")
            cv.itemconfig(txt_id, fill="#ffffff" if active else "#8b949e")

    def apply_preset(self, height, fps, bitrate):
        self.height_var.set(height)
        self.fps_var.set(fps)
        self.bitrate_var.set(bitrate)
        for pg in self.pill_groups:
            pg.refresh()
        self.on_profile_change()

    def _schedule_telemetry_update(self):
        """Odświeża co 250 ms pasek VU audio, bitrate Mbps oraz status połączenia TV."""
        try:
            clients_cnt = len(core.BROADCASTER.clients)
            mbps = core.BROADCASTER.current_mbps if clients_cnt > 0 else 0.0
            h = self.height_var.get()
            fps = self.fps_var.get()

            if clients_cnt > 0:
                self.live_badge.configure(
                    text=f"● NA ŻYWO NA TV ({mbps:.1f} Mbps)",
                    bg="#122d1f",
                    fg="#3fb950",
                )
            else:
                self.live_badge.configure(
                    text=f"● GOTOWY (Port :{self.port})",
                    bg="#16233a",
                    fg="#58a6ff",
                )

            self.lbl_tele_video.configure(text=f"{mbps:.1f} Mbps • {h}p @ {fps} FPS")

            # Aktualizacja nazwy urządzenia audio i paska VU Meter
            dev_name = core.BROADCASTER.audio_device_name
            if len(dev_name) > 22:
                dev_name = dev_name[:20] + "…"
            self.lbl_audio_dev.configure(text=dev_name)

            w = max(10, self.vu_canvas.winfo_width())
            lvl = core.BROADCASTER.audio_level
            bar_w = int(w * min(1.0, lvl))
            color = "#3fb950" if lvl < 0.75 else "#d29922"
            self.vu_canvas.coords(self.vu_rect, 0, 0, bar_w, 10)
            self.vu_canvas.itemconfig(self.vu_rect, fill=color)
        except Exception:
            pass
        self.root.after(250, self._schedule_telemetry_update)

    def append_log(self, text):
        clean = text.strip()
        if not clean:
            return
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {clean}\n"

        def _update():
            self.lbl_last_status.configure(text=f"[{ts}] {clean}")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        try:
            self.root.after(0, _update)
        except Exception:
            pass

    def _init_backend(self):
        self.append_log("Sprawdzanie portu 8000 i inicjalizacja kodera GPU...")
        free_port_if_in_use(self.port)

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        enc_id, enc_label, _ = core.detect_best_h264_encoder(ffmpeg_exe, self.encoder_var.get())
        short_enc = enc_label.split("(")[0].strip()
        mode_str = "DX11 ddagrab" if core.BROADCASTER.use_ddagrab else "GDI"
        self.root.after(0, lambda: self.lbl_tele_enc.configure(text=f"{short_enc} • {mode_str}"))
        self.append_log(f"Aktywny koder: {enc_label} ({enc_id})")

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
            self.append_log(f"Serwer DLNA aktywny na: {core.BASE_HTTP_URL}")
        except Exception as exc:
            self.append_log(f"Błąd bindowania portu {self.port}: {exc}")

    def _scan_tvs_worker(self, initial_auto_push=False):
        hint = self.manual_ip_var.get().strip() or "192.168.0.114"
        self.append_log(f"Skanowanie sieci LAN w poszukiwaniu telewizora (priorytet: {hint})...")
        found = core.discover_all_dlna_renderers(hint_ip=hint)
        self.renderers = found

        if found:
            labels = [f"📺 {name} ({ip})" for name, _ctrl, ip in found]
            first_ip = found[0][2]
            local_ip = core.get_preferred_ip(first_ip)
            core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"

            def _upd():
                self.tv_combo["values"] = labels
                self.tv_combo.current(0)
                self.manual_ip_var.set(first_ip)

            self.root.after(0, _upd)
            self.append_log(f"Połączono z telewizorem: {found[0][0]} ({first_ip})")
            if initial_auto_push:
                time.sleep(1.6)
                self._push_worker()
        else:
            local_ip = core.get_preferred_ip(hint)
            core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"

            def _upd_none():
                self.tv_combo["values"] = [f"Brak odpowiedzi UPnP (użyj IP: {hint})"]
                self.tv_combo.current(0)

            self.root.after(0, _upd_none)
            self.append_log("Nie wykryto odpowiedzi UPnP (upewnij się, że TV jest włączony).")

    def scan_tvs_async(self):
        threading.Thread(target=self._scan_tvs_worker, args=(False,), daemon=True).start()

    def _on_tv_selected(self, _event=None):
        idx = self.tv_combo.current()
        if 0 <= idx < len(self.renderers):
            self.manual_ip_var.set(self.renderers[idx][2])
            self._save_settings()

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
        self._save_settings()
        core.BROADCASTER.set_stream_profile(mon, height=h, fps=fps, bitrate=br, encoder=enc)
        self.push_to_tv_async()

    def _push_worker(self):
        self._save_settings()
        ctrl_url, tv_ip = self._get_active_control_url()
        if not ctrl_url:
            self.append_log(f"Nie znaleziono usługi AVTransport na TV ({tv_ip}). Sprawdź czy TV jest włączony.")
            return
        local_ip = core.get_preferred_ip(tv_ip)
        core.BASE_HTTP_URL = f"http://{local_ip}:{self.port}"
        mon = self.monitor_var.get()
        h = self.height_var.get()
        stream_url = f"{core.BASE_HTTP_URL}/live/mon{mon}_{h}.mpg?t={int(time.time())}"
        self.append_log(f"Przełączanie na TV ({tv_ip}) -> Ekran {mon}, {h}p @ {self.fps_var.get()} FPS...")
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
