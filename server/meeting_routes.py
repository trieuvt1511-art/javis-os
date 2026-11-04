"""Authenticated meeting routes, registered on the existing application."""
import asyncio
import json
import localefmt
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from meeting_store import MeetingStore, MAX_BYTES


def register(app, resolve_brain, summarize):
    router = APIRouter(prefix="/meetings")
    jobs = {}

    def store(brain):
        return MeetingStore(resolve_brain(brain))

    async def body(request, limit):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > limit:
                raise HTTPException(413, "Audio exceeds limit")
        return bytes(content)

    def checked(fn, *args):
        try:
            return fn(*args)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("")
    async def listing(brain: str = "brain"):
        s = store(brain)
        rows = s.list()
        for row in rows:
            if row["state"] in ("converting", "transcribing", "summarizing") and (str(s.root), row["id"]) not in jobs:
                row.update(s.update(row["id"], state="error", error="Interrupted; retry processing saved audio"))
        return rows

    @router.post("")
    async def create(request: Request, brain: str = "brain"):
        try:
            data = json.loads(await body(request, 8192))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, localefmt.chu("Dữ liệu cuộc họp không hợp lệ", "Invalid meeting data")) from exc
        return checked(store(brain).create, data.get("title", ""), data.get("language", "vi"), data.get("mime", "audio/webm"))

    @router.post("/{ident}/chunks/{index}")
    async def chunk(ident: str, index: int, request: Request, brain: str = "brain"):
        if index < 0 or index > 100000:
            raise HTTPException(400, "Invalid chunk index")
        content = await body(request, MAX_BYTES)
        return checked(store(brain).append, ident, index, content)

    @router.post("/{ident}/finish")
    async def finish(ident: str, brain: str = "brain"):
        return checked(store(brain).finish, ident)

    @router.get("/{ident}")
    async def status(ident: str, brain: str = "brain"):
        data = checked(store(brain).read, ident)
        if data["state"] in ("converting", "transcribing", "summarizing") and (str(store(brain).root), ident) not in jobs:
            return store(brain).update(ident, state="error", error="Interrupted; retry processing saved audio")
        return data

    async def run(s, ident, operation):
        key = (str(s.root), ident)
        try:
            if operation == "transcribe":
                await asyncio.to_thread(s.transcribe, ident)
            else:
                s.update(ident, state="summarizing", error="")
                transcript = json.loads((s.folder(ident)/"transcript.json").read_text())
                result = await asyncio.wait_for(summarize(transcript), timeout=600)
                s.save_minutes(ident, result)
        except Exception as exc:
            s.update(ident, state="error", error=str(exc)[:300])
        finally:
            jobs.pop(key, None)

    @router.post("/{ident}/{operation}")
    async def process(ident: str, operation: str, brain: str = "brain"):
        if operation not in ("transcribe", "summarize"):
            raise HTTPException(404)
        s = store(brain)
        data = checked(s.read, ident)
        if data["state"] == "recording":
            raise HTTPException(409, "Finish recording first")
        if operation == "summarize" and not (s.folder(ident)/"transcript.json").is_file():
            raise HTTPException(409, "Transcribe first")
        key = (str(s.root), ident)
        if key in jobs:
            raise HTTPException(409, "Meeting is already processing")
        s.update(ident, state="transcribing" if operation == "transcribe" else "summarizing", error="")
        jobs[key] = asyncio.create_task(run(s, ident, operation))
        return {"state":"processing"}

    @router.get("/{ident}/files/{name}")
    async def file(ident: str, name: str, brain: str = "brain"):
        s = store(brain)
        data = checked(s.read, ident)
        allowed = {data.get("audio"), "transcript.md", "transcript.json", "minutes.md", "minutes.json"}
        if name not in allowed or not (s.folder(ident)/name).is_file():
            raise HTTPException(404)
        return FileResponse(s.folder(ident)/name, headers={"Cache-Control":"no-store"})

    app.include_router(router)
