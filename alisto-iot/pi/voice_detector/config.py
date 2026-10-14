"""Central settings for the ALISTO voice detector."""

# --- Audio capture (ReSpeaker 2-Mic HAT) ---
SAMPLE_RATE = 16000        # webrtcvad only accepts 8000/16000/32000/48000
CHANNELS = 2                # ReSpeaker 2-Mic HAT is stereo; we downmix to mono
FRAME_MS = 30                # webrtcvad only accepts 10/20/30 ms frames
INPUT_DEVICE = None          # None = default input device; set to a sounddevice index if needed

# --- Voice activity detection ---
VAD_AGGRESSIVENESS = 1       # 0-3, higher = more aggressive at filtering non-speech
                              # (2-3 missed real speech on this mic; 0 blobbed everything
                              # together; 1 is the middle ground)
SILENCE_HANGOVER_MS = 450    # how much trailing silence ends an utterance
                              # (raised from 300ms: a brief pause between "Alisto"
                              # and "Tabang" was ending the utterance mid-phrase)
MIN_UTTERANCE_MS = 300         # utterances shorter than this are discarded as noise
                                # (wake word "Alisto" and single keywords are short)
MAX_UTTERANCE_MS = 4000       # safety cap so a stuck VAD can't buffer forever
NOISE_FLOOR_WINDOW_FRAMES = 100  # ~3s of history (at 30ms/frame) for the noise floor estimate
NOISE_FLOOR_MIN_SAMPLES = 20      # don't trust the floor until it has this many real samples
# --- Microphone gain (TLV320AIC3x codec on the ReSpeaker 2-Mic HAT) ---
# The card shipped with the capture PGA at 15 (7.5 dB of a possible 59.5 dB).
# Real test at 3 m: speech arrived at rms 20-28, no better than the noise.
# PGA 40 (20 dB) lifted 3 m speech to rms 74-146 while the noise floor only
# rose ~1.3x -- about 10 dB better signal-to-noise, and "Alisto tabang" was
# recognised at 3 m. Applied at every startup (the mixer setting does not
# survive a reboot). Each step is 0.5 dB; None = leave the card alone.
CAPTURE_PGA = 40
CAPTURE_CARD = "seeed2micvoicec"

# --- Audio preprocessing (see measure_noise.py for the measurements behind it) ---
# The mic input carries a constant DC offset (~950) plus a huge sub-100 Hz
# wobble (26 Hz / 120 Hz, +-800-1200). Both are inaudible but dwarf the actual
# voice (speech ~240 RMS, silence ~15 RMS once they're removed), so the
# loudness gate saw "floor ~900, speech ~950-1000" -- a meaningless 5-10% gap
# -- and kept firing whisper on nothing (~5s of deafness each time). With
# AUDIO_PREPROCESS on, every frame is high-passed first, the gate measures
# real (AC) loudness, utterances are level-normalised before whisper, and the
# first moments after the mic restarts are ignored. Set it to False to go back
# to the old behaviour (and the old gate numbers below) exactly.
AUDIO_PREPROCESS = True
HIGHPASS_HZ = 100
HIGHPASS_TAPS = 257            # linear-phase FIR: sharp cutoff, ~8 ms delay, cheap with numpy
SETTLE_MS_AFTER_RESUME = 600   # mic restart makes a DC step/click -- ignore it
FRAME_GAP_RESTART_S = 0.25     # no frames for this long = the mic was paused
NORMALIZE_TARGET_RMS = 2000    # whisper sees speech around this level
NORMALIZE_MAX_GAIN = 60        # 3 m speech arrives at rms ~35-95: x16 left it far below target
NORMALIZE_MAX_PEAK = 30000     # never let the gain clip

if AUDIO_PREPROCESS:
    NOISE_FLOOR_STARTUP_FALLBACK = 60   # real silence measures ~15, real speech ~100-240
    NOISE_FLOOR_MIN = 26                # the floor never counts as quieter than this
    NOISE_FLOOR_PERCENTILE = 30
    NOISE_FLOOR_MULTIPLIER = 2.2        # threshold >= 57. Measured at CAPTURE_PGA 40: quiet-room floor
                                        # 24-27 with small noise bursts at rms 41-51 (1.5x = threshold
                                        # 36-41 accepted them: 8 false whisper runs in 3 quiet minutes),
                                        # versus speech at 3-5 m rms 74-200 and at 1 m 150-400. This
                                        # sits between the two. (At the old PGA 15 the numbers were
                                        # noise 7-27, 3 m speech 20-34, and 1.5 was right.)
else:
    NOISE_FLOOR_STARTUP_FALLBACK = 800   # measured real speech is 1300-2900 RMS -- this must stay
                                           # well under that, an emergency system must not miss real
                                           # speech even if that means occasionally accepting noise
    NOISE_FLOOR_MIN = 0
    NOISE_FLOOR_PERCENTILE = 30       # low percentile of recent loudness = the "quiet baseline"
    NOISE_FLOOR_MULTIPLIER = 1.15     # real measurement (via vad.py's [gate] debug log): in this
                                    # room, ambient floor ~900-936 and normal speaking volume
                                    # only ~950-985 -- a very tight ~5-10% gap. 1.8 was WAY above
                                    # real speech, rejecting genuine "Alisto Tabang" attempts
                                    # outright. 1.3 (the old DTW-era default) still leaves little
                                    # margin. Biased low on purpose: missing a real emergency is
                                    # worse than occasionally reacting to noise, even though each
                                    # false trigger now costs ~4-5s with whisper instead of ~1s.
                                    # Consider raising mic gain (PGA) instead of this multiplier
                                    # if false triggers are still too frequent -- widening the
                                    # actual gap between speech and noise helps more than tuning
                                    # a threshold squeezed between two close numbers.

# --- MFCC ---
N_MFCC = 19                  # raised from 13 for finer spectral resolution
N_MEL_FILTERS = 26
N_FFT = 512
FRAME_LEN_MS = 25
FRAME_STEP_MS = 20           # coarser than the usual 10ms: halves frame count (and DTW cost)
                              # with minimal accuracy impact for whole-word matching
PRE_EMPHASIS = 0.97

# --- DTW matching ---
DTW_BAND_RADIUS = 15         # Sakoe-Chiba band width, keeps matching fast on the Pi
DETECTION_THRESHOLD = 40.0   # tune with calibrate.py against your own recordings

# --- Templates ---
TEMPLATES_DIR = "templates"   # templates/<GROUP>/<GROUP>_NN.wav, see templates_lib.py
MIN_TEMPLATE_FRAMES = 50       # ~1000ms at 20ms/frame -- shorter templates get excluded from
                                # matching (they kept acting as false-match magnets for noise)

# --- Wake word + keyword flow ---
KEYWORD_LISTEN_S = 4.0        # how long to listen for a category keyword after the wake word
