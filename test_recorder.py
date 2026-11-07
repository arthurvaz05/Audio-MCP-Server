"""Locks the measured Teams call-detection behavior (~20 sockets in call, 0 idle).
Run: .venv/bin/python3 test_recorder.py"""
from recorder import count_media_sockets_from_lsof

# Real lsof -nP -iUDP output captured OUT of a call (2026-07-07):
# only QUIC(:443) with peer + a wildcard listener remain.
IDLE = """\
COMMAND     PID       USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
Microsoft 66171  arthurvaz   88u  IPv6 0x1234      0t0    UDP [2804:14d::1]:57716->[2603:1056::2]:443
Microsoft 66171  arthurvaz   89u  IPv6 0x1235      0t0    UDP [2804:14d::1]:57791->[2001:4860::8844]:443
Microsoft 66177  arthurvaz   90u  IPv4 0x1236      0t0    UDP *:50071
"""

# Real shape captured IN a call: media sockets bound to concrete local IPs
# on high ephemeral ports (subset of the ~20 measured).
IN_CALL = IDLE + """\
Microsoft 66177  arthurvaz   91u  IPv4 0x2001      0t0    UDP 192.168.0.240:50006
Microsoft 66177  arthurvaz   92u  IPv4 0x2002      0t0    UDP 192.168.0.240:50031
Microsoft 66177  arthurvaz   93u  IPv6 0x2003      0t0    UDP [2804:14d::d5e8]:50007
Microsoft 66177  arthurvaz   94u  IPv6 0x2004      0t0    UDP [2804:14d::bc37]:50012
"""

assert count_media_sockets_from_lsof("") == 0, "empty output"
assert count_media_sockets_from_lsof(IDLE) == 0, "idle Teams must count 0"
assert count_media_sockets_from_lsof(IN_CALL) == 4, "in-call media sockets"
# port suffix must not false-match (:5443 is not :443)
TRICKY = IDLE + "Microsoft 66177 arthurvaz 95u IPv4 0x3001 0t0 UDP 192.168.0.240:5443\n"
assert count_media_sockets_from_lsof(TRICKY) == 1, ":5443 is media, not QUIC"
print("all detection tests passed")

# Output routing: headphones must keep sound in the headphones while recording.
from recorder import pick_multi_output
WITH_FONE = ["MacBook Pro Speakers", "BlackHole 2ch", "Multi-Output sem Fone",
             "Multi-Output Fone", "External Headphones"]
NO_FONE = [d for d in WITH_FONE if d != "Multi-Output Fone"]
assert pick_multi_output("MacBook Pro Speakers", WITH_FONE) == "Multi-Output sem Fone"
assert pick_multi_output("Multi-Output sem Fone", WITH_FONE) == "Multi-Output sem Fone"
assert pick_multi_output("Multi-Output Fone", WITH_FONE) == "Multi-Output sem Fone"  # stale from crash: safe default
assert pick_multi_output("External Headphones", WITH_FONE) == "Multi-Output Fone"
assert pick_multi_output("External Headphones", NO_FONE) == "Multi-Output sem Fone", "not created yet: old behavior"
assert pick_multi_output("", WITH_FONE) == "Multi-Output sem Fone", "SwitchAudioSource failed"
print("all routing tests passed")

# Call detection: Teams app OR Chrome (Meet / Teams web).
import recorder
# Real Chrome idle capture 2026-11-07: 8 sockets, all mDNS.
CHROME_IDLE = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n" + \
    "Google 1330 arthurvaz 40u IPv4 0x1 0t0 UDP *:5353\n" * 8
assert recorder.count_media_sockets_from_lsof(CHROME_IDLE) == 0, "idle Chrome counts 0"

_real_probe = recorder.count_app_media_sockets
calls = {}
recorder.count_app_media_sockets = lambda pattern: calls.get(pattern, 0)  # boundary: lsof probe
calls = {"Microsoft Teams.app": 0, "Google Chrome.app": 3}
assert recorder.is_in_call(), "Chrome-only call"
calls = {"Microsoft Teams.app": 4, "Google Chrome.app": 0}
assert recorder.is_in_call(), "Teams-only call"
calls = {}
assert not recorder.is_in_call(), "idle"
assert recorder.count_call_media_sockets() == 0
recorder.count_app_media_sockets = _real_probe
print("all call-detection tests passed")
