"""SSE framing and protocol usage, independent of TCP chunk boundaries."""

import json

SSE_DONE = object()


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
    if not raw:
        return None
    if raw == "[DONE]":
        return SSE_DONE
    return json.loads(raw)


def chat_choice_output(choice):
    """Recognize result-bearing deltas without requiring nonempty text output."""
    if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
        raise ValueError("invalid_stream_choice")
    delta = choice["delta"]
    output = False
    for key in ("content", "reasoning", "reasoning_content", "refusal"):
        value = delta.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid_stream_delta")
        output = output or bool(value)
    functions = []
    if delta.get("function_call") is not None:
        functions.append(delta["function_call"])
    if delta.get("tool_calls") is not None:
        calls = delta["tool_calls"]
        if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
            raise ValueError("invalid_stream_tools")
        for call in calls:
            if call.get("function") is not None:
                functions.append(call["function"])
    for function in functions:
        if not isinstance(function, dict):
            raise ValueError("invalid_stream_function")
        for key in ("name", "arguments"):
            value = function.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError("invalid_stream_function")
            output = output or bool(value)
    if delta.get("audio") is not None:
        audio = delta["audio"]
        if not isinstance(audio, dict):
            raise ValueError("invalid_stream_audio")
        for key in ("data", "transcript"):
            value = audio.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError("invalid_stream_audio")
            output = output or bool(value)
    reason = choice.get("finish_reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("invalid_stream_finish_reason")
    return output, output or bool(reason)


class UsageCollector:
    def __init__(self, protocol):
        self.protocol = protocol
        self.usage = {}
        self.complete = False
        self.failed = False
        self.result_ready = False

    def accept(self, value):
        if value is SSE_DONE and self.protocol == "chat/completions":
            self.complete = True
        if not isinstance(value, dict):
            return
        if self.protocol == "chat/completions" and "choices" in value:
            choices = value["choices"]
            if not isinstance(choices, list):
                raise ValueError("invalid_stream_choices")
            for choice in choices:
                has_output, has_result = chat_choice_output(choice)
                self.result_ready = self.result_ready or has_result
                if has_output:
                    # Earlier cumulative counts cannot describe newly emitted output.
                    # A snapshot on this same frame is applied below, after invalidation.
                    self.usage = {}
        kind = value.get("type")
        if kind == "error" or value.get("error") or kind in {"response.failed", "response.incomplete"}:
            self.failed = True
        candidate = value.get("response", value.get("message", value))
        if isinstance(candidate, dict) and "usage" in candidate:
            snapshot = candidate["usage"]
            if self.protocol == "messages":
                if isinstance(snapshot, dict):
                    self.usage.update(snapshot)
            else:
                # Chat/Responses counts are snapshots, not additive Messages deltas.
                self.usage = dict(snapshot) if isinstance(snapshot, dict) else {}
        if self.protocol == "messages" and kind == "message_stop":
            self.complete = True
        elif self.protocol == "responses" and kind in {"response.completed", "response.incomplete"}:
            self.complete = True
