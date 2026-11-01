#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LG 55LM670S-ZA Hardware H.264 (RTX 3070 Ti NVENC 60 FPS) + WASAPI Audio Streamer
================================================================================
Full DLNA MediaServer (Smart Share) + AVTransport push for LG NetCast 3.0.

Why this runs at 60 FPS with audio:
- It bypasses the slow 2012 WebKit browser and uses the LG TV's native hardware
  video/audio ASIC decoder (Smart Share / DLNA).
- The NVIDIA GeForce RTX 3070 Ti encodes the screen in real time (h264_nvenc,
  Main profile @ Level 4.1, 1280x720 60 FPS or 1920x1080, 0 B-frames).
- PC audio (Realtek speakers, WASAPI loopback) is captured in a single thread
  and muxed as 48 kHz stereo MP2 into the MPEG-TS stream.
- The server announces itself on the home network as the DLNA server
  "[PC] Desktop RTX 60FPS", visible on the TV under INPUT / Smart Share.
"""

import argparse
import collections
import html
import os
import queue
import random
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
# DLNA / UPnP constants for LG NetCast 3.0 (LGE_DLNA_SDK/1.5.0)
# ==============================================================================
SERVER_UUID = "uuid:8f9a1b2c-3d4e-5f6a-7b8c-9d0e1f2a3b4c"
FRIENDLY_NAME = "[PC] Desktop RTX 60FPS + Audio"

DLNA_ORG_FEATURES = (
    "DLNA.ORG_PN=MPEG_TS_HD_NA_ISO;"
    "DLNA.ORG_OP=00;"
    "DLNA.ORG_CI=1;"
    "DLNA.ORG_FLAGS=8D700000000000000000000000000000"
)
DLNA_PROTOCOL_INFO = f"http-get:*:video/mpeg:{DLNA_ORG_FEATURES}"


# ==============================================================================
# Global stream state and MPEG-TS broadcaster
# ==============================================================================
class StreamBroadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()
        self.active_monitor = 1
        self.active_height = 1080
        self.active_fps = 60
        self.active_bitrate = "6M"
        self.active_encoder = "auto"
        self.use_ddagrab = True
        self.ffmpeg_proc = None
        self.status_callback = None
        self.bytes_counter = 0
        self.last_rate_ts = time.monotonic()
        self.current_mbps = 0.0
        self.audio_level = 0.0
        self.audio_device_name = "Wykrywanie..."
        # QPC seconds per second of the TV's clock, calibrated by DelayMonitor
        self.clock_ratio = 1.0
        self.timeline = None  # StreamTimeline of the running ffmpeg
        self.delay_callback = None
        self.playback_starts = {}  # client IP -> (QPC time of its first keyframe, StreamTimeline)

    def notify_status(self, msg):
        print(msg)
        if self.status_callback:
            try:
                self.status_callback(msg.strip())
            except Exception:
                pass

    def set_stream_profile(self, mon_idx, height=None, fps=None, bitrate=None, encoder=None):
        with self.lock:
            changed = False
            if mon_idx is not None and self.active_monitor != mon_idx:
                self.active_monitor = mon_idx
                changed = True
            if height is not None and self.active_height != height:
                self.active_height = height
                changed = True
            if fps is not None and self.active_fps != fps:
                self.active_fps = fps
                changed = True
            if bitrate is not None and self.active_bitrate != bitrate:
                self.active_bitrate = bitrate
                changed = True
            if encoder is not None and self.active_encoder != encoder:
                self.active_encoder = encoder
                changed = True
            if changed:
                self.notify_status(
                    f"  [Profile] Switching: Monitor {self.active_monitor}, "
                    f"{self.active_height}p @ {self.active_fps} FPS ({self.active_bitrate})"
                )
                self.restart_encoder()

    def restart_encoder(self):
        """Kills ffmpeg; ffmpeg_producer_loop relaunches it with the current profile."""
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.kill()
            except Exception:
                pass

    def start_playback(self, client_ip, t_keyframe):
        with self.lock:
            self.playback_starts[client_ip] = (t_keyframe, self.timeline)

    def end_playback(self, client_ip, t_keyframe):
        with self.lock:
            start = self.playback_starts.get(client_ip)
            if start and start[0] == t_keyframe:
                del self.playback_starts[client_ip]

    def playback_start(self, client_ip):
        with self.lock:
            return self.playback_starts.get(client_ip)

    def add_client(self):
        # A 240-chunk queue (~900 KB) absorbs Wi-Fi micro-stalls:
        # NEVER drop single packets from the middle of an H.264 frame (no glitches/macroblocking!)
        q = queue.Queue(maxsize=240)
        with self.lock:
            self.clients.add(q)
        return q

    def remove_client(self, q):
        with self.lock:
            self.clients.discard(q)

    def broadcast(self, chunk):
        if not chunk or not self.clients:
            return
        item = (time.perf_counter(), chunk)
        with self.lock:
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(item)
                except queue.Full:
                    # If the client stopped reading entirely (>900 KB backlog), flush the whole queue
                    try:
                        while not q.empty():
                            q.get_nowait()
                        q.put_nowait(item)
                    except Exception:
                        dead.append(q)
            for q in dead:
                self.clients.discard(q)


BROADCASTER = StreamBroadcaster()
AUDIO_QUEUE = queue.Queue(maxsize=80)
RUNNING = True
TS_PACKET = 188


def find_keyframe_start(chunk):
    """
    Returns the offset in a chunk of whole MPEG-TS packets where a client can start decoding,
    or -1. ffmpeg flags the first packet of every keyframe (and of every audio PES) with
    random_access_indicator and writes the PAT/PMT right before a video keyframe.
    """
    pat = -1
    for p in range(0, len(chunk), TS_PACKET):
        if ((chunk[p + 1] & 0x1F) << 8 | chunk[p + 2]) == 0:
            pat = p
            continue
        # PUSI + adaptation field with random_access_indicator
        if not (chunk[p + 1] & 0x40 and chunk[p + 3] & 0x20 and chunk[p + 4] and chunk[p + 5] & 0x40):
            continue
        pes = p + 5 + chunk[p + 4]
        if chunk[pes:pes + 3] == b"\x00\x00\x01" and 0xE0 <= chunk[pes + 3] <= 0xEF:
            if 0 <= pat and p - pat <= 3 * TS_PACKET:
                return pat
    return -1


# ==============================================================================
# Single global WASAPI loopback thread (no duplicates, no log spam)
# ==============================================================================
def global_wasapi_capture_thread():
    frames_per_chunk = 960  # 20 ms @ 48000 Hz
    while RUNNING:
        try:
            with pyaudio.PyAudio() as p:
                loopback = p.get_default_wasapi_loopback()
                dev_rate = int(loopback.get("defaultSampleRate", 48000))
                dev_ch = min(2, int(loopback.get("maxInputChannels", 2)))
                BROADCASTER.audio_device_name = loopback.get("name", "WASAPI Loopback")
                print(
                    f"  [Audio] Capturing audio from: {loopback['name']} "
                    f"({dev_rate} Hz, {dev_ch} ch)"
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
            print(f"  [Audio] Waiting for an audio device ({exc})...")
            time.sleep(2.0)


def resize_pcm_frames(pcm, n_out, frame=4):
    """Stretches/squeezes PCM to n_out frames by repeating/dropping single frames spread evenly."""
    n_in = len(pcm) // frame
    k = abs(n_out - n_in)
    if k == 0:
        return bytes(pcm)
    parts = []
    prev = 0
    for i in range(1, k + 1):
        cut = i * n_in // (k + 1) * frame
        parts.append(pcm[prev:cut])
        if n_out > n_in:
            parts.append(pcm[cut - frame:cut])
            prev = cut
        else:
            prev = cut + frame
    parts.append(pcm[prev:])
    return b"".join(parts)


class StreamTimeline:
    """
    Maps time since the encoder started (QPC seconds) to stream time, in segments of constant
    rate: QPC seconds per stream second = clock_ratio * speed. clock_ratio matches the stream
    to the TV's clock; speed > 1 plays it slightly fast so a TV that fell behind catches up.
    Video follows the timeline through setpts, updated live with ffmpeg's interactive "c"
    command on stdin; audio by the number of samples fed.
    """

    LEAD = 3.0  # s; a new segment starts this far ahead, after ffmpeg has applied the command

    def __init__(self, proc, t0, clock_ratio):
        self.proc = proc
        self.t0 = t0
        self.clock_ratio = clock_ratio
        self.speed = 1.0
        self.lock = threading.Lock()
        self.segments = [(0.0, 0.0, clock_ratio, 1.0)]  # (input time, stream time, rate, speed)

    def segment(self, s):
        segments = self.segments
        return next((seg for seg in reversed(segments) if s >= seg[0]), segments[0])

    def to_stream(self, s):
        p, x, rate, _ = self.segment(s)
        return x + (s - p) / rate

    def from_stream(self, x):
        segments = self.segments
        p, x0, rate, _ = next((seg for seg in reversed(segments) if x >= seg[1]), segments[0])
        return p + (x - x0) * rate

    def set(self, clock_ratio=None, speed=None):
        with self.lock:
            if clock_ratio is not None:
                self.clock_ratio = clock_ratio
            if speed is not None:
                self.speed = speed
            p0, x0, r0, _ = self.segments[-1]
            p = max(time.perf_counter() - self.t0, p0) + self.LEAD
            x, rate = x0 + (p - p0) / r0, self.clock_ratio * self.speed
            # Readers iterate without the lock: swap in a new list instead of appending
            self.segments = self.segments + [(p, x, rate, self.speed)]
            # Frames before the new segment keep the previous one (T = input time in seconds)
            expr = f"if(lt(T,{p:.6f}),{x0:.6f}+(T-{p0:.6f})/{r0:.9f},{x:.6f}+(T-{p:.6f})/{rate:.9f})/TB"
            try:
                self.proc.stdin.write(f"csetpts -1 expr {expr}\n".encode("ascii"))
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass


def read_tail(stream, tail):
    """Reads a stream to the end, keeping its last lines in the tail deque."""
    for line in iter(stream.readline, b""):
        tail.append(line.decode("utf-8", errors="ignore").strip())


def feed_ffmpeg_audio(audio_srv, proc, timeline):
    """
    Feeds PCM to ffmpeg over loopback TCP paced by the StreamTimeline, not by the sound card.

    ffmpeg timestamps the s16le input by sample count, while ddagrab stamps video with QPC
    (mapped to the same timeline in setpts). The sound card clock differs from QPC (0.07-0.2%
    here), so writing WASAPI data as it arrives makes audio fall behind video during a movie;
    ffmpeg 7's scheduler then throttles the video input to wait for audio, and the stream
    stutters harder and harder after a few minutes. Here the byte count written always equals
    elapsed stream time:
    - a 50 ms cushion of captured audio absorbs WASAPI callback jitter,
    - the timeline's speed is applied by dropping single frames spread evenly (0.5% while
      catching up), and clock drift by repeating/dropping a few more (inaudible, up to ~0.4%),
    - no audio (nothing playing) or a capture stall is padded with silence.
    """
    frame = 4  # s16le stereo
    bytes_per_sec = 48000 * frame
    block = bytes_per_sec // 100  # write in >= 10 ms blocks
    cushion = bytes_per_sec // 20
    max_spare = cushion * 5

    # ffmpeg connects once it has opened the video input
    audio_srv.settimeout(0.5)
    conn = None
    try:
        while RUNNING and conn is None and proc.poll() is None:
            try:
                conn, _ = audio_srv.accept()
            except socket.timeout:
                pass
    finally:
        audio_srv.close()
    if conn is None:
        return
    conn.settimeout(None)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    while not AUDIO_QUEUE.empty():
        try:
            AUDIO_QUEUE.get_nowait()
        except Exception:
            break

    pending = bytearray()
    primed = False
    avg_spare = 0.0
    correction = 0.0  # frames still to repeat (+) or drop (-)
    in_frac = 0.0  # fraction of a captured frame owed to the timeline's speed
    written = 0
    t_start = t_last = time.perf_counter()

    with conn:
        while RUNNING:
            try:
                pending += AUDIO_QUEUE.get(timeout=0.01)
                while True:
                    pending += AUDIO_QUEUE.get_nowait()
            except queue.Empty:
                pass

            now = time.perf_counter()
            s = now - t_start
            due = int(timeline.to_stream(s) * bytes_per_sec) // frame * frame - written
            if due < block:
                continue
            dt, t_last = now - t_last, now
            n_out = due // frame
            want = n_out * timeline.segment(s)[3] + in_frac
            n_in = int(want)
            in_frac = want - n_in

            spare = len(pending) - n_in * frame
            if spare > max_spare:
                # Long ffmpeg stall: drop stale audio to keep latency low
                del pending[: spare - cushion]
                spare = cushion
            if not primed and spare >= cushion:
                primed, avg_spare, correction = True, float(spare), 0.0

            if not primed:
                out = b"\x00" * due
            elif spare < 0:
                # Underrun: playback stopped or capture stalled -> silence until the cushion refills
                out = bytes(pending[:due]).ljust(due, b"\x00")
                pending.clear()
                primed = False
            else:
                # Clock drift: steer the (2 s averaged) spare audio back to the cushion over ~5 s
                avg_spare += (spare - avg_spare) * min(1.0, dt / 2.0)
                correction += (cushion - avg_spare) / frame * dt / 5.0
                k_max = max(1, n_out // 200)
                k = max(-k_max, min(k_max, int(correction)))
                if k < 0:
                    k = max(k, -(spare // frame))
                correction = max(-10.0, min(10.0, correction - k))
                take = (n_in - k) * frame
                out = resize_pcm_frames(pending[:take], n_out, frame)
                del pending[:take]

            try:
                conn.sendall(out)
            except OSError:
                return
            written += due


# ==============================================================================
# Automatic detection of the best H.264 encoder (NVIDIA / AMD / Intel / CPU)
# ==============================================================================
DETECTED_ENCODER = None  # (name, build_args(bitrate))


def detect_best_h264_encoder(ffmpeg_exe, requested="auto"):
    """
    Probes the system for hardware GPU encoders (NVIDIA NVENC -> AMD AMF -> Intel QSV)
    and falls back to the multithreaded CPU encoder (libx264 ultrafast) when no GPU is available.
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
            no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
            r = subprocess.run(test_cmd, capture_output=True, timeout=3, creationflags=no_win)
            if r.returncode == 0:
                DETECTED_ENCODER = (enc_id, label, builder)
                return DETECTED_ENCODER
        except Exception:
            continue

    DETECTED_ENCODER = candidates[-1]
    return DETECTED_ENCODER


# ==============================================================================
# FFmpeg encoder: universal GPU/CPU pipeline + WASAPI audio
# ==============================================================================
def ffmpeg_producer_loop(default_monitor=1, fps=60, height=1080, bitrate="6M", encoder_pref="auto"):
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    BROADCASTER.active_monitor = default_monitor
    BROADCASTER.active_height = height
    BROADCASTER.active_fps = fps
    BROADCASTER.active_bitrate = bitrate
    BROADCASTER.active_encoder = encoder_pref

    # Raise the Python process priority so Unity editors/games don't starve it of CPU time
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)
    except Exception:
        pass

    while RUNNING:
        mon_idx = BROADCASTER.active_monitor
        cur_height = BROADCASTER.active_height
        actual_fps = BROADCASTER.active_fps
        cur_bitrate = BROADCASTER.active_bitrate
        enc_id, enc_label, enc_builder = detect_best_h264_encoder(ffmpeg_exe, BROADCASTER.active_encoder)

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

        # DirectX 11 ddagrab capture (with automatic fallback to gdigrab)
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

        # Captured frames are stamped with QPC time, which can run off the TV's clock (0.19% fast
        # on this PC): the TV then plays slower than the stream arrives and the delay grows forever.
        # StreamTimeline retimes them, starting with this setpts.
        clock_ratio = BROADCASTER.clock_ratio
        vf_chain += f",setpts=PTS/{clock_ratio:.9f}"

        # Audio arrives over loopback TCP, so stdin is free for StreamTimeline's setpts commands
        # (ffmpeg ignores keyboard commands while an input reads stdin)
        audio_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        audio_srv.bind(("127.0.0.1", 0))
        audio_srv.listen(1)

        cmd = [
            ffmpeg_exe,
            "-hide_banner",
            "-loglevel", "error",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            *video_input_args,
            # Input 1: WASAPI system audio
            "-probesize", "32",
            "-analyzeduration", "0",
            "-f", "s16le",
            "-ar", "48000",
            "-ac", "2",
            "-thread_queue_size", "128",
            "-i", f"tcp://127.0.0.1:{audio_srv.getsockname()[1]}",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-vf", vf_chain,
            # setpts drops the frame rate: keep frames on a 1/fps grid instead of the capture jitter
            "-r", str(actual_fps),
            "-threads", "4",
            # Arguments of the detected encoder (NVENC / AMF / QSV / libx264)
            *enc_builder(cur_bitrate),
            "-flags", "+global_header",
            "-bsf:v", "h264_metadata=level=4.1,dump_extra=freq=keyframe",
            # MP2 stereo audio encoder
            "-c:a", "mp2",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",
            # MPEG-TS container
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
            f"{out_w}x{out_h} @ {actual_fps} FPS ({cur_bitrate}) + Audio WASAPI, clock x{clock_ratio:.6f}"
        )
        t_launch = time.perf_counter()
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=no_win,
        )
        BROADCASTER.ffmpeg_proc = proc
        timeline = StreamTimeline(proc, t_launch, clock_ratio)
        BROADCASTER.timeline = timeline

        # Drain stderr: every setpts command prints a reply, and a full pipe would stall ffmpeg
        stderr_tail = collections.deque(maxlen=20)
        stderr_reader = threading.Thread(target=read_tail, args=(proc.stderr, stderr_tail), daemon=True)
        stderr_reader.start()
        threading.Thread(target=feed_ffmpeg_audio, args=(audio_srv, proc, timeline), daemon=True).start()

        try:
            stdout_fd = proc.stdout.fileno()
            carry = b""
            while RUNNING:
                # 15040 bytes = 80 * 188-byte MPEG-TS packets (4x less GIL overhead in Python)
                chunk = os.read(stdout_fd, 15040)
                if not chunk:
                    break
                # Pipe reads split packets; broadcast whole ones so clients can start at a keyframe
                chunk = carry + chunk
                cut = len(chunk) - len(chunk) % TS_PACKET
                carry = chunk[cut:]
                BROADCASTER.broadcast(chunk[:cut])
        except Exception:
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass

        # If ddagrab failed within 1.5 s, fall back to gdigrab
        if RUNNING and BROADCASTER.use_ddagrab and (time.perf_counter() - t_launch) < 1.5:
            stderr_reader.join(timeout=1.0)
            err_lines = [line for line in stderr_tail if line]
            if err_lines:
                print(f"  [!] Info ddagrab ({err_lines[-1]}), switching to gdigrab...")
                BROADCASTER.use_ddagrab = False

        if RUNNING:
            time.sleep(0.3)


# ==============================================================================
# Built-in UPnP / DLNA MediaServer (visible in Smart Share / INPUT)
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
    """Announces our DLNA MediaServer over SSDP so it shows up in Smart Share on the TV right away."""
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

    # Listen for M-SEARCH queries from the LG TV
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
# Universal UPnP AVTransport1 controller (discovers any DLNA TV on the LAN)
# ==============================================================================
def discover_all_dlna_renderers(hint_ip=None, preferred_name=None):
    """
    Discovers all DLNA TVs and renderers (AVTransport:1) on the local network.
    Returns a list of tuples [(friendly_name, control_url, tv_ip), ...] sorted by priority:
    preferred_name > LG 55LM670S > hint_ip > the rest.
    """
    # The name outranks the IP: DHCP may hand the TV's old address to another device.
    q = '"'
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        f"MAN: {q}ssdp:discover{q}\r\n"
        "MX: 1\r\n"
        "ST: urn:schemas-upnp-org:service:AVTransport:1\r\n\r\n"
    ).encode("ascii")

    # Collect local IPv4 addresses from all active network adapters (Wi-Fi / Ethernet)
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
            renderers.append((fname, base + ctrl_path, parsed.hostname))
        except Exception:
            pass

    def rank(entry):
        name, _ctrl, ip = entry
        if preferred_name and name == preferred_name:
            return 0
        if "55LM670S" in name:
            return 1
        if hint_ip and ip == hint_ip:
            return 2
        return 3

    renderers.sort(key=rank)
    return renderers


def find_lg_55lm670s_avtransport(hint_ip="192.168.0.114"):
    renderers = discover_all_dlna_renderers(hint_ip=hint_ip)
    if renderers:
        return renderers[0][1], renderers[0][2]
    return None, None


def avtransport_soap(control_url, action, body_inner, timeout=4):
    """Calls one AVTransport:1 action on a renderer and returns the response XML."""
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
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="ignore")


def push_stream_to_tv_dlna(control_url, stream_url):
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
        '<item id="101" parentID="0" restricted="1">'
        "<dc:title>PC Desktop (60 FPS + Audio)</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="{DLNA_PROTOCOL_INFO}" size="10737418240" duration="99:00:00.000">{stream_url}</res>'
        "</item></DIDL-Lite>"
    )

    try:
        try:
            avtransport_soap(control_url, "Stop", "<InstanceID>0</InstanceID>")
            time.sleep(0.3)
        except Exception:
            pass

        avtransport_soap(
            control_url,
            "SetAVTransportURI",
            f"<InstanceID>0</InstanceID>"
            f"<CurrentURI>{stream_url}</CurrentURI>"
            f"<CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>",
        )
        time.sleep(0.5)
        avtransport_soap(control_url, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        BROADCASTER.notify_status(f"  [DLNA Push] TV accepted the stream: {stream_url}")
        DELAY_MONITOR.watch(control_url, stream_url)
        return True
    except urllib.error.HTTPError as he:
        err_txt = he.read().decode("utf-8", errors="ignore")
        if "701" in err_txt:
            BROADCASTER.notify_status(
                "  [DLNA Push] The TV has a Smart app open (code 701) -> Press [EXIT] on the TV remote!"
            )
        else:
            BROADCASTER.notify_status(f"  [DLNA Push] TV response ({he.code})")
    except Exception as exc:
        BROADCASTER.notify_status(f"  [DLNA Push] {exc}")
    return False


def stop_tv_dlna(control_url):
    """Stops DLNA playback on the TV."""
    if not control_url:
        return False
    DELAY_MONITOR.unwatch()
    try:
        avtransport_soap(control_url, "Stop", "<InstanceID>0</InstanceID>", timeout=3)
        BROADCASTER.notify_status("  [DLNA Stop] Playback stopped on the TV.")
        return True
    except Exception as exc:
        BROADCASTER.notify_status(f"  [DLNA Stop] {exc}")
        return False


# ==============================================================================
# TV delay measurement and clock-rate calibration
# ==============================================================================
def parse_rel_time(soap_xml):
    """Seconds of AVTransport RelTime (H:MM:SS[.fff]), or None when the renderer has none."""
    m = re.search(r"<RelTime>(\d+):(\d+):(\d+(?:\.\d+)?)</RelTime>", soap_xml)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


class DelayMonitor:
    """
    Measures how far the TV lags behind the PC and keeps that delay from growing, without
    restarting the TV's playback.

    The TV's playback position (AVTransport RelTime) counts from the first keyframe it was sent,
    read from ffmpeg at QPC time q0. The frame on screen is thus at stream time
    stream(q0) + RelTime, and StreamTimeline maps that back to its capture time; the delay is now
    minus that. RelTime is truncated to whole seconds, so each sample overestimates the delay by
    up to 1 s: the delay is the minimum over WINDOW seconds of samples polled at irregular times.

    RelTime also ticks at the TV's own clock, which calibrates clock_ratio: QPC runs 0.19% fast
    on this PC, so a stream timed by QPC alone outruns the TV and the delay grows forever.
    Delay that builds up anyway (before the calibration, after a Wi-Fi stall) is played off by
    running the stream CATCHUP faster until it is back near the lowest delay of this playback.
    Only a delay RESYNC_EXCESS above it (e.g. the TV was paused) restarts the TV's playback.
    """

    WINDOW = 15.0  # s of samples per delay estimate
    SETTLE = 30.0  # s after the first keyframe before the delay counts as the target
    CAL_MIN_SPAN = 300.0  # s of TV clock estimates before the rate is precise enough (~100 ppm)
    JUMP = 0.3  # s; a larger step between TV clock estimates is a stall or a pause, not drift
    CATCHUP = 0.005  # 0.5% faster: 1 s of delay played off per 200 s, a 9-cent pitch shift
    CATCHUP_START = 0.5  # s above the lowest delay of this playback
    CATCHUP_STOP = 0.2
    RESYNC_EXCESS = 5.0
    RESYNC_INTERVAL = 600.0  # s between automatic resyncs

    def __init__(self):
        self.lock = threading.Lock()
        self.control_url = None
        self.stream_url = None
        self.last_resync = float("-inf")
        self.shown = None
        self.start = None
        self.catching_up = False
        self.reset()

    def reset(self, start=None):
        """Forgets the playback; start = (q0, StreamTimeline) of the TV's current connection."""
        self.set_catching_up(False)
        self.start = start
        self.ratio_ref = start[1].clock_ratio if start else 1.0
        self.samples = collections.deque()  # (QPC time, delay bound, TV clock offset bound)
        self.filled_since = None
        self.points = []  # (QPC time, TV clock offset) every WINDOW s, for the clock calibration
        self.baseline = None
        self.delay = None
        self.last_rel = None
        self.last_rel_t = 0.0

    def set_catching_up(self, on):
        if on != self.catching_up and self.start:
            self.start[1].set(speed=1.0 + self.CATCHUP if on else 1.0)
        self.catching_up = on

    def watch(self, control_url, stream_url):
        with self.lock:
            self.control_url, self.stream_url = control_url, stream_url
            self.reset()

    def unwatch(self):
        with self.lock:
            self.control_url = None
            self.reset()
        self.publish(None)

    def add_sample(self, start, t, rel):
        """Feeds the TV's RelTime polled at QPC time t; updates delay, baseline and clock_ratio."""
        if start != self.start:
            self.reset(start)
        q0, timeline = start
        if rel != self.last_rel:
            self.last_rel, self.last_rel_t = rel, t
        delay = t - timeline.t0 - timeline.from_stream(timeline.to_stream(q0 - timeline.t0) + rel)
        if not rel or t - self.last_rel_t > 2.5 or delay < 0:
            # Prebuffering, paused or a stale position from the previous playback
            self.samples.clear()
            self.filled_since = self.delay = None
            return
        if self.filled_since is None:
            self.filled_since = t
        # QPC time minus TV time: its slope is the clock mismatch, whatever the stream's speed
        self.samples.append((t, delay, t - rel * self.ratio_ref))
        while t - self.samples[0][0] > self.WINDOW:
            self.samples.popleft()
        if t - self.filled_since < 5.0:
            return
        self.delay = min(d for _, d, _ in self.samples)
        if t - self.filled_since < self.WINDOW:
            return
        if t - q0 > self.SETTLE:
            self.baseline = self.delay if self.baseline is None else min(self.baseline, self.delay)
        if not self.points or t - self.points[-1][0] >= self.WINDOW:
            offset = min(u for _, _, u in self.samples)
            if self.points and abs(offset - self.points[-1][1]) > self.JUMP:
                self.points = []
            self.points.append((t, offset))
            self.calibrate(timeline)

    def update(self, start, t, rel):
        """Feeds one RelTime sample and switches the catch-up; returns the excess delay or None."""
        if rel is not None:
            self.add_sample(start, t, rel)
        excess = None
        if self.delay is not None and self.baseline is not None:
            excess = self.delay - self.baseline
        if excess is None or excess < self.CATCHUP_STOP or excess > self.RESYNC_EXCESS:
            self.set_catching_up(False)
        elif excess > self.CATCHUP_START:
            self.set_catching_up(True)
        return excess

    def calibrate(self, timeline):
        """The QPC-minus-TV offset grows by (1 - ratio_ref / true ratio) s per QPC second."""
        if self.points[-1][0] - self.points[0][0] < self.CAL_MIN_SPAN:
            return
        n = len(self.points)
        mx = sum(x for x, _ in self.points) / n
        my = sum(y for _, y in self.points) / n
        sxx = sum((x - mx) ** 2 for x, _ in self.points)
        slope = sum((x - mx) * (y - my) for x, y in self.points) / sxx
        residuals = sum((y - my - slope * (x - mx)) ** 2 for x, y in self.points)
        slope_err = (residuals / (n - 2) / sxx) ** 0.5
        measured = self.ratio_ref / (1.0 - slope)
        if not 0.99 < measured < 1.01:
            return
        BROADCASTER.clock_ratio = measured
        # Retime the running stream only by a correction well beyond the estimate's noise
        if abs(measured - timeline.clock_ratio) > max(2e-5, 3 * slope_err):
            timeline.set(clock_ratio=measured)

    def publish(self, delay, catching_up=False):
        shown = None if delay is None else (round(delay, 1), catching_up)
        if shown == self.shown:
            return
        self.shown = shown
        if BROADCASTER.delay_callback:
            try:
                BROADCASTER.delay_callback(delay, catching_up)
            except Exception:
                pass

    def resync(self, excess):
        with self.lock:
            control_url, stream_url = self.control_url, self.stream_url
        if not control_url:
            return
        BROADCASTER.notify_status(f"  [Delay] The TV fell {excess:.1f} s further behind, resyncing...")
        # Same cache-busting ?t= as the GUI's pushes
        stream_url = re.sub(r"([?&]t=)\d+", lambda m: m.group(1) + str(int(time.time())), stream_url)
        push_stream_to_tv_dlna(control_url, stream_url)

    def run(self):
        while RUNNING:
            # Irregular polling, so the samples hit every fraction of RelTime's whole second
            time.sleep(random.uniform(0.5, 0.9))
            with self.lock:
                control_url = self.control_url
            start = BROADCASTER.playback_start(urlparse(control_url).hostname) if control_url else None
            if start is None:
                with self.lock:
                    self.reset()
                self.publish(None)
                continue
            try:
                t_sent = time.perf_counter()
                rel = parse_rel_time(
                    avtransport_soap(control_url, "GetPositionInfo", "<InstanceID>0</InstanceID>", timeout=2)
                )
                t = (t_sent + time.perf_counter()) / 2
            except Exception:
                continue
            with self.lock:
                if control_url != self.control_url:
                    continue
                excess = self.update(start, t, rel)
                delay, catching_up = self.delay, self.catching_up
            self.publish(delay, catching_up)
            if excess is not None and excess > self.RESYNC_EXCESS and t - self.last_resync > self.RESYNC_INTERVAL:
                self.last_resync = t
                self.resync(excess)


DELAY_MONITOR = DelayMonitor()


# ==============================================================================
# HTTP server handling both the DLNA MediaServer and the MPEG-TS stream
# ==============================================================================
BASE_HTTP_URL = "http://192.168.0.85:8000"


class DLNAHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Silence polling from a stale browser tab (/frame.jpg)
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

            # Disable Nagle's algorithm (TCP_NODELAY) to send packets in < 1 ms
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            except Exception:
                pass

            self.send_response(200)
            self.send_dlna_stream_headers()
            self.end_headers()

            client_q = BROADCASTER.add_client()
            client_ip = self.client_address[0]
            print(
                f"  [STREAM] TV ({self.address_string()}) is receiving: "
                f"Monitor {req_mon}, {req_h}p @ 60 FPS + Audio!"
            )
            t_keyframe = None
            try:
                while RUNNING:
                    t_read, chunk = client_q.get(timeout=5.0)
                    if t_keyframe is None:
                        # Start at a keyframe: a clean decoder start, and the TV's playback position
                        # then counts from a known capture time (DelayMonitor)
                        start = find_keyframe_start(chunk)
                        if start < 0:
                            continue
                        chunk = chunk[start:]
                        t_keyframe = t_read
                        BROADCASTER.start_playback(client_ip, t_keyframe)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, socket.error, queue.Empty):
                pass
            finally:
                BROADCASTER.remove_client(client_q)
                BROADCASTER.end_playback(client_ip, t_keyframe)
                print(f"  [STREAM] TV ({self.address_string()}) stopped playback.")
            return

        # Silence stale /frame.jpg requests from a previously open browser tab
        if self.path.startswith("/frame.jpg"):
            self.send_response(204)
            self.end_headers()
            return

        # Info page on port 8000
        info_html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>LG 55LM670S DLNA Streamer</title>
<style>body{{background:#0d1117;color:#fff;font-family:Arial,sans-serif;padding:40px;font-size:22px;}}
h1{{color:#58a6ff;}} li{{margin:14px 0;}}</style></head>
<body>
<h1>RTX 3070 Ti hardware stream (60 FPS + Audio) is ACTIVE!</h1>
<p>To get full 60 FPS and stereo audio through the TV's hardware decoder (instead of the slow web browser):</p>
<ol>
  <li>Press <b>EXIT</b> on the remote to close the web browser.</li>
  <li>Press <b>INPUT</b> or open <b>Smart Share</b> on the TV remote.</li>
  <li>Pick the <b>{FRIENDLY_NAME}</b> server and select <b>1. Monitor 1 (60 FPS + Audio)</b>!</li>
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
        help="Video encoder (default: auto - detects NVIDIA/AMD/Intel/CPU)",
    )
    parser.add_argument("--tv-ip", type=str, default="192.168.0.114", help="Optional TV IP address")
    args = parser.parse_args()

    renderers = discover_all_dlna_renderers(hint_ip=args.tv_ip)
    selected_idx = 0
    if renderers:
        tv_name, ctrl_url, tv_ip = renderers[0]
    else:
        tv_name, ctrl_url, tv_ip = ("(none)", None, args.tv_ip)

    local_ip = get_preferred_ip(tv_ip or "8.8.8.8")
    BASE_HTTP_URL = f"http://{local_ip}:{args.port}"
    stream_url = f"{BASE_HTTP_URL}/live/mon{args.monitor}_{args.height}.mpg"

    print("=" * 78)
    print("  Universal DLNA H.264 Streamer (NVIDIA / AMD / Intel / CPU 60FPS) + Audio")
    print("=" * 78)
    print(f"  PC server address                     :  {BASE_HTTP_URL}")
    print(f"  MPEG-TS stream                        :  {stream_url}")
    if renderers:
        print("  DLNA TVs found on the LAN             :")
        for idx, (r_name, r_ctrl, r_ip) in enumerate(renderers):
            marker = "-->" if idx == selected_idx else "   "
            print(f"   {marker} [{idx + 1}] {r_name} ({r_ip})")
    else:
        print("  DLNA TVs found                        :  None (make sure the TV is on)")
    print("-" * 78)
    print("  CONTROLS IN THIS CONSOLE WINDOW (TV on a regular channel/HDMI, after pressing EXIT):")
    print("    [ENTER]     -> Start / refresh the current stream on the TV")
    print("    [1] + ENTER -> Monitor 1 : Full HD 1080p @ 60 FPS + Audio")
    print("    [2] + ENTER -> Monitor 1 : HD 720p Ultra-Low-Lag @ 60 FPS + Audio")
    print("    [3] + ENTER -> Monitor 2 : Full HD 1080p @ 60 FPS + Audio")
    print("    [4] + ENTER -> Monitor 2 : HD 720p Ultra-Low-Lag @ 60 FPS + Audio")
    if len(renderers) > 1:
        print("    [t] + ENTER -> Switch the target DLNA TV to the next one on the list")
    print("=" * 78)

    # 1. Start the single WASAPI loopback thread and the TV delay monitor
    threading.Thread(target=global_wasapi_capture_thread, daemon=True).start()
    threading.Thread(target=DELAY_MONITOR.run, daemon=True).start()

    # 2. Start the FFmpeg encoder (GPU/CPU auto-detection)
    threading.Thread(
        target=ffmpeg_producer_loop,
        args=(args.monitor, args.fps, args.height, args.bitrate, args.encoder),
        daemon=True,
    ).start()

    # 3. Start the HTTP/DLNA server
    server = ThreadingHTTPServer(("0.0.0.0", args.port), DLNAHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 4. Start UPnP SSDP announcements
    threading.Thread(
        target=ssdp_Responder_and_announcer,
        args=(local_ip, args.port, tv_ip),
        daemon=True,
    ).start()

    # 5. Try an automatic DLNA AVTransport push after 2 seconds
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
                print(f"  [TV] Target renderer switched to: {tv_name} ({tv_ip})")

            if not ctrl_url:
                renderers = discover_all_dlna_renderers(hint_ip=args.tv_ip)
                if renderers:
                    tv_name, ctrl_url, tv_ip = renderers[selected_idx % len(renderers)]

            if ctrl_url:
                push_stream_to_tv_dlna(ctrl_url, stream_url)
            else:
                print("  [!] No TV AVTransport service found on the network.")
    except (KeyboardInterrupt, EOFError):
        print("\n[!] Stopping server...")
        RUNNING = False
        server.shutdown()


if __name__ == "__main__":
    main()
