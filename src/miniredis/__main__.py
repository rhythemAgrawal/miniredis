import asyncio

from miniredis.server import main
from miniredis.store import store, expiration_sweeper


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.create_task(expiration_sweeper(store))
