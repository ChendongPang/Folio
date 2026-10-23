"""Shared visual specialist: receive authored requests, never own business prose."""

from pathlib import Path

from folio.visuals.workflow import VisualSession, build_visuals


class VisualAgent:
    """Reuse the existing bounded HTML/Mermaid workflow and content-addressed cache.

    The author owns VisualRequest/source blocks; this specialist may generate and
    revise figure assets only. A shared session joins prefetch and final delivery
    under the same concurrency, per-identity attempt budget and cache locks.
    """

    role = "visual"

    def __init__(self, folder, call, progress, measure=None, *, session=None, builder=None):
        self.folder = Path(folder)
        self.call = call
        self.progress = progress
        self.measure = measure
        self.session = session or VisualSession()
        self.builder = builder or build_visuals

    async def build(self, plan, states, *, write_manifest=True):
        return await self.builder(
            plan,
            states,
            self.folder,
            self.call,
            self.progress,
            self.measure,
            session=self.session,
            write_manifest=write_manifest,
        )
