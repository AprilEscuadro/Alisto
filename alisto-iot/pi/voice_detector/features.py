"""Hand-rolled MFCC extraction using only NumPy (no librosa / python_speech_features).

Standard pipeline: pre-emphasis -> framing -> Hamming window -> power spectrum
-> mel filterbank -> log -> DCT-II -> cepstral mean normalization.
"""

import numpy as np

import config


def _hz_to_mel(hz):
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel):
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(n_filters, n_fft, sample_rate):
    low_mel = _hz_to_mel(0)
    high_mel = _hz_to_mel(sample_rate / 2)
    mel_points = np.linspace(low_mel, high_mel, n_filters + 2)
    hz_points = _mel_to_hz(mel_points)
    bin_points = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)

    fbank = np.zeros((n_filters, n_fft // 2 + 1))
    for m in range(1, n_filters + 1):
        f_left, f_center, f_right = bin_points[m - 1], bin_points[m], bin_points[m + 1]
        if f_center > f_left:
            fbank[m - 1, f_left:f_center] = (
                np.arange(f_left, f_center) - f_left
            ) / (f_center - f_left)
        if f_right > f_center:
            fbank[m - 1, f_center:f_right] = (
                f_right - np.arange(f_center, f_right)
            ) / (f_right - f_center)
    return fbank


def _dct_matrix(n_mfcc, n_filters):
    # DCT-II basis, orthonormal-ish scaling (matches the common MFCC convention).
    n = np.arange(n_filters)
    basis = np.zeros((n_mfcc, n_filters))
    for k in range(n_mfcc):
        basis[k] = np.cos(np.pi * k * (2 * n + 1) / (2 * n_filters))
    basis[0] *= np.sqrt(1.0 / n_filters)
    basis[1:] *= np.sqrt(2.0 / n_filters)
    return basis


_FBANK = _mel_filterbank(config.N_MEL_FILTERS, config.N_FFT, config.SAMPLE_RATE)
_DCT = _dct_matrix(config.N_MFCC, config.N_MEL_FILTERS)


def _static_mfcc(signal):
    emphasized = np.append(signal[0], signal[1:] - config.PRE_EMPHASIS * signal[:-1])

    frame_len = int(round(config.FRAME_LEN_MS / 1000 * config.SAMPLE_RATE))
    frame_step = int(round(config.FRAME_STEP_MS / 1000 * config.SAMPLE_RATE))
    signal_len = len(emphasized)
    num_frames = 1 + max(0, (signal_len - frame_len) // frame_step)
    if num_frames < 1:
        return np.zeros((0, config.N_MFCC))

    pad_len = num_frames * frame_step + frame_len
    padded = np.append(emphasized, np.zeros(pad_len - signal_len))

    indices = (
        np.tile(np.arange(frame_len), (num_frames, 1))
        + np.tile(np.arange(0, num_frames * frame_step, frame_step), (frame_len, 1)).T
    )
    frames = padded[indices] * np.hamming(frame_len)

    mag = np.abs(np.fft.rfft(frames, config.N_FFT))
    power = (mag ** 2) / config.N_FFT

    filter_banks = power @ _FBANK.T
    filter_banks = np.where(filter_banks == 0, np.finfo(float).eps, filter_banks)
    log_fb = np.log(filter_banks)

    mfcc = log_fb @ _DCT.T
    mfcc -= np.mean(mfcc, axis=0, keepdims=True)  # cepstral mean normalization
    return mfcc


def _delta(feat, n=2):
    # Standard regression-based delta: captures how each coefficient is
    # changing over time, not just its per-frame value. Static MFCC alone
    # can't tell two differently-worded utterances of similar duration
    # apart nearly as well as static+delta together.
    if len(feat) == 0:
        return feat
    denom = 2 * sum(i * i for i in range(1, n + 1))
    padded = np.pad(feat, ((n, n), (0, 0)), mode="edge")
    delta = np.zeros_like(feat)
    for t in range(len(feat)):
        acc = np.zeros(feat.shape[1])
        for i in range(1, n + 1):
            acc += i * (padded[t + n + i] - padded[t + n - i])
        delta[t] = acc / denom
    return delta


def compute_mfcc(signal):
    """signal: 1D float array (mono, any range) -> (num_frames, 3*N_MFCC) array
    of [static MFCC | delta | delta-delta] concatenated per frame.

    Delta-delta (acceleration, i.e. the delta of the delta) is standard in
    ASR feature sets alongside static+delta -- it captures curvature in how
    the spectrum changes, which helps separate short, similar-duration
    utterances that static+delta alone can still confuse.
    """
    signal = signal.astype(np.float64)
    if len(signal) == 0:
        return np.zeros((0, config.N_MFCC * 3))

    static = _static_mfcc(signal)
    if len(static) == 0:
        return np.zeros((0, config.N_MFCC * 3))

    delta = _delta(static)
    delta2 = _delta(delta)
    return np.hstack([static, delta, delta2])
