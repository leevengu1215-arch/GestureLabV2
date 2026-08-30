#!/usr/bin/env python3
import csv
import gzip
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "sessions"
INDEX = APP_DIR / "index.html"


def session_directory(session_id):
    """Schema 2.0 stores a session by time ID; participant IDs are UI-only."""
    return DATA_DIR / safe_name(session_id)


def capture_directory(session_dir, capture_index):
    """Create the operator-friendly raw-data layout for one scene/capture."""
    root = Path(session_dir) / f"capture{int(capture_index)}"
    for name in ("camera1", "camera2", "camera3", "watches"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def capture_state(session_dir):
    """Return canonical captures plus retained invalid attempts."""
    session_dir = Path(session_dir)
    session_id = safe_name(session_dir.name)
    captures = []
    for capture_dir in sorted(session_dir.glob("capture*")):
        match = re.fullmatch(r"capture(\d+)", capture_dir.name)
        if not match:
            continue
        index = int(match.group(1))
        marker = capture_dir / "capture_mode.json"
        pending = {}
        try:
            pending = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        captures.append({
            "capture_index": index,
            "capture_dir": capture_dir.name,
            "status": pending.get("status", "recording"),
            "camera_mode": pending.get("camera_mode", "usb"),
            "external_camera_dir": pending.get("external_camera_dir", f"capture{index}/camera3"),
        })
    used = {item["capture_index"] for item in captures}
    captures.sort(key=lambda item: item["capture_index"])
    next_index = 1
    while next_index in used:
        next_index += 1
    discarded = []
    manifest = session_dir / "discarded" / "index.jsonl"
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                discarded.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {
        "session_id": session_id,
        "captures": captures,
        "discarded": discarded,
        "next_capture_index": next_index,
        "finalized": (session_dir / "FINALIZED.json").exists(),
    }


def invalidate_capture(session_dir, capture_index, reason):
    """Archive a bad attempt and release its canonical Capture number."""
    session_dir = Path(session_dir)
    session_id = safe_name(session_dir.name)
    capture_index = int(capture_index)
    if capture_index < 1:
        raise RuntimeError("Capture 编号必须为正整数")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    root = session_dir / "discarded"
    existing = sorted(root.glob(f"capture_{capture_index:02d}_attempt_*")) if root.exists() else []
    attempt = len(existing) + 1
    archive = root / f"capture_{capture_index:02d}_attempt_{attempt:02d}_{stamp}"
    archive.mkdir(parents=True, exist_ok=False)

    moved = []
    raw_capture = session_dir / f"capture{capture_index}"
    if raw_capture.exists():
        destination = archive / "raw_capture"
        shutil.move(str(raw_capture), destination)
        moved.append("raw_capture/")

    # Legacy packages stored every camera directly under sessions/<id>/videos.
    video_dir = session_dir / "videos"
    video_archive = archive / "videos"
    if video_dir.exists():
        for meta_path in list(video_dir.glob("*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if int(meta.get("capture_segment") or 0) != capture_index:
                continue
            video_archive.mkdir(parents=True, exist_ok=True)
            video_path = Path(meta.get("file", ""))
            if video_path.exists() and video_path.parent == video_dir:
                destination = video_archive / video_path.name
                shutil.move(str(video_path), destination)
                moved.append(f"videos/{destination.name}")
            destination = video_archive / meta_path.name
            shutil.move(str(meta_path), destination)
            moved.append(f"videos/{destination.name}")

    session_json = session_dir / "session.json"
    if session_json.exists():
        try:
            index = json.loads(session_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            index = {}
        index["captures"] = [
            item for item in index.get("captures", [])
            if int(item.get("capture_index", -1)) != capture_index
        ]
        partial = session_dir / "session.json.partial"
        partial.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(partial, session_json)

    record = {
        "capture_index": capture_index,
        "attempt": attempt,
        "status": "invalid",
        "reason": str(reason or "主试标记本段作废"),
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "archive_dir": str(archive.relative_to(session_dir)),
        "files": moved,
    }
    root.mkdir(parents=True, exist_ok=True)
    with (root / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def now_ms():
    return int(time.time() * 1000)


def safe_name(value):
    allowed = []
    for ch in str(value).strip():
        if ch.isalnum() or ch in ("-", "_", "."):
            allowed.append(ch)
        elif ch.isspace():
            allowed.append("_")
    return "".join(allowed).strip("._") or "unnamed"






def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length).decode("utf-8")
    return json.loads(body) if body else {}


def write_json(handler, payload, status=200):
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def copy_candidate_files(source_dir, target_dir, start_ms, end_ms, extensions, margin_ms=8000):
    source = Path(source_dir).expanduser()
    if not source.exists() or not source.is_dir():
        return [], f"Source directory does not exist: {source}"

    start_s = (start_ms - margin_ms) / 1000
    end_s = (end_ms + margin_ms) / 1000
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    extensions = {e.lower().lstrip(".") for e in extensions if e.strip()}

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if extensions and path.suffix.lower().lstrip(".") not in extensions:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if start_s <= mtime <= end_s:
            rel = path.relative_to(source)
            dest = target_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied.append(str(rel))
    return copied, None


def append_manifest(session_dir, row):
    manifest = session_dir / "manifest.csv"
    exists = manifest.exists()
    fields = [
        "trial_id",
        "participant_id",
        "session_id",
        "gesture",
        "rep",
        "scene_id",
        "task_group",
        "scheme",
        "start_iso",
        "end_iso",
        "duration_ms",
        "source_dir",
        "copied_file_count",
        "success",
        "difficulty",
        "fatigue",
        "confidence",
        "notes",
    ]
    with manifest.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def scan_eng_references(root_dir):
    root = Path(root_dir)
    pattern = re.compile(r"fitness/capture/(\d+-Online)/(Eng-\d+-800Hz\.csv)")
    rows = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".log", ".txt", ".gz"}:
            continue
        try:
            if path.suffix.lower() == ".gz":
                lines = gzip.open(path, "rt", encoding="utf-8", errors="ignore")
            else:
                lines = path.open("r", encoding="utf-8", errors="ignore")
            with lines as f:
                for line_no, line in enumerate(f, 1):
                    for match in pattern.finditer(line):
                        rows.append(
                            {
                                "capture_id": match.group(1),
                                "filename": match.group(2),
                                "path": f"fitness/capture/{match.group(1)}/{match.group(2)}",
                                "log_file": str(path.relative_to(root)),
                                "line": line_no,
                                "raw": line.strip()[:500],
                            }
                        )
        except OSError:
            continue
    return rows


def append_event(session_dir, event):
    event = dict(event)
    event["saved_at_ms"] = now_ms()
    event["saved_at_iso"] = datetime.now(timezone.utc).isoformat()

    events_jsonl = session_dir / "workflow_events.jsonl"
    with events_jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    events_csv = session_dir / "workflow_events.csv"
    exists = events_csv.exists()
    fields = ["saved_at_iso", "phase", "step", "status", "operator", "notes"]
    with events_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({k: event.get(k, "") for k in fields})


def _is_within(child, parent):
    """True if `child` is `parent` or nested inside it (both must be resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def copytree_snapshot(source_dir, target_dir):
    source = Path(source_dir).expanduser()
    if not source.exists() or not source.is_dir():
        return f"Source directory does not exist: {source}"
    # Guard against recursive self-copy: if the destination lives inside the
    # source directory (or vice versa), shutil.copytree would copy the target
    # into itself over and over, filling the disk. Reject such requests.
    try:
        source_res = source.resolve()
        target_res = Path(target_dir).resolve()
    except OSError as exc:
        return f"Cannot resolve copy paths: {exc}"
    if source_res == target_res:
        return f"Refusing to copy: source and destination are the same directory ({source_res})."
    if _is_within(target_res, source_res):
        return (
            "Refusing to copy: destination is inside the source directory "
            f"({target_res} is within {source_res}); this would recurse and fill the disk."
        )
    if _is_within(source_res, target_res):
        return (
            "Refusing to copy: source is inside the destination directory "
            f"({source_res} is within {target_res})."
        )
    if target_dir.exists():
        shutil.rmtree(target_dir)
    ignore = shutil.ignore_patterns(".DS_Store", "__MACOSX")
    shutil.copytree(source, target_dir, ignore=ignore)
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self.serve_file(INDEX)
        if parsed.path == "/api/status":
            return write_json(
                self,
                {
                    "ok": True,
                    "data_dir": str(DATA_DIR),
                    "hdf5_enabled": False,
                    "storage_schema": "raw-capture-folders/1.0",
                },
            )
        if parsed.path == "/api/material":
            params = parse_qs(parsed.query)
            material_path = params.get("path", [""])[0]
            if not material_path:
                self.send_error(400, "Missing material path")
                return
            return self.serve_file(Path(material_path).expanduser())
        if parsed.path == "/api/list":
            sessions = []
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            for session in sorted(DATA_DIR.glob("*")):
                if session.is_dir():
                    sessions.append(session.name)
            return write_json(self, {"sessions": sessions})
        if parsed.path == "/api/capture_state":
            params = parse_qs(parsed.query)
            session = safe_name(params.get("session_id", [""])[0])
            if not session:
                return write_json(self, {"ok": False, "error": "缺少 Session ID"}, status=400)
            return write_json(self, {"ok": True, **capture_state(session_directory(session))})
        return self.serve_file(APP_DIR / unquote(parsed.path).lstrip("/"))

    def serve_file(self, path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/trial":
            return self.save_trial()
        if parsed.path == "/api/config":
            payload = read_json(self)
            participant = safe_name(payload.get("participant_id", "participant"))
            session = safe_name(payload.get("session_id", datetime.now().strftime("%Y%m%d_%H%M%S")))
            session_dir = session_directory(session)
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "config.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return write_json(self, {"ok": True, "session_dir": str(session_dir)})
        if parsed.path == "/api/import_log":
            return self.import_log()
        if parsed.path == "/api/event":
            return self.save_event()
        if parsed.path == "/api/video":
            return self.save_video(parsed)
        if parsed.path == "/api/prepare_capture":
            payload = read_json(self)
            session = safe_name(payload.get("session_id", ""))
            capture_index = int(payload.get("capture_segment") or 0)
            if not session or capture_index < 1:
                return write_json(self, {"ok": False, "error": "Session 或 Capture 编号无效"}, status=400)
            capture_dir = capture_directory(session_directory(session), capture_index)
            marker = {
                "capture_index": capture_index,
                "camera_mode": safe_name(payload.get("camera_mode", "usb")) or "usb",
                "status": safe_name(payload.get("status", "recording")) or "recording",
                "external_camera_dir": f"capture{capture_index}/camera3",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            (capture_dir / "capture_mode.json").write_text(
                json.dumps(marker, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return write_json(self, {
                "ok": True,
                **marker,
                "capture_dir": str(capture_dir),
                "external_camera_path": str(capture_dir / "camera3"),
            })
        if parsed.path == "/api/invalidate_capture":
            payload = read_json(self)
            session = safe_name(payload.get("session_id", ""))
            try:
                record = invalidate_capture(
                    session_directory(session),
                    int(payload.get("capture_index") or 0),
                    payload.get("reason", ""),
                )
            except Exception as exc:
                return write_json(self, {"ok": False, "error": str(exc)}, status=400)
            return write_json(self, {"ok": True, "discarded": record, **capture_state(session_directory(session))})
        if parsed.path == "/api/finalize_session":
            payload = read_json(self)
            session = safe_name(payload.get("session_id", ""))
            session_dir = session_directory(session)
            state = capture_state(session_dir)
            if not state["captures"]:
                return write_json(self, {"ok": False, "error": "整理完成前至少需要 1 个 Capture"}, status=400)
            indexes = [item["capture_index"] for item in state["captures"]]
            if indexes != list(range(1, len(indexes) + 1)):
                return write_json(self, {"ok": False, "error": "Capture 编号必须从 01 连续排列"}, status=400)
            invalid = [item for item in state["captures"] if item["status"] != "valid"]
            if invalid:
                return write_json(self, {"ok": False, "error": "仍有未通过校验的 Capture，请先重录或补齐手表数据"}, status=400)
            finalized = {
                "session_id": session,
                "finalized_at": datetime.now(timezone.utc).isoformat(),
                "captures": state["captures"],
                "discarded_attempts": len(state["discarded"]),
            }
            (session_dir / "FINALIZED.json").write_text(json.dumps(finalized, ensure_ascii=False, indent=2), encoding="utf-8")
            return write_json(self, {"ok": True, **finalized})
        self.send_error(404)

    def save_trial(self):
        payload = read_json(self)
        participant = safe_name(payload.get("participant_id", "participant"))
        session = safe_name(payload.get("session_id", "session"))
        trial_id = safe_name(payload.get("trial_id", str(now_ms())))
        source_dir = payload.get("source_dir", "")
        extensions = payload.get("extensions", ["csv", "gz", "log", "txt"])
        start_ms = int(payload.get("start_ms", now_ms()))
        end_ms = int(payload.get("end_ms", now_ms()))

        session_dir = session_directory(session)
        trial_dir = session_dir / "trials" / trial_id
        files_dir = trial_dir / "files"
        trial_dir.mkdir(parents=True, exist_ok=True)

        copied, warning = ([], None)
        if source_dir:
            copied, warning = copy_candidate_files(source_dir, files_dir, start_ms, end_ms, extensions)

        payload["saved_at_ms"] = now_ms()
        payload["copied_files"] = copied
        payload["copy_warning"] = warning
        (trial_dir / "trial.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        q = payload.get("questionnaire", {})
        append_manifest(
            session_dir,
            {
                "trial_id": trial_id,
                "participant_id": participant,
                "session_id": session,
                "gesture": payload.get("gesture", ""),
                "rep": payload.get("rep", ""),
                "scene_id": payload.get("scene_id", ""),
                "task_group": payload.get("task_group", ""),
                "scheme": payload.get("scheme", ""),
                "start_iso": payload.get("start_iso", ""),
                "end_iso": payload.get("end_iso", ""),
                "duration_ms": end_ms - start_ms,
                "source_dir": source_dir,
                "copied_file_count": len(copied),
                "success": q.get("success", ""),
                "difficulty": q.get("difficulty", ""),
                "fatigue": q.get("fatigue", ""),
                "confidence": q.get("confidence", ""),
                "notes": q.get("notes", ""),
            },
        )
        return write_json(
            self,
            {
                "ok": True,
                "trial_dir": str(trial_dir),
                "copied_files": copied,
                "warning": warning,
            },
        )

    def import_log(self):
        payload = read_json(self)
        participant = safe_name(payload.get("participant_id", "participant"))
        session = safe_name(payload.get("session_id", "session"))
        source_dir = payload.get("source_dir", "")
        import_id = safe_name(payload.get("import_id", datetime.now().strftime("%Y%m%d_%H%M%S")))

        session_dir = session_directory(session)
        import_dir = session_dir / "phone_logs" / import_id
        session_dir.mkdir(parents=True, exist_ok=True)

        warning = copytree_snapshot(source_dir, import_dir)
        if warning:
            return write_json(self, {"ok": False, "error": warning}, status=400)

        eng_refs = scan_eng_references(import_dir)
        index_json = import_dir / "eng_index.json"
        index_csv = import_dir / "eng_index.csv"
        index_json.write_text(json.dumps(eng_refs, ensure_ascii=False, indent=2), encoding="utf-8")
        with index_csv.open("w", newline="", encoding="utf-8") as f:
            fields = ["capture_id", "filename", "path", "log_file", "line", "raw"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(eng_refs)

        unique_captures = sorted({row["capture_id"] for row in eng_refs})
        return write_json(
            self,
            {
                "ok": True,
                "import_dir": str(import_dir),
                "eng_reference_count": len(eng_refs),
                "capture_ids": unique_captures,
                "eng_index_json": str(index_json),
                "eng_index_csv": str(index_csv),
            },
        )

    def save_event(self):
        payload = read_json(self)
        participant = safe_name(payload.get("participant_id", "participant"))
        session = safe_name(payload.get("session_id", "session"))
        session_dir = session_directory(session)
        session_dir.mkdir(parents=True, exist_ok=True)
        append_event(session_dir, payload)
        return write_json(self, {"ok": True, "event_log": str(session_dir / "workflow_events.csv")})

    def save_video(self, parsed):
        params = parse_qs(parsed.query)
        participant = safe_name(params.get("participant_id", ["participant"])[0])
        session = safe_name(params.get("session_id", ["session"])[0])
        label = safe_name(params.get("label", [str(now_ms())])[0])
        capture_segment = int(params.get("capture_segment", ["0"])[0] or 0)
        camera_id = safe_name(params.get("camera_id", [""])[0])
        length = int(self.headers.get("Content-Length", "0"))
        session_dir = session_directory(session)
        if capture_segment < 1:
            return write_json(self, {"ok": False, "error": "缺少有效的 Capture 编号"}, status=400)
        capture_dir = capture_directory(session_dir, capture_segment)
        camera_number = {"cam-01": 1, "cam-02": 2, "cam-03": 3}.get(camera_id)
        if camera_number is None:
            return write_json(self, {"ok": False, "error": f"无效摄像头编号：{camera_id}"}, status=400)
        video_dir = capture_dir / f"camera{camera_number}"
        requested_container = safe_name(params.get("container", [""])[0]).lower()
        request_mime = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        container = "mp4" if requested_container == "mp4" or request_mime == "video/mp4" else "webm"
        mime_type = "video/mp4" if container == "mp4" else "video/webm"
        target = video_dir / f"{label}.{container}"
        with target.open("wb") as f:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
            f.flush()
            os.fsync(f.fileno())
        written = target.stat().st_size if target.exists() else 0
        if length <= 0 or written != length:
            target.unlink(missing_ok=True)
            return write_json(
                self,
                {"ok": False, "error": f"视频保存不完整：应接收 {length} bytes，实际写入 {written} bytes"},
                status=500,
            )
        meta = {
            "participant_id": participant,
            "session_id": session,
            "label": label,
            "file": str(target),
            "bytes": written,
            "saved_at_ms": now_ms(),
            "saved_at_iso": datetime.now(timezone.utc).isoformat(),
            "capture_segment": capture_segment,
            "capture_dir": capture_dir.name,
            "camera_id": camera_id,
            "container": container,
            "mime_type": mime_type,
            "start_epoch_ms": int(params.get("start_epoch_ms", ["0"])[0] or 0),
            "end_epoch_ms": now_ms(),
        }
        (video_dir / f"{label}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return write_json(self, {"ok": True, "video_file": str(target), "bytes": meta["bytes"]})

class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # Avoid a slow reverse DNS lookup on some macOS setups.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("GESTURE_LAB_PORT", "8766"))
    server = LocalHTTPServer(("127.0.0.1", port), Handler)
    print(f"Gesture Lab running at http://127.0.0.1:{port}", flush=True)
    print(f"Data directory: {DATA_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
