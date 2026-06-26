import asyncio
import uvloop

from miniredis.server import main
from miniredis.log_config import setup_logging


if __name__ == "__main__":
    setup_logging()
    uvloop.run(main())
