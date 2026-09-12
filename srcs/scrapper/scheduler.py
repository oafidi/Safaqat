#!/usr/bin/env python3

import os
import signal
import subprocess
import sys
import time


INTERVAL_SECONDS = int(os.getenv("SCRAPER_INTERVAL_SECONDS", "3600"))
child = None
stopping = False


def stop(signum, frame):
    global stopping
    stopping = True
    if child is not None and child.poll() is None:
        child.terminate()


def wait_until(deadline):
    while not stopping:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1))


def main():
    global child

    if INTERVAL_SECONDS < 1:
        raise ValueError("SCRAPER_INTERVAL_SECONDS must be at least 1")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping:
        print("Starting scheduled scraper run", flush=True)
        child = subprocess.Popen([sys.executable, "/app/scrapper.py"])
        return_code = child.wait()
        child = None

        if stopping:
            break

        if return_code != 0:
            print(
                f"Scraper exited with status {return_code}; it will retry at the next interval",
                file=sys.stderr,
                flush=True,
            )

        # Measure the interval from completion. A full catalogue scrape can
        # take longer than the interval and must not trigger another run
        # immediately after it finishes.
        wait_until(time.monotonic() + INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
