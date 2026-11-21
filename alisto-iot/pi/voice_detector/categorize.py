"""Classifies a transcribed Cebuano utterance into an emergency category by
keyword search, and provides the SMS message to send for each category.

This does NOT need the transcript to be a perfect/exact match against one
fixed phrase -- it just needs a handful of the right words to have
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
         "suno",  # real test (whisper base, 3 m): "naay sunog" -> "ay suno."
         # "kayo"/"kayu" were removed: whisper hears "tabang KAAYO" as
         # "babang kayo", and those two short keywords made it a FIRE alert.
         "kalayo", "kalayu", "fire",
         # Added to reach 25 per category: more fire words in Cebuano,
         # Tagalog (Whisper runs in "tl" mode) and English. Deliberately NOT
         # "apoy": it is inside "kapoy" (tired, MEDICAL) and FIRE is checked first.
         "liyab", "maapoy", "dilaab", "usok", "sindi", "bumbero", "bombero",
         "gasul", "pagbuto", "nagbuto", "kuryente", "flame", "smoke", "burn",
         "spark",
         "sonug", "sonog",  # real test (whisper base): "nasunog" -> "sonug"
         # round-2 test: "nagdilaab" -> "nagdilap"/"dillab", "naay aso" ("aso"
         # alone is NOT used: it is inside "baso", "kaso")
         "dilap", "dillab", "naayaso", "nagaso",
         "gisuno",  # round-2 test: "gisunog" -> "gisuno." ("suno" is whole-word only)
         "sunub",   # live test: "sunog" -> "alistot, sunub." (no keyword matched)
         # Pieces of the fire words themselves (>= 4-5 letters), so a transcript
         # where whisper garbled one END of the word still matches something:
         "unog", "kalay", "alayo", "dilaa", "ilaab", "ayaso",
         ],
    ),
    (
        "INTRUDER",
        [
            "kaila", "nisulod", "nisulud", "sulod", "sulud", "makagawas",
            "sulun",  # real test (whisper base): "nisulod" -> "ni-sulun"
            "na-trap", "natrap", "tulisan", "kawatan", "magnanakaw",
            "nangawat", "abatan",  # "abatan" -- real test: Whisper heard
                                    # "kawatan" as "abatan"
            # Added to reach 25 per category: stealing, weapons, strangers,
            # being locked in, doors/windows, police.
            "nagnakaw", "nanakaw", "holdap", "holdup", "baril", "pusil",
            "estranghero", "nakulong", "pultahan", "bintana", "pulis",
            # real tests (whisper base): "nangawat" -> "nangawad",
            # "nisulod" -> "ni-solod" / "nisulut", "tulisan" -> "tulisat"
            "nangawad", "solod", "sulut", "tulisat",
            # round-2 test: "tawo sa gawas" -> "tao sa doas", "nakulong" -> "kolong"
            "doas", "kulong", "kolong",
            # pieces of kawatan / nangawat / tulisan (one end may be garbled)
            "kawat", "awatan", "ngawat", "angawat", "tulisa", "ulisan",
        ],
    ),
    (
        "MEDICATION",
        [
            "tambal", "tambl", "tambalan", "gamot", "gamut",
            # Added to reach 25 per category: pills/doses, prescription,
            # pharmacy, and the common elderly medicines (the app's own
            # reminder example is "diatabs").
            "tablet", "pildoras", "pill", "kapsula", "capsule", "dosis",
            "dose", "reseta", "botika", "botica", "insulin", "diatabs",
            "metformin", "amlodipine", "losartan", "vitamins", "bitamina",
            "biogesic", "paracetamol", "medisina",
            "tampal",  # real test (whisper base): "tambal" -> "tampal"
            # real test (whisper base): "gamot" -> "damot". Also a real word
            # ("stingy"), added on purpose because it only counts after "Alisto".
            "damot",
            # round-2 test: "tambal" -> "tambay", "gamot" -> "damunas"
            # ("tambay" instead of "tamba": "tamba" sits inside other words)
            "tambay", "damun",
            "ambal",  # piece of "tambal" (whisper hears the first letters wrongly)
            # variants of "gamot": "amot" (not when it is part of "kamot", the
            # hand -- see _BLOCKED_BY) and "damut"; "damot"/"gamut" are above.
            "amot", "damut",
        ],
    ),
    (
        "FALL_INJURY",
        [
            "nahulog", "hulog", "holo",  # "holo" -- real test: Whisper heard
                                          # "nahulog" as "ng holo"
            "hulok",  # real test (whisper base): "nahulog ko" -> "na huloko"
                      # (not "hulo": that would also match "hulot", "ran out")
            "natumba", "tumba", "nadagma", "dagma", "nabali", "bali",
            "vale",  # real test: Whisper heard "nabali" as "vale"
            "naigo", "nasamdan", "samdan", "nagdugo", "dugo",
            "dogo",  # real test: Whisper heard "dugo" as "dogo"
            "bukog", "naipit", "aksidente", "na-lock", "nalock",
            "makalihok", "makatindog",
            "dulas",  # "nadulas" = slipped
            # real test (whisper base): "nahulog" -> "hula-go"
            "hulago", "hula-go",
            "i-go-go", "igogo",  # real test (whisper base): "naigo ko" -> "i-go-go"
            # round-2 test: "nadulas" -> "dolas", "natumba ko" -> "tong bako"
            "dolas", "tongba",
            # pieces of nabali / nadulas / naigo / naipit
            "nabal", "abali", "adulas", "nadulas", "aigo", "aipit", "naipi",
        ],
    ),
    (
        "MEDICAL_SYMPTOM",
        [
            "nahimatay", "himatay", "kakuyapon",
            "himata",  # real test (whisper base): "mahimatay" -> "himatai"
            "sahit",   # real test (whisper base): "sakit" -> "isa hit"
            "lugan",   # real test (whisper base): "dughan" -> "lugan"
            "yapon",  # real test: Whisper heard "kakuyapon" as "yapon"
            "huyang", "kahuyang", "luya", "kapoy", "sakit", "dughan",
            "nalipong", "lipong", "atakihon", "doktor", "ginhawa",
            "hawa",  # real test: Whisper heard "makaginhawa" as "hawah"
            "mahimatay", "medical",
            # Added to reach 25: fever, vomiting, cold, asthma/breathless.
            "hilanat", "nagsuka", "sipon", "hubak",
            # real tests (whisper base): "huyang" -> "buyang",
            # "lisod ko ginhawa" -> "kung ginawa"
            "buyang", "ginawa",
            # round-2 test: "sakit" -> "sa kib", "nagsuka" -> "nagsoka",
            # "hilanat" -> "hila na", "nagkurog" -> "nagkoron"
            "sakib", "nagsoka", "hilana", "kurog", "nagkoron",
            # pieces of huyang / ginhawa / nagkurog / nagsuka
            "huyan", "uyang", "ginhaw", "inhawa", "nagkurog", "agsuka", "nagsuk",
        ],
    ),
    (
        "GENERAL_HELP",
        ["tabang", "abang", "bang", "emergency", "problema", "alert",
         "assistance", "help",
         # Added to reach 25: "help"/"danger" in Tagalog (saklolo, tulong),
         # Cebuano (bulig, palihug), Spanish-derived and English words.
         "saklolo", "tulong", "bulig", "palihug", "mayday", "urgent",
         "delikado", "peligro", "ambulansya", "ambulance", "rescue",
         "respond", "emerhensya", "ayuda", "singgit", "kalisod",
         "panalipdi",
         "emergen",  # real test (whisper base): "emergency" -> "emergents"
         "proble", "oblema",  # pieces of "problema" / English "problem"
         ],
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


# Short keywords that are also INSIDE ordinary words. Checked against the
# ordinary words in everyday speech (see the false-positive test), these made a
# plain sentence look like an emergency:
#   "suno"  <- "sunod" (next)        -> FIRE
#   "bali"  <- "balik", "baliw", "balita" -> FALL_INJURY
#   "hawa"  <- "hawak" (hold)        -> MEDICAL_SYMPTOM
# They still count, but only as a WHOLE word ("na bali akong tiil", "suno."),
# never as a piece of another word. The long forms (sunog, nabali, ginhawa...)
# stay plain substrings, so a transcript with the words glued together still
# matches them.
_WHOLE_WORD_ONLY = {"suno", "bali", "hawa"}


def _normalize_spaced(text):
    """Lowercase letters only, single spaces, padded -- for whole-word checks."""
    text = re.sub(r"[^a-z\s]", " ", text.lower())
    return " " + re.sub(r"\s+", " ", text).strip() + " "


# A keyword that sits inside another common word: ignore it there. "amot"
# (gamot, medicine) is inside "kamot" (hand), which appears in injuries
# ("nabali akong kamot"); MEDICATION is checked before FALL_INJURY, so without
# this such an injury would be reported as a medicine problem.
_BLOCKED_BY = {"amot": ("kamot",)}


def _hit(keyword, compact, spaced):
    if keyword in _WHOLE_WORD_ONLY:
        return f" {keyword} " in spaced
    for blocker in _BLOCKED_BY.get(keyword, ()):
        compact = compact.replace(blocker, "")
    return keyword in compact


def categorize(transcript):
    """Returns (category, message) for the given transcript text.

    Falls back to "UNKNOWN" (generic message) if no keyword matches --
    still triggers the emergency flow, just without a specific category.
    """
    compact = _normalize(transcript)
    spaced = _normalize_spaced(transcript)
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(_hit(kw, compact, spaced) for kw in keywords):
            return category, MESSAGES[category]
    return "UNKNOWN", MESSAGES["UNKNOWN"]
