import asyncio
import sys


def main() -> int:
    from .ui import App

    try:
        asyncio.run(App().run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
