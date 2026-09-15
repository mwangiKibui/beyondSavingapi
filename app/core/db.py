import asyncpg
from fastapi import Request


async def get_pool(request: Request) -> asyncpg.Pool | None:
    return request.app.state.db_pool
