"""Cleans what whisper heard into something readable for the family's app.

Whisper (Tagalog mode, no Cebuano model) writes the spoken phrase with odd
spellings: "alistot abang.", "alistoon na hula-go.", "a listo na ay kawatan.".
The app shows this as the alert's "Detected phrase", so it is cleaned in two
honest steps (the raw text is always kept on the alert as well):

1. If the text is close enough to ONE of the predefined phrases below (the same
   phrases the system is built and tested for), show that phrase:
       "alistot abang."  ->  "Alisto, tabang"
   "Close enough" is strict: a high similarity AND a clear lead over the
   second-best phrase, AND the phrase must belong to the same emergency
   category the keyword matcher (categorize.py) already chose. An ambiguous
   text is never forced onto a wrong phrase.
2. Otherwise show what was heard, only with the wake word spelled "Alisto"
   ("alistoon na hula-go." -> "Alisto na hula-go"). No other word is changed
   or invented.
"""

import re
from difflib import SequenceMatcher

# The predefined phrases (what the elder is expected to say), grouped by the
# category names used in categorize.py and written the way they should be
# shown. Add a phrase here to make the app show it cleanly.
KNOWN_PHRASES_BY_CATEGORY = {
    "FIRE": [
        "Alisto, naay sunog", "Alisto, nasunog ang balay", "Alisto, sunog",
        "Alisto, naay kalayo", "Alisto, nasunog ang kusina",
        "Alisto, naay nagdilaab", "Alisto, naay aso sa kusina",
        "Alisto, nagdilaab ang kalayo", "Alisto, naay gisunog",
        "Alisto, tabang, sunog",
    ],
    "INTRUDER": [
        "Alisto, naay nisulod", "Alisto, naay kawatan",
        "Alisto, naay nisulod nga tawo", "Alisto, naay nangawat",
        "Alisto, naay tulisan", "Alisto, naay nangawat sa balay",
        "Alisto, naay nisulod sa bintana", "Alisto, naay kawatan sa sulod",
        "Alisto, nakulong ko",
    ],
    "MEDICATION": [
        "Alisto, nahurot na akong tambal", "Alisto, kinahanglan nako ang tambal",
        "Alisto, wala koy tambal", "Alisto, ang akong gamot",
        "Alisto, oras na sa akong tambal", "Alisto, oras na sa tambal",
        "Alisto, wala na akong tambal", "Alisto, nakalimot ko sa tambal",
        "Alisto, nahurot ang akong gamot", "Alisto, kinahanglan nako ang gamot",
    ],
    "FALL_INJURY": [
        "Alisto, nahulog ko", "Alisto, natumba ko", "Alisto, nadagma ko",
        "Alisto, nagdugo ako", "Alisto, nabali akong tiil", "Alisto, naigo ko",
        "Alisto, nahulog ko sa hagdan", "Alisto, nadulas ko",
        "Alisto, natumba ko sa kwarto", "Alisto, nasamdan ko",
        "Alisto, naipit akong tiil",
    ],
    "MEDICAL_SYMPTOM": [
        "Alisto, naay sakit akong dughan", "Alisto, mahimatay ko",
        "Alisto, huyang kaayo ko", "Alisto, lisod ko ginhawa",
        "Alisto, nalipong ko", "Alisto, sakit akong ulo",
        "Alisto, nasakit akong dughan", "Alisto, nagsuka ko",
        "Alisto, nahimatay ko", "Alisto, hilanat ko", "Alisto, nagkurog ko",
    ],
    "GENERAL_HELP": [
        "Alisto, tabang", "Alisto, tabang kaayo", "Alisto, naay problema",
        "Alisto, emergency", "Alisto, palihug tabang", "Alisto, tabang palihug",
        "Alisto, naa koy problema", "Alisto, kinahanglan nako ug tabang",
        "Alisto, emergency kini", "Alisto, tabangi ko",
    ],
}

KNOWN_PHRASES = [p for ps in KNOWN_PHRASES_BY_CATEGORY.values() for p in ps]

MIN_SIMILARITY = 0.80   # how alike the heard text and the phrase must be (0-1)
MIN_LEAD = 0.04         # and how far ahead of the second-best phrase

# Same fragments as whisper_asr.WAKE_FRAGMENTS (kept here so this module stands
# alone); a token containing one of them is the spoken "Alisto".
_WAKE = ("alist", "alisto", "alista", "aristo", "alerto", "ales", "alyst",
         "alest", "listo")
# Sounds whisper tacks onto "Alisto" ("alistoon", "alistot", "alistos"...).
_WAKE_TAIL = {"", "on", "ong", "ow", "oh", "t", "s", "p", "h", "n", "ng", "w",
              "k", "d", "m", "a", "u", "i", "r"}


def _letters(text):
    """Lowercase letters only, no spaces: the form two texts are compared in."""
    return re.sub(r"[^a-z]", "", (text or "").lower())


def best_known_phrase(text, category=None):
    """Returns (phrase or None, similarity, lead). With a specific `category`
    only that category's phrases are considered; for UNKNOWN (the keyword
    matcher found nothing) no phrase is offered at all."""
    if category == "UNKNOWN":
        return None, 0.0, 0.0
    phrases = KNOWN_PHRASES_BY_CATEGORY.get(category) if category else None
    if not phrases:
        phrases = KNOWN_PHRASES
    # Two readings of the heard text are tried and the closer one counts:
    #  - with the wake word spelled "Alisto" first, so a sound whisper glued
    #    onto it ("alistoon", "alistot") does not count against the phrase:
    #    "alistoon sonob" is compared as "alisto sonob";
    #  - exactly as heard, because that trailing letter is sometimes really the
    #    first letter of the next word: "alistos o nob" is "alisto sonob".
    readings = {r for r in (_letters(fix_wake_word(text)), _letters(text)) if r}
    if not readings:
        return None, 0.0, 0.0

    def similarity(phrase):
        target = _letters(phrase)
        return max(SequenceMatcher(None, r, target).ratio() for r in readings)

    scored = sorted(((similarity(p), p) for p in phrases), reverse=True)
    best = scored[0]
    # The lead is measured against the best phrase of ANY category, so a text
    # that is nearly as close to a different category's phrase is rejected.
    others = sorted((similarity(p), p) for p in KNOWN_PHRASES if p != best[1])
    second = others[-1][0] if others else 0.0
    return best[1], best[0], best[0] - second


def fix_wake_word(text):
    """Spells the wake word "Alisto" and changes nothing else."""
    tokens = [t for t in re.split(r"\s+", (text or "").strip()) if t]
    words = [re.sub(r"[^a-z]", "", t.lower()) for t in tokens]
    for i, word in enumerate(words):
        span = 1
        if not any(frag in word for frag in _WAKE):
            # "alis, to": the wake word split over two tokens
            if i + 1 < len(words) and any(
                    frag in word + words[i + 1] for frag in _WAKE):
                word, span = word + words[i + 1], 2
            else:
                continue
        # "a listo": a lone letter before it belongs to the wake word
        start = i - 1 if i > 0 and len(words[i - 1]) <= 1 else i
        rest = ""
        for frag in ("alisto", "alista", "listo"):
            if frag in word:
                rest = word[word.index(frag) + len(frag):]
                break
        out = ["Alisto"]
        if rest and rest not in _WAKE_TAIL:
            out.append(rest)          # "alistotabang" -> Alisto tabang
        return " ".join(tokens[:start] + out + tokens[i + span:]).strip(" .,")
    return (text or "").strip(" .,")


def display_phrase(text, category=None):
    """What the app shows as the alert's detected phrase. `category` is what
    categorize.categorize() returned for the same text."""
    phrase, sim, lead = best_known_phrase(text, category)
    if phrase is not None and sim >= MIN_SIMILARITY and lead >= MIN_LEAD:
        return phrase
    cleaned = fix_wake_word(text)
    return cleaned[:1].upper() + cleaned[1:] if cleaned else ""
