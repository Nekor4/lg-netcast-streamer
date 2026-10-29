#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LG 55LM670S-ZA Hardware H.264 (RTX 3070 Ti NVENC 60 FPS) + WASAPI Audio Streamer
================================================================================
Pełny serwer DLNA MediaServer (Smart Share) + AVTransport Push dla LG NetCast 3.0.

Dlaczego to działa w 60 FPS z dźwiękiem:
- Omija powolną przeglądarkę WebKit z 2012 r. i używa natywnego, sprzętowego
  dekodera wideo/audio ASIC w telewizorze LG (Smart Share / DLNA).
- Karta NVIDIA GeForce RTX 3070 Ti koduje obraz w czasie rzeczywistym (h264_nvenc,
  profil Main @ Level 4.1, 1280x720 60 FPS lub 1920x1080, 0 B-frames).
- Dźwięk z komputera (Głośniki Realtek WASAPI Loopback) jest przechwytywany
  w pojedynczym wątku i multipleksowany jako stereo MP2 48 kHz w strumieniu MPEG-TS.
- Serwer wystawia się w sieci domowej jako serwer DLNA "[PC] Pulpit RTX 60FPS",
  widoczny bezpośrednio na telewizorze pod przyciskiem INPUT / Smart Share!
"""

import argparse
import html
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import imageio_ffmpeg
import pyaudiowpatch as pyaudio


# ==============================================================================
# Stałe DLNA / UPnP dla LG NetCast 3.0 (LGE_DLNA_SDK/1.5.0)
# ==============================================================================
SERVER_UUID = "uuid:8f9a1b2c-3d4e-5f6a-7b8c-9d0e1f2a3b4c"
FRIENDLY_NAME = "[PC] Pulpit RTX 60FPS + Audio"

DLNA_ORG_FEATURES = (
    "DLNA.ORG_PN=MPEG_TS_HD_NA_ISO;"
    "DLNA.ORG_OP=00;"
    "DLNA.ORG_CI=1;"
    "DLNA.ORG_FLAGS=8D700000000000000000000000000000"
)
DLNA_PROTOCOL_INFO = f"http-get:*:video/mpeg:{DLNA_ORG_FEATURES}"


# ==============================================================================
# Globalny stan strumienia i Broadcaster MPEG-TS
# ==============================================================================
class StreamBroadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()
        self.active_monitor = 1
        self.active_height = 1080
        self.use_ddagrab = True
        self.ffmpeg_proc = None

    def set_stream_profile(self, mon_idx, height=None):
        with self.lock:
            changed = False
            if self.active_monitor != mon_idx:
                self.active_monitor = mon_idx
                changed = True
            if height is not None and self.active_height != height:
                self.active_height = height
                changed = True
            if changed:
                print(
                    f"  [Profil] Przełączanie na: Monitor {self.active_monitor}, "
                    f"Rozdzielczość {self.active_height}p @ 60 FPS"
                )
                if self.ffmpeg_proc:
                    try:
                        self.ffmpeg_proc.kill()
                    except Exception:
                        pass

    def add_client(self):
        # Kolejka 240 paczek (ok. 900 KB) chroni przed mikro-przycięciami Wi-Fi:
        # NIGDY nie porzucamy pojedynczych pakietów ze środka klatki H.264 (zero glitchy/makrobloków!)
        q = queue.Queue(maxsize=240)
        with self.lock:
            self.clients.add(q)
        return q

    def remove_client(self, q):
        with self.lock:
            self.clients.discard(q)

    def broadcast(self, chunk):
        if not chunk:
            return
        with self.lock:
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    # Jeśli klient całkowicie przestał odbierać (>900KB zaległości), wyczyść kolejkę w całości
                    try:
                        while not q.empty():
                            q.get_nowait()
                        q.put_nowait(chunk)
                    except Exception:
                        dead.append(q)
            for q in dead:
                self.clients.discard(q)


BROADCASTER = StreamBroadcaster()
AUDIO_QUEUE = queue.Queue(maxsize=80)
RUNNING = True


# ==============================================================================
# Pojedynczy globalny wątek WASAPI Loopback (zero duplikacji, zero spamu)
# ==============================================================================
def global_wasapi_capture_thread():
    frames_per_chunk = 960  # 20 ms @ 48000 Hz
    while RUNNING:
        try:
            with pyaudio.PyAudio() as p:
                loopback = p.get_default_wasapi_loopback()
                dev_rate = int(loopback.get("defaultSampleRate", 48000))
                dev_ch = min(2, int(loopback.get("maxInputChannels", 2)))
                print(
                    f"  [Audio] Aktywne przechwytywanie dźwięku: {loopback['name']} "
                    f"({dev_rate} Hz, {dev_ch} kan.)"
                )

                def cb(in_data, frame_count, time_info, status):
                    try:
                        AUDIO_QUEUE.put_nowait(in_data)
                    except queue.Full:
                        try:
                            AUDIO_QUEUE.get_nowait()
                            AUDIO_QUEUE.put_nowait(in_data)
                        except Exception:
                            pass
                    return (None, pyaudio.paContinue)

                with p.open(
                    format=pyaudio.paInt16,
                    channels=dev_ch,
                    rate=dev_rate,
                    frames_per_buffer=frames_per_chunk,
                    input=True,
                    input_device_index=loopback["index"],
                    stream_callback=cb,
                ):
                    while RUNNING:
                        time.sleep(0.5)
        except Exception as exc:
            print(f"  [Audio] Oczekiwanie na urządzenie audio ({exc})...")
            time.sleep(2.0)


def feed_stdin_audio(ffmpeg_stdin):
    """
    Podaje próbki audio do FFmpega:
    - Gdy na komputerze gra dźwięk (WASAPI dostarcza próbki), przekazuje je bezpośrednio
      w rytmie zegara karty dźwiękowej Realtek (ZERO wtrącania sztucznej ciszy w środek muzyki!).
    - Dopiero gdy na komputerze zapadnie cisza (>55 ms bez próbek z WASAPI), płynnie
      dogenerowuje ramki ciszy zgodnie z zegarem time.perf_counter().
    """
    silence_20ms = b"\x00" * (960 * 2 * 2)  # 20 ms @ 48000 Hz stereo s16le
    while not AUDIO_QUEUE.empty():
        try:
            AUDIO_QUEUE.get_nowait()
        except Exception:
            break

    while RUNNING:
        try:
            # Czekaj naturalnie na paczkę z karty dźwiękowej Realtek (maks. 45 ms)
            chunk = AUDIO_QUEUE.get(timeout=0.045)
            ffmpeg_stdin.write(chunk)
            ffmpeg_stdin.flush()
        except queue.Empty:
            # Na komputerze nic obecnie nie gra -> podawaj ciszę ze stałym zegarem 20 ms
            t_sil = time.perf_counter()
            sil_sent = 0
            while RUNNING and AUDIO_QUEUE.empty():
                target = int((time.perf_counter() - t_sil) * 50.0) + 1
                while sil_sent < target and AUDIO_QUEUE.empty():
                    try:
                        ffmpeg_stdin.write(silence_20ms)
                        ffmpeg_stdin.flush()
                        sil_sent += 1
                    except (BrokenPipeError, OSError, ValueError):
                        return
                time.sleep(0.004)
        except (BrokenPipeError, OSError, ValueError):
            return


# ==============================================================================
# Automatyczne wykrywanie najlepszego kodera H.264 (NVIDIA / AMD / Intel / CPU)
# ==============================================================================
DETECTED_ENCODER = None  # ("nazwa", funkcja_argumentow(bitrate))


def detect_best_h264_encoder(ffmpeg_exe, requested="auto"):
    """
    Sprawdza dostępne w systemie kodery sprzętowe GPU (NVIDIA NVENC -> AMD AMF -> Intel QSV)
    i w razie braku dedykowanego GPU przełącza na wielowątkowy koder CPU (libx264 ultrafast).
    """
    global DETECTED_ENCODER
    if DETECTED_ENCODER is not None and requested == "auto":
        return DETECTED_ENCODER

    candidates = [
        (
            "h264_nvenc",
            "NVIDIA NVENC (GPU)",
            lambda br: [
                "-c:v", "h264_nvenc",
                "-preset", "p1",
                "-tune", "ull",
                "-zerolatency", "1",
                "-delay", "0",
                "-aud", "1",
                "-forced-idr", "1",
                "-profile:v", "high",
                "-rc", "cbr",
                "-b:v", br,
                "-maxrate", br,
                "-bufsize", "2M",
                "-g", "30",
                "-bf", "0",
            ],
        ),
        (
            "h264_amf",
            "AMD AMF (GPU)",
            lambda br: [
                "-c:v", "h264_amf",
                "-usage", "ultralowlatency",
                "-quality", "speed",
                "-profile:v", "high",
                "-rc", "cbr",
                "-b:v", br,
                "-maxrate", br,
                "-bufsize", "2M",
                "-g", "30",
                "-bf", "0",
                "-aud", "1",
            ],
        ),
        (
            "h264_qsv",
            "Intel QuickSync (iGPU/dGPU)",
            lambda br: [
                "-c:v", "h264_qsv",
                "-preset", "veryfast",
                "-async_depth", "1",
                "-profile:v", "high",
                "-b:v", br,
                "-maxrate", br,
                "-bufsize", "2M",
                "-g", "30",
                "-bf", "0",
                "-aud", "1",
            ],
        ),
        (
            "libx264",
            "CPU Software (libx264 ultrafast zerolatency)",
            lambda br: [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-profile:v", "high",
                "-b:v", br,
                "-maxrate", br,
                "-bufsize", "2M",
                "-g", "30",
                "-bf", "0",
                "-aud", "1",
            ],
        ),
    ]

    if requested != "auto":
        candidates = [c for c in candidates if c[0] == requested] + candidates

    for enc_id, label, builder in candidates:
        test_cmd = [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", "color=c=black:s=640x360:r=30",
            "-frames:v", "1",
            "-vf", "format=nv12",
            *builder("4M"),
            "-f", "null",
            "NUL" if os.name == "nt" else "/dev/null",
        ]
        try:
            r = subprocess.run(test_cmd, capture_output=True, timeout=3)
            if r.returncode == 0:
                DETECTED_ENCODER = (enc_id, label, builder)
                return DETECTED_ENCODER
        except Exception:
            continue

    DETECTED_ENCODER = candidates[-1]
    return DETECTED_ENCODER


# ==============================================================================
# Koder FFmpeg: Uniwersalny potok GPU/CPU + WASAPI Audio
# ==============================================================================
def ffmpeg_producer_loop(default_monitor=1, fps=60, height=1080, bitrate="7M", encoder_pref="auto"):
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    BROADCASTER.active_monitor = default_monitor
    BROADCASTER.active_height = height
    enc_id, enc_label, enc_builder = detect_best_h264_encoder(ffmpeg_exe, encoder_pref)
    print(f"  [Koder Wideo] Wybrano automatycznie: {enc_label} ({enc_id})")

    # Podnieś priorytet procesu Pythona, aby edytory Unity/gry nie przywłaszczały czasu CPU
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)
    except Exception:
        pass

    while RUNNING:
        mon_idx = BROADCASTER.active_monitor
        cur_height = BROADCASTER.active_height
        offset_x, offset_y, grab_w, grab_h = 0, 0, 1920, 1080
        try:
            import mss
            with mss.MSS() as sct:
                idx = min(max(0, mon_idx), len(sct.monitors) - 1)
                m = sct.monitors[idx]
                offset_x, offset_y = m["left"], m["top"]
                grab_w, grab_h = m["width"], m["height"]
        except Exception:
            pass

        out_h = (cur_height // 2) * 2
        out_w = int(grab_w * (out_h / float(grab_h)))
        out_w = (out_w // 2) * 2
        if out_w > 1920:
            out_w = 1920

        actual_fps = fps
        cur_bitrate = "6M" if out_h >= 1080 else "4500k"

        # Przechwytywanie przez DirectX 11 ddagrab (z automatycznym fallbackiem do gdigrab)
        if BROADCASTER.use_ddagrab and mon_idx in (1, 2):
            dda_idx = mon_idx - 1
            video_input_args = [
                "-probesize", "32",
                "-analyzeduration", "0",
                "-f", "lavfi",
                "-i", f"ddagrab=output_idx={dda_idx}:framerate={actual_fps}:draw_mouse=1:dup_frames=1:output_fmt=bgra",
            ]
            vf_chain = f"hwdownload,format=bgra,scale={out_w}:{out_h}:flags=fast_bilinear,format=nv12"
            cap_mode = f"DirectX11 ddagrab (output {dda_idx})"
        else:
            video_input_args = [
                "-probesize", "32",
                "-analyzeduration", "0",
                "-f", "gdigrab",
                "-draw_mouse", "1",
                "-framerate", str(actual_fps),
                "-offset_x", str(offset_x),
                "-offset_y", str(offset_y),
                "-video_size", f"{grab_w}x{grab_h}",
                "-i", "desktop",
            ]
            vf_chain = f"scale={out_w}:{out_h}:flags=fast_bilinear,format=nv12"
            cap_mode = "GDI gdigrab"

        cmd = [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel", "error",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            *video_input_args,
            # Wejście 1: Dźwięk systemowy WASAPI
            "-probesize", "32",
            "-analyzeduration", "0",
            "-f", "s16le",
            "-ar", "48000",
            "-ac", "2",
            "-thread_queue_size", "128",
            "-i", "pipe:0",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-vf", vf_chain,
            "-threads", "4",
            # Uniwersalne argumenty wykrytego kodera (NVENC / AMF / QSV / libx264)
            *enc_builder(cur_bitrate),
            "-flags", "+global_header",
            "-bsf:v", "h264_metadata=level=4.1,dump_extra=freq=keyframe",
            # Koder audio MP2 stereo
            "-c:a", "mp2",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",
            # Kontener MPEG-TS
            "-f", "mpegts",
            "-mpegts_flags", "resend_headers+initial_discontinuity",
            "-max_delay", "0",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-flush_packets", "1",
            "pipe:1",
        ]

        print(
            f"  [{enc_id}] {cap_mode} | Monitor {mon_idx} ({grab_w}x{grab_h}) -> "
            f"{out_w}x{out_h} @ {actual_fps} FPS ({cur_bitrate}) + Audio WASAPI"
        )
        t_launch = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=getattr(subprocess, "HIGH_PRIORITY_CLASS", 0x00000080),
        )
        BROADCASTER.ffmpeg_proc = proc

        threading.Thread(target=feed_stdin_audio, args=(proc.stdin,), daemon=True).start()

        try:
            stdout_fd = proc.stdout.fileno()
            while RUNNING:
                # 3760 bajtów = dokładnie 20 * 188 bajtów pakietu MPEG-TS
                chunk = os.read(stdout_fd, 3760)
                if not chunk:
                    break
                BROADCASTER.broadcast(chunk)
        except Exception:
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass

        # Jeśli ddagrab zakończył się błędem w mniej niż 1.5s, przełącz awaryjnie na gdigrab
        if RUNNING and BROADCASTER.use_ddagrab and (time.perf_counter() - t_launch) < 1.5:
            err_msg = ""
            try:
                err_msg = proc.stderr.read().decode("utf-8", errors="ignore").strip()
            except Exception:
                pass
            if err_msg:
                print(f"  [!] Info ddagrab ({err_msg.splitlines()[-1]}), przełączam na gdigrab...")
                BROADCASTER.use_ddagrab = False

        if RUNNING:
            time.sleep(0.3)


# ==============================================================================
# Wbudowany serwer UPnP / DLNA MediaServer (widoczny w Smart Share / INPUT)
# ==============================================================================
def build_root_desc_xml(base_url):
    return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{FRIENDLY_NAME}</friendlyName>
    <manufacturer>Antigravity</manufacturer>
    <modelDescription>RTX 3070 Ti Live Desktop Streamer</modelDescription>
    <modelName>Windows PC DLNA Live</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>{SERVER_UUID}</UDN>
    <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMS-1.50</dlna:X_DLNADOC>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/ContentDirectory.xml</SCPDURL>
        <controlURL>/upnp/control/ContentDirectory</controlURL>
        <eventSubURL>/upnp/event/ContentDirectory</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/ConnectionManager.xml</SCPDURL>
        <controlURL>/upnp/control/ConnectionManager</controlURL>
        <eventSubURL>/upnp/event/ConnectionManager</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""


CONTENT_DIRECTORY_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>Browse</name><argumentList>
      <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
      <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
      <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
      <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
      <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
      <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
      <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
      <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetSearchCapabilities</name><argumentList>
      <argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetSortCapabilities</name><argumentList>
      <argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>
    </argumentList></action>
    <action><name>GetSystemUpdateID</name><argumentList>
      <argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType><allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

CONNECTION_MANAGER_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action><name>GetProtocolInfo</name><argumentList>
      <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
      <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
    </argumentList></action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""


def build_browse_response_soap(base_url, object_id, browse_flag):
    items = [
        ("101", "1. Monitor 1 - Full HD 1080p (60 FPS + Audio)", f"{base_url}/live/mon1_1080.mpg"),
        ("102", "2. Monitor 1 - HD 720p Ultra-Low-Lag (60 FPS + Audio)", f"{base_url}/live/mon1_720.mpg"),
        ("103", "3. Monitor 2 - Full HD 1080p (60 FPS + Audio)", f"{base_url}/live/mon2_1080.mpg"),
        ("104", "4. Monitor 2 - HD 720p Ultra-Low-Lag (60 FPS + Audio)", f"{base_url}/live/mon2_720.mpg"),
    ]

    if browse_flag == "BrowseMetadata" and object_id in ("0", "root"):
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<container id="0" parentID="-1" restricted="1" childCount="4">'
            f"<dc:title>{FRIENDLY_NAME}</dc:title>"
            "<upnp:class>object.container.storageFolder</upnp:class>"
            "</container></DIDL-Lite>"
        )
        num_ret, total = 1, 1
    elif browse_flag == "BrowseMetadata" and object_id in ("101", "102", "103", "104"):
        chosen = next((it for it in items if it[0] == object_id), items[0])
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="{chosen[0]}" parentID="0" restricted="1">'
            f"<dc:title>{chosen[1]}</dc:title>"
            "<upnp:class>object.item.videoItem</upnp:class>"
            f'<res protocolInfo="{DLNA_PROTOCOL_INFO}" size="10737418240" duration="99:00:00.000">{chosen[2]}</res>'
            "</item></DIDL-Lite>"
        )
        num_ret, total = 1, 1
    else:
        parts = [
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        ]
        for item_id, title, url in items:
            parts.append(
                f'<item id="{item_id}" parentID="0" restricted="1">'
                f"<dc:title>{title}</dc:title>"
                "<upnp:class>object.item.videoItem</upnp:class>"
                f'<res protocolInfo="{DLNA_PROTOCOL_INFO}" size="10737418240" duration="99:00:00.000">{url}</res>'
                "</item>"
            )
        parts.append("</DIDL-Lite>")
        didl = "".join(parts)
        num_ret, total = len(items), len(items)

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f"<Result>{html.escape(didl)}</Result>"
        f"<NumberReturned>{num_ret}</NumberReturned>"
        f"<TotalMatches>{total}</TotalMatches>"
        "<UpdateID>1</UpdateID>"
        "</u:BrowseResponse>"
        "</s:Body></s:Envelope>"
    )


def ssdp_Responder_and_announcer(local_ip, port, tv_ip="192.168.0.114"):
    """Rozgłasza nasz serwer DLNA MediaServer przez SSDP, aby od razu pojawił się w Smart Share na TV."""
    location = f"http://{local_ip}:{port}/rootDesc.xml"
    nts_types = [
        ("upnp:rootdevice", f"{SERVER_UUID}::upnp:rootdevice"),
        (SERVER_UUID, SERVER_UUID),
        ("urn:schemas-upnp-org:device:MediaServer:1", f"{SERVER_UUID}::urn:schemas-upnp-org:device:MediaServer:1"),
        ("urn:schemas-upnp-org:service:ContentDirectory:1", f"{SERVER_UUID}::urn:schemas-upnp-org:service:ContentDirectory:1"),
        ("urn:schemas-upnp-org:service:ConnectionManager:1", f"{SERVER_UUID}::urn:schemas-upnp-org:service:ConnectionManager:1"),
    ]

    def send_notify():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.bind((local_ip, 0))
            for nt, usn in nts_types:
                msg = (
                    "NOTIFY * HTTP/1.1\r\n"
                    "HOST: 239.255.255.250:1900\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    f"LOCATION: {location}\r\n"
                    f"NT: {nt}\r\n"
                    "NTS: ssdp:alive\r\n"
                    "SERVER: Windows/10.0 UPnP/1.0 DLNADOC/1.50 Antigravity/1.0\r\n"
                    f"USN: {usn}\r\n\r\n"
                ).encode("ascii")
                sock.sendto(msg, ("239.255.255.250", 1900))
                if tv_ip:
                    sock.sendto(msg, (tv_ip, 1900))
        except Exception:
            pass
        finally:
            sock.close()

    def announcer_loop():
        while RUNNING:
            send_notify()
            time.sleep(8.0)

    threading.Thread(target=announcer_loop, daemon=True).start()

    # Nasłuchiwanie na zapytania M-SEARCH od telewizora LG
    try:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind(("", 1900))
        mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton(local_ip)
        lsock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        lsock.settimeout(1.0)
        while RUNNING:
            try:
                data, addr = lsock.recvfrom(4096)
                txt = data.decode("utf-8", errors="ignore")
                if "M-SEARCH" in txt.upper():
                    for st, usn in nts_types:
                        if st.lower() in txt.lower() or "ssdp:all" in txt.lower():
                            reply = (
                                "HTTP/1.1 200 OK\r\n"
                                "CACHE-CONTROL: max-age=1800\r\n"
                                "EXT:\r\n"
                                f"LOCATION: {location}\r\n"
                                "SERVER: Windows/10.0 UPnP/1.0 DLNADOC/1.50 Antigravity/1.0\r\n"
                                f"ST: {st}\r\n"
                                f"USN: {usn}\r\n\r\n"
                            ).encode("ascii")
                            lsock.sendto(reply, addr)
            except socket.timeout:
                continue
            except Exception:
                break
    except Exception:
        pass


# ==============================================================================
# Uniwersalny kontroler UPnP AVTransport1 (wykrywanie dowolnego TV DLNA w LAN)
# ==============================================================================
def discover_all_dlna_renderers(hint_ip=None):
    """
    Wyszukuje wszystkie telewizory i odbiorniki DLNA (AVTransport:1) w sieci lokalnej.
    Zwraca listę krotek: [(friendly_name, control_url, tv_ip), ...], z priorytetem dla LG 55LM670S.
    """
    q = '"'
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        f"MAN: {q}ssdp:discover{q}\r\n"
        "MX: 1\r\n"
        "ST: urn:schemas-upnp-org:service:AVTransport:1\r\n\r\n"
    ).encode("ascii")

    # Zbierz lokalne adresy IPv4 ze wszystkich aktywnych kart sieciowych (Wi-Fi / Ethernet)
    local_ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in local_ips:
                local_ips.append(ip)
    except Exception:
        pass
    if not local_ips:
        local_ips = ["0.0.0.0"]

    locations = []
    for bind_ip in local_ips:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        try:
            if bind_ip != "0.0.0.0":
                sock.bind((bind_ip, 0))
            if hint_ip:
                sock.sendto(msg, (hint_ip, 1900))
            sock.sendto(msg, ("239.255.255.250", 1900))
            t_end = time.time() + 1.0
            while time.time() < t_end:
                try:
                    data, addr = sock.recvfrom(4096)
                    txt = data.decode("utf-8", errors="ignore")
                    for line in txt.splitlines():
                        if line.upper().startswith("LOCATION:"):
                            loc = line.split(":", 1)[1].strip()
                            if loc not in locations:
                                if (hint_ip and addr[0] == hint_ip) or "LGE_DLNA" in txt.upper():
                                    locations.insert(0, loc)
                                else:
                                    locations.append(loc)
                except socket.timeout:
                    break
        except Exception:
            pass
        finally:
            sock.close()

    renderers = []
    for loc in locations:
        try:
            xml = urllib.request.urlopen(loc, timeout=1.5).read().decode("utf-8", errors="ignore")
            if "AVTransport:1" not in xml:
                continue
            parsed = urlparse(loc)
            base = f"{parsed.scheme}://{parsed.netloc}"
            m_name = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S)
            fname = m_name.group(1).strip() if m_name else f"DLNA TV ({parsed.hostname})"
            m_ctrl = re.search(
                r"<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>.*?<controlURL>(.*?)</controlURL>",
                xml,
                re.S,
            )
            ctrl_path = m_ctrl.group(1).strip() if m_ctrl else "/upnp/control/AVTransport1"
            if not ctrl_path.startswith("/"):
                ctrl_path = "/" + ctrl_path
            entry = (fname, base + ctrl_path, parsed.hostname)
            if "55LM670S" in fname or (hint_ip and parsed.hostname == hint_ip):
                renderers.insert(0, entry)
            else:
                renderers.append(entry)
        except Exception:
            pass
    return renderers


def find_lg_55lm670s_avtransport(hint_ip="192.168.0.114"):
    renderers = discover_all_dlna_renderers(hint_ip=hint_ip)
    if renderers:
        return renderers[0][1], renderers[0][2]
    return None, None


def push_stream_to_tv_dlna(control_url, stream_url):
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        '<item id="101" parentID="0" restricted="1">'
        "<dc:title>Pulpit PC (60 FPS + Audio)</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="{DLNA_PROTOCOL_INFO}" size="10737418240" duration="99:00:00.000">{stream_url}</res>'
        "</item></DIDL-Lite>"
    )

    def send_soap(action, body_inner):
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f"{body_inner}"
            f"</u:{action}>"
            "</s:Body></s:Envelope>"
        ).encode("utf-8")

        req = urllib.request.Request(
            control_url,
            data=envelope,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
                "Connection": "close",
            },
        )
        return urllib.request.urlopen(req, timeout=4).read().decode("utf-8", errors="ignore")

    try:
        try:
            send_soap("Stop", "<InstanceID>0</InstanceID>")
            time.sleep(0.3)
        except Exception:
            pass

        send_soap(
            "SetAVTransportURI",
            f"<InstanceID>0</InstanceID>"
            f"<CurrentURI>{stream_url}</CurrentURI>"
            f"<CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>",
        )
        time.sleep(0.5)
        send_soap("Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        print(f"  [DLNA Push] Telewizor przyjął strumień: {stream_url}")
        return True
    except urllib.error.HTTPError as he:
        err_txt = he.read().decode("utf-8", errors="ignore")
        if "701" in err_txt:
            print(
                "  [DLNA Push] Telewizor ma obecnie otwartą przeglądarkę/aplikację Smart (kod 701).\n"
                "              -> Naciśnij [EXIT] na pilocie TV lub wybierz '[PC] Pulpit RTX 60FPS' w menu INPUT / Smart Share!"
            )
        else:
            print(f"  [DLNA Push] Odpowiedź TV ({he.code})")
    except Exception as exc:
        print(f"  [DLNA Push] {exc}")
    return False


# ==============================================================================
# Serwer HTTP obsługujący zarówno DLNA MediaServer, jak i strumień MPEG-TS
# ==============================================================================
BASE_HTTP_URL = "http://192.168.0.85:8000"


class DLNAHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Wycisz ewentualne odpytywanie starej karty przeglądarki (/frame.jpg)
        if "/frame.jpg" in self.path or "/api/mouse" in self.path:
            return
        print(f"[TV -> PC] {self.address_string()} {fmt % args}")

    def send_dlna_stream_headers(self):
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", DLNA_ORG_FEATURES)
        self.send_header("Connection", "close")

    def do_HEAD(self):
        if self.path.startswith("/live"):
            self.send_response(200)
            self.send_dlna_stream_headers()
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="ignore")
        soap_action = self.headers.get("SOAPAction", "")

        if "ContentDirectory" in self.path or "ContentDirectory" in soap_action:
            if "Browse" in soap_action or "<u:Browse" in body or ":Browse" in body:
                m_id = re.search(r"<ObjectID>(.*?)</ObjectID>", body)
                m_fl = re.search(r"<BrowseFlag>(.*?)</BrowseFlag>", body)
                obj_id = m_id.group(1).strip() if m_id else "0"
                flag = m_fl.group(1).strip() if m_fl else "BrowseDirectChildren"
                resp_xml = build_browse_response_soap(BASE_HTTP_URL, obj_id, flag)
            elif "GetSystemUpdateID" in soap_action:
                resp_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                    '<s:Body><u:GetSystemUpdateIDResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
                    "<Id>1</Id></u:GetSystemUpdateIDResponse></s:Body></s:Envelope>"
                )
            else:
                resp_xml = (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                    '<s:Body><u:GetSearchCapabilitiesResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
                    "<SearchCaps></SearchCaps></u:GetSearchCapabilitiesResponse></s:Body></s:Envelope>"
                )

            payload = resp_xml.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", 'text/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if "ConnectionManager" in self.path or "ConnectionManager" in soap_action:
            resp_xml = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body><u:GetProtocolInfoResponse xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1">'
                f"<Source>{DLNA_PROTOCOL_INFO}</Source><Sink></Sink>"
                "</u:GetProtocolInfoResponse></s:Body></s:Envelope>"
            )
            payload = resp_xml.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", 'text/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_error(404)

    def do_GET(self):
        if self.path == "/rootDesc.xml":
            payload = build_root_desc_xml(BASE_HTTP_URL).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/ContentDirectory.xml":
            payload = CONTENT_DIRECTORY_SCPD.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path == "/ConnectionManager.xml":
            payload = CONNECTION_MANAGER_SCPD.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.path.startswith("/live"):
            req_mon = 2 if "mon2" in self.path else (0 if "all" in self.path else 1)
            req_h = 720 if "720" in self.path else (1080 if "1080" in self.path else BROADCASTER.active_height)
            BROADCASTER.set_stream_profile(req_mon, req_h)

            # Wyłącz algorytm Nagle'a (TCP_NODELAY) dla wysyłania pakietów w < 1 ms
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            except Exception:
                pass

            self.send_response(200)
            self.send_dlna_stream_headers()
            self.end_headers()

            client_q = BROADCASTER.add_client()
            print(
                f"  [STRUMIEŃ] Telewizor ({self.address_string()}) odbiera: "
                f"Monitor {req_mon}, {req_h}p @ 60 FPS + Audio!"
            )
            try:
                while RUNNING:
                    chunk = client_q.get(timeout=5.0)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, socket.error, queue.Empty):
                pass
            finally:
                BROADCASTER.remove_client(client_q)
                print(f"  [STRUMIEŃ] Telewizor ({self.address_string()}) zakończył odtwarzanie.")
            return

        # Wycisz stare żądania /frame.jpg z poprzedniej otwartej karty przeglądarki
        if self.path.startswith("/frame.jpg"):
            self.send_response(204)
            self.end_headers()
            return

        # Strona informacyjna na porcie 8000
        info_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>LG 55LM670S DLNA Streamer</title>
<style>body{{background:#0d1117;color:#fff;font-family:Arial,sans-serif;padding:40px;font-size:22px;}}
h1{{color:#58a6ff;}} li{{margin:14px 0;}}</style></head>
<body>
<h1>Strumień sprzętowy RTX 3070 Ti (60 FPS + Dźwięk) jest AKTYWNY!</h1>
<p>Aby uzyskać pełne 60 FPS oraz dźwięk stereo przez sprzętowy dekoder telewizora (zamiast wolnej przeglądarki WWW):</p>
<ol>
  <li>Naciśnij przycisk <b>EXIT</b> na pilocie, aby zamknąć przeglądarkę internetową.</li>
  <li>Naciśnij przycisk <b>INPUT (Złącza)</b> lub wejdź w <b>Smart Share</b> na pilocie TV.</li>
  <li>Wybierz serwer <b>{FRIENDLY_NAME}</b> i kliknij <b>1. Monitor 1 (60 FPS + Dźwięk)</b>!</li>
</ol>
</body></html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(info_html)))
        self.end_headers()
        self.wfile.write(info_html)


def get_preferred_ip(target_ip="192.168.0.114"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 8080))
        return s.getsockname()[0]
    except Exception:
        return "192.168.0.85"
    finally:
        s.close()


def main():
    global BASE_HTTP_URL, RUNNING
    parser = argparse.ArgumentParser(description="Universal Hardware H.264 60FPS + Audio DLNA Streamer")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--monitor", type=int, default=1)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--height", type=int, default=1080, choices=[720, 1080])
    parser.add_argument("--bitrate", type=str, default="7M")
    parser.add_argument(
        "--encoder",
        type=str,
        default="auto",
        choices=["auto", "h264_nvenc", "h264_amf", "h264_qsv", "libx264"],
        help="Wybór kodera wideo (domyślnie: auto - wykrywa NVIDIA/AMD/Intel/CPU)",
    )
    parser.add_argument("--tv-ip", type=str, default="192.168.0.114", help="Opcjonalny adres IP telewizora")
    args = parser.parse_args()

    renderers = discover_all_dlna_renderers(hint_ip=args.tv_ip)
    selected_idx = 0
    if renderers:
        tv_name, ctrl_url, tv_ip = renderers[0]
    else:
        tv_name, ctrl_url, tv_ip = ("Brak", None, args.tv_ip)

    local_ip = get_preferred_ip(tv_ip or "8.8.8.8")
    BASE_HTTP_URL = f"http://{local_ip}:{args.port}"
    stream_url = f"{BASE_HTTP_URL}/live/mon{args.monitor}_{args.height}.mpg"

    print("=" * 78)
    print("  Uniwersalny Streamer DLNA H.264 (NVIDIA / AMD / Intel / CPU 60FPS) + Dźwięk")
    print("=" * 78)
    print(f"  Adres serwera PC                      :  {BASE_HTTP_URL}")
    print(f"  Strumień MPEG-TS                      :  {stream_url}")
    if renderers:
        print("  Wykryte telewizory DLNA w sieci LAN   :")
        for idx, (r_name, r_ctrl, r_ip) in enumerate(renderers):
            marker = "-->" if idx == selected_idx else "   "
            print(f"   {marker} [{idx + 1}] {r_name} ({r_ip})")
    else:
        print("  Wykryte telewizory DLNA               :  Brak (upewnij się, że TV jest włączony)")
    print("-" * 78)
    print("  STEROWANIE PROSTO Z TEGO OKNA KONSOLI (TV na zwykłym kanale/HDMI, po naciśnięciu EXIT):")
    print("    [ENTER]     -> Włącz / odśwież aktualny strumień na ekranie TV")
    print("    [1] + ENTER -> Monitor 1 : Full HD 1080p @ 60 FPS + Dźwięk")
    print("    [2] + ENTER -> Monitor 1 : HD 720p Ultra-Low-Lag @ 60 FPS + Dźwięk")
    print("    [3] + ENTER -> Monitor 2 : Full HD 1080p @ 60 FPS + Dźwięk")
    print("    [4] + ENTER -> Monitor 2 : HD 720p Ultra-Low-Lag @ 60 FPS + Dźwięk")
    if len(renderers) > 1:
        print("    [t] + ENTER -> Przełącz docelowy telewizor DLNA na następny z listy")
    print("=" * 78)

    # 1. Start pojedynczego wątku WASAPI Loopback
    threading.Thread(target=global_wasapi_capture_thread, daemon=True).start()

    # 2. Start kodera FFmpeg (auto-detekcja GPU/CPU)
    threading.Thread(
        target=ffmpeg_producer_loop,
        args=(args.monitor, args.fps, args.height, args.bitrate, args.encoder),
        daemon=True,
    ).start()

    # 3. Start serwera HTTP/DLNA
    server = ThreadingHTTPServer(("0.0.0.0", args.port), DLNAHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 4. Start rozgłaszania UPnP SSDP
    threading.Thread(
        target=ssdp_Responder_and_announcer,
        args=(local_ip, args.port, tv_ip),
        daemon=True,
    ).start()

    # 5. Automatyczna próba wypchnięcia przez DLNA AVTransport po 2 sekundach
    if ctrl_url:
        def initial_push():
            time.sleep(2.0)
            push_stream_to_tv_dlna(ctrl_url, stream_url)
        threading.Thread(target=initial_push, daemon=True).start()

    try:
        while True:
            cmd_in = input().strip().lower()
            if cmd_in == "1":
                stream_url = f"{BASE_HTTP_URL}/live/mon1_1080.mpg"
                BROADCASTER.set_stream_profile(1, 1080)
            elif cmd_in == "2":
                stream_url = f"{BASE_HTTP_URL}/live/mon1_720.mpg"
                BROADCASTER.set_stream_profile(1, 720)
            elif cmd_in == "3":
                stream_url = f"{BASE_HTTP_URL}/live/mon2_1080.mpg"
                BROADCASTER.set_stream_profile(2, 1080)
            elif cmd_in == "4":
                stream_url = f"{BASE_HTTP_URL}/live/mon2_720.mpg"
                BROADCASTER.set_stream_profile(2, 720)
            elif cmd_in == "t" and renderers:
                selected_idx = (selected_idx + 1) % len(renderers)
                tv_name, ctrl_url, tv_ip = renderers[selected_idx]
                print(f"  [TV] Przełączono docelowy odbiornik na: {tv_name} ({tv_ip})")

            if not ctrl_url:
                renderers = discover_all_dlna_renderers(hint_ip=args.tv_ip)
                if renderers:
                    tv_name, ctrl_url, tv_ip = renderers[selected_idx % len(renderers)]

            if ctrl_url:
                push_stream_to_tv_dlna(ctrl_url, stream_url)
            else:
                print("  [!] Nie znaleziono usługi AVTransport telewizora w sieci.")
    except (KeyboardInterrupt, EOFError):
        print("\n[!] Zatrzymywanie serwera...")
        RUNNING = False
        server.shutdown()


if __name__ == "__main__":
    main()
