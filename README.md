# LG NetCast 3.0 (55LM670S-ZA) Hardware Screen & Audio Streamer

Niskolatencyjny streamer obrazu pulpitu Windows (**Full HD 1080p @ 60 FPS**) oraz dźwięku systemowego (**WASAPI Loopback Stereo**) przeznaczony dla starszych telewizorów **LG Smart TV z systemem NetCast 3.0 (np. seria LM670S z 2012 r.)**.

Zamiast korzystać z powolnej wbudowanej przeglądarki WebKit, projekt wykorzystuje **natywny sprzętowy dekoder H.264 + Audio ASIC telewizora** poprzez protokół **UPnP / DLNA (`AVTransport1` Push oraz `ContentDirectory` MediaServer)**.

---

## Główne możliwości

- **Przechwytywanie 60 FPS przez DirectX 11 (`ddagrab`):** Pobiera obraz bezpośrednio z kompozytora DWM w systemie Windows 10/11 (z automatycznym fallbackiem do `gdigrab`).
- **Sprzętowe kodowanie NVIDIA NVENC (`h264_nvenc`):**
  - Profil `High` w trybie *Ultra-Low-Latency* (`-preset p1 -tune ull -zerolatency 1 -delay 0 -bf 0`).
  - Przepisanie metadanych strumienia (`h264_metadata=level=4.1`) pozwalające na odtwarzanie **Full HD 1080p @ 60 FPS** na starszych dekoderach sprzętowych z 2012 r. wymagających flagi `Level 4.1`.
  - Powtarzanie nagłówków `SPS`/`PPS`/`AUD` na każdej klatce kluczowej (`dump_extra=freq=keyframe`).
- **Przechwytywanie dźwięku systemowego Windows (`PyAudioWPatch` / WASAPI Loopback):**
  - Przesyła dźwięk prosto z domyślnych głośników/słuchawek bez konieczności instalowania wirtualnych kabli audio.
  - Automatycznie generuje ramki ciszy, gdy na komputerze nic nie gra, utrzymując ciągłość strumienia MPEG-TS.
- **Sterowanie z konsoli PC:** Przełączanie monitorów (`Monitor 1` / `Monitor 2`) oraz rozdzielczości (`1080p60` / `720p60`) w locie za pomocą klawiszy `1`–`4`.

---

## Wymagania

- Windows 10 / 11 z kartą graficzną NVIDIA obsługującą `h264_nvenc`
- Python 3.10+
- Pakiety z `requirements.txt`:
  ```bash
  pip install -r requirements.txt
  ```

---

## Uruchomienie

### 1. Interfejs okienkowy (GUI) – Zalecany (bez okna konsoli w tle)
Kliknij dwukrotnie **`LG_Streamer.pyw`** lub **`uruchom_stream.bat`**.
Otworzy się ultra-lekkie, ciemne okno aplikacji (w 100% kompatybilne z przyciąganiem do stref **PowerToys FancyZones** i bez żadnej konsoli w tle) pozwalające jednym kliknięciem:
- wybrać telewizor DLNA z listy wykrytych odbiorników w sieci LAN,
- przełączać monitor (`Ekran 1` / `Ekran 2` / `Oba` lub klawiszami `F1` / `F2` / `F3`) w locie,
- zmieniać rozdzielczość (`1080p Full HD` / `720p Szybki`), płynność (`60 FPS` / `30 FPS`), bitrate i koder GPU (`NVIDIA NVENC` / `AMD AMF` / `Intel QuickSync` / `CPU`),
- włączać (`▶ WŁĄCZ / PRZEŁĄCZ NA TV`) oraz zatrzymywać (`⏹ STOP`) strumień.

### 2. Tryb konsolowy (CLI)
Kliknij dwukrotnie **`uruchom_konsole.bat`** lub uruchom z terminala:

```bash
python netcast_hw_streamer.py --port 8000 --monitor 1 --fps 60 --height 1080 --bitrate 6M --encoder auto
```

> **Wskazówka:** Upewnij się, że telewizor jest na zwykłym kanale TV lub wejściu HDMI (wyjdź z menu Smart Home / Przeglądarki przyciskiem `EXIT` na pilocie), aby telewizor automatycznie przyjął strumień przez `AVTransport1`.

### Skróty w oknie konsoli:
- **`ENTER`** – Włącz / odśwież aktualny strumień na ekranie TV
- **`1` + `ENTER`** – Monitor 1: Full HD `1080p @ 60 FPS` + Dźwięk
- **`2` + `ENTER`** – Monitor 1: HD `720p Ultra-Low-Lag @ 60 FPS` + Dźwięk
- **`3` + `ENTER`** – Monitor 2: Full HD `1080p @ 60 FPS` + Dźwięk
- **`4` + `ENTER`** – Monitor 2: HD `720p Ultra-Low-Lag @ 60 FPS` + Dźwięk
