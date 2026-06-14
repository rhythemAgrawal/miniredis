import asyncio

from miniredis.server import main
from miniredis.logging import setup_logging


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
