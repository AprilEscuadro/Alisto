"""When should the Pi write its (correct) time into the ESP32's DS3231 RTC?

The DS3231 keeps the time through a power cut so an offline boot still gets
the right clock (alisto_main.sync_clock_from_rtc reads it at every boot). But
nothing ever WROTE the time into it: the firmware's CMD:SET_TIME was "meant to
be used once during setup" by hand. A fresh RTC (or one whose coin cell died)
therefore stayed at 2000-01-01 forever.

So alisto_main.rtc_keeper_loop() now does it: whenever the Pi's clock is known
to be right (NTP synchronized) it reads the RTC and, if the RTC is missing the
time or has drifted, writes the Pi's time into it. The decision lives here so
it can be tested without hardware.
"""

from datetime import datetime

MIN_PLAUSIBLE_YEAR = 2024   # same cutoff as sync_clock_from_rtc: earlier = reset
MAX_DRIFT_S = 5             # a healthy DS3231 drifts ~1 minute a year


def parse_rtc(text):
    """'YYYY-MM-DD HH:MM:SS' from the ESP32 -> datetime, or None."""
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def format_set_time(dt):
    """The argument for CMD:SET_TIME: (local time, like sync_clock_from_rtc)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def decide_rtc_write(rtc_text, system_now, ntp_synced,
                     max_drift_s=MAX_DRIFT_S, min_year=MIN_PLAUSIBLE_YEAR):
    """Returns (write: bool, reason: str).
    `system_now` is the Pi's local time as a naive datetime.
    Only ever writes when the Pi's own clock is trustworthy (ntp_synced)."""
    if not ntp_synced:
        return False, "Pi clock not NTP-synchronized yet"
    if rtc_text is None:
        return False, "no answer from the RTC"
    rtc = parse_rtc(rtc_text)
    if rtc is None:
        return False, f"unreadable RTC reply {rtc_text!r}"
    if rtc.year < min_year:
        return True, f"RTC reset/uninitialised ({rtc_text})"
    drift = abs((system_now - rtc).total_seconds())
    if drift > max_drift_s:
        return True, f"RTC drifted {int(drift)} s"
    return False, "RTC in sync"
