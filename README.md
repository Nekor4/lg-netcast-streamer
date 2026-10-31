# LG NetCast 3.0 (55LM670S-ZA) Hardware Screen & Audio Streamer

Low-latency streamer of the Windows desktop (**Full HD 1080p @ 60 FPS**) and system audio (**WASAPI Loopback Stereo**) for older **LG Smart TVs running NetCast 3.0 (e.g. the 2012 LM670S series)**.

Instead of the slow built-in WebKit browser, the project uses the TV's **native hardware H.264 decoder + audio ASIC** over **UPnP / DLNA (`AVTransport1` push and a `ContentDirectory` MediaServer)**.

---

## Key features

- **60 FPS capture via DirectX 11 (`ddagrab`):** Grabs frames straight from the DWM compositor on Windows 10/11 (with automatic fallback to `gdigrab`).
- **NVIDIA NVENC hardware encoding (`h264_nvenc`):**
  - `High` profile in *Ultra-Low-Latency* mode (`-preset p1 -tune ull -zerolatency 1 -delay 0 -bf 0`).
  - Rewritten stream metadata (`h264_metadata=level=4.1`) that lets older 2012 hardware decoders requiring the `Level 4.1` flag play **Full HD 1080p @ 60 FPS**.
  - `SPS`/`PPS`/`AUD` headers repeated on every keyframe (`dump_extra=freq=keyframe`).
- **Windows system audio capture (`PyAudioWPatch` / WASAPI Loopback):**
  - Sends audio straight from the default speakers/headphones, with no virtual audio cables to install.
  - Generates silence frames automatically when nothing is playing, keeping the MPEG-TS stream continuous.
- **Control from the PC console:** Switch monitors (`Monitor 1` / `Monitor 2`) and resolution (`1080p60` / `720p60`) on the fly with keys `1`–`4`.

---

## Requirements

- Windows 10 / 11 with an NVIDIA GPU supporting `h264_nvenc`
- Python 3.10+
- Packages from `requirements.txt`:
  ```bash
  pip install -r requirements.txt
  ```

---

## Running

### 1. Window GUI – recommended (no console window in the background)
Double-click **`LG_Streamer.pyw`** or **`uruchom_stream.bat`**.
An ultra-lightweight dark app window opens (fully compatible with **PowerToys FancyZones** snapping, with no console in the background) that lets you, with one click:
- pick a DLNA TV from the list of renderers found on the LAN,
- switch the monitor on the fly (`Ekran 1` / `Ekran 2` / `Oba`, or keys `F1` / `F2` / `F3`),
- change the resolution (`1080p Full HD` / `720p Szybki`), frame rate (`60 FPS` / `30 FPS`), bitrate and GPU encoder (`NVIDIA NVENC` / `AMD AMF` / `Intel QuickSync` / `CPU`),
- start (`▶ WŁĄCZ / PRZEŁĄCZ NA TV`) and stop (`⏹ STOP`) the stream.

### 2. Console mode (CLI)
Double-click **`uruchom_konsole.bat`** or run from a terminal:

```bash
python netcast_hw_streamer.py --port 8000 --monitor 1 --fps 60 --height 1080 --bitrate 6M --encoder auto
```

> **Tip:** Make sure the TV is on a regular TV channel or an HDMI input (leave the Smart Home / browser menu with the `EXIT` button on the remote) so the TV accepts the stream automatically via `AVTransport1`.

### Console window shortcuts:
- **`ENTER`** – Start / refresh the current stream on the TV
- **`1` + `ENTER`** – Monitor 1: Full HD `1080p @ 60 FPS` + audio
- **`2` + `ENTER`** – Monitor 1: HD `720p Ultra-Low-Lag @ 60 FPS` + audio
- **`3` + `ENTER`** – Monitor 2: Full HD `1080p @ 60 FPS` + audio
- **`4` + `ENTER`** – Monitor 2: HD `720p Ultra-Low-Lag @ 60 FPS` + audio
