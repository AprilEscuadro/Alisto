"""Two calibration modes:

1. `python calibrate.py` (no args) — auto-computes a suggested
   DETECTION_THRESHOLD from ALL enrolled groups (wake word + every keyword),
   using leave-one-out cross-validation within each group. Matches the same
   k-nearest-neighbor group-average scoring used at runtime (see
   dtw.group_match), not a raw single-template distance.

2. `python calibrate.py --live` — the old interactive mode: listens
   continuously and prints the group-match distance for every utterance
   heard, useful for checking real-world non-match behavior (background
   noise, other words) against the auto-computed threshold.
"""

import sys

import config
import dtw
import keyword_map
import templates_lib

K = 3  # must match the k used by dtw.group_match at runtime


def _active_groups():
    """Only the groups actually used by the current system (WAKE + whatever
    is in keyword_map.py) -- old/dropped keyword enrollments left on disk
    are ignored so they don't skew calibration or show up in live testing."""
    groups = templates_lib.load_all_groups(config.TEMPLATES_DIR)
    active_names = {keyword_map.WAKE_GROUP} | set(keyword_map.KEYWORD_TO_CATEGORY)
    return {g: t for g, t in groups.items() if g in active_names}


def auto_threshold():
    groups = _active_groups()

    all_scores = []
    print(f"Loaded {len(groups)} group(s): {', '.join(groups)}\n")

    for group, templates in groups.items():
        names = list(templates.keys())
        if len(names) < 2:
            print(f"  {group}: only {len(names)} sample(s), need >=2 to calibrate -- skipped")
            continue

        # Precompute all pairwise distances within this group once.
        pair_dist = {}
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                d = dtw.dtw_distance(
                    templates[names[i]], templates[names[j]], radius=config.DTW_BAND_RADIUS
                )
                pair_dist[(names[i], names[j])] = d
                pair_dist[(names[j], names[i])] = d

        # Leave-one-out, k-NN group-average: for each template, treat it as
        # a stand-in for a genuine new utterance and score it against the
        # REST of its own group the same way group_match() would at runtime.
        print(f"  {group}:")
        for held_out in names:
            others = [n for n in names if n != held_out]
            dists = sorted(pair_dist[(held_out, o)] for o in others)
            k_use = min(K, len(dists))
            avg = sum(dists[:k_use]) / k_use
            all_scores.append(avg)
            print(f"    {held_out} vs rest of group (k={k_use} avg): {avg:.2f}")

    if not all_scores:
        print("Not enough samples anywhere to calibrate. Enroll more with enroll.py.")
        return

    worst = max(all_scores)
    mean = sum(all_scores) / len(all_scores)
    suggested = worst * 1.15  # safety margin above the worst same-word score seen

    print(f"\nGroup-average scores across all groups: mean={mean:.2f}  worst={worst:.2f}")
    print(f"Suggested DETECTION_THRESHOLD: {suggested:.2f}")
    print(f"(currently set to {config.DETECTION_THRESHOLD} in config.py)")

    answer = input("\nWrite this into config.py now? [y/N] ").strip().lower()
    if answer == "y":
        _write_threshold(suggested)
        print(f"config.py updated: DETECTION_THRESHOLD = {suggested:.2f}")
    else:
        print("Not written. Edit config.py manually if you want to apply it.")

    print(
        "\nNote: this threshold is based only on your own templates matching "
        "each other WITHIN their own group. It does NOT verify that a "
        "different group/word scores higher -- run 'python calibrate.py "
        "--live' and say a mix of different keywords / other words / stay "
        "silent to confirm real-world separation."
    )


def _write_threshold(value):
    with open("config.py") as f:
        lines = f.readlines()
    with open("config.py", "w") as f:
        for line in lines:
            if line.startswith("DETECTION_THRESHOLD"):
                f.write(f"DETECTION_THRESHOLD = {value:.2f}\n")
            else:
                f.write(line)


def live_mode():
    import audio_io
    import features
    import vad

    groups = _active_groups()
    print(f"Loaded {len(groups)} group(s): {', '.join(groups)}. Listening... (Ctrl+C to stop)\n")

    segmenter = vad.Segmenter()
    for frame in audio_io.frame_stream():
        utterance = segmenter.push(frame)
        if utterance is None:
            continue
        query_mfcc = features.compute_mfcc(utterance)
        group, dist, all_dists = dtw.group_match(query_mfcc, groups, k=K)
        duration_s = len(utterance) / config.SAMPLE_RATE
        verdict = f"MATCH ({group})" if dist < config.DETECTION_THRESHOLD else "no match"
        ranked = sorted(all_dists.items(), key=lambda kv: kv[1])[:3]
        ranked_str = ", ".join(f"{g}={d:.2f}" for g, d in ranked)
        print(f"utterance={duration_s:.2f}s  best={group}  distance={dist:.2f}  [{verdict}]  (top3: {ranked_str})")


if __name__ == "__main__":
    try:
        if "--live" in sys.argv:
            live_mode()
        else:
            auto_threshold()
    except KeyboardInterrupt:
        pass
