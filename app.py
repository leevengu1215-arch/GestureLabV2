#!/usr/bin/env python3
import csv
import gzip
import hashlib
import io
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

try:
    import h5py
    import numpy as np
except ImportError:
    h5py = None
    np = None

try:
    from capture_hdf5_v2 import build_capture_hdf5 as build_capture_hdf5_v2
except ImportError:
    build_capture_hdf5_v2 = None


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
    for path in sorted(session_dir.glob(f"{session_id}_capture_*.h5")):
        match = re.search(r"_capture_(\d+)\.h5$", path.name)
        if not match:
            continue
        index = int(match.group(1))
        status = "unknown"
        if h5py is not None:
            try:
                with h5py.File(path, "r") as h5:
                    status = str(h5.attrs.get("status", "unknown"))
            except OSError:
                status = "unreadable"
        captures.append({"capture_index": index, "filename": path.name, "status": status})
    used = {item["capture_index"] for item in captures}
    for marker in sorted(session_dir.glob("capture*/capture_mode.json")):
        match = re.fullmatch(r"capture(\d+)", marker.parent.name)
        if not match:
            continue
        index = int(match.group(1))
        if index in used:
            continue
        try:
            pending = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        captures.append({
            "capture_index": index,
            "filename": "",
            "status": pending.get("status", "awaiting_external_video"),
            "camera_mode": pending.get("camera_mode", "wireless_manual"),
            "external_camera_dir": pending.get("external_camera_dir", f"capture{index}/camera3"),
        })
        used.add(index)
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
    h5_path = session_dir / f"{session_id}_capture_{capture_index:02d}.h5"
    if h5_path.exists():
        if h5py is not None:
            with h5py.File(h5_path, "r+") as h5:
                h5.attrs["status"] = "invalid"
                h5.flush()
        destination = archive / f"{session_id}_capture_{capture_index:02d}_invalid_attempt_{attempt:02d}.h5"
        shutil.move(str(h5_path), destination)
        moved.append(destination.name)

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


def session_tag(value):
    tag = re.sub(r"[^A-Za-z0-9TZ-]+", "-", str(value).strip()).strip("-")
    return tag or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def normalized_column(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def parse_numeric_csv(raw):
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None

    rows = list(csv.reader(io.StringIO(text)))
    header_index = None
    for index, row in enumerate(rows):
        normalized = [normalized_column(item) for item in row]
        if len(row) >= 2 and any(
            token in column
            for column in normalized
            for token in ("timestamp", "currentms", "eventms", "accel", "gyro")
        ):
            header_index = index
            break
    if header_index is None:
        return None

    columns = [item.strip() or f"column_{i + 1}" for i, item in enumerate(rows[header_index])]
    metadata = {}
    for row in rows[:header_index]:
        line = ",".join(row).strip().lstrip("#").strip()
        match = re.match(r"([^:=]+)\\s*[:=]\\s*(.+)", line)
        if match:
            metadata[match.group(1).strip()] = match.group(2).strip()

    numeric_rows = []
    for row in rows[header_index + 1:]:
        if not row or not any(item.strip() for item in row):
            continue
        if len(row) != len(columns):
            return None
        try:
            numeric_rows.append([float(item.strip()) for item in row])
        except ValueError:
            return None
    if not numeric_rows:
        return None
    return columns, metadata, np.asarray(numeric_rows, dtype=np.float64)


def classify_watch_signal(path, columns=None):
    source = path.name.lower()
    normalized = [normalized_column(item) for item in (columns or [])]
    joined = " ".join(normalized)
    if any(token in source or token in joined for token in ("accel", "accelerometer", "gsensor")):
        return "accel"
    if any(token in source or token in joined for token in ("gyro", "gyroscope")):
        return "gyroscope"
    if any(token in source or token in joined for token in ("magnetic", "magnetometer")):
        return "magnetic"
    if "barometer" in source or "pressure" in joined:
        return "barometer"
    for signal in ("ppg-green", "ppg-red", "ppg-ir"):
        if signal in source or signal.replace("-", "") in joined:
            return signal
    return safe_name(path.stem).lower()


def column_index(columns, candidates):
    normalized = [normalized_column(item) for item in columns]
    for candidate in candidates:
        candidate = normalized_column(candidate)
        if candidate in normalized:
            return normalized.index(candidate)
    return None


def write_numeric_watch_views(group, signal, parsed, string_dtype):
    if not parsed:
        group.attrs["numeric_table_available"] = False
        return
    columns, metadata, table = parsed
    row_chunk = max(1, min(4096, table.shape[0]))
    dataset = group.create_dataset(
        "table",
        data=table,
        compression="gzip",
        compression_opts=4,
        shuffle=True,
        chunks=(row_chunk, table.shape[1]),
    )
    columns_json = json.dumps(columns, ensure_ascii=False)
    group.attrs["columns_json"] = columns_json
    group.attrs["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
    group.attrs["numeric_table_available"] = True

    sampling_rate = next(
        (
            value
            for key, value in metadata.items()
            if normalized_column(key) in {"samplerate", "samplingrate", "samplingratehz"}
        ),
        None,
    )
    if sampling_rate is not None:
        try:
            group.attrs["sampling_rate_hz"] = float(re.findall(r"[-+]?\\d*\\.?\\d+", str(sampling_rate))[0])
        except (IndexError, ValueError):
            pass

    if signal != "accel":
        return
    current_index = column_index(columns, ("CurrentTimestamp(ms)", "current_ms", "currenttimestamp"))
    event_index = column_index(
        columns,
        ("EventTimestamp(ms)", "event_ms", "eventtimestamp", "EventTimestamp(ns)", "EventTimestamp(us)"),
    )
    x_index = column_index(columns, ("x", "accel_x", "accelerometer_x", "axis_x"))
    y_index = column_index(columns, ("y", "accel_y", "accelerometer_y", "axis_y"))
    z_index = column_index(columns, ("z", "accel_z", "accelerometer_z", "axis_z"))
    if None in (current_index, event_index, x_index, y_index, z_index):
        raise RuntimeError("Accel CSV 缺少 current_ms、event_ms 或 x/y/z 列，不能按 xgr.capture 1.0 发布")

    current_ms = table[:, current_index]
    event_ms = table[:, event_index].copy()
    event_name = normalized_column(columns[event_index])
    if event_name.endswith("ns"):
        event_ms /= 1_000_000.0
    elif event_name.endswith("us"):
        event_ms /= 1_000.0
    timestamps = group.create_group("timestamps")
    timestamps.create_dataset(
        "current_ms", data=current_ms, compression="gzip", compression_opts=4, shuffle=True
    )
    timestamps.create_dataset(
        "event_ms", data=event_ms, compression="gzip", compression_opts=4, shuffle=True
    )
    values = group.create_dataset(
        "values",
        data=table[:, [x_index, y_index, z_index]],
        compression="gzip",
        compression_opts=4,
        shuffle=True,
        chunks=(row_chunk, 3),
    )
    values.attrs["columns_json"] = json.dumps(["x", "y", "z"])


def validate_capture_hdf5(path, source_rows):
    required_groups = ("meta", "video", "watch", "events", "sync", "integrity")
    with h5py.File(path, "r") as check:
        if check.attrs.get("schema_name") != "xgr.capture" or check.attrs.get("schema_version") != "1.0":
            raise RuntimeError("HDF5 schema 校验失败")
        for group_name in required_groups:
            if group_name not in check:
                raise RuntimeError(f"HDF5 缺少必填路径：/{group_name}")
        for attribute in ("session_id", "participant_id", "capture_id", "capture_segment", "status", "start_epoch_ms"):
            if attribute not in check["meta"].attrs:
                raise RuntimeError(f"HDF5 /meta 缺少必填属性：{attribute}")
        for camera_id in ("cam-01", "cam-02", "cam-03"):
            camera = check[f"video/{camera_id}"]
            if "available" not in camera.attrs:
                raise RuntimeError(f"HDF5 /video/{camera_id} 缺少 available")
            if bool(camera.attrs["available"]) and "encoded" not in camera:
                raise RuntimeError(f"HDF5 /video/{camera_id} 缺少 encoded")
        for signal_name, signal in check["watch"].items():
            if "source_csv" not in signal:
                raise RuntimeError(f"HDF5 /watch/{signal_name} 缺少 source_csv")
            if signal_name == "accel":
                for required in ("timestamps/current_ms", "timestamps/event_ms", "values"):
                    if required not in signal:
                        raise RuntimeError(f"HDF5 /watch/accel 缺少 {required}")
        parallel_groups = {
            "events": ("epoch_ms", "event_type", "payload_json", "source_line"),
            "integrity": ("source_files", "source_sizes", "sha256", "datasets"),
        }
        for group_name, datasets in parallel_groups.items():
            lengths = [len(check[f"{group_name}/{dataset}"]) for dataset in datasets]
            if len(set(lengths)) != 1:
                raise RuntimeError(f"HDF5 /{group_name} 平行数组长度不一致")
        for source_file, _, digest, dataset_path in source_rows:
            if Path(source_file).is_absolute() or re.match(r"^[A-Za-z]:[\\\\/]", source_file):
                raise RuntimeError("HDF5 integrity 中禁止机器绝对路径")
            if sha256_bytes(check[dataset_path][...].tobytes()) != digest:
                raise RuntimeError(f"SHA-256 校验失败：{dataset_path}")


def build_capture_hdf5(session_dir, payload):
    if h5py is None or np is None:
        raise RuntimeError("当前 Python 缺少 h5py/numpy，无法生成 HDF5")
    segment = int(payload.get("capture_segment", 0))
    if segment < 1:
        raise RuntimeError("capture_segment 必须大于 0")
    tag = session_tag(payload.get("session_tag") or payload.get("session_id"))
    width = max(2, len(str(segment)))
    filename = f"{tag}_capture_{segment:0{width}d}.h5"
    target = session_dir / filename
    partial = session_dir / f"{filename}.partial"
    if target.exists():
        raise RuntimeError(f"Capture 已存在，禁止覆盖：{filename}")

    start_ms = int(payload.get("start_epoch_ms") or 0)
    end_ms = int(payload.get("end_epoch_ms") or 0)
    video_meta = []
    for meta_path in (session_dir / "videos").glob("*.json") if (session_dir / "videos").exists() else []:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if int(meta.get("capture_segment") or 0) == segment:
            video_meta.append(meta)

    watch_source_dir = str(payload.get("watch_source_dir", "") or "").strip()
    watch_root = Path(watch_source_dir).expanduser() if watch_source_dir else None
    watch_files = (
        sorted(
            path
            for path in watch_root.rglob("*.csv")
            if path.name.lower() not in {"eng_index.csv", "workflow_events.csv", "manifest.csv"}
        )
        if watch_root is not None and watch_root.exists()
        else []
    )
    requested_status = payload.get("status", "valid")
    if requested_status not in {"valid", "incomplete", "invalid", "superseded"}:
        requested_status = "invalid"
    actual_status = requested_status if video_meta and watch_files else "incomplete"

    events = []
    events_file = session_dir / "workflow_events.jsonl"
    if events_file.exists():
        for line_no, line in enumerate(events_file.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            notes = event.get("notes", "")
            explicit = f'"capture_segment":{segment}' in str(notes).replace(" ", "")
            epoch = int(event.get("saved_at_ms") or 0)
            if explicit or (start_ms and end_ms and start_ms <= epoch <= end_ms):
                events.append((epoch, event.get("status", ""), line, line_no))

    source_rows = []
    string_dtype = h5py.string_dtype("utf-8")
    with h5py.File(partial, "w") as h5:
        h5.attrs["schema_name"] = "xgr.capture"
        h5.attrs["schema_version"] = "1.0"
        meta_group = h5.create_group("meta")
        meta_values = {
            "session_id": payload.get("session_id", ""),
            "participant_id": payload.get("participant_id", ""),
            "capture_id": f"cap-{segment:03d}",
            "capture_segment": segment,
            "status": actual_status,
            "start_epoch_ms": start_ms,
            "end_epoch_ms": end_ms,
            "scene_id": payload.get("scene_id", ""),
            "scene_label": payload.get("scene_label", ""),
            "operator": payload.get("operator", ""),
            "batch_id": payload.get("batch_id", ""),
            "started_at": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat() if start_ms else "",
            "ended_at": datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat() if end_ms else "",
        }
        required_meta = {"session_id", "participant_id", "capture_id", "capture_segment", "status", "start_epoch_ms"}
        for key, value in meta_values.items():
            if key in required_meta or value not in (None, ""):
                meta_group.attrs[key] = value
        meta_group.create_dataset("notes", data=str(payload.get("notes", "")), dtype=string_dtype)

        video_group = h5.create_group("video")
        video_channels = []
        for camera_index in range(1, 4):
            camera_id = f"cam-{camera_index:02d}"
            group = video_group.create_group(camera_id)
            match = next((item for item in video_meta if item.get("camera_id") == camera_id), None)
            video_path = Path(match.get("file", "")) if match else None
            if not match or not video_path or not video_path.exists():
                group.attrs["available"] = False
                group.attrs["reason"] = "not-recorded"
                continue
            data = video_path.read_bytes()
            dataset = group.create_dataset("encoded", data=np.frombuffer(data, dtype=np.uint8))
            dataset.attrs["sha256"] = sha256_bytes(data)
            group.attrs["available"] = True
            container = str(match.get("container") or video_path.suffix.lstrip(".") or "webm").lower()
            mime_type = str(match.get("mime_type") or ("video/mp4" if container == "mp4" else "video/webm"))
            group.attrs["container"] = container
            group.attrs["mime_type"] = mime_type
            group.attrs["start_epoch_ms"] = int(match.get("start_epoch_ms") or start_ms)
            group.attrs["end_epoch_ms"] = int(match.get("end_epoch_ms") or end_ms)
            rel = video_path.relative_to(session_dir).as_posix()
            source_rows.append((rel, len(data), sha256_bytes(data), f"/video/{camera_id}/encoded"))
            video_channels.append(camera_id)

        watch_group = h5.create_group("watch")
        used_signal_names = set()
        watch_channels = []
        for watch_path in watch_files:
            data = watch_path.read_bytes()
            parsed = parse_numeric_csv(data)
            signal = classify_watch_signal(watch_path, parsed[0] if parsed else None)
            base_signal = signal or "signal"
            suffix = 2
            while signal in used_signal_names:
                signal = f"{base_signal}-{suffix}"
                suffix += 1
            used_signal_names.add(signal)
            group = watch_group.create_group(signal)
            chunk_bytes = max(1, min(len(data), 8 * 1024 * 1024))
            dataset = group.create_dataset(
                "source_csv",
                data=np.frombuffer(data, dtype=np.uint8),
                compression="gzip",
                compression_opts=4,
                shuffle=True,
                chunks=(chunk_bytes,),
            )
            dataset.attrs["sha256"] = sha256_bytes(data)
            write_numeric_watch_views(group, signal, parsed, string_dtype)
            try:
                rel = watch_path.relative_to(session_dir).as_posix()
            except ValueError:
                rel = f"phone_logs/{watch_path.relative_to(watch_root).as_posix()}"
            source_rows.append((rel, len(data), sha256_bytes(data), f"/watch/{signal}/source_csv"))
            watch_channels.append(signal)
        event_group = h5.create_group("events")
        event_group.create_dataset("epoch_ms", data=np.asarray([row[0] for row in events], dtype=np.int64))
        event_group.create_dataset("event_type", data=np.asarray([row[1] for row in events], dtype=object), dtype=string_dtype)
        event_group.create_dataset("payload_json", data=np.asarray([row[2] for row in events], dtype=object), dtype=string_dtype)
        event_group.create_dataset("source_line", data=np.asarray([row[3] for row in events], dtype=np.int64))
        sync = h5.create_group("sync")
        markers = [row[2] for row in events if row[1] in {"camera_flash_marker", "imu_clap_marker", "gesture_start", "gesture_end"}]
        sync.create_dataset("markers", data=np.asarray(markers, dtype=object), dtype=string_dtype)
        integrity = h5.create_group("integrity")
        integrity.create_dataset("source_files", data=np.asarray([r[0] for r in source_rows], dtype=object), dtype=string_dtype)
        integrity.create_dataset("source_sizes", data=np.asarray([r[1] for r in source_rows], dtype=np.int64))
        integrity.create_dataset("sha256", data=np.asarray([r[2] for r in source_rows], dtype=object), dtype=string_dtype)
        integrity.create_dataset("datasets", data=np.asarray([r[3] for r in source_rows], dtype=object), dtype=string_dtype)
        h5.flush()

    validate_capture_hdf5(partial, source_rows)
    os.replace(partial, target)

    session_index = session_dir / "session.json"
    index = json.loads(session_index.read_text(encoding="utf-8")) if session_index.exists() else {}
    index.update({
        "session_id": payload.get("session_id", ""),
        "session_tag": tag,
        "capture_storage": {"format": "hdf5", "schema_name": "xgr.capture", "schema_version": "1.0", "filename_template": "<session_tag>_capture_NN.h5"},
    })
    captures = [item for item in index.get("captures", []) if int(item.get("capture_segment", -1)) != segment]
    captures.append({
        "capture_id": f"cap-{segment:03d}",
        "capture_segment": segment,
        "scene_id": payload.get("scene_id", ""),
        "scene_label": payload.get("scene_label", ""),
        "status": actual_status,
        "start_epoch_ms": start_ms,
        "end_epoch_ms": end_ms,
        "hdf5_file": filename,
        "video_channels": video_channels,
        "watch_channels": watch_channels,
    })
    index["captures"] = sorted(captures, key=lambda item: item["capture_segment"])
    session_partial = session_dir / "session.json.partial"
    session_partial.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(session_partial, session_index)
    return target


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
                    "hdf5_ready": h5py is not None and np is not None,
                    "hdf5_schema": "xgr.capture/2.0",
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
        if parsed.path == "/api/export_hdf5":
            return self.export_hdf5()
        if parsed.path == "/api/prepare_capture":
            payload = read_json(self)
            session = safe_name(payload.get("session_id", ""))
            capture_index = int(payload.get("capture_segment") or 0)
            if not session or capture_index < 1:
                return write_json(self, {"ok": False, "error": "Session 或 Capture 编号无效"}, status=400)
            capture_dir = capture_directory(session_directory(session), capture_index)
            marker = {
                "capture_index": capture_index,
                "camera_mode": "wireless_manual",
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

    def export_hdf5(self):
        payload = read_json(self)
        participant = safe_name(payload.get("participant_id", "participant"))
        session = safe_name(payload.get("session_id", "session"))
        session_dir = session_directory(session)
        session_dir.mkdir(parents=True, exist_ok=True)
        try:
            if build_capture_hdf5_v2 is None:
                raise RuntimeError("缺少 xgr.capture 2.0 导出器或 imageio-ffmpeg")
            target = build_capture_hdf5_v2(session_dir, payload)
        except Exception as exc:
            return write_json(self, {"ok": False, "error": str(exc)}, status=400)
        return write_json(self, {"ok": True, "hdf5_file": str(target), "filename": target.name})


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
