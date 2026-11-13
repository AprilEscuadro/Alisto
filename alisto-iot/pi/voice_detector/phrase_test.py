"""Says-and-checks test for the emergency PHRASES, per category.

After changing the whisper model (tiny -> base) the spelling whisper produces
changes, so the keyword lists in categorize.py may be missing new variants.
This reads a phrase list, asks you to say each phrase, runs the EXACT live
pipeline (high-pass, trim, normalize, whisper_asr.transcribe, has_wake_word,
categorize) and reports, per category, what was heard and whether the right
category came out. Nothing is changed -- it only prints which transcripts
would need a new keyword fragment in categorize.py.

Setup:
    sudo systemctl stop alisto-emergency.service      # the mic must be free
    cd /home/alisto/voice_detector
    python3 phrase_test.py                            # uses phrases.txt here
    python3 phrase_test.py --delay=20                 # time to walk to 3 m
    python3 phrase_test.py --only=FIRE,INTRUDER       # just some categories
    python3 phrase_test.py --repeat=2                 # say each phrase twice
    python3 phrase_test.py --file=my_phrases.txt
    sudo systemctl start alisto-emergency.service     # when finished!

Phrase file: one phrase per line as  CATEGORY|what to say   (# = comment).
Categories are the ones in categorize.py: FIRE, INTRUDER, MEDICATION,
FALL_INJURY, MEDICAL_SYMPTOM, GENERAL_HELP.
"""

import os
import sys
import time

import audio_io
import categorize
import vad
import whisper_asr
from ab_test_whisper import arg_value, trim_to_speech

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phrases.txt")
TAKE_SECONDS = 4.0
TMP_WAV = "/tmp/phrase_test.wav"


def parse_phrases(text):
    """'CATEGORY|phrase' lines -> [(category, phrase)]; skips blanks/comments
    and anything with an unknown category (reported by the caller)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        category, phrase = line.split("|", 1)
        out.append((category.strip().upper(), phrase.strip()))
    return out


def judge(expected, transcript):
    """('OK' | 'WAKE' | 'MISS', category_or_dash) for one transcript.
    OK   = wake word heard AND the right category;
    WAKE = emergency would trigger but with another category (e.g. UNKNOWN);
    MISS = no wake word, so nothing would trigger at all."""
    if not whisper_asr.has_wake_word(transcript):
        return "MISS", "-"
    got = categorize.categorize(transcript)[0]
    return ("OK" if got == expected else "WAKE"), got


def say_and_transcribe(i, n, category, phrase):
    print(f"\n[{i}/{n}] {category}: say  \"{phrase}\"")
    for c in (2, 1):
        print(f"   in {c}...", end="\r", flush=True)
        time.sleep(1)
    print("   SAY IT NOW   ")
    # Terminal bell: lets you hear when to speak when you are too far from the
    # screen to read it (a beep from the laptop's terminal).
    sys.stdout.write("\a")
    sys.stdout.flush()
    raw = audio_io.record_seconds(TAKE_SECONDS)
    y = vad.HighPass().process(raw)
    y = vad.normalize_level(trim_to_speech(y))
    audio_io.save_wav(TMP_WAV, y)
    return whisper_asr.transcribe(TMP_WAV)


def main():
    path = arg_value("file", DEFAULT_FILE)
    if not os.path.exists(path):
        print(f"Phrase file not found: {path}")
        return 1
    with open(path, encoding="utf-8") as f:
        phrases = parse_phrases(f.read())
    known = {c for c, _ in categorize._CATEGORY_KEYWORDS}
    bad = sorted({c for c, _ in phrases} - known)
    if bad:
        print(f"Unknown categories in {path}: {', '.join(bad)} (known: {', '.join(sorted(known))})")
        return 1
    only = arg_value("only")
    if only:
        wanted = {c.strip().upper() for c in only.split(",")}
        phrases = [(c, p) for c, p in phrases if c in wanted]
    if not phrases:
        print("No phrases to test.")
        return 1
    # --repeat=N: say every phrase N times (one after another), for more
    # samples of how whisper spells the same words.
    phrases = phrases * max(1, int(arg_value("repeat", "1")))

    # Print the whole list first, so it can be read (or memorised) before
    # walking away from the screen. One take is ~10 s: 2 s countdown, 4 s
    # recording, then ~4 s while whisper works -- speak at each beep.
    print(f"{len(phrases)} takes. In order:")
    for i, (category, phrase) in enumerate(phrases, 1):
        print(f"  {i:>2}. [{category}] {phrase}")
    print()

    for left in range(int(arg_value("delay", "0")), 0, -1):
        print(f"  walk to your spot... starting in {left:2d} s", end="\r", flush=True)
        time.sleep(1)

    results = []  # (category, phrase, transcript, status, got)
    for i, (category, phrase) in enumerate(phrases, 1):
        text = say_and_transcribe(i, len(phrases), category, phrase)
        status, got = judge(category, text)
        results.append((category, phrase, text, status, got))
        print(f"   heard: {text!r}")
        print(f"   -> {status}" + (f" (category {got})" if status != "MISS" else ""))

    print("\n=== SUMMARY per category (OK = wake word + right category)")
    print(f"{'category':<16} {'phrases':>7} {'OK':>4} {'other cat':>10} {'MISS':>5}")
    order = [c for c, _ in categorize._CATEGORY_KEYWORDS]
    for category in order:
        rows = [r for r in results if r[0] == category]
        if not rows:
            continue
        ok = sum(r[3] == "OK" for r in rows)
        wake = sum(r[3] == "WAKE" for r in rows)
        miss = sum(r[3] == "MISS" for r in rows)
        print(f"{category:<16} {len(rows):>7} {ok:>4} {wake:>10} {miss:>5}")

    fixable = [r for r in results if r[3] == "WAKE"]
    if fixable:
        print("\n=== Triggered, but with another category -- a keyword fragment "
              "for the intended word would fix these:")
        for category, phrase, text, _, got in fixable:
            print(f"  wanted {category:<15} got {got:<15} heard {text!r}   (said: {phrase})")
    missed = [r for r in results if r[3] == "MISS"]
    if missed:
        print("\n=== No wake word heard (would NOT trigger):")
        for category, phrase, text, _, _ in missed:
            print(f"  heard {text!r}   (said: {phrase})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
