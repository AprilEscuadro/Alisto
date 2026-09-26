"""Loads enrolled word/phrase templates, grouped by label, computing MFCC once
at startup.

Templates live under TEMPLATES_DIR/<GROUP>/<GROUP>_NN.wav -- e.g.
templates/WAKE/WAKE_01.wav, templates/SUNOG/SUNOG_01.wav. Each GROUP is
either the wake word or one keyword; multiple groups can map to the same
emergency category (see keyword_map.py).
"""

import glob
import os

import audio_io
import config
import features


def _group_dir(templates_dir, group):
    return os.path.join(templates_dir, group)


def list_groups(templates_dir):
    if not os.path.isdir(templates_dir):
        return []
    return sorted(
        d for d in os.listdir(templates_dir)
        if os.path.isdir(os.path.join(templates_dir, d))
    )


def load_group(templates_dir, group):
    """Returns {sample_name: mfcc} for one group (e.g. 'WAKE' or 'SUNOG').

    Templates shorter than MIN_TEMPLATE_FRAMES are skipped: very short
    recordings carry little distinguishing content and repeatedly turned out
    to act as "magnets" for short noise bursts (a tap, a click) during
    testing, causing false matches. Longer samples from the same enrollment
    session are kept and remain plenty for robust matching.
    """
    paths = sorted(glob.glob(os.path.join(_group_dir(templates_dir, group), "*.wav")))
    templates = {}
    for path in paths:
        name = os.path.splitext(os.path.basename(path))[0]
        audio = audio_io.load_wav(path)
        mfcc = features.compute_mfcc(audio)
        if len(mfcc) < config.MIN_TEMPLATE_FRAMES:
            continue
        templates[name] = mfcc
    return templates


def load_all_groups(templates_dir):
    """Returns {group_name: {sample_name: mfcc}} for every enrolled group."""
    groups = list_groups(templates_dir)
    if not groups:
        raise RuntimeError(
            f"No template groups found under {templates_dir}/. Run enroll.py first."
        )
    result = {}
    for group in groups:
        templates = load_group(templates_dir, group)
        if templates:
            result[group] = templates
    if not result:
        raise RuntimeError(
            f"Template group folders exist under {templates_dir}/ but contain no .wav files."
        )
    return result
