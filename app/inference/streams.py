"""SSE framing and protocol usage, independent of TCP chunk boundaries."""

import json


class SSEDecoder:
    def __init__(self):
        self.buffer = ""

    def feed(self, text):
        self.buffer += text
        # Normalize line endings across arbitrary chunks without losing a trailing CR.
        self.buffer = self.buffer.replace("\r\n", "\n")
        events = []
        while "\n\n" in self.buffer:
            event, self.buffer = self.buffer.split("\n\n", 1)
            events.append(event)
        return events


def event_data(event):
    raw = "\n".join(line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:"))
    if not raw or raw == "[DONE]":
        return None
    return json.loads(raw)


class UsageCollector:
    def __init__(self, protocol):
        self.protocol = protocol
        self.usage = {}
        self.complete = False
        self.failed = False

    def accept(self, value):
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if kind == "error" or value.get("error") or kind == "response.failed":
            self.failed = True
        candidate = value.get("response", value.get("message", value))
        if isinstance(candidate, dict) and isinstance(candidate.get("usage"), dict):
            self.usage.update(candidate["usage"])
        if self.protocol == "messages" and kind == "message_stop":
            self.complete = True
        elif self.protocol == "responses" and kind in {"response.completed", "response.incomplete"}:
            self.complete = True
        elif self.protocol == "chat/completions" and value.get("usage"):
            self.complete = True
