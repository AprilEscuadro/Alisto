"""Microphone capture (ReSpeaker 2-Mic HAT) and WAV helpers."""

import queue
import wave

import numpy as np
import sounddevice as sd

import config

FRAME_SAMPLES = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)


class FrameStream:
    """Continuous mic input, iterable for mono int16 frames of FRAME_MS length.

    The mic callback keeps recording in the background even while the
    consumer is busy elsewhere (e.g. blocked for ~4-5s in a whisper.cpp
    subprocess call) -- frames just pile up in a queue. If the consumer then
    resumes without clearing that backlog, it spends time working through
    STALE audio captured during the busy period instead of genuinely live
    audio. If someone spoke again during that window (e.g. repeating the
    phrase while waiting), that gets queued and only processed AFTER the
    current one finishes, compounding the delay each time. Call drain()
    right after a slow operation to discard the backlog and stay in sync
    with real time.
    """

    def __init__(self):
        self._q = queue.Queue()

        def callback(indata, frames, time_info, status):
            self._q.put(indata.copy())

        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=config.CHANNELS,
            dtype="int16",
            blocksize=FRAME_SAMPLES,
            device=config.INPUT_DEVICE,
            callback=callback,
        )
        self._stream.start()

    def __iter__(self):
        return self

    def __next__(self):
        block = self._q.get()
        if config.CHANNELS > 1:
            return block.mean(axis=1).astype(np.int16)
        return block[:, 0]

    def drain(self):
        """Discards any backlogged frames. Returns how many were dropped."""
        dropped = 0
        while True:
            try:
                self._q.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        return dropped

    def pause(self):
        """Stops mic capture entirely -- nothing is recorded while paused.
        Use while busy transcribing so there's no backlog to drain at all
        (cleaner than draining after the fact): the device genuinely isn't
        listening during that window, matching what the status LED signals."""
        self._stream.stop()

    def resume(self):
        """Resumes mic capture after pause(). Also drains any stray frame(s)
        that may have queued right at the pause/resume boundary."""
        self._stream.start()
        return self.drain()


def frame_stream():
    """Yields mono int16 frames of exactly FRAME_MS length, forever.

    Kept for simple scripts (enroll.py, calibrate.py) that don't need
    drain() -- alisto_main.py uses FrameStream directly instead.
    """
    yield from FrameStream()


def record_seconds(duration_s):
    """Blocking record of `duration_s` seconds, downmixed to mono int16."""
    frames = int(duration_s * config.SAMPLE_RATE)
    audio = sd.rec(frames, samplerate=config.SAMPLE_RATE, channels=config.CHANNELS, dtype="int16")
    sd.wait()
    if config.CHANNELS > 1:
        audio = audio.mean(axis=1).astype(np.int16)
    else:
        audio = audio[:, 0]
    return audio


def save_wav(path, mono_int16):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(config.SAMPLE_RATE)
        wf.writeframes(mono_int16.tobytes())


def load_wav(path):
    with wave.open(path, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16)
