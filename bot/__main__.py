"""Entry point: python -m bot"""

import asyncio
import os
import sys


def main():
    # Guard for pythonw.exe (headless) — stdout/stderr are None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    from bot.app import run

    # Windows default is ProactorEventLoop which supports subprocesses.
    # Do NOT set WindowsSelectorEventLoopPolicy — it breaks subprocess support.
    asyncio.run(run())


if __name__ == "__main__":
    main()
