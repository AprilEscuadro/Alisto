"""Segments a continuous frame stream into discrete utterances using WebRTC VAD.

This is what removes the old "process everything, all the time" approach:
we only ever hand off audio to the matcher once a full utterance (speech
bounded by silence) has been captured.
"""

import collections

import numpy as np
import webrtcvad

import config


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
        self._reset()

    def _reset(self):
        self._buffer = []
        self._silence_run = 0
        self._in_speech = False

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
        return float(np.percentile(self._rms_history, config.NOISE_FLOOR_PERCENTILE))

    def push(self, frame):
        """frame: mono int16 numpy array of exactly one FRAME_MS block.

        Returns a finished utterance (mono int16 numpy array) once speech is
        followed by enough trailing silence AND it's loud enough relative to
        the current ambient noise floor, else None.
        """
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
        return utterance
