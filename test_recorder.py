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
