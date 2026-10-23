"""Small, ordered-consumption window for independent supplement material selection."""

import asyncio


class SupplementSelections:
    """Preload materials only; edits and their live document context stay sequential."""

    FIELDS = {"evidence", "business_record", "global_facts", "dependencies", "evidence_selected"}

    def __init__(self, engine, sources):
        self.engine = engine
        self.sources = iter(sources)
        self.tasks = {}

    def fill(self):
        while len(self.tasks) < 3 and (entry := next(self.sources, None)) is not None:
            index, source = entry
            self.tasks[index] = asyncio.create_task(
                self.prepare(source), name=f"supplement-selection-{index}"
            )

    async def prepare(self, source):
        selected = await self.engine.prepare_context("repair", source)
        return {field: selected[field] for field in self.FIELDS if field in selected}

    async def __aenter__(self):
        self.fill()
        return self

    async def get(self, index):
        self.fill()
        task = self.tasks[index]
        try:
            # The owner cancels and drains the window once. Propagating caller
            # cancellation directly into this await would cancel it twice.
            return await asyncio.shield(task)
        finally:
            if task.done():
                del self.tasks[index]

    async def __aexit__(self, exc_type, exc, traceback):
        for task in self.tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()
        drained = asyncio.gather(*self.tasks.values(), return_exceptions=True)
        interrupted = False
        while not drained.done():
            try:
                await asyncio.shield(drained)
            except asyncio.CancelledError:
                # A second external cancellation cannot abandon HTTP cleanup.
                interrupted = True
        self.tasks.clear()
        if interrupted:
            raise asyncio.CancelledError
