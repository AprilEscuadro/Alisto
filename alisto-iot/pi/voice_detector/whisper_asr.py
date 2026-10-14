"""Runs whisper.cpp on a short WAV clip and checks it for the wake word.

Whisper has no native Cebuano support, so the closest available language
(Tagalog, "tl") is used as an approximation -- real testing showed it renders
"Alisto Tabang" as things like "Ali's to tabang" / "alistutan, abang.", not a
perfect transcription. has_wake_word() therefore checks for recognizable
FRAGMENTS of "Alisto" rather than requiring an exact match.

Once the wake word is confirmed, the REST of the transcript is handed to
categorize.categorize() to figure out which of the known emergency phrases
(fire/intruder/medication/fall/medical/general) was actually said -- see that
module for the keyword lists. This is what lets the system recognize many
different real phrasings instead of one fixed "Alisto Tabang" script, now
that we have actual speech-to-text instead of exact-template matching.

--audio-ctx 768 and -bs 1 (greedy decoding, no beam search) cut a 3s clip's
processing time from ~17.6s down to ~4.5s on a Pi 3B+ -- both matter, don't
drop them.
"""

import os
import re
import shutil
import subprocess
import time
from collections import Counter

WHISPER_BIN = "/home/alisto/whisper.cpp/build/bin/whisper-cli"
WHISPER_MODEL = "/home/alisto/whisper.cpp/models/ggml-tiny-q5_1.bin"
WHISPER_LANG = "tl"
OUTPUT_PREFIX = "/home/alisto/voice_detector/whisper_out"
OUTPUT_TXT = OUTPUT_PREFIX + ".txt"
TIMEOUT_S = 7  # successful runs consistently finish in ~4.5-4.7s -- fail fast
               # rather than block listening for a long hang
NUM_THREADS = "3"  # leave 1 of the Pi's 4 cores free for the ESP32 serial
                    # reader / LED blink threads running concurrently

# Real testing showed whisper-cli occasionally hangs completely on some
# utterance (rare, not yet reproduced on demand) instead of its usual ~4-5s.
# alisto_main.py reuses one fixed wav path for every utterance, so the
# offending clip gets overwritten by the NEXT utterance before it can be
# inspected. Save a timestamped copy whenever a timeout happens so the next
# occurrence leaves evidence to actually diagnose.
DEBUG_DIR = "/home/alisto/voice_detector/whisper_timeouts"

# Fragments, not whole words -- "alist" alone already covers "alisto",
# "alista", "alistutan", "alistong", etc. since it's a substring check.
# "aristo"/"alerto" cover plausible ASR mishears (l/r confusion, or Whisper
# latching onto the real Tagalog word "alerto" which sounds similar).
# "ales" -- real test: Whisper heard "Alisto" as "Ales do".
# "alyst" -- real test: Whisper heard "Alisto" as "Alystos".
# "alest" -- real test: Whisper heard "Alisto" as "Alestor".
WAKE_FRAGMENTS = ("alist", "alisto", "alista", "aristo", "alerto", "ales", "alyst", "alest")


def transcribe(wav_path):
    """Runs whisper-cli on wav_path, returns lowercased transcription text
    (empty string on any failure/timeout -- treated as "no match", never
    crashes the caller)."""
    try:
        if os.path.exists(OUTPUT_TXT):
            os.remove(OUTPUT_TXT)
        subprocess.run(
            [
                WHISPER_BIN, "-m", WHISPER_MODEL, "-l", WHISPER_LANG,
                "-f", wav_path, "--audio-ctx", "768", "-bs", "1", "-nf",
                "-t", NUM_THREADS, "-nt", "-otxt", "-of", OUTPUT_PREFIX,
            ],
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
        with open(OUTPUT_TXT) as f:
            text = f.read().strip().lower()
        if _is_noise_loop(text):
            print(f"whisper: repetition loop discarded as noise ({text[:40]!r}...)")
            return ""
        return text
    except subprocess.TimeoutExpired:
        _save_timeout_copy(wav_path)
        print(f"whisper transcription TIMED OUT after {TIMEOUT_S}s -- clip saved for analysis")
        return ""
    except Exception as e:
        print(f"whisper transcription failed: {e}")
        return ""


def _is_noise_loop(text):
    """True for Whisper's classic hallucination on non-speech audio: one
    token repeated over and over until it runs out of decode budget
    ("[song ang ang ang ang ...", real log output on steady background
    noise). Those transcripts are meaningless -- and since the wake-word
    check is a lenient fragment match, a loop that happens to contain
    "ales"/"alest" could in principle raise a false emergency. A loop whose
    repeated word is itself wake-word-like is deliberately NOT discarded,
    so someone genuinely repeating "Alisto, Alisto, Alisto..." in a panic
    still triggers."""
    words = [w.strip("[]().,!?") for w in text.split()]
    words = [w for w in words if w]
    if len(words) < 8:
        return False
    top_word, top_count = Counter(words).most_common(1)[0]
    if top_count / len(words) < 0.6:
        return False
    return not any(frag in top_word for frag in WAKE_FRAGMENTS)


def _save_timeout_copy(wav_path):
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        dest = os.path.join(DEBUG_DIR, f"timeout_{int(time.time())}.wav")
        shutil.copy(wav_path, dest)
        print(f"  -> saved to {dest}")
    except Exception as e:
        print(f"  -> failed to save debug copy: {e}")


def has_wake_word(text):
    # Strip spaces before checking -- real test: Whisper split "Alisto" into
    # separate words ("al is to tabang"), which broke a plain substring
    # check on the raw (spaced) text. Words getting split like this is a
    # recurring Whisper quirk, not a one-off, so fix it structurally here
    # rather than adding more space-containing fragments one at a time.
    # Same idea for punctuation: real test at 3 m heard "ali's to tarbang" --
    # the apostrophe broke "alist" apart. Keep letters only.
    compact = re.sub(r"[^a-z]", "", text.lower())
    return any(frag in compact for frag in WAKE_FRAGMENTS)
