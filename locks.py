import asyncio

stats_lock = asyncio.Lock()
active_lock = asyncio.Lock()