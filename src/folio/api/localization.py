"""Localize JSON and SSE display fields while preserving streaming and downloads."""

import json
import re

from folio.i18n import localize_payload
from folio.runtime.language import normalize_language


class LocalizationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        language = normalize_language(headers.get(b"accept-language", b"").decode("latin1"))
        pending_start = None
        buffer = b""
        mode = None

        def frame(raw):
            try:
                lines = raw.decode("utf-8").splitlines()
            except UnicodeDecodeError:
                # Preserve malformed or truncated upstream bytes for the existing
                # stream error handling instead of failing inside localization.
                return raw
            data = [line[5:].lstrip() for line in lines if line.startswith("data:")]
            if not data:
                return raw
            event = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
            try:
                payload = json.loads("\n".join(data))
            except ValueError:
                return raw
            payload = localize_payload(payload, language, event=event)
            remaining = [line for line in lines if not line.startswith("data:")]
            remaining.append("data: " + json.dumps(payload, ensure_ascii=False))
            return "\n".join(remaining).encode("utf-8")

        async def localized_send(message):
            nonlocal pending_start, buffer, mode
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                normalized_headers = {key.lower(): value for key, value in response_headers}
                content_type = normalized_headers.get(b"content-type", b"").lower().split(b";", 1)[0].strip()
                mode = "json" if content_type == b"application/json" or content_type.endswith(b"+json") else "sse" if content_type == b"text/event-stream" else None
                if b"content-disposition" in normalized_headers or normalized_headers.get(b"content-encoding", b"identity").lower() != b"identity":
                    # Downloaded artifacts are immutable, including JSON audit files.
                    mode = None
                if mode:
                    vary = [part.strip() for key, value in response_headers if key.lower() == b"vary"
                            for part in value.split(b",") if part.strip()]
                    if b"*" not in vary and b"accept-language" not in {part.lower() for part in vary}:
                        vary.append(b"Accept-Language")
                    response_headers = [(k, v) for k, v in response_headers
                                        if k.lower() not in {b"content-length", b"content-language", b"vary", b"etag", b"content-md5", b"digest"}]
                    response_headers.extend([(b"content-language", language.encode()),
                                             (b"vary", b", ".join(vary))])
                    message = message | {"headers": response_headers}
                if mode == "json":
                    pending_start = message
                    return
                await send(message)
                return
            if message["type"] != "http.response.body" or not mode:
                await send(message)
                return
            buffer += message.get("body", b"")
            if mode == "json":
                if message.get("more_body", False):
                    return
                try:
                    payload = localize_payload(json.loads(buffer), language)
                    buffer = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                except ValueError:
                    pass
                await send(pending_start | {"headers": pending_start["headers"] + [(b"content-length", str(len(buffer)).encode())]})
                await send(message | {"body": buffer})
                buffer = b""
                return
            # SSE frames can straddle ASGI chunks, including UTF-8 byte boundaries.
            output = []
            while match := re.search(rb"\r?\n\r?\n", buffer):
                raw, buffer = buffer[:match.start()], buffer[match.end():]
                output.append(frame(raw) + b"\n\n")
            if not message.get("more_body", False) and buffer:
                output.append(frame(buffer))
                buffer = b""
            if output or not message.get("more_body", False):
                await send(message | {"body": b"".join(output)})

        await self.app(scope, receive, localized_send)
