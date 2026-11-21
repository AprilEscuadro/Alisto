"""When should the device speak "your SIM load is low"?

Pure decision logic (no hardware), kept out of alisto_main.py so it can be
tested on its own. alisto_main.load_balance_loop() calls decide() after every
successful balance check.

Rules:
  * Low = a readable balance under LOW_LOAD_THRESHOLD (the app flags the same
    PHP 10 as "low").
  * Announce once when the load first drops below it, then again every
    REPEAT_S while it is still low.
  * Re-arm as soon as a reading is back at/above the threshold (the SIM was
    reloaded), so the NEXT low period announces again.
  * No spoken warnings during quiet hours (a voice at 3 AM would startle an
    elderly person): the announcement is deferred, and the caller re-checks
    soon after the quiet period ends.
  * Never while the device is busy (an emergency is in progress).
  * An unreadable balance (None) changes nothing.
"""

LOW_LOAD_THRESHOLD = 10.0     # pesos; same as the app's "load low" flag
REPEAT_S = 12 * 60 * 60       # re-announce this often while still low
QUIET_START_HOUR = 21         # no announcements from 9 pm ...
QUIET_END_HOUR = 7            # ... until 7 am


def in_quiet_hours(hour):
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def decide(amount, last_announced, now, hour, busy=False):
    """Returns (action, new_last_announced). action is one of:
        "announce"  speak the warning now (caller stores the new timestamp)
        "defer"     low and due, but not a good moment -- try again soon
        "none"      nothing to do
    `last_announced` and `now` use the same clock (time.monotonic());
    `hour` is the local hour 0-23."""
    if amount is None:
        return ("none", last_announced)
    if amount >= LOW_LOAD_THRESHOLD:
        return ("none", None)  # healthy again: re-arm
    if last_announced is not None and now - last_announced < REPEAT_S:
        return ("none", last_announced)  # already warned recently
    if busy or in_quiet_hours(hour):
        return ("defer", last_announced)
    return ("announce", now)
