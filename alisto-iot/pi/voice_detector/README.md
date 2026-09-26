# ALISTO voice detector (Whisper replacement)

A lightweight, offline keyword-spotter for detecting the phrase "Alisto Tabang"
on a Raspberry Pi 3B+, built from MFCC + Dynamic Time Warping instead of a full
speech-recognition model. No PyTorch, no Whisper.

## Why replace Whisper

Whisper is a general-purpose transcriber. Running it on a Pi 3B+ (no GPU/NPU)
just to detect one fixed phrase is slow (the ~60s delay you saw) and heavy
(PyTorch + a Whisper model can easily use 1.5-2GB, which is most of an 8GB
SD card). This approach only needs NumPy + two small libraries, and detection
latency is roughly "however long it takes you to say the phrase" instead of a
full minute.

## 1. Free up space on the Pi first

```bash
pip uninstall -y openai-whisper torch torchvision torchaudio
pip cache purge
df -h    # confirm space was freed
```

## 2. Install dependencies

```bash
sudo apt-get install -y libportaudio2   # sounddevice needs PortAudio
pip install -r requirements.txt
```

## 3. Enroll the phrase

Record 5-8 reference samples (ideally from each family member who might use
the device):

```bash
python enroll.py
```

Samples are saved as WAV files in `templates/`.

## 4. Calibrate the detection threshold

```bash
python calibrate.py
```

Say the phrase a few times, then say other things (your name, random
sentences, background noise). Watch the printed DTW distances: matches should
cluster low, non-matches should cluster clearly higher. Set
`DETECTION_THRESHOLD` in `config.py` roughly halfway between the two clusters.

## 5. Standalone smoke test (optional)

`detector.py` + `trigger.py` here let you test detection in isolation (prints
"EMERGENCY PHRASE DETECTED" instead of doing anything real):

```bash
python detector.py
```

## 6. Real deployment

The actual production entry point is `../alisto_main.py` (one level up), not
`detector.py`. It imports the pieces from this folder (`vad`, `features`,
`dtw`, `audio_io`, `templates_lib`, `config`) and wires phrase detection into
the real ESP32 button + SMS + audio-announcement flow. See `../DEPLOY.md`.

## Tuning if needed

- **Too many false triggers:** lower `DETECTION_THRESHOLD`, or add more
  enrolled templates covering different tones of voice.
- **Missing real phrases:** raise `DETECTION_THRESHOLD` slightly, or re-record
  templates closer to the mic's real-world position/distance.
- **VAD cutting off too early/late:** adjust `SILENCE_HANGOVER_MS` in
  `config.py`.
- **Background noise triggering VAD constantly:** raise
  `VAD_AGGRESSIVENESS` (max 3).

## How it works

1. `audio_io.py` streams 30ms mono frames from the ReSpeaker mic.
2. `vad.py` (WebRTC VAD) groups frames into utterances — audio bounded by
   silence — so nothing is processed until someone actually finishes speaking.
3. `features.py` computes MFCCs for the utterance by hand (pre-emphasis,
   framing, FFT power spectrum, mel filterbank, log, DCT) — the same features
   full ASR systems use, but here they just get compared, not decoded.
4. `dtw.py` compares the utterance's MFCCs against each enrolled template
   using Dynamic Time Warping (handles the fact that you won't say the phrase
   at exactly the same speed every time), banded for speed.
5. If the best match is under `DETECTION_THRESHOLD`, `detector.py` calls
   `trigger.on_emergency_detected()`.
