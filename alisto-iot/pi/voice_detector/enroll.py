"""Record reference samples for the wake word or one keyword.

Run once per group during setup (and again if you change speakers/mic
placement). Aim for 5-8 samples per group, ideally from each family member
who might use the device.

Each recording is checked with the same VAD used at runtime: if no clear
speech utterance is found in it (too quiet, mistimed, or silent), you're
asked to redo that sample immediately instead of finding out later during
calibration.

Usage:
    python enroll.py WAKE      # the wake word ("Alisto")
    python enroll.py SUNOG     # one keyword (see keyword_map.py for the full list)
"""

import os
import sys
import time

import numpy as np

import audio_io
import config
import keyword_map
import vad

RECORD_SECONDS = 2.5


def _extract_utterance(audio):
    """Returns (ok, reason, utterance). `utterance` is just the isolated
    detected speech (not the whole fixed-length recording) -- this is what
    actually gets saved, so templates are the same kind of short, tightly
    trimmed clip that real-time matching produces, instead of being padded
    with 1-2s of leading/trailing silence that dilutes and slows matching."""
    frame_samples = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)
    segmenter = vad.Segmenter(require_loud=False)
    found_utterance = None

    num_frames = len(audio) // frame_samples
    for i in range(num_frames):
        frame = audio[i * frame_samples : (i + 1) * frame_samples]
        result = segmenter.push(frame)
        if result is not None:
            found_utterance = result
            break

    if found_utterance is None:
        # flush: feed a little trailing silence so a still-open utterance
        # (recording cut off mid-speech) gets forced closed and reported
        silence = np.zeros(frame_samples, dtype=np.int16)
        for _ in range(20):
            result = segmenter.push(silence)
            if result is not None:
                found_utterance = result
                break

    if found_utterance is None:
        return False, "no speech detected at all (too quiet, or you spoke before/after the window)", None

    duration_ms = len(found_utterance) / config.SAMPLE_RATE * 1000
    if duration_ms < config.MIN_UTTERANCE_MS:
        return False, f"detected speech was too short ({duration_ms:.0f}ms)", None

    return True, f"detected {duration_ms:.0f}ms of speech", found_utterance


def main():
    if len(sys.argv) != 2:
        valid = [keyword_map.WAKE_GROUP] + sorted(keyword_map.KEYWORD_TO_CATEGORY)
        print("Usage: python enroll.py <GROUP>")
        print("Valid groups:", ", ".join(valid))
        sys.exit(1)

    group = sys.argv[1].upper()
    valid_groups = {keyword_map.WAKE_GROUP} | set(keyword_map.KEYWORD_TO_CATEGORY)
    if group not in valid_groups:
        print(f"Unknown group '{group}'. Valid groups: {', '.join(sorted(valid_groups))}")
        sys.exit(1)

    group_dir = os.path.join(config.TEMPLATES_DIR, group)
    os.makedirs(group_dir, exist_ok=True)
    existing = [f for f in os.listdir(group_dir) if f.endswith(".wav")]
    start_index = len(existing) + 1

    what = "the wake word" if group == keyword_map.WAKE_GROUP else f"the keyword '{group}'"
    print(f"Enrolling {what}")
    print("Press Enter, then say it right after the beep. Ctrl+C to stop.\n")

    index = start_index
    while True:
        input(f"[{index}] Press Enter when ready...")
        for i in (3, 2, 1):
            print(i)
            time.sleep(0.5)
        print("Speak now!")
        audio = audio_io.record_seconds(RECORD_SECONDS)

        ok, reason, utterance = _extract_utterance(audio)
        if not ok:
            print(f"REDO THIS ONE: {reason}. Try again, speaking clearly right after 'Speak now!'.\n")
            continue

        path = os.path.join(group_dir, f"{group}_{index:02d}.wav")
        audio_io.save_wav(path, utterance)
        print(f"Saved {path} ({reason})\n")
        index += 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDone enrolling.")
