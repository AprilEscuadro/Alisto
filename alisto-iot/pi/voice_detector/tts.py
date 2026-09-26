"""Generates the emergency announcement audio LIVE via Piper TTS, instead of
relying on a pre-recorded file. This means:
  - no permanent storage spent on the announcement -- it's synthesized fresh
    each time into one fixed temp file that gets overwritten on the next
    trigger, not kept around
  - the announcement can say WHICH category of emergency was detected
    (fire/intruder/medication/fall/medical/general), matching categorize.py
    instead of a single generic clip

No Cebuano (or Tagalog) voice exists for Piper, so this speaks English --
kept short and simple enough to still be understandable in the moment.

Voice model: en_US-ryan-low (male voice, per request -- "Alisto" reads as
masculine). Chosen specifically because its native sample rate is 16000 Hz
-- exactly what the ESP32's I2S playback expects, so its raw output can be
streamed straight over serial with zero conversion.

IMPORTANT: uses the piper Python API (PiperVoice), NOT the `piper` CLI.
Real testing showed the CLI takes ~14-16s per call regardless of sentence
length or --output-raw vs --output-file -- that's the ONNX model being
reloaded from disk and re-initialized from scratch on every single
invocation. The Python API loads the model ONCE (paid at startup via
preload(), not on the first real emergency) and keeps it in memory for the
life of the process, so each actual announcement only costs real synthesis
time (a fraction of a second for a short sentence), not a full model load.
"""

import os

import numpy as np
from piper import PiperVoice, SynthesisConfig

PIPER_MODEL = "/home/alisto/piper_voices/en_US-ryan-low.onnx"  # male voice
OUTPUT_RAW = "/home/alisto/alisto_audio/emergency_announce.raw"
REMINDER_OUTPUT_RAW = "/home/alisto/alisto_audio/reminder_announce.raw"  # separate
# file from OUTPUT_RAW above -- reminders and emergency announcements are
# triggered independently, so using the same path risks one overwriting the
# other mid-stream if they ever land close together in time.
WIFI_OUTPUT_RAW = "/home/alisto/alisto_audio/wifi_announce.raw"  # own file,
# same reasoning -- a WiFi-connected announcement could in principle land
# close in time to an emergency or reminder, so it gets its own path.
PAIRED_OUTPUT_RAW = "/home/alisto/alisto_audio/paired_announce.raw"  # own
# file too, same reasoning as above.

# volume > 1.0 boosts loudness, but normalize_audio=True already maxes
# peaks to full scale by default -- pushing volume much past ~1.6-1.7 just
# clips the waveform (the "basag"/harsh distortion real testing kept
# hearing) instead of getting meaningfully louder. 1.6 is the practical
# ceiling on the software side. Combined with the MAX98357A GAIN pin now
# wired to VIN (the loudest of its 3 simple settings: floating/GND/VIN),
# this is close to the maximum achievable without physically changing the
# speaker -- real testing confirmed the speaker itself (3W, 4 ohm, a small
# unit) is the actual ceiling on absolute loudness at this point, not the
# amp or software settings.
SYN_CONFIG = SynthesisConfig(volume=1.6)

ANNOUNCEMENTS = {
    "FIRE": "Fire emergency detected. Sending SMS in 5 seconds.",
    "INTRUDER": "Intruder emergency detected. Sending SMS in 5 seconds.",
    "MEDICATION": "Medication emergency detected. Sending SMS in 5 seconds.",
    "FALL_INJURY": "Fall or injury detected. Sending SMS in 5 seconds.",
    "MEDICAL_SYMPTOM": "Medical emergency detected. Sending SMS in 5 seconds.",
    "GENERAL_HELP": "Emergency detected. Sending SMS in 5 seconds.",
    "UNKNOWN": "Emergency detected. Sending SMS in 5 seconds.",
}

_voice = None  # loaded lazily (or via preload()), kept for the process's life


def _get_voice():
    global _voice
    if _voice is None:
        print("Loading Piper TTS voice model (one-time cost)...")
        _voice = PiperVoice.load(PIPER_MODEL)
        print("Piper TTS voice model loaded.")
    return _voice


def preload():
    """Call once at startup so the ~14s model-load cost is paid before the
    first real emergency, not during it."""
    _get_voice()


FADE_MS = 15  # short enough to be inaudible as an effect of its own, long
# enough to remove the instant jump between full-volume speech and true
# digital silence that a repeated reminder needs between repeats -- that
# jump was audible as a "knocking"/popping sound on the MAX98357A amp each
# time a repeat started or ended. The single-shot emergency announcement
# (repeats=1, no gap) never hit this, which is why only reminders "knocked."
FADE_SAMPLES = int(16000 * FADE_MS / 1000)


def _apply_fade(pcm_bytes, fade_in, fade_out):
    """Linear-ramps the first/last FADE_SAMPLES of one utterance's raw
    16-bit mono PCM to/from silence, softening the edge against the
    silence gap on either side (see FADE_MS's doc comment)."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).copy()
    count = min(FADE_SAMPLES, len(samples))
    if count <= 0:
        return pcm_bytes
    ramp = np.linspace(0.0, 1.0, count, endpoint=False, dtype=np.float32)
    if fade_in:
        samples[:count] = (samples[:count].astype(np.float32) * ramp).astype(np.int16)
    if fade_out:
        samples[-count:] = (samples[-count:].astype(np.float32) * ramp[::-1]).astype(np.int16)
    return samples.tobytes()


def _synthesize_to_file(text, output_path, repeats=1, gap_s=0.7):
    """Shared synthesis helper -- writes raw 16kHz/16-bit/mono PCM (the
    ESP32's exact I2S format, no WAV header) to `output_path`. Returns
    output_path on success, None on failure -- callers already handle a
    missing/unavailable audio file gracefully (skip the announcement,
    continue with whatever flow they're in), so a TTS failure is never
    allowed to block anything.

    repeats > 1 says `text` multiple times with `gap_s` of silence between
    each -- for reminders that should behave like an alarm (repeat until
    noticed) rather than a one-shot announcement. Silence for 16-bit PCM is
    just zero bytes, so no separate silence-generation step is needed --
    but each repeat's own edges get a short fade (see _apply_fade()) so
    that silence starts and ends smoothly instead of clicking."""
    try:
        voice = _get_voice()
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        gap_bytes = bytes(int(gap_s * 16000) * 2)  # 16-bit mono -> 2 bytes/sample
        with open(output_path, "wb") as out:
            for i in range(repeats):
                utterance = b"".join(
                    chunk.audio_int16_bytes
                    for chunk in voice.synthesize(text, syn_config=SYN_CONFIG)
                )
                if repeats > 1:
                    utterance = _apply_fade(
                        utterance, fade_in=(i > 0), fade_out=(i < repeats - 1))
                out.write(utterance)
                if i < repeats - 1:
                    out.write(gap_bytes)
        return output_path
    except Exception as e:
        print(f"piper TTS failed: {e}")
        return None


def synthesize_announcement(category):
    """Generates the spoken emergency announcement for `category`. Said
    once -- it's immediately followed by the 5-second cancel countdown,
    which already gives the listener something to notice/react to."""
    text = ANNOUNCEMENTS.get(category, ANNOUNCEMENTS["UNKNOWN"])
    return _synthesize_to_file(text, OUTPUT_RAW)


def synthesize_reminder(medicine, dosage=None):
    """Generates a spoken medicine reminder naming the medicine (and dosage,
    if given -- the app's medication_reminder schema doesn't actually store
    one, so this is normally called without it), repeated 3 times like an
    alarm -- nothing else in the flow demands attention the way the
    emergency announcement's countdown does, so the reminder needs to earn
    that attention itself.

    Real testing found this repeats>1 path (the only one that ever
    differed from the emergency announcement's repeats=1 path) producing
    garbled/robotic audio, traced to alisto_main.py's stream_audio()
    drifting off its pacing schedule over the longer combined stream
    (fixed now to correct against a monotonic clock instead of a blind
    per-chunk sleep) -- not the audio content generated here, which is why
    reverting to repeats=1 as a stopgap made it clear again."""
    text = f"Time to take your {medicine}, {dosage}." if dosage else f"Time to take your {medicine}."
    return _synthesize_to_file(text, REMINDER_OUTPUT_RAW, repeats=3)


def synthesize_wifi_connected():
    """Said once, right after the app's WiFi setup flow successfully
    connects the Pi to the customer's home network -- the setup process has
    no other way to confirm success back to the person standing next to the
    device (see alisto_main.py's wifi flag-file watcher for how this gets
    triggered)."""
    text = "Connected to your home WiFi. Alisto is now online."
    return _synthesize_to_file(text, WIFI_OUTPUT_RAW)


def synthesize_device_paired(name):
    """Said once, the first time this device is detected as claimed by a
    customer in the app (see firestore_backend.py + alisto_main.py's
    device_pairing_watcher_loop)."""
    text = f"Congratulations {name}, your device is connected successfully."
    return _synthesize_to_file(text, PAIRED_OUTPUT_RAW)
