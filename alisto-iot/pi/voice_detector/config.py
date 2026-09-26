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
NOISE_FLOOR_STARTUP_FALLBACK = 800   # measured real speech is 1300-2900 RMS -- this must stay
                                       # well under that, an emergency system must not miss real
                                       # speech even if that means occasionally accepting noise
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
