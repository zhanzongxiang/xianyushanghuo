"""Concurrency shared by one Telegram client's files and preview downloads."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class DownloadScheduler:
    request_limit = 8

    def __init__(self, file_concurrency: int):
        self.files = asyncio.Semaphore(file_concurrency)
        self.requests = asyncio.Semaphore(self.request_limit)
        self.condition = asyncio.Condition()
        self.pending_files = 0
        self.background_active = 0

    @asynccontextmanager
    async def file(self):
        async with self.condition:
            self.pending_files += 1
        try:
            async with self.files:
                yield
        finally:
            async with self.condition:
                self.pending_files -= 1
                self.condition.notify_all()

    @asynccontextmanager
    async def background(self):
        async with self.condition:
            await self.condition.wait_for(
                lambda: self.background_active < (1 if self.pending_files else 4))
            self.background_active += 1
        try:
            async with self.requests:
                yield
        finally:
            async with self.condition:
                self.background_active -= 1
                self.condition.notify_all()
