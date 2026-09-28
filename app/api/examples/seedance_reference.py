"""pip install httpx; set API_BASE, API_KEY, REFERENCE_FILE (JPEG or PNG).

Resume the same request by running again with the same STATE_FILE.
Use a different STATE_FILE only for an intentionally NEW paid generation.
State contains temporary media links: keep it private, never commit it.
"""

import json
import os
import time
import uuid
from pathlib import Path

import httpx


def save_state(path, state):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(state, stream)
    temporary.replace(path)


def run():
    base = os.environ["API_BASE"].rstrip("/")  # Origin only, without /v1.
    if not base.startswith("https://"):
        raise ValueError("API_BASE must use HTTPS")
    headers = {"Authorization": "Bearer " + os.environ["API_KEY"]}
    state_path = Path(os.environ.get("STATE_FILE", "seedance-request.json"))
    output = Path(os.environ.get("OUTPUT_FILE", "result.mp4"))
    # Do not enable automatic retries of paid POST requests.
    with httpx.Client(timeout=120, follow_redirects=False) as client:
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state["api_base"] != base:
                raise ValueError("STATE_FILE belongs to a different API_BASE")
        else:
            source = Path(os.environ["REFERENCE_FILE"])
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}[source.suffix.lower()]
            ticket = client.post(
                base + "/v1/media/uploads",
                headers=headers,
                json={
                    "model": "seedance-2.5",
                    "type": "image",
                    "content_type": mime,
                    "size_bytes": source.stat().st_size,
                },
            )
            ticket.raise_for_status()
            media = ticket.json()
            # Signed storage URL: NEVER attach the partner API key here.
            if not media["upload_url"].startswith("https://"):
                raise ValueError("Upload URL must use HTTPS")
            with source.open("rb") as stream:
                upload = client.put(
                    media["upload_url"],
                    content=stream,
                    headers={"Content-Type": mime, "Content-Length": str(source.stat().st_size)},
                )
            upload.raise_for_status()
            state = {
                "api_base": base,
                "idempotency_key": str(uuid.uuid4()),
                "body": {
                    "model": "seedance-2.5",
                    "prompt": "Animate the image in @Image 1 with a gentle camera move.",
                    "reference_images": [{"url": media["media_url"]}],
                    "duration": 4,
                    "resolution": "480p",
                    "aspect_ratio": "9:16",
                },
            }
            # Persist the SAME key and body BEFORE submitting the paid request.
            save_state(state_path, state)
        if "request_id" not in state:
            submitted = client.post(
                base + "/v1/videos/generations",
                headers={**headers, "Idempotency-Key": state["idempotency_key"]},
                json=state["body"],
            )
            submitted.raise_for_status()
            state["request_id"] = submitted.json()["request_id"]
            save_state(state_path, state)
        print("request_id:", state["request_id"])
        status_url = base + "/v1/videos/" + state["request_id"]
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            response = client.get(status_url, headers=headers)
            response.raise_for_status()
            result = response.json()
            if result["status"] in ("failed", "expired"):
                raise RuntimeError(result.get("error", result["status"]))
            if result["status"] == "done":
                # Construct our own URL; never send credentials to a returned external URL.
                with client.stream("GET", status_url + "/content", headers=headers) as download:
                    download.raise_for_status()
                    temporary = output.with_suffix(output.suffix + ".part")
                    with temporary.open("wb") as stream:
                        for chunk in download.iter_bytes():
                            stream.write(chunk)
                    temporary.replace(output)
                print("Saved:", output)
                return
            time.sleep(10)
        raise TimeoutError("Still pending. Run again with the SAME STATE_FILE to resume.")


if __name__ == "__main__":
    run()
