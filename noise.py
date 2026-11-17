#!/usr/bin/env python3
# Noisy host (h6): sends a lot of useless UDP data to make a traffic jam.
# Plain Python, so we don't need iperf.
#
# Usage:  python3 noise.py --to 10.0.0.5 --mbps 20 --seconds 600

import argparse
import signal
import socket
import sys
import time

JUNK_PORT = 6000          # nobody listens there, the data is thrown away
SIZE = 1400               # bytes per packet (fits in one Ethernet frame)


def main():
    p = argparse.ArgumentParser(description="make a traffic jam with UDP junk")
    p.add_argument("--to", required=True, help="where to send the junk, e.g. 10.0.0.5")
    p.add_argument("--mbps", type=float, default=20, help="how fast to send (Mbit/s)")
    p.add_argument("--seconds", type=float, default=600, help="how long")
    args = p.parse_args()
    if args.mbps <= 0 or args.seconds <= 0:
        print("ERROR: --mbps and --seconds must be bigger than 0", file=sys.stderr)
        sys.exit(2)

    running = [True]

    def stop(*_):
        running[0] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    junk = b"N" * SIZE
    pps = args.mbps * 1e6 / 8 / SIZE        # packets per second
    tick = 0.01                              # send a small burst every 10 ms
    per_tick = pps * tick
    owed = 0.0
    sent = 0
    start = time.time()
    end = start + args.seconds
    next_time = start
    print("noise: sending %.1f Mbit/s to %s:%d for %.0f s" % (
        args.mbps, args.to, JUNK_PORT, args.seconds), flush=True)
    while running[0] and time.time() < end:
        owed += per_tick
        while owed >= 1:
            owed -= 1
            try:
                sock.sendto(junk, (args.to, JUNK_PORT))
                sent += 1
            except OSError:
                pass        # buffer full or network down: just go on
        next_time += tick
        pause = next_time - time.time()
        if pause > 0:
            time.sleep(pause)
        elif pause < -1:
            next_time = time.time()     # we fell far behind, don't try to catch up
    sock.close()
    took = max(time.time() - start, 0.001)
    print("noise: stopped, sent %d packets (%.1f Mbit/s)" % (
        sent, sent * SIZE * 8 / 1e6 / took), flush=True)


if __name__ == "__main__":
    main()
