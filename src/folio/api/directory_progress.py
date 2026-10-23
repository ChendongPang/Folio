"""Readable directory activity, with stable rows and payload-free model status."""

from time import monotonic

MODEL_STATUSES = {"waiting", "connected", "thinking", "responding"}


class DirectoryProgress:
    def __init__(self):
        self.started = self.step_started = monotonic()
        self.activities = []
        self.model_status = None

    def update(self, value):
        if isinstance(value, str):
            text = value.strip()
            if not text or (self.activities and self.activities[-1]["text"] == text):
                return
            if self.activities:
                self.activities[-1]["status"] = "completed"
            self.activities.append({
                "id": f"directory-{len(self.activities) + 1}",
                "text": text, "status": "running",
            })
            self.step_started = monotonic()
            self.model_status = None
        elif isinstance(value, dict) and value.get("status") in MODEL_STATUSES:
            # Only known transport states cross into the user interface. The
            # model's JSON, reasoning, headers and request details never do.
            self.model_status = value["status"]

    def event(self):
        activity = dict(self.activities[-1]) if self.activities else None
        return {
            "text": activity["text"] if activity else "正在准备目录研究。",
            "activity": activity,
            "elapsed_seconds": int(monotonic() - self.started),
            "step_elapsed_seconds": int(monotonic() - self.step_started),
            "model_status": self.model_status,
        }

    def finish(self, *, completed):
        if self.activities:
            self.activities[-1]["status"] = "completed" if completed else "failed"
        return [dict(item) for item in self.activities]
