"""Segments a continuous frame stream into discrete utterances using WebRTC VAD.

This is what removes the old "process everything, all the time" approach:
we only ever hand off audio to the matcher once a full utterance (speech
bounded by silence) has been captured.
"""

import collections

import numpy as np
import webrtcvad

import config


class HighPass:
    """Streaming FIR high-pass (windowed-sinc, spectral inversion) that strips
    the microphone's DC offset and its sub-100 Hz wobble -- see config.py's
    AUDIO_PREPROCESS note. numpy only (no scipy on the Pi); keeps the tail of
    the previous frame so frame boundaries are seamless."""

    def __init__(self, cutoff_hz=None, taps=None):
        cutoff_hz = cutoff_hz or config.HIGHPASS_HZ
        n = taps or config.HIGHPASS_TAPS
        if n % 2 == 0:
            n += 1  # odd length -> exact integer-sample centre
        mid = (n - 1) // 2
        k = np.arange(n) - mid
        fc = cutoff_hz / config.SAMPLE_RATE
        lowpass = 2 * fc * np.sinc(2 * fc * k) * np.blackman(n)
        lowpass /= lowpass.sum()
        h = -lowpass
        h[mid] += 1.0
        self._h = h
        self._n = n
        self._tail = None

    def process(self, frame):
        """frame: int16 array -> int16 array of the same length."""
        x = frame.astype(np.float64)
        if self._tail is None:
            # Start from the frame's own level, not zero -- otherwise the very
            # first frame sees a giant fake step from 0 up to the DC offset.
            self._tail = np.full(self._n - 1, x.mean())
        buf = np.concatenate([self._tail, x])
        y = np.convolve(buf, self._h, mode="valid")
        self._tail = buf[-(self._n - 1):]
        return np.clip(y, -32768, 32767).astype(np.int16)


def normalize_level(utterance):
    """Scales a (high-passed) utterance up towards config.NORMALIZE_TARGET_RMS
    so whisper gets speech at a sane level instead of ~1% of full scale.
    Capped by NORMALIZE_MAX_GAIN and by peak, so it can't clip, and never
    turns the volume down."""
    x = utterance.astype(np.float64)
    level = float(np.sqrt(np.mean(x ** 2)))
    peak = float(np.max(np.abs(x))) or 1.0
    if level < 1.0:
        return utterance
    gain = min(config.NORMALIZE_TARGET_RMS / level, config.NORMALIZE_MAX_GAIN,
               config.NORMALIZE_MAX_PEAK / peak)
    if gain <= 1.0:
        return utterance
    return np.clip(x * gain, -32768, 32767).astype(np.int16)


class Segmenter:
    def __init__(self, require_loud=True):
        # require_loud=False skips the amplitude noise-floor gate entirely --
        # used by enroll.py, where a human directly supervises and confirms
        # each recording, so there's no need to guard against background
        # noise, and every recording is short enough to rarely build up
        # real floor history before the word is already spoken.
        self.require_loud = require_loud
        self.vad = webrtcvad.Vad(config.VAD_AGGRESSIVENESS)
        self.hangover_frames = max(1, config.SILENCE_HANGOVER_MS // config.FRAME_MS)
        self.min_frames = max(1, config.MIN_UTTERANCE_MS // config.FRAME_MS)
        self.max_frames = max(1, config.MAX_UTTERANCE_MS // config.FRAME_MS)
        # Ambient noise floor, estimated as a low percentile of recent raw
        # frame loudness -- deliberately independent of webrtcvad's own
        # speech/non-speech call. Broadband noise (rain, fans) can fool that
        # classifier into flagging it as "speech" most of the time, which
        # would otherwise starve a floor estimate that only learns from
        # frames webrtcvad calls non-speech (it would never update).
        self._rms_history = collections.deque(maxlen=config.NOISE_FLOOR_WINDOW_FRAMES)
        self._highpass = HighPass() if config.AUDIO_PREPROCESS else None
        self._settle_frames = 0
        self._reset()

    def _reset(self):
        self._buffer = []
        self._silence_run = 0
        self._in_speech = False

    def restart(self):
        """Call when the mic stream was paused and resumed (nothing was
        listening for a while). Restarting the stream makes the codec emit a DC
        step/click, which used to be heard as a ~0.5s "utterance" and trigger
        a useless whisper run -- which paused the mic again, and so on. Drops
        any half-built utterance, resets the filter, and ignores the next
        SETTLE_MS_AFTER_RESUME of audio."""
        self._reset()
        if self._highpass is not None:
            self._highpass = HighPass()
            self._settle_frames = max(1, config.SETTLE_MS_AFTER_RESUME // config.FRAME_MS)

    @staticmethod
    def _rms(samples):
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

    def _noise_floor(self):
        # Don't trust a percentile computed from too few samples -- a single
        # loud outlier frame (e.g. a click/pop right as a recording stream
        # opens) getting misclassified as speech can freeze history at just
        # that one value, poisoning the floor for the entire utterance that
        # follows. Fall back to a generous fixed floor until enough real
        # background samples have accumulated to make the percentile meaningful.
        if len(self._rms_history) < config.NOISE_FLOOR_MIN_SAMPLES:
            return config.NOISE_FLOOR_STARTUP_FALLBACK
        floor = float(np.percentile(self._rms_history, config.NOISE_FLOOR_PERCENTILE))
        return max(floor, config.NOISE_FLOOR_MIN)

    def push(self, frame):
        """frame: mono int16 numpy array of exactly one FRAME_MS block.

        Returns a finished utterance (mono int16 numpy array) once speech is
        followed by enough trailing silence AND it's loud enough relative to
        the current ambient noise floor, else None.
        """
        if self._highpass is not None:
            frame = self._highpass.process(frame)
            if self._settle_frames > 0:
                self._settle_frames -= 1
                return None  # filter warming up / mic just restarted

        is_speech = self.vad.is_speech(frame.tobytes(), config.SAMPLE_RATE)

        if not self._in_speech:
            # Only accumulate history outside an active utterance, so a real
            # utterance's own loudness never pollutes the floor estimate.
            self._rms_history.append(self._rms(frame))

        if is_speech:
            self._in_speech = True
            self._silence_run = 0
            self._buffer.append(frame)
        elif self._in_speech:
            self._silence_run += 1
            self._buffer.append(frame)  # keep a little trailing silence for DTW stability
            if self._silence_run >= self.hangover_frames:
                return self._finish()
        # else: pure silence outside an utterance, nothing to do

        if self._in_speech and len(self._buffer) >= self.max_frames:
            return self._finish()  # safety cap so a stuck VAD can't buffer forever

        return None

    def _finish(self):
        frames = self._buffer
        floor = self._noise_floor()
        self._reset()
        if len(frames) < self.min_frames:
            return None  # too short, treat as noise

        utterance = np.concatenate(frames)
        rms = self._rms(utterance)
        threshold = floor * config.NOISE_FLOOR_MULTIPLIER
        if self.require_loud and rms < threshold:
            print(f"  [gate] REJECTED: rms={rms:.0f} floor={floor:.0f} threshold={threshold:.0f}")
            return None  # not meaningfully louder than the current ambient floor
        print(f"  [gate] accepted: rms={rms:.0f} floor={floor:.0f} threshold={threshold:.0f}")
        if config.AUDIO_PREPROCESS:
            return normalize_level(utterance)
        return utterance
