"""Main entry point: continuously listens for the ALISTO emergency phrase and
calls trigger.on_emergency_detected() as soon as a match is found.

Latency is roughly: (time you take to say the phrase) + SILENCE_HANGOVER_MS
+ a few tens of ms of MFCC/DTW compute -- not the ~60s Whisper was taking.

Usage:
    python detector.py
"""

import time

import audio_io
import config
import dtw
import features
import templates_lib
import vad
from trigger import on_emergency_detected


def main():
    templates = templates_lib.load_templates()
    print(f"Loaded {len(templates)} template(s) for '{config.PHRASE_LABEL}'.")
    print("Listening...\n")

    segmenter = vad.Segmenter()
    for frame in audio_io.frame_stream():
        utterance = segmenter.push(frame)
        if utterance is None:
            continue

        start = time.monotonic()
        query_mfcc = features.compute_mfcc(utterance)
        name, dist = dtw.best_match(query_mfcc, templates)
        elapsed_ms = (time.monotonic() - start) * 1000

        if dist < config.DETECTION_THRESHOLD:
            print(f"MATCH ({name}, distance={dist:.2f}, compute={elapsed_ms:.0f}ms)")
            on_emergency_detected()
        else:
            print(f"no match (best={name}, distance={dist:.2f})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
