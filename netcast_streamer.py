#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LG NetCast 3.0 (55LM670S-ZA) Low-Latency Screen Streamer & USB App Builder
==========================================================================
Server streaming the Windows desktop straight to the browser or a USB app
on the LG 55LM670S-ZA TV (NetCast 3.0 / 2012 WebKit).

Features:
- Compatible with the 2012 WebKit engine (plain ES5 JS, no latency-adding buffering)
- Double-buffered frames (Zero-Lag Pull) plus a classic MJPEG stream
- Draws the PC mouse cursor onto the captured image (Win32 API)
- Optional PC cursor control with the LG Magic Remote
- Handles LG remote color buttons (Red/Green/Yellow/Blue, 1-9)
- USB package generator (/lgapps/installed/...) for LG NetCast TVs
"""

import argparse
import ctypes
from ctypes import wintypes
import io
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import mss
from PIL import Image, ImageDraw


# ==============================================================================
# Win32 API: mouse cursor (position readout + optional control from the TV remote)
# ==============================================================================
user32 = ctypes.windll.user32

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", POINT),
    ]

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


def get_cursor_pos():
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return None


def move_and_click_pc_mouse(norm_x, norm_y, monitor_rect, action="click"):
    """Moves the PC cursor to the spot pointed at with the Magic Remote on the TV."""
    try:
        target_x = int(monitor_rect["left"] + norm_x * monitor_rect["width"])
        target_y = int(monitor_rect["top"] + norm_y * monitor_rect["height"])
        user32.SetCursorPos(target_x, target_y)
        if action == "click":
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        elif action == "rclick":
            user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
    except Exception:
        pass


# ==============================================================================
# Global state and screen capture thread (Zero-Lag Producer)
# ==============================================================================
class StreamState:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame_event = threading.Condition()
        self.latest_jpeg = b""
        self.frame_id = 0
        self.fps = 0.0

        # Settings controlled from the TV (on the fly)
        self.monitor_index = 1       # 1 = primary monitor in mss
        self.monitor_count = 1
        self.monitor_rect = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        self.scale_height = 720      # 720p by default (smoothest on 2.4 GHz Wi-Fi and the TV's CPU)
        self.jpeg_quality = 70       # 10..95
        self.draw_cursor = True      # Draw the PC mouse cursor on the TV screen
        self.remote_control = False  # Whether clicks on the TV click on the PC
        self.target_fps = 30
        self.running = True


STATE = StreamState()


def draw_mouse_pointer(img, mon_rect, scale_x, scale_y):
    """Draws a clear, high-contrast mouse cursor on the image sent to the TV."""
    pos = get_cursor_pos()
    if not pos:
        return
    cx, cy = pos
    rel_x = cx - mon_rect["left"]
    rel_y = cy - mon_rect["top"]
    if not (0 <= rel_x <= mon_rect["width"] and 0 <= rel_y <= mon_rect["height"]):
        return

    px = int(rel_x * scale_x)
    py = int(rel_y * scale_y)

    draw = ImageDraw.Draw(img)
    # Cursor arrow with a black outline and white/yellow fill (easy to see on the TV)
    poly = [
        (px, py),
        (px, py + 18),
        (px + 5, py + 14),
        (px + 9, py + 22),
        (px + 12, py + 20),
        (px + 8, py + 12),
        (px + 14, py + 12),
    ]
    draw.polygon(poly, fill=(255, 255, 0), outline=(0, 0, 0))


def make_fallback_image(width, height, message):
    """Creates an info card when GDI BitBlt is temporarily unavailable (e.g. UAC / lock screen)."""
    img = Image.new("RGB", (width, height), color=(18, 22, 34))
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 30, width - 30, height - 30], outline=(88, 166, 255), width=3)
    draw.text((60, 70), "LG 55LM670S-ZA - Strumien PC aktywny", fill=(88, 166, 255))
    draw.text((60, 110), message, fill=(240, 246, 252))
    draw.text((60, 150), f"Czas serwera: {time.strftime('%H:%M:%S')}", fill=(165, 214, 255))
    return img


def capture_loop():
    """Background thread that captures the screen and compresses frames into RAM."""
    with mss.MSS() as sct:
        STATE.monitor_count = max(1, len(sct.monitors) - 1)
        frame_counter = 0
        t0 = time.perf_counter()

        while STATE.running:
            loop_start = time.perf_counter()
            try:
                mon_idx = min(max(0, STATE.monitor_index), len(sct.monitors) - 1)
                mon = sct.monitors[mon_idx]
                STATE.monitor_rect = dict(mon)

                target_h = STATE.scale_height
                try:
                    sct_img = sct.grab(mon)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    orig_w, orig_h = img.size
                    scale_x = 1.0
                    scale_y = 1.0

                    if target_h and target_h < orig_h:
                        target_w = int(orig_w * (target_h / float(orig_h)))
                        img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
                        scale_x = target_w / float(orig_w)
                        scale_y = target_h / float(orig_h)

                    if STATE.draw_cursor:
                        draw_mouse_pointer(img, mon, scale_x, scale_y)
                except Exception as grab_err:
                    # E.g. a UAC prompt or running from a sandbox without desktop access
                    img = make_fallback_image(
                        1280,
                        720,
                        f"Brak dostepu GDI BitBlt w tej sesji ({grab_err}). Uruchom uruchom_stream.bat ze swojego pulpitu.",
                    )

                buf = io.BytesIO()
                img.save(
                    buf,
                    format="JPEG",
                    quality=STATE.jpeg_quality,
                    optimize=False,
                    subsampling=2,  # 4:2:0 - fastest hardware decoding on LG TVs
                )
                jpeg_bytes = buf.getvalue()

                with STATE.frame_event:
                    STATE.latest_jpeg = jpeg_bytes
                    STATE.frame_id += 1
                    STATE.frame_event.notify_all()

                frame_counter += 1
                now = time.perf_counter()
                if now - t0 >= 1.0:
                    STATE.fps = round(frame_counter / (now - t0), 1)
                    frame_counter = 0
                    t0 = now

            except Exception as exc:
                print(f"[!] Błąd pętli przechwytywania: {exc}")
                time.sleep(0.2)

            elapsed = time.perf_counter() - loop_start
            sleep_time = max(0.002, (1.0 / max(5, STATE.target_fps)) - elapsed)
            time.sleep(sleep_time)


# ==============================================================================
# HTML5 / ES5 app optimized for LG NetCast 3.0 (55LM670S-ZA)
# ==============================================================================
NETCAST_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>LG NetCast PC Streamer</title>
<style type="text/css">
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body {
    width: 100%; height: 100%;
    background-color: #000000;
    color: #ffffff;
    font-family: Arial, Helvetica, sans-serif;
    overflow: hidden;
    user-select: none;
    -webkit-user-select: none;
  }
  #stage {
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    width: 100%; height: 100%;
    text-align: center;
  }
  .screen-buf {
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    object-fit: contain;
  }
  #hud {
    position: absolute;
    bottom: 24px;
    left: 4%;
    width: 92%;
    background: rgba(15, 18, 26, 0.88);
    border: 2px solid #3a445e;
    border-radius: 10px;
    padding: 14px 20px;
    z-index: 100;
    transition: opacity 0.3s;
  }
  #hud.hidden {
    opacity: 0;
    pointer-events: none;
  }
  .hud-row {
    display: block;
    clear: both;
    margin-bottom: 8px;
  }
  .hud-title {
    font-size: 20px;
    font-weight: bold;
    color: #58a6ff;
    float: left;
  }
  .hud-stats {
    font-size: 18px;
    color: #a5d6ff;
    float: right;
  }
  .clearfix { clear: both; }
  .btn-bar {
    margin-top: 10px;
  }
  .btn {
    display: inline-block;
    background: #21262d;
    color: #f0f6fc;
    border: 2px solid #484f58;
    border-radius: 6px;
    padding: 8px 14px;
    margin-right: 8px;
    margin-bottom: 6px;
    font-size: 16px;
    font-weight: bold;
    cursor: pointer;
  }
  .btn:hover, .btn.active {
    background: #1f6feb;
    border-color: #79c0ff;
  }
  .dot-red { color: #ff5555; }
  .dot-green { color: #50fa7b; }
  .dot-yellow { color: #f1fa8c; }
  .dot-blue { color: #8be9fd; }
</style>
</head>
<body>

<div id="stage">
  <!-- Two image buffers for lag-free frame swapping on 2012 WebKit -->
  <img id="buf0" class="screen-buf" style="z-index: 2;" src="" alt="" />
  <img id="buf1" class="screen-buf" style="z-index: 1;" src="" alt="" />
</div>

<div id="hud">
  <div class="hud-row">
    <div class="hud-title">LG 55LM670S-ZA &mdash; Strumień Pulpitu PC</div>
    <div class="hud-stats" id="statsText">Łączenie...</div>
    <div class="clearfix"></div>
  </div>
  <div class="btn-bar">
    <span class="btn" onclick="setRes(540)">[1] 540p (Ultra Szybki)</span>
    <span class="btn" onclick="setRes(720)">[2] 720p (HD)</span>
    <span class="btn" onclick="setRes(1080)">[3] 1080p (Full HD)</span>
    <span class="btn" onclick="cycleQuality()"><span class="dot-red">&#9679;</span> [4/Czerw] Jakość JPEG</span>
    <span class="btn" onclick="switchMonitor()"><span class="dot-green">&#9679;</span> [5/Ziel] Monitor</span>
    <span class="btn" id="btnMouse" onclick="toggleRemoteMouse()"><span class="dot-yellow">&#9679;</span> [6/Żółty] Mysz z TV: WYŁ</span>
    <span class="btn" id="btnMode" onclick="toggleStreamMode()"><span class="dot-blue">&#9679;</span> [7/Nieb] Tryb: Zero-Lag</span>
    <span class="btn" onclick="hideHud()">[0/OK] Ukryj panel</span>
  </div>
</div>

<script type="text/javascript">
  // Plain ES5 (compatible with the 2012 WebKit browser in LG NetCast 3.0)
  var buf0 = document.getElementById('buf0');
  var buf1 = document.getElementById('buf1');
  var hud = document.getElementById('hud');
  var statsText = document.getElementById('statsText');
  var btnMouse = document.getElementById('btnMouse');
  var btnMode = document.getElementById('btnMode');

  var activeBuf = 0;
  var loading = false;
  var streamMode = 'pull'; // 'pull' (Zero-Lag) or 'mjpeg'
  var clientFrames = 0;
  var clientFps = 0;
  var lastFpsTime = (new Date()).getTime();
  var hudTimer = null;
  var remoteMouseEnabled = false;
  var currentQuality = 70;
  var currentRes = 720;

  function showHudTemporarily() {
    hud.className = '';
    if (hudTimer) {
      clearTimeout(hudTimer);
    }
    hudTimer = setTimeout(function() {
      hud.className = 'hidden';
    }, 5000);
  }

  function hideHud() {
    if (hud.className === 'hidden') {
      showHudTemporarily();
    } else {
      hud.className = 'hidden';
    }
  }

  function requestNextFrame() {
    if (streamMode !== 'pull' || loading) {
      return;
    }
    loading = true;
    var nextImg = (activeBuf === 0) ? buf1 : buf0;
    var now = (new Date()).getTime();
    nextImg.src = '/frame.jpg?t=' + now + '_' + clientFrames;
  }

  function onFrameLoaded(which) {
    if (streamMode !== 'pull') {
      return;
    }
    if (which === 0) {
      buf0.style.zIndex = 2;
      buf1.style.zIndex = 1;
      activeBuf = 0;
    } else {
      buf1.style.zIndex = 2;
      buf0.style.zIndex = 1;
      activeBuf = 1;
    }
    loading = false;
    clientFrames++;

    var now = (new Date()).getTime();
    if (now - lastFpsTime >= 1000) {
      clientFps = Math.round((clientFrames * 1000) / (now - lastFpsTime));
      clientFrames = 0;
      lastFpsTime = now;
      statsText.innerHTML = 'TV FPS: ' + clientFps + ' kl./s | Rozdz.: ' + currentRes + 'p | Jakość: ' + currentQuality + '%';
    }

    // Immediately fetch the next freshest frame from the server
    setTimeout(requestNextFrame, 4);
  }

  function onFrameError() {
    if (streamMode !== 'pull') {
      return;
    }
    loading = false;
    setTimeout(requestNextFrame, 300);
  }

  buf0.onload = function() { onFrameLoaded(0); };
  buf1.onload = function() { onFrameLoaded(1); };
  buf0.onerror = onFrameError;
  buf1.onerror = onFrameError;

  function sendConfig(params) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/config?' + params + '&_t=' + (new Date()).getTime(), true);
    xhr.onreadystatechange = function() {
      if (xhr.readyState === 4 && xhr.status === 200) {
        try {
          var data = JSON.parse(xhr.responseText);
          currentRes = data.scale_height;
          currentQuality = data.jpeg_quality;
          remoteMouseEnabled = data.remote_control;
          btnMouse.innerHTML = '<span class="dot-yellow">&#9679;</span> [6/Żółty] Mysz z TV: ' + (remoteMouseEnabled ? 'WŁ' : 'WYŁ');
          statsText.innerHTML = 'TV FPS: ' + clientFps + ' | PC FPS: ' + data.fps + ' | ' + currentRes + 'p | Jakość: ' + currentQuality + '% | Mon: ' + data.monitor_index;
        } catch (e) {}
      }
    };
    xhr.send(null);
    showHudTemporarily();
  }

  function setRes(h) {
    currentRes = h;
    sendConfig('scale_height=' + h);
  }

  function cycleQuality() {
    if (currentQuality === 70) { currentQuality = 85; }
    else if (currentQuality === 85) { currentQuality = 50; }
    else { currentQuality = 70; }
    sendConfig('jpeg_quality=' + currentQuality);
  }

  function switchMonitor() {
    sendConfig('next_monitor=1');
  }

  function toggleRemoteMouse() {
    remoteMouseEnabled = !remoteMouseEnabled;
    sendConfig('remote_control=' + (remoteMouseEnabled ? '1' : '0'));
  }

  function toggleStreamMode() {
    if (streamMode === 'pull') {
      streamMode = 'mjpeg';
      btnMode.innerHTML = '<span class="dot-blue">&#9679;</span> [7/Nieb] Tryb: MJPEG';
      buf1.style.display = 'none';
      buf0.style.zIndex = 2;
      buf0.src = '/stream.mjpg?t=' + (new Date()).getTime();
    } else {
      streamMode = 'pull';
      btnMode.innerHTML = '<span class="dot-blue">&#9679;</span> [7/Nieb] Tryb: Zero-Lag';
      buf1.style.display = 'block';
      loading = false;
      requestNextFrame();
    }
    showHudTemporarily();
  }

  // Handle LG Magic Remote pointer clicks on the image
  document.getElementById('stage').onclick = function(ev) {
    var e = ev || window.event;
    var w = window.innerWidth || document.documentElement.clientWidth || 1920;
    var h = window.innerHeight || document.documentElement.clientHeight || 1080;
    var nx = e.clientX / w;
    var ny = e.clientY / h;

    if (remoteMouseEnabled) {
      var xhr = new XMLHttpRequest();
      xhr.open('GET', '/api/mouse?x=' + nx.toFixed(4) + '&y=' + ny.toFixed(4) + '&a=click&_t=' + (new Date()).getTime(), true);
      xhr.send(null);
    } else {
      showHudTemporarily();
    }
  };

  // Handle LG NetCast remote keys (digits 0-9, OK, color buttons)
  document.onkeydown = function(ev) {
    var e = ev || window.event;
    var k = e.keyCode || e.which;

    // 49='1', 50='2', 51='3', 52='4', 53='5', 54='6', 55='7', 48='0', 13=OK
    // LG NetCast color keys: 403=Red, 404=Green, 405=Yellow, 406=Blue
    if (k === 49) { setRes(540); }
    else if (k === 50) { setRes(720); }
    else if (k === 51) { setRes(1080); }
    else if (k === 52 || k === 403) { cycleQuality(); }
    else if (k === 53 || k === 404) { switchMonitor(); }
    else if (k === 54 || k === 405) { toggleRemoteMouse(); }
    else if (k === 55 || k === 406) { toggleStreamMode(); }
    else if (k === 48 || k === 13) { hideHud(); }
    else { showHudTemporarily(); }
  };

  // Start the stream
  showHudTemporarily();
  requestNextFrame();
</script>
</body>
</html>
"""


# ==============================================================================
# HTTP server serving the browser and the NetCast app
# ==============================================================================
class NetCastRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Silence console logging of every single frame
        if "/frame.jpg" in self.path or "/api/mouse" in self.path:
            return
        print(f"[HTTP] {self.address_string()} - {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            body = NETCAST_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/frame.jpg":
            jpeg = STATE.latest_jpeg
            if not jpeg:
                self.send_error(503, "Brak klatki")
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(jpeg)
            except (ConnectionResetError, BrokenPipeError, socket.error):
                pass
            return

        if path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Connection", "close")
            self.end_headers()
            last_id = -1
            try:
                while STATE.running:
                    with STATE.frame_event:
                        STATE.frame_event.wait(timeout=0.5)
                        if STATE.frame_id == last_id:
                            continue
                        jpeg = STATE.latest_jpeg
                        last_id = STATE.frame_id

                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpeg)).encode("ascii") + b"\r\n\r\n"
                    )
                    self.wfile.write(header)
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, socket.error):
                pass
            return

        if path == "/api/config":
            with STATE.lock:
                if "scale_height" in qs:
                    val = int(qs["scale_height"][0])
                    if val in (540, 720, 1080):
                        STATE.scale_height = val
                if "jpeg_quality" in qs:
                    val = int(qs["jpeg_quality"][0])
                    STATE.jpeg_quality = max(20, min(95, val))
                if "next_monitor" in qs:
                    # Cycle: 1 -> 2 -> ... -> 0 (all) -> 1
                    STATE.monitor_index = (STATE.monitor_index + 1) % (STATE.monitor_count + 1)
                if "remote_control" in qs:
                    STATE.remote_control = qs["remote_control"][0] == "1"

                resp = {
                    "scale_height": STATE.scale_height,
                    "jpeg_quality": STATE.jpeg_quality,
                    "monitor_index": STATE.monitor_index,
                    "monitor_count": STATE.monitor_count,
                    "remote_control": STATE.remote_control,
                    "fps": STATE.fps,
                }

            payload = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/mouse":
            if STATE.remote_control and "x" in qs and "y" in qs:
                try:
                    nx = float(qs["x"][0])
                    ny = float(qs["y"][0])
                    action = qs.get("a", ["click"])[0]
                    move_and_click_pc_mouse(nx, ny, STATE.monitor_rect, action=action)
                except ValueError:
                    pass
            body = b'{"ok":1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404, "Not Found")


# ==============================================================================
# USB app structure generator for LG NetCast (/lgapps/installed/100001/)
# ==============================================================================
def build_netcast_usb_package(output_dir, server_url):
    """
    Creates a ready-made directory structure for a USB stick (FAT32):
      usb_netcast_pack/lgapps/installed/100001/
    which can be copied straight into the root of the stick.
    """
    app_dir = os.path.join(output_dir, "lgapps", "installed", "100001")
    os.makedirs(app_dir, exist_ok=True)

    # 1. index.html redirecting to or embedding our PC server
    usb_index = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>PC Screen Stream</title>
<meta http-equiv="refresh" content="0; url={server_url}" />
<style type="text/css">
  body {{ background: #000; color: #fff; font-family: Arial, sans-serif; text-align: center; padding-top: 20%; }}
  a {{ color: #58a6ff; font-size: 28px; }}
</style>
</head>
<body>
  <h1>Łączenie z komputerem PC ({server_url})...</h1>
  <p><a href="{server_url}">Kliknij tutaj, jeśli ekran nie przełączy się automatycznie</a></p>
  <script type="text/javascript">
    window.location.replace("{server_url}");
  </script>
</body>
</html>
"""
    with open(os.path.join(app_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(usb_index)

    # 2. info.xml / lgconfig.xml for the NetCast 3.0 environment
    lgconfig_xml = """<?xml version="1.0" encoding="UTF-8"?>
<lg_app_Wait>
    <app_info>
        <id>100001</id>
        <name>PC Screen Streamer</name>
        <version>1.0.0</version>
        <type>WEB</type>
        <resolution>1920x1080</resolution>
        <url>index.html</url>
        <icon>icon.png</icon>
    </app_info>
</lg_app_Wait>
"""
    with open(os.path.join(app_dir, "lgconfig.xml"), "w", encoding="utf-8") as f:
        f.write(lgconfig_xml)
    with open(os.path.join(app_dir, "info.xml"), "w", encoding="utf-8") as f:
        f.write(lgconfig_xml)

    # 3. 130x130 PNG app icon required by the NetCast launcher
    icon = Image.new("RGB", (130, 130), color=(25, 95, 200))
    draw = ImageDraw.Draw(icon)
    draw.rectangle([12, 22, 118, 92], outline=(255, 255, 255), width=4)
    draw.rectangle([45, 96, 85, 108], fill=(255, 255, 255))
    draw.text((34, 48), "PC -> TV", fill=(255, 255, 0))
    icon.save(os.path.join(app_dir, "icon.png"), format="PNG")

    return app_dir


# ==============================================================================
# Detection of the PC and LG TV IP addresses on the local network
# ==============================================================================
def get_local_ipv4_addresses():
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    return ips


def discover_lg_netcast_tv(timeout=1.5):
    """Sends an SSDP M-SEARCH query looking for an LG NetCast / DLNA TV on the LAN."""
    ssdp_Msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        "MX: 1\r\n"
        "ST: ssdp:all\r\n\r\n"
    ).encode("ascii")

    found_tvs = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    try:
        sock.sendto(ssdp_Msg, ("239.255.255.250", 1900))
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                data, addr = sock.recvfrom(4096)
                text = data.decode("utf-8", errors="ignore")
                if any(k in text.upper() for k in ("LG", "NETCAST", "DLNA", "LGE", "UPNP")):
                    ip = addr[0]
                    if ip not in found_tvs:
                        server_line = ""
                        for line in text.splitlines():
                            if line.upper().startswith(("SERVER:", "LOCATION:", "USN:")):
                                server_line += line.strip() + " | "
                        found_tvs[ip] = server_line
            except socket.timeout:
                break
    except Exception:
        pass
    finally:
        sock.close()
    return found_tvs


def main():
    parser = argparse.ArgumentParser(description="LG 55LM670S-ZA NetCast Screen Streamer")
    parser.add_argument("--port", type=int, default=8000, help="Port serwera HTTP (domyślnie: 8000)")
    parser.add_argument("--fps", type=int, default=30, help="Docelowy FPS przechwytywania (domyślnie: 30)")
    parser.add_argument("--height", type=int, default=720, choices=[540, 720, 1080], help="Rozdzielczość pionowa")
    parser.add_argument("--quality", type=int, default=70, help="Jakość JPEG 20-95 (domyślnie: 70)")
    args = parser.parse_args()

    STATE.target_fps = args.fps
    STATE.scale_height = args.height
    STATE.jpeg_quality = args.quality

    ips = get_local_ipv4_addresses()
    primary_ip = next((ip for ip in ips if ip.startswith("192.168.")), ips[0] if ips else "127.0.0.1")
    server_url = f"http://{primary_ip}:{args.port}"

    # Generate the USB package for NetCast
    usb_out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usb_netcast_pack")
    app_dir = build_netcast_usb_package(usb_out, server_url)

    print("=" * 72)
    print("  LG NetCast 3.0 (55LM670S-ZA) - Serwer Strumieniowania Obrazu PC")
    print("=" * 72)
    print(f"  Główny adres dla TV :  {server_url}")
    for ip in ips:
        if ip != primary_ip:
            print(f"  Alternatywny adres  :  http://{ip}:{args.port}")
    print(f"  Paczka USB NetCast  :  {app_dir}")
    print("-" * 72)

    print("  [Szukam urządzeń UPnP / LG TV w sieci domowej...]")
    tvs = discover_lg_netcast_tv(timeout=1.2)
    if tvs:
        for ip, info in tvs.items():
            print(f"   -> Wykryto w LAN: {ip} ({info[:65]}...)")
    else:
        print("   -> Brak odpowiedzi SSDP (TV jest wyłączony lub w innej podsieci).")
    print("=" * 72)
    print("  JAK URUCHOMIĆ NA TELEWIZORZE LG 55LM670S-ZA:")
    print("  Sposób A (Najprostszy - bez pendrive'a):")
    print("    1. Na pilocie TV naciśnij przycisk [HOME / SMART].")
    print("    2. Otwórz wbudowaną aplikację [Internet / Web Browser].")
    print(f"    3. Wpisz na górze adres:  {server_url}  i dodaj do Zakładek.")
    print("  Sposób B (Z pendrive'a USB):")
    print("    1. Skopiuj folder 'lgapps' z katalogu 'usb_netcast_pack' na pendrive FAT32.")
    print("    2. Włóż pendrive do TV -> Moje Aplikacje (My Apps) -> ikona USB.")
    print("=" * 72)

    # Start the screen capture thread
    cap_thread = threading.Thread(target=capture_loop, daemon=True)
    cap_thread.start()

    # Start the multithreaded HTTP server
    server = ThreadingHTTPServer(("0.0.0.0", args.port), NetCastRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] Zatrzymywanie serwera...")
        STATE.running = False
        server.server_close()


if __name__ == "__main__":
    main()
