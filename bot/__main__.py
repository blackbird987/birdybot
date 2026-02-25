"""Entry point: python -m bot"""

import asyncio
import sys


def main():
    from bot.app import run

    # Windows default is ProactorEventLoop which supports subprocesses.
    # Do NOT set WindowsSelectorEventLoopPolicy — it breaks subprocess support.
    asyncio.run(run())


if __name__ == "__main__":
    main()
