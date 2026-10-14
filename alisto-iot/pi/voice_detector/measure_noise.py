"""Measures what the microphone actually hears, to find out WHERE the
background noise comes from and how far speech stands above it.

Why: the live logs show the ambient floor (~820-1070 RMS) is almost the same
as normal speech (~950-1220), so the loudness gate keeps accepting noise and
every false utterance costs ~5s of whisper time during which the device is
deaf. Whether that noise is acoustic (the room) or electrical (amp / power /
WiFi coupling into the mic, like the speaker "knock" was) decides the fix:
move/shield the mic or lower the gain, versus tuning the software gate.

Run on the Pi with the service STOPPED (it owns the mic):

    sudo systemctl stop alisto-emergency.service
    cd /home/alisto/voice_detector && python3 measure_noise.py
    sudo systemctl start alisto-emergency.service

Three phases, each ~6s: (1) normal room silence, (2) mic covered with a
finger/cloth, (3) you say "Alisto tabang" a few times at your usual distance.
"""

import sys

import numpy as np

import config

SR = config.SAMPLE_RATE
FRAME = int(SR * config.FRAME_MS / 1000)


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


def band_shares(mono):
    """Fraction of the signal's energy in broad frequency bands -- the quickest
    way to tell mains hum (<200 Hz) from broadband hiss from high-frequency
    switching/WiFi whine (>4 kHz)."""
    x = mono.astype(np.float64)
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    total = spec.sum() or 1.0
    bands = [(0, 100), (100, 500), (500, 2000), (2000, 4000), (4000, 8000)]
    return [(lo, hi, float(spec[(freqs >= lo) & (freqs < hi)].sum() / total)) for lo, hi in bands]


def strongest_tones(mono, n=3):
    """The n strongest narrow peaks (Hz) -- a steady tone (50/60 Hz hum, an
    amp or regulator whine) shows up here; plain room noise doesn't."""
    x = mono.astype(np.float64)
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    spec[freqs < 20] = 0
    idx = np.argsort(spec)[::-1]
    picked = []
    for i in idx:
        if all(abs(freqs[i] - f) > 30 for f in picked):
            picked.append(float(freqs[i]))
        if len(picked) == n:
            break
    return picked


def frame_rms(mono, remove_dc=False):
    """Per-frame RMS. remove_dc=True subtracts each frame's own mean first --
    i.e. the AC loudness, with the microphone's constant offset taken out."""
    n = len(mono) // FRAME
    out = []
    for i in range(n):
        f = np.asarray(mono[i * FRAME:(i + 1) * FRAME], dtype=np.float64)
        out.append(rms(f - f.mean()) if remove_dc else rms(f))
    return np.array(out)


def analyse(stereo):
    """stereo: int16 array (samples, channels). Returns a dict of measurements.
    The *_raw numbers are what vad.py measures today (DC offset included);
    the *_ac numbers have the DC offset removed, which is what loudness
    should actually mean."""
    mono = stereo.mean(axis=1) if stereo.ndim > 1 else stereo
    raw = frame_rms(mono)
    ac = frame_rms(mono, remove_dc=True)
    n = len(mono) // FRAME
    frame_means = np.array([float(np.mean(mono[i * FRAME:(i + 1) * FRAME])) for i in range(n)])
    out = {
        "rms": rms(mono),
        "per_channel": [rms(stereo[:, c]) for c in range(stereo.shape[1])] if stereo.ndim > 1 else None,
        "dc": float(mono.mean()),
        "dc_wobble": float(frame_means.std()),   # how much the DC level itself drifts
        "peak": int(np.max(np.abs(mono))),
        "frame_p30": float(np.percentile(raw, 30)),   # raw: what vad.py calls the noise floor
        "frame_p90": float(np.percentile(raw, 90)),
        "ac_p30": float(np.percentile(ac, 30)),
        "ac_p50": float(np.percentile(ac, 50)),
        "ac_p90": float(np.percentile(ac, 90)),
        "ac_max": float(ac.max()),
        "bands": band_shares(mono),
        "tones": strongest_tones(mono),
    }
    return out


def record(seconds):
    import sounddevice as sd
    a = sd.rec(int(seconds * SR), samplerate=SR, channels=config.CHANNELS,
               dtype="int16", device=config.INPUT_DEVICE)
    sd.wait()
    return a


def show(title, m):
    print(f"\n--- {title} ---")
    print(f"  RMS overall: {m['rms']:.0f}   noise-floor estimate (30th pct of frames): {m['frame_p30']:.0f}   "
          f"90th pct: {m['frame_p90']:.0f}   peak: {m['peak']}")
    if m["per_channel"]:
        print("  per channel RMS: " + "  ".join(f"ch{i}={v:.0f}" for i, v in enumerate(m["per_channel"])))
    print(f"  DC offset: {m['dc']:.0f}   (DC level wobbles by +-{m['dc_wobble']:.0f} between frames)")
    print(f"  AC loudness (DC removed), per 30ms frame: 30th pct {m['ac_p30']:.0f}   median {m['ac_p50']:.0f}   "
          f"90th pct {m['ac_p90']:.0f}   loudest {m['ac_max']:.0f}")
    print("  energy by band: " + "  ".join(f"{lo}-{hi}Hz {s * 100:.0f}%" for lo, hi, s in m["bands"]))
    print("  strongest tones (Hz): " + ", ".join(f"{t:.0f}" for t in m["tones"]))


def countdown(msg, seconds=3):
    import time
    print(f"\n{msg}")
    for i in range(seconds, 0, -1):
        print(f"  starting in {i}...", flush=True)
        time.sleep(1)
    print("  RECORDING", flush=True)


def interpret(room, covered, speech):
    print("\n=== WHAT THIS MEANS ===")
    dc_share = room["dc"] ** 2 / max(room["rms"] ** 2, 1)
    print(f"The microphone has a constant DC offset of {room['dc']:.0f}. vad.py currently counts it as loudness:"
          f" {dc_share * 100:.0f}% of the room's measured energy is just that offset, not sound.")
    print(f"Real (AC) room noise floor: {room['ac_p30']:.0f}   vs the {room['frame_p30']:.0f} the gate uses today.")
    ac_snr = 20 * np.log10(max(speech["ac_p90"], 1) / max(room["ac_p50"], 1))
    print(f"Speech (90th pct, DC removed) {speech['ac_p90']:.0f} vs room median {room['ac_p50']:.0f}  "
          f"=>  {ac_snr:.1f} dB above the room noise.")
    if ac_snr < 6:
        print("  -> Speech is NOT clearly louder than the noise even with DC removed: raise the mic")
        print("     gain / move closer, or the noise itself (spikes, hum) must come down first.")
    else:
        print("  -> With DC removed there IS a healthy gap: fixing the gate's measurement (remove DC")
        print("     before the RMS) and raising its multiplier should cut the false triggers a lot.")
    print()
    ratio = covered["ac_p50"] / max(room["ac_p50"], 1)
    print(f"Covering the mic changed the (AC) noise to {ratio * 100:.0f}% of the open-room level.")
    if ratio > 0.8:
        print("  -> Barely changed: the noise is ELECTRICAL (picked up inside the mic/HAT/wiring),")
        print("     not the room. Fix at the source: move the mic HAT away from the amp/Pi power,")
        print("     shorten/shield the wires, add a ferrite, or lower the mic gain (PGA).")
    elif ratio < 0.5:
        print("  -> Dropped a lot: the noise is ACOUSTIC (the room: fan, street, speaker hiss).")
        print("     Fix at the room/placement; the software gate is a reasonable place to tune.")
    else:
        print("  -> Partly both: electrical noise plus some room noise.")
    snr = 20 * np.log10(max(speech["frame_p90"], 1) / max(room["frame_p30"], 1))
    print(f"Speech peaks ~{speech['frame_p90']:.0f} vs room floor ~{room['frame_p30']:.0f}  =>  {snr:.1f} dB above the floor.")
    print("  (the gate needs the floor x 1.15 = +1.2 dB; a comfortable margin is 6+ dB)")
    low = room["bands"][0][2]
    if low > 0.5:
        print(f"Most room-noise energy ({low * 100:.0f}%) is below 100 Hz: mains/power hum.")
    if room["bands"][-1][2] + room["bands"][-2][2] > 0.5:
        print("Most room-noise energy is above 2 kHz: hiss / switching noise.")


def main():
    print("ALISTO mic noise measurement. The speaker should be silent and nothing else running.")
    countdown("PHASE 1/3: stay QUIET (normal room, mic uncovered) for 6 seconds.")
    room = analyse(record(6))
    show("Room silence", room)

    countdown("PHASE 2/3: COVER the microphone holes with a finger or thick cloth, stay quiet, 6 seconds.")
    covered = analyse(record(6))
    show("Mic covered", covered)

    countdown("PHASE 3/3: say 'Alisto tabang' about 4 times, normal voice and distance, 8 seconds.")
    speech = analyse(record(8))
    show("Speech", speech)

    interpret(room, covered, speech)


if __name__ == "__main__":
    sys.exit(main())
