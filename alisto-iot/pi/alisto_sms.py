"""Sends the ALISTO emergency SMS via the SIM7600 GSM module using AT commands.

Rebuilt after the original file was lost in the Trixie->Bookworm OS
reinstall. AT port is ttyUSB3 (SIM7600 exposes several ttyUSB interfaces --
diagnostic/GPS/AT/modem/aux -- confirmed by testing which one actually
replies OK to a plain "AT"). ModemManager auto-claims this port on boot and
must stay disabled, otherwise it blocks direct access.
"""

import re
import time

import serial

SIM_PORT = "/dev/serial/by-id/usb-SimTech__Incorporated_SimTech__Incorporated_0123456789ABCDEF-if02-port0"
SIM_BAUD = 115200
RECIPIENT = "+639659536513"  # 09659536513 in international format -- used
# only as a last-resort fallback when no emergency_contact numbers could be
# read from Firestore (or the cache) at all; see trigger_emergency() in
# alisto_main.py, which normally passes each contact's own number in.

DEFAULT_MESSAGE = (
    "ALISTO ALERT!\n"
    "Emergency button/phrase activated. Please check immediately!"
)

AT_TIMEOUT_S = 5
SEND_TIMEOUT_S = 30  # widened from 15 -- real testing showed a send that
# gets all the way to the "> " prompt correctly can still take longer than
# 15s to get its final +CMGS confirmation over PH cellular networks.
# Giving up too early meant retrying while the modem was still mid-
# transmission on the FIRST attempt, which left it unresponsive to every
# subsequent AT command (even a plain "AT") for the rest of the emergency.


def _send_at(ser, cmd, wait_s=AT_TIMEOUT_S):
    ser.write((cmd + "\r\n").encode())
    time.sleep(0.3)
    deadline = time.monotonic() + wait_s
    buf = b""
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf += chunk
            if b"OK" in buf or b"ERROR" in buf:
                break
        else:
            time.sleep(0.1)
    return buf.decode(errors="ignore")


def send_sms(message=None, recipient=None):
    text = message or DEFAULT_MESSAGE
    target = recipient or RECIPIENT
    try:
        ser = serial.Serial(SIM_PORT, SIM_BAUD, timeout=1)
        time.sleep(0.5)

        # DIAGNOSTIC: every intermediate step now prints its own response
        # -- a full-timeout empty final response could mean the modem
        # never even answered the first "AT" (wedged/unresponsive), or
        # got that far but silently dropped the CMGS body. Narrowing
        # which step actually goes quiet is the point.
        at_resp = _send_at(ser, "AT")
        print(f"send_sms: AT -> {at_resp!r}")
        if "OK" not in at_resp:
            # Modem isn't even answering a plain "AT" -- almost certainly
            # still busy finishing a previous attempt's in-flight network
            # transmission, not actually dead. Piling more commands
            # (AT+CMGF, AT+CMGS...) onto that state is exactly what left
            # it wedged for the rest of the emergency in real testing.
            # Bail out now and let the caller's retry delay give it real
            # recovery time instead.
            print("send_sms: modem not responding to AT -- likely still busy from a previous attempt, aborting.")
            ser.close()
            return False

        cmgf_resp = _send_at(ser, "AT+CMGF=1")
        print(f"send_sms: AT+CMGF=1 -> {cmgf_resp!r}")

        ser.write(f'AT+CMGS="{target}"\r'.encode())
        time.sleep(0.5)
        prompt_resp = ser.read(ser.in_waiting or 1)
        print(f"send_sms: AT+CMGS prompt -> {prompt_resp!r}")

        ser.write(text.encode() + bytes([26]))  # Ctrl+Z sends the message
        deadline = time.monotonic() + SEND_TIMEOUT_S
        buf = b""
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf += chunk
                if b"+CMGS" in buf or b"ERROR" in buf:
                    break
            else:
                time.sleep(0.2)
        resp = buf.decode(errors="ignore")

        if "+CMGS" not in resp and "ERROR" not in resp:
            # Timed out with no confirmation at all -- the modem may still
            # be sitting in "waiting for more SMS text" mode if our Ctrl+Z
            # was somehow missed, which would silently swallow every
            # subsequent AT command (even a plain "AT") as more message
            # body text instead of treating it as a command. This exactly
            # matches what real testing showed: every attempt after a
            # timed-out one got zero response, including to "AT" itself.
            # ESC (0x1B) is the standard way to abort SMS text entry and
            # force the modem back to command mode before giving up, so
            # the NEXT attempt doesn't inherit this same stuck state.
            ser.write(bytes([27]))
            time.sleep(0.5)
            ser.read(ser.in_waiting or 1)  # discard whatever that produced

        ser.close()

        print(f"SIM7600 response (to {target}):", resp.strip())
        return "+CMGS" in resp

    except Exception as e:
        print(f"send_sms to {target} failed:", e)
        return False


def read_sim_health():
    """Snapshot of what the SIM/modem can currently do, for working out WHY
    an SMS failed (and for a startup log line). Returns a dict:
        sim         True/False -- AT+CPIN? says READY (False = missing/locked)
        registered  True/False -- attached to the cellular network (CREG/CEREG)
        csq, dbm    signal quality 0-31 and the dBm it means (None = unknown)
        error       set only if the modem itself didn't answer
    Caller must hold gps.port_lock -- same single AT port as send_sms()."""
    result = {"sim": None, "registered": None, "csq": None, "dbm": None}
    try:
        ser = serial.Serial(SIM_PORT, SIM_BAUD, timeout=1)
        time.sleep(0.5)
        try:
            if "OK" not in _send_at(ser, "AT", wait_s=3):
                result["error"] = "modem not responding"
                return result
            result["sim"] = "READY" in _send_at(ser, "AT+CPIN?", wait_s=3)

            match = re.search(r"\+CSQ:\s*(\d+),", _send_at(ser, "AT+CSQ", wait_s=3))
            if match and int(match.group(1)) != 99:  # 99 = not known/detectable
                result["csq"] = int(match.group(1))
                result["dbm"] = -113 + 2 * result["csq"]

            registered = False
            for cmd, tag in (("AT+CREG?", "CREG"), ("AT+CEREG?", "CEREG")):
                match = re.search(rf"\+{tag}:\s*\d+,\s*(\d+)", _send_at(ser, cmd, wait_s=3))
                # 1 = registered (home), 5 = registered (roaming)
                if match and int(match.group(1)) in (1, 5):
                    registered = True
            result["registered"] = registered
        finally:
            ser.close()
    except Exception as e:
        result["error"] = str(e)
    return result


def sms_failure_reason(health):
    """Best guess at why an SMS just failed, from read_sim_health():
    MODEM (not answering), SIM (missing/locked), SIGNAL (not on the
    network), else LOAD -- registered with a working SIM but the carrier
    still refused/never delivered it, which for a prepaid SIM is almost
    always no load/credit."""
    if health.get("error"):
        return "MODEM"
    if health.get("sim") is False:
        return "SIM"
    if health.get("registered") is False:
        return "SIGNAL"
    return "LOAD"


def read_own_sim_number():
    """Attempts to read this SIM's own phone number via AT+CNUM. Many
    prepaid SIMs (the common case for PH telcos -- Globe/Smart/TNT/TM)
    never have the MSISDN field programmed by the carrier, so this often
    returns None even on a genuinely working, correctly inserted SIM --
    that's a real hardware/carrier limitation, not a bug here. Callers
    should treat None as "couldn't auto-detect" and fall back to manual
    entry (see the app's Device Information screen), not as an error."""
    try:
        ser = serial.Serial(SIM_PORT, SIM_BAUD, timeout=1)
        time.sleep(0.5)
        resp = _send_at(ser, "AT+CNUM")
        ser.close()
    except Exception as e:
        print(f"read_own_sim_number failed: {e}")
        return None

    # Typical successful reply: +CNUM: "","639171234567",145 -- the "145"
    # type code means international format, but real testing showed the
    # SIM7600 doesn't always include the "+" itself even then, so it's
    # added back here rather than trusting the modem's own formatting.
    match = re.search(r'\+CNUM:\s*"[^"]*",\s*"(\+?\d+)"', resp)
    if not match:
        return None
    number = match.group(1)
    return number if number.startswith("+") else f"+{number}"


if __name__ == "__main__":
    ok = send_sms("ALISTO test message.")
    print("Sent." if ok else "Failed.")
