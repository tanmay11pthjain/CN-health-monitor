#!/usr/bin/env python3
# Puts REAL load on this laptop so the agent sends real alerts (for the demo).
#
#   python stress.py cpu 30        all CPU cores busy for 30 seconds
#   python stress.py mem 85 30     use memory until the laptop is at 85 %, hold 30 s
#
# Stops by itself; Ctrl+C stops it early. Memory never goes above 92 %.

import multiprocessing
import sys
import time


def burn(seconds):
    end = time.time() + seconds
    x = 0
    while time.time() < end:
        x = (x * 31 + 7) % 1000003


def cpu(seconds):
    n = multiprocessing.cpu_count()
    print("Loading %d CPU cores for %d s ..." % (n, seconds), flush=True)
    procs = [multiprocessing.Process(target=burn, args=(seconds,)) for _ in range(n)]
    for p in procs:
        p.start()
    try:
        for p in procs:
            p.join()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()
    print("done", flush=True)


def mem(target, seconds):
    import psutil
    target = min(target, 92)
    blocks = []
    print("Filling memory up to %d %% ..." % target, flush=True)
    try:
        while psutil.virtual_memory().percent < target:
            blocks.append(bytearray(50 * 2**20))   # 50 MB, really allocated
        print("memory at %.1f %%, holding for %d s" % (psutil.virtual_memory().percent, seconds),
              flush=True)
        time.sleep(seconds)
    except (KeyboardInterrupt, MemoryError):
        pass
    blocks.clear()
    print("done, memory released", flush=True)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "cpu":
            cpu(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
        elif sys.argv[1] == "mem":
            mem(int(sys.argv[2]) if len(sys.argv) > 2 else 85,
                int(sys.argv[3]) if len(sys.argv) > 3 else 30)
        else:
            raise IndexError
    except (IndexError, ValueError):
        print(__doc__ or "usage: python stress.py cpu 30  |  python stress.py mem 85 30")
        sys.exit(2)
