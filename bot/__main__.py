"""Entry point: python -m bot"""

import asyncio
import sys


def main():
    from bot.app import run

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(run())


if __name__ == "__main__":
    main()
