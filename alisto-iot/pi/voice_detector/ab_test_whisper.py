"""A/B test of whisper models and settings on REAL "Alisto tabang" recordings.

Why: the live log shows whisper tiny mangling the phrase ("alistro tarbam",
"ang listong tapang"), so only ~3-4 of 8 tries trigger. A bigger model (base)
may recognise it better, but on this Pi 4 its encoder is ~2x slower, and
whisper_asr.TIMEOUT_S is 7 s. Lowering --audio-ctx (the live clips are <= 4 s,
so the default 768 = ~15 s of context is mostly padding) cuts encode time.

Both candidates must be judged on the SAME clips, with the service stopped
(so timing is not distorted by the mic loop competing for the CPU):

    sudo systemctl stop alisto-emergency.service
    cd /home/alisto/voice_detector
    python3 ab_test_whisper.py            # record 8 takes, then compare
    python3 ab_test_whisper.py --reuse    # re-test the same clips again
    python3 ab_test_whisper.py --delay=15 # 15 s to walk to your test distance first
    python3 ab_test_whisper.py --only=1,4 # test only settings 1 and 4 of COMBOS
    sudo systemctl start alisto-emergency.service     # when finished!

Stand where the device normally hears you (about 3 m), speak normally.
The clips get the same processing as live: high-pass filter, trim to the
speech, normalize to rms 2000 (see vad.py).
"""

import os
import subprocess
import sys
import time

import numpy as np

import audio_io
import categorize
import config
import vad
import whisper_asr

CLIP_DIR = "/home/alisto/voice_detector/ab_clips"
TAKES = 8
TAKE_SECONDS = 3.0
MODELS_DIR = "/home/alisto/whisper.cpp/models"
OUT_PREFIX = "/tmp/ab_whisper_out"
THREADS = "3"          # same as production (one core stays free for the ESP32 reader)
RUN_TIMEOUT_S = 30     # generous: we want the real time, not a timeout

# (label, model file, --audio-ctx).  The first one is what runs live today.
COMBOS = [
    ("tiny  ctx768 (now)", "ggml-tiny-q5_1.bin", 768),
    ("base  ctx768", "ggml-base-q5_1.bin", 768),
    ("base  ctx384", "ggml-base-q5_1.bin", 384),
    ("base  ctx256", "ggml-base-q5_1.bin", 256),
    ("base  ctx512", "ggml-base-q5_1.bin", 512),
    ("base  ctx640", "ggml-base-q5_1.bin", 640),
]

FRAME = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)


def trim_to_speech(y):
    """Cuts the clip down to the speech (like the live segmenter does), with a
    little padding, so normalize_level() scales the SPEECH, not the silence."""
    n = len(y) // FRAME * FRAME
    if n == 0:
        return y
    frames = y[:n].reshape(-1, FRAME).astype(np.float64)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    # The absolute floor was 150 for the old box (noise floor ~60). In the new
    # quieter box (floor ~40) a 3 m voice can sit below 150, so nothing counted
    # as speech, the clip was left untrimmed and normalized on mostly noise
    # (base then answered '...'). 40 keeps a floor without hiding quiet speech.
    threshold = max(2.5 * float(np.median(rms)), 40.0)
    loud = np.where(rms > threshold)[0]
    if len(loud) == 0:
        return y
    first = max(0, int(loud[0]) - 5)
    last = min(len(rms), int(loud[-1]) + 6)
    return y[first * FRAME:last * FRAME]


def arg_value(name, default=None):
    """--name=value from the command line."""
    prefix = f"--{name}="
    for a in sys.argv[1:]:
        if a.startswith(prefix):
            return a[len(prefix):]
    return default


def record_takes():
    # --delay=N: wait N seconds before the first take, so you can walk to the
    # distance you actually want to test (e.g. 3 m) instead of standing at the
    # keyboard.
    delay = int(arg_value("delay", "0"))
    for left in range(delay, 0, -1):
        print(f"  walk to your spot... starting in {left:2d} s", end="\r", flush=True)
        time.sleep(1)
    if delay:
        print(" " * 50)
    os.makedirs(CLIP_DIR, exist_ok=True)
    for old in os.listdir(CLIP_DIR):
        if old.endswith(".wav"):
            os.remove(os.path.join(CLIP_DIR, old))
    print(f"Recording {TAKES} takes of 'Alisto tabang' ({TAKE_SECONDS:.0f} s each).")
    print("Say it once per take, at your normal distance from the device.\n")
    for i in range(1, TAKES + 1):
        for c in (2, 1):
            print(f"  take {i}/{TAKES} in {c}...", end="\r", flush=True)
            time.sleep(1)
        print(f"  take {i}/{TAKES}: SAY IT NOW          ")
        raw = audio_io.record_seconds(TAKE_SECONDS)
        y = vad.HighPass().process(raw)
        y = vad.normalize_level(trim_to_speech(y))
        audio_io.save_wav(os.path.join(CLIP_DIR, f"take_{i:02d}.wav"), y)
    print("\nRecorded.\n")


def run_whisper(model, ctx, wav):
    txt = OUT_PREFIX + ".txt"
    if os.path.exists(txt):
        os.remove(txt)
    start = time.monotonic()
    try:
        subprocess.run(
            [whisper_asr.WHISPER_BIN, "-m", os.path.join(MODELS_DIR, model),
             "-l", whisper_asr.WHISPER_LANG, "-f", wav, "--audio-ctx", str(ctx),
             "-bs", "1", "-nf", "-t", THREADS, "-nt", "-otxt", "-of", OUT_PREFIX],
            capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "", RUN_TIMEOUT_S
    elapsed = time.monotonic() - start
    try:
        with open(txt) as f:
            text = f.read().strip().lower()
    except OSError:
        text = ""
    if whisper_asr._is_noise_loop(text):
        text = ""
    return text, elapsed


def main():
    if "--reuse" not in sys.argv:
        record_takes()
    clips = sorted(os.path.join(CLIP_DIR, f) for f in os.listdir(CLIP_DIR)
                   if f.endswith(".wav"))
    if not clips:
        print("No clips found. Run without --reuse first.")
        return 1
    # --only=1,4: test just those settings (numbers as in the list below).
    only = arg_value("only")
    combos = COMBOS if not only else [
        COMBOS[int(i) - 1] for i in only.split(",") if i.strip()]
    for label, model, _ in combos:
        if not os.path.exists(os.path.join(MODELS_DIR, model)):
            print(f"Missing model file: {model}")
            return 1

    summary = []
    for label, model, ctx in combos:
        print(f"=== {label}")
        hits, times = 0, []
        for clip in clips:
            text, secs = run_whisper(model, ctx, clip)
            ok = whisper_asr.has_wake_word(text)
            hits += ok
            times.append(secs)
            # Same two steps as live: wake word, then the category keywords
            # from categorize.py (read-only here -- nothing is changed).
            category = categorize.categorize(text)[0] if ok else "-"
            print(f"  {os.path.basename(clip)}  {secs:4.1f}s  "
                  f"{'HIT ' if ok else 'miss'}  {category:<13} {text[:50]!r}")
        summary.append((label, hits, len(clips), float(np.median(times)), max(times)))
        print()

    print("=== SUMMARY (same clips, service stopped)")
    print(f"{'setting':<20} {'hits':>7} {'median s':>9} {'worst s':>8}")
    for label, hits, n, med, worst in summary:
        print(f"{label:<20} {hits:>3}/{n:<3} {med:>9.1f} {worst:>8.1f}")
    print("\nLive code gives up at whisper_asr.TIMEOUT_S = "
          f"{whisper_asr.TIMEOUT_S} s and the mic loop competes for the CPU, so "
          "a setting needs a worst time well under that (about 4.5 s) to be safe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
