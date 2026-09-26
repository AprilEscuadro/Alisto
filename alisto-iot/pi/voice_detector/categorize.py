"""Classifies a transcribed Cebuano utterance into an emergency category by
keyword search, and provides the SMS message to send for each category.

This does NOT need the transcript to be a perfect/exact match against one of
the 71 known phrases -- it just needs a handful of the right words to have
come through the (imperfect, multilingual) Whisper transcription. That's why
this runs on top of real speech-to-text rather than fixed-phrase template
matching: new phrasings work automatically as long as they use similar
vocabulary.

Order matters: categories are checked top-to-bottom, first match wins, so
more specific/urgent categories (fire, intruder) are listed before generic
ones (general help).
"""

import re

# (category, [keywords]) -- keywords are checked as substrings of the
# normalized transcript. Order = priority (most specific/urgent first).
#
# Each list includes the "textbook" Cebuano spelling PLUS common variants
# Whisper is likely to actually produce, since it has no native Cebuano
# support and is running in Tagalog mode ("tl") as the closest approximation
# (real testing showed e.g. "Tabang" come out as "abang"). Expect to keep
# adding fragments here as real usage turns up new mis-transcriptions --
# this list is meant to grow over time, unlike DTW templates which needed
# exact re-recording.
_CATEGORY_KEYWORDS = [
    (
        "FIRE",
        ["sunog", "sunug", "onog",  # "onog" -- real test: Whisper heard
                                     # "sunog" as "o nogg"
         "sono",  # real test: Whisper heard "sunog" as "sono"
         "kalayo", "kayo", "kayu", "kalayu", "fire"],
    ),
    (
        "INTRUDER",
        [
            "kaila", "nisulod", "nisulud", "sulod", "sulud", "makagawas",
            "na-trap", "natrap", "tulisan", "kawatan", "magnanakaw",
            "nangawat", "abatan",  # "abatan" -- real test: Whisper heard
                                    # "kawatan" as "abatan"
        ],
    ),
    ("MEDICATION", ["tambal", "tambl", "tambalan", "gamot", "gamut"]),
    (
        "FALL_INJURY",
        [
            "nahulog", "hulog", "holo",  # "holo" -- real test: Whisper heard
                                          # "nahulog" as "ng holo"
            "natumba", "tumba", "nadagma", "dagma", "nabali", "bali",
            "vale",  # real test: Whisper heard "nabali" as "vale"
            "naigo", "nasamdan", "samdan", "nagdugo", "dugo",
            "dogo",  # real test: Whisper heard "dugo" as "dogo"
            "bukog", "naipit", "aksidente", "na-lock", "nalock",
            "makalihok", "makatindog",
        ],
    ),
    (
        "MEDICAL_SYMPTOM",
        [
            "nahimatay", "himatay", "kakuyapon",
            "yapon",  # real test: Whisper heard "kakuyapon" as "yapon"
            "huyang", "kahuyang", "luya", "kapoy", "sakit", "dughan",
            "nalipong", "lipong", "atakihon", "doktor", "ginhawa",
            "hawa",  # real test: Whisper heard "makaginhawa" as "hawah"
            "mahimatay", "medical",
        ],
    ),
    (
        "GENERAL_HELP",
        ["tabang", "abang", "bang", "emergency", "problema", "alert",
         "assistance", "help"],
    ),
]

MESSAGES = {
    "FIRE": (
        "ALISTO ALERT!\n"
        "Possible FIRE emergency detected. Immediate assistance needed!"
    ),
    "INTRUDER": (
        "ALISTO ALERT!\n"
        "Possible INTRUDER / security emergency detected. Please check immediately!"
    ),
    "MEDICATION": (
        "ALISTO ALERT!\n"
        "Medication-related emergency detected. Please assist with medicine issue!"
    ),
    "FALL_INJURY": (
        "ALISTO ALERT!\n"
        "Possible fall or injury detected. Please check for injuries!"
    ),
    "MEDICAL_SYMPTOM": (
        "ALISTO ALERT!\n"
        "Medical symptoms detected (pain, weakness, or fainting). Please seek help!"
    ),
    "GENERAL_HELP": (
        "ALISTO ALERT!\n"
        "Emergency assistance requested. Please respond immediately!"
    ),
    "UNKNOWN": (
        "ALISTO ALERT!\n"
        "Emergency button/phrase activated. Please check immediately!"
    ),
}


def _normalize(text):
    text = text.lower()
    text = re.sub(r"[^a-z\s-]", " ", text)
    text = re.sub(r"\s+", "", text)  # strip ALL spaces, not just collapse --
    # real test: Whisper split "Alisto" into "al is to", which broke a plain
    # substring check on spaced text. Keywords here are single words, so
    # removing spaces entirely only helps (rejoins accidentally-split words)
    # and never breaks an existing match.
    return text.strip()


def categorize(transcript):
    """Returns (category, message) for the given transcript text.

    Falls back to "UNKNOWN" (generic message) if no keyword matches --
    still triggers the emergency flow, just without a specific category.
    """
    normalized = _normalize(transcript)
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in normalized for kw in keywords):
            return category, MESSAGES[category]
    return "UNKNOWN", MESSAGES["UNKNOWN"]
