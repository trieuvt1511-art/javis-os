"""Durable, offline meeting audio and timestamped transcripts. No automatic recording or network STT."""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 512 * 1024 * 1024
LOCK = threading.RLock()


def atomic(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def stamp(seconds):
    ms = round(float(seconds) * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02}.{ms % 1000:03}"


class MeetingStore:
    def __init__(self, brain):
        self.root = Path(brain).resolve() / "meetings"
        self.root.mkdir(parents=True, exist_ok=True)

    def folder(self, ident):
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ValueError("Invalid meeting identifier")
        path = self.root / ident
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Meeting not found")
        return path

    def read(self, ident):
        return json.loads((self.folder(ident) / "status.json").read_text())

    def update(self, ident, **values):
        with LOCK:
            data = self.read(ident)
            data.update(values, updated=datetime.now(timezone.utc).isoformat())
            atomic(self.folder(ident) / "status.json", data)
            return data

    def create(self, title, language, mime):
        if language not in ("vi", "es", "en", "auto"):
            raise ValueError("Unsupported language")
        if mime not in ("audio/webm", "audio/ogg", "audio/mp4", "audio/wav", "audio/mpeg", "audio/flac"):
            raise ValueError("Unsupported audio type")
        ident = uuid.uuid4().hex
        path = self.root / ident
        path.mkdir()
        atomic(path / "status.json", dict(id=ident, title=str(title)[:200], language=language,
               mime=mime, state="recording", bytes=0, chunks=0, error="", created=datetime.now(timezone.utc).isoformat()))
        return self.read(ident)

    def append(self, ident, index, audio):
        with LOCK:
            data = self.read(ident)
            if data["state"] != "recording":
                raise ValueError("Recording is closed")
            path = self.folder(ident) / f"chunk-{index:06d}"
            if index < data["chunks"]:
                if path.read_bytes() != audio:
                    raise ValueError("Chunk differs from saved audio")
                return data
            if index != data["chunks"] or not audio:
                raise ValueError("Missing or empty audio chunk")
            if data["bytes"] + len(audio) > MAX_BYTES:
                raise ValueError("Audio exceeds 512 MiB")
            with open(path, "wb") as output:
                output.write(audio)
                output.flush()
                os.fsync(output.fileno())
            return self.update(ident, bytes=data["bytes"] + len(audio), chunks=index + 1)

    def finish(self, ident):
        with LOCK:
            data = self.read(ident)
            if data["state"] != "recording":
                return data
            if not data["chunks"]:
                raise ValueError("No recorded audio")
            ext = {"audio/webm":"webm", "audio/ogg":"ogg", "audio/mp4":"m4a", "audio/wav":"wav", "audio/mpeg":"mp3", "audio/flac":"flac"}[data["mime"]]
            folder = self.folder(ident)
            target = folder / ("audio." + ext)
            with open(target.with_suffix(".part"), "wb") as output:
                for i in range(data["chunks"]):
                    with open(folder / f"chunk-{i:06d}", "rb") as source:
                        shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            target.with_suffix(".part").replace(target)
            return self.update(ident, state="saved", audio=target.name)

    def list(self):
        rows = []
        for path in self.root.glob("*/status.json"):
            try:
                rows.append(self.read(path.parent.name))
            except (OSError, ValueError):
                continue
        return sorted(rows, key=lambda d:d["created"], reverse=True)

    def transcribe(self, ident, model=None):
        folder = self.folder(ident)
        data = self.read(ident)
        model = Path(model or os.getenv("JAVIS_MEETING_MODEL", str(self.root / "models/ggml-small.bin")))
        cli = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        try:
            self.update(ident, state="converting", error="")
            if not model.is_file() or ".en." in model.name:
                raise ValueError("Multilingual Whisper model is missing")
            if not data.get("audio"):
                raise ValueError("Finish recording first")
            subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(folder/data["audio"]),
                "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(folder/"normalized.wav")],
                check=True, capture_output=True, timeout=600)
            self.update(ident, state="transcribing", model=model.name, tool=cli)
            subprocess.run([cli, "-m", str(model), "-f", str(folder/"normalized.wav"), "-l", data["language"],
                "-oj", "-of", str(folder/"whisper"), "-t", "4", "-ng"], check=True, capture_output=True, timeout=7200)
            raw = json.loads((folder/"whisper.json").read_text())
            segments = []
            for row in raw.get("transcription", []):
                text = row.get("text", "").strip()
                if text:
                    segments.append(dict(id=len(segments)+1, start=row["offsets"]["from"]/1000,
                                         end=row["offsets"]["to"]/1000, text=text))
            if not segments:
                raise ValueError("No speech detected")
            atomic(folder/"transcript.json", dict(language=data["language"], model=model.name, segments=segments))
            (folder/"transcript.md").write_text("# Transcript\n\n" + "\n\n".join(
                f"[{stamp(s['start'])} - {stamp(s['end'])}] {s['text']}" for s in segments), encoding="utf-8")
            return self.update(ident, state="transcribed", transcript="transcript.md")
        except Exception as exc:
            self.update(ident, state="error", error=f"{type(exc).__name__}: {str(exc)[:250]}")
            raise

    def save_minutes(self, ident, result):
        folder = self.folder(ident)
        segments = {s["id"]:s for s in json.loads((folder/"transcript.json").read_text())["segments"]}
        provenance = result.get("provenance") or {}
        lines = ["# Biên bản cuộc họp", "", "Bản nháp AI, cần người dự họp kiểm lại.", "",
                 "Model: " + str(provenance.get("model") or "chưa xác định") +
                 "; nguồn: " + str(provenance.get("provider") or "chưa xác định"), ""]
        for key, label in [("topics","Nội dung chính"),("decisions","Quyết định"),("actions","Việc cần làm")]:
            lines += ["## " + label, ""]
            rows = result.get(key)
            if not isinstance(rows, list):
                raise ValueError("Invalid minutes structure")
            if not rows:
                lines += ["Chưa xác định.", ""]
            for row in rows:
                refs = row.get("segments", [])
                if not refs or any(type(i) is not int or i not in segments for i in refs):
                    raise ValueError("Minutes contain invalid transcript references")
                text = str(row.get("text", "")).strip()
                if not text:
                    raise ValueError("Empty conclusion")
                text = text.replace(chr(0x2014), "-")
                citations = " ".join(f"[{stamp(segments[i]['start'])} - {stamp(segments[i]['end'])}]" for i in refs)
                lines.append("- " + text + " " + citations)
                if key == "actions":
                    lines.append("  Người phụ trách: " + str(row.get("owner") or "chưa xác định") +
                                 "; Hạn: " + str(row.get("due") or "chưa xác định"))
            lines.append("")
        atomic(folder/"minutes.json", result)
        (folder/"minutes.md").write_text("\n".join(lines), encoding="utf-8")
        return self.update(ident, state="complete", minutes="minutes.md", error="")
