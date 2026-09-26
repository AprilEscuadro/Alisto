"""Maps enrolled keyword group names to emergency categories.

Simplified to two words total: the wake word ("Alisto") is heard first,
then the system listens briefly for the confirmation word ("Tabang"). The
larger multi-keyword categorization (fire/medical/intruder/etc, one per
71-phrase category) was dropped after repeated real-world testing showed the
extra keywords were fragile with only a handful of enrolled samples each --
reliability matters more than category granularity for an emergency device.

Add a new keyword by: 1) adding an entry here, 2) enrolling samples for it
with `python enroll.py <GROUP_NAME>`.
"""

import categorize

WAKE_GROUP = "WAKE"

KEYWORD_TO_CATEGORY = {
    "TABANG": "GENERAL_HELP",
}


def message_for_group(group):
    """Returns (category, sms_message) for a matched keyword group name.
    Falls back to the generic UNKNOWN message for an unrecognized group."""
    category = KEYWORD_TO_CATEGORY.get(group, "UNKNOWN")
    return category, categorize.MESSAGES[category]
