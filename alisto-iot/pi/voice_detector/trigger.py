"""Called the instant the phrase is detected.

TODO: replace the body of on_emergency_detected() with your existing
"emergency detected, sending in 5 seconds" announcement + cancel-window +
SIM7600 SMS logic. This is just a placeholder so detector.py runs standalone
for testing.
"""

import time


def on_emergency_detected():
    print("!!! EMERGENCY PHRASE DETECTED !!!")
    print("(placeholder) Would now announce, start 5s cancel window, then send SMS.")
    time.sleep(1)
