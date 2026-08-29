"""Minimal encoded-video HDF5 writer. One file is one immutable capture."""

import csv
import hashlib
import io
import json
import math
import os
import re
from pathlib import Path

import h5py
import numpy as np


SCHEMA_NAME = "xgr.capture"
SCHEMA_VERSION = "2.1-encoded"
CAMERAS = ("cam-01", "cam-02", "cam-03")
REQUIRED_WATCH = ("accel", "gyroscope")
STRING = h5py.string_dtype("utf-8")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _notes(event):
    raw = event.get("notes", "")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return {"notes": str(raw)}


def _read_events(session_dir, capture_index, start_epoch, end_epoch):
    rows = []
    path = session_dir / "workflow_events.jsonl"
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        detail = _notes(event)
        epoch = float(detail.get("epoch_ms") or event.get("saved_at_ms") or 0)
        if start_epoch <= epoch <= end_epoch:
            event["_detail"] = detail
            event["_epoch"] = epoch
            rows.append(event)
    return sorted(rows, key=lambda item: item["_epoch"])


def _marker(events, kind, position):
    candidates = []
    for event in events:
        if event.get("status") != kind:
            continue
        detail = event["_detail"]
        stage_key = str(detail.get("stage_key", ""))
        marker_position = str(detail.get("calibration_position", ""))
        is_end = marker_position == "end" or stage_key.endswith(":end")
        if (position == "end") == is_end:
            values = detail.get("clap_epoch_ms") if kind == "imu_clap_marker" else detail.get("epoch_ms")
            if isinstance(values, list):
                candidates.extend(float(value) for value in values)
            elif values:
                candidates.append(float(values))
    return (candidates[-1] if position == "end" else candidates[0]) if candidates else math.nan


def _video_metadata(session_dir, capture_index):
    result = {}
    paths = []
    capture_dir = session_dir / f"capture{capture_index}"
    if capture_dir.exists():
        paths.extend(capture_dir.glob("camera*/*.json"))
    legacy_dir = session_dir / "videos"
    if legacy_dir.exists():
        paths.extend(legacy_dir.glob("*.json"))
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if int(item.get("capture_segment") or 0) != capture_index:
            continue
        camera = item.get("camera_id")
        video_path = Path(item.get("file", ""))
        if camera in CAMERAS and video_path.exists():
            previous = result.get(camera)
            saved_at = float(item.get("saved_at_ms") or path.stat().st_mtime * 1000)
            previous_saved_at = float(previous[0].get("saved_at_ms") or 0) if previous else -1
            if saved_at >= previous_saved_at:
                result[camera] = (item, video_path)
    return result


def _write_video(group, path, meta, cam1_start_epoch, flash_start, flash_end):
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError(f"视频文件为空：{path.name}")
    chunk_size = min(size, 8 * 1024 * 1024)
    encoded = group.create_dataset("encoded", shape=(size,), dtype=np.uint8, chunks=(chunk_size,))
    digest = hashlib.sha256()
    offset = 0
    with path.open("rb") as source:
        while True:
            raw = source.read(chunk_size)
            if not raw:
                break
            digest.update(raw)
            encoded[offset:offset + len(raw)] = np.frombuffer(raw, dtype=np.uint8)
            offset += len(raw)
    start_epoch = float(meta.get("start_epoch_ms") or 0)
    end_epoch = float(meta.get("end_epoch_ms") or start_epoch)
    duration = max(0.0, end_epoch - start_epoch)
    group.create_dataset("time_range", data=np.asarray([0.0, duration], dtype=np.float64))
    group.create_dataset(
        "aligned_time_range",
        data=np.asarray([start_epoch - cam1_start_epoch, end_epoch - cam1_start_epoch], dtype=np.float64),
    )
    group.attrs.update({
        "available": True,
        "storage": "encoded",
        "container": str(meta.get("container") or path.suffix.lstrip(".") or "mp4"),
        "mime_type": str(meta.get("mime_type") or "video/mp4"),
        "encoded_sha256": digest.hexdigest(),
        "start_epoch_ms": start_epoch,
        "end_epoch_ms": end_epoch,
        "start_flash_ms": np.float64(flash_start),
        "end_flash_ms": np.float64(flash_end),
        "timestamp_source": "recording-metadata",
    })
    return duration


def _decode_csv(path):
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        return None
    rows = list(csv.reader(io.StringIO(text)))
    header = None
    for index, row in enumerate(rows):
        normalized = [_norm(value) for value in row]
        if any("timestamp" in value or value in {"time", "currentms", "eventms"} for value in normalized):
            header = index
            break
    if header is None:
        return None
    columns = [value.strip() or f"column-{index + 1}" for index, value in enumerate(rows[header])]
    values = []
    for row in rows[header + 1:]:
        if len(row) != len(columns) or not any(value.strip() for value in row):
            continue
        try:
            values.append([float(value) for value in row])
        except ValueError:
            continue
    return (columns, np.asarray(values, dtype=np.float64)) if values else None


def _norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _signal_name(path, columns):
    text = _norm(path.name + " " + " ".join(columns))
    if any(value in text for value in ("accel", "accelerometer", "gsensor")):
        return "accel"
    if any(value in text for value in ("gyro", "gyroscope")):
        return "gyroscope"
    if any(value in text for value in ("magnetic", "magnetometer")):
        return "magnetic"
    if any(value in text for value in ("barometer", "pressure")):
        return "barometer"
    for signal in ("ppg-green", "ppg-red", "ppg-ir"):
        if signal.replace("-", "") in text:
            return signal
    return None


def _column(columns, candidates):
    normalized = [_norm(value) for value in columns]
    for candidate in candidates:
        candidate = _norm(candidate)
        if candidate in normalized:
            return normalized.index(candidate)
    return None


def _watch_arrays(root, start_epoch, end_epoch):
    collected = {}
    if not root or not root.exists():
        return collected
    for path in root.rglob("*.csv"):
        parsed = _decode_csv(path)
        if not parsed:
            continue
        columns, table = parsed
        signal = _signal_name(path, columns)
        if not signal:
            continue
        time_index = _column(columns, ("EventTimestamp(ms)", "CurrentTimestamp(ms)", "timestamp", "time", "event_ms", "current_ms"))
        if time_index is None:
            continue
        if signal in REQUIRED_WATCH:
            axes = [_column(columns, (axis, f"{signal}_{axis}", f"axis_{axis}")) for axis in "xyz"]
            if any(index is None for index in axes):
                continue
            value = table[:, axes]
            out_columns = ["x", "y", "z"]
        else:
            value_indexes = [index for index in range(table.shape[1]) if index != time_index]
            value = table[:, value_indexes]
            out_columns = [columns[index] for index in value_indexes]
        times = table[:, time_index].astype(np.float64)
        name = _norm(columns[time_index])
        if name.endswith("ns"):
            times /= 1_000_000.0
        elif name.endswith("us"):
            times /= 1_000.0
        if np.nanmedian(times) > 100_000_000_000:
            mask = (times >= start_epoch) & (times <= end_epoch)
            times, value = times[mask], value[mask]
        if len(times):
            collected.setdefault(signal, []).append((times, value, out_columns))
    result = {}
    for signal, chunks in collected.items():
        times = np.concatenate([chunk[0] for chunk in chunks])
        values = np.concatenate([chunk[1] for chunk in chunks])
        order = np.argsort(times, kind="stable")
        times, values = times[order], values[order]
        keep = np.r_[True, np.diff(times) > 0]
        result[signal] = (times[keep], values[keep], chunks[0][2])
    return result


def _watch_alignment(times, values, cam1_start_epoch, clap_start, clap_end):
    if not len(times):
        return np.asarray([], dtype=np.float64), np.asarray([math.nan, math.nan]), "unavailable"
    if np.nanmedian(times) > 100_000_000_000:
        aligned = times - cam1_start_epoch
        bias = np.asarray([aligned[0] - times[0], aligned[-1] - times[-1]], dtype=np.float64)
        return aligned, bias, "epoch-clock"
    if values.shape[1] >= 3 and np.isfinite(clap_start):
        magnitude = np.linalg.norm(values[:, :3], axis=1)
        edge = max(1, len(times) // 3)
        start_raw = times[int(np.argmax(magnitude[:edge]))]
        if np.isfinite(clap_end):
            end_index = edge + int(np.argmax(magnitude[edge:]))
            end_raw = times[end_index]
            if end_raw > start_raw:
                start_bias, end_bias = clap_start - start_raw, clap_end - end_raw
                aligned = times + np.interp(times, [start_raw, end_raw], [start_bias, end_bias])
                return aligned, np.asarray([start_bias, end_bias]), "clap-linear"
        bias = clap_start - start_raw
        return times + bias, np.asarray([bias, bias]), "clap-offset"
    return np.full(len(times), np.nan), np.asarray([math.nan, math.nan]), "unavailable"


def _event_type(status):
    allowed = {
        "gesture_start", "gesture_end", "baseline_start", "baseline_end",
        "static_start", "static_end", "pre_gesture_start", "pre_gesture_end",
        "camera_flash_marker", "imu_clap_marker",
    }
    return status if status in allowed else status or "workflow"


def _write_events(h5, events, cam1_start_epoch):
    group = h5.create_group("events")
    output = []
    active = {}
    counters = {}
    for event in events:
        kind = _event_type(event.get("status", ""))
        detail = dict(event["_detail"])
        detail.pop("participant_id", None)
        phase = "pre-gesture" if kind.startswith("pre_gesture") else kind.split("_", 1)[0]
        base = re.sub(r"_(start|end)$", "", kind)
        key = (base, detail.get("deck_id", ""), detail.get("page_index", ""), event.get("step", ""))
        if kind.endswith("_start"):
            counters[base] = counters.get(base, 0) + 1
            active[key] = f"{base}-{counters[base]:03d}"
        event_id = active.get(key)
        if not event_id:
            counters[base] = counters.get(base, 0) + 1
            event_id = f"{base}-{counters[base]:03d}"
        if kind.endswith("_end"):
            active.pop(key, None)
        output.append((event_id, kind, phase, event["_epoch"] - cam1_start_epoch, _json(detail)))
    output.sort(key=lambda row: row[3])
    group.create_dataset("event_id", data=np.asarray([row[0] for row in output], dtype=object), dtype=STRING)
    group.create_dataset("event_type", data=np.asarray([row[1] for row in output], dtype=object), dtype=STRING)
    group.create_dataset("phase", data=np.asarray([row[2] for row in output], dtype=object), dtype=STRING)
    group.create_dataset("time", data=np.asarray([row[3] for row in output], dtype=np.float64))
    group.create_dataset("payload_json", data=np.asarray([row[4] for row in output], dtype=object), dtype=STRING)
    return output


def _write_sync(h5, event_rows):
    sync = h5.create_group("sync")
    starts = {row[0]: row for row in event_rows if row[1].endswith("_start")}
    counts = {}
    for row in event_rows:
        if not row[1].endswith("_end") or row[0] not in starts or row[3] < starts[row[0]][3]:
            continue
        start = starts[row[0]]
        phase = start[2]
        counts[phase] = counts.get(phase, 0) + 1
        item = sync.create_group(f"{phase}-{counts[phase]:02d}")
        item.create_dataset("aligned_time", data=np.asarray([start[3], row[3]], dtype=np.float64))
        item.create_dataset("event_id", data=start[0], dtype=STRING)
        try:
            label = json.loads(start[4]).get("page_title", "")
        except json.JSONDecodeError:
            label = ""
        item.create_dataset("label", data=label, dtype=STRING)


def validate_capture(path):
    with h5py.File(path, "r") as h5:
        if h5.attrs.get("schema_name") != SCHEMA_NAME or h5.attrs.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("HDF5 schema 属性不正确")
        for name in ("video", "watch", "events", "sync"):
            if name not in h5:
                raise RuntimeError(f"HDF5 缺少 /{name}")
        for camera in CAMERAS:
            if camera not in h5["video"]:
                raise RuntimeError(f"HDF5 缺少 /video/{camera}")
            group = h5[f"video/{camera}"]
            if bool(group.attrs.get("available", False)):
                for dataset in ("encoded", "time_range", "aligned_time_range"):
                    if dataset not in group:
                        raise RuntimeError(f"/{camera} 缺少 {dataset}")
                if len(group["encoded"]) == 0:
                    raise RuntimeError(f"/{camera} 没有写入编码视频")
                if len(group["time_range"]) != 2 or len(group["aligned_time_range"]) != 2:
                    raise RuntimeError(f"/{camera} 视频时间范围无效")
        if not bool(h5["video/cam-01"].attrs.get("available", False)):
            raise RuntimeError("/video/cam-01 不可用")
        for signal in REQUIRED_WATCH:
            if signal not in h5["watch"]:
                continue
            group = h5[f"watch/{signal}"]
            if len(group["value"]) != len(group["time"]) or len(group["value"]) != len(group["aligned_time/value"]):
                raise RuntimeError(f"/watch/{signal} 数值与时间长度不一致")
            if group["value"].ndim != 2 or group["value"].shape[1] != 3:
                raise RuntimeError(f"/watch/{signal}/value 必须为 (N, 3)")
        event_lengths = [len(h5[f"events/{name}"]) for name in ("event_id", "event_type", "phase", "time", "payload_json")]
        if len(set(event_lengths)) != 1:
            raise RuntimeError("/events 平行数据集长度不一致")


def build_capture_hdf5(session_dir, payload):
    session_dir = Path(session_dir)
    capture_index = int(payload.get("capture_segment") or 0)
    session_id = re.sub(r"[^0-9TZ]+", "", str(payload.get("session_id", "")))
    if not session_id or capture_index < 1:
        raise RuntimeError("Session 必须使用时间 ID，Capture 编号必须为正整数")
    start_epoch = float(payload.get("start_epoch_ms") or 0)
    end_epoch = float(payload.get("end_epoch_ms") or 0)
    if not start_epoch or end_epoch <= start_epoch:
        raise RuntimeError("Capture 起止时间无效")
    filename = f"{session_id}_capture_{capture_index:02d}.h5"
    target, partial = session_dir / filename, session_dir / f"{filename}.partial"
    if target.exists():
        raise RuntimeError(f"Capture 已存在，禁止覆盖：{filename}")
    events = _read_events(session_dir, capture_index, start_epoch, end_epoch)
    flash_start_epoch = _marker(events, "camera_flash_marker", "start")
    flash_end_epoch = _marker(events, "camera_flash_marker", "end")
    clap_start_epoch = _marker(events, "imu_clap_marker", "start")
    clap_end_epoch = _marker(events, "imu_clap_marker", "end")
    videos = _video_metadata(session_dir, capture_index)
    if "cam-01" not in videos:
        raise RuntimeError("Cam-01 视频缺失，不能建立统一时间轴")
    cam1_start_epoch = float(videos["cam-01"][0].get("start_epoch_ms") or start_epoch)
    manual_watch_root = session_dir / f"capture{capture_index}" / "watches"
    watch_root = Path(payload.get("watch_source_dir", "")).expanduser() if payload.get("watch_source_dir") else None
    if manual_watch_root.exists() and any(manual_watch_root.rglob("*.csv")):
        watch_root = manual_watch_root
    watch = _watch_arrays(watch_root, start_epoch, end_epoch)
    missing = [signal for signal in REQUIRED_WATCH if signal not in watch]
    requested_status = str(payload.get("status") or "incomplete")
    status = "incomplete"
    try:
        with h5py.File(partial, "w") as h5:
            h5.attrs.update({
                "schema_name": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
                "session_id": session_id, "capture_index": np.uint32(capture_index),
                "timebase": "cam-01", "time_unit": "ms", "status": status,
                "video_storage": "encoded-container",
            })
            video_group = h5.create_group("video")
            for camera in CAMERAS:
                group = video_group.create_group(camera)
                if camera not in videos:
                    group.attrs["available"] = False
                    group.attrs["alignment_status"] = "unavailable"
                    continue
                meta, path = videos[camera]
                _write_video(
                    group, path, meta, cam1_start_epoch,
                    flash_start_epoch - cam1_start_epoch if np.isfinite(flash_start_epoch) else math.nan,
                    flash_end_epoch - cam1_start_epoch if np.isfinite(flash_end_epoch) else math.nan,
                )
                if camera == "cam-01":
                    group["aligned_time_range"][...] = group["time_range"][...]
            watch_group = h5.create_group("watch")
            clap_start = clap_start_epoch - cam1_start_epoch if np.isfinite(clap_start_epoch) else math.nan
            clap_end = clap_end_epoch - cam1_start_epoch if np.isfinite(clap_end_epoch) else math.nan
            for signal, (times, values, columns) in watch.items():
                group = watch_group.create_group(signal)
                aligned, bias, alignment_status = _watch_alignment(times, values, cam1_start_epoch, clap_start, clap_end)
                group.create_dataset("value", data=values.astype(np.float32), compression="gzip", compression_opts=4, shuffle=True)
                group.create_dataset("time", data=times.astype(np.float64))
                aligned_group = group.create_group("aligned_time")
                aligned_group.create_dataset("value", data=aligned)
                aligned_group.create_dataset("bias", data=bias)
                if signal in REQUIRED_WATCH:
                    aligned_group.create_dataset("start_clap", data=np.float64(clap_start))
                    aligned_group.create_dataset("end_clap", data=np.float64(clap_end))
                group.attrs["columns_json"] = _json(columns)
                group.attrs["unit"] = "unknown"
                group.attrs["alignment_status"] = alignment_status
                group.attrs["alignment_residual_ms"] = abs(float(bias[1] - bias[0])) if np.all(np.isfinite(bias)) else math.nan
            event_rows = _write_events(h5, events, cam1_start_epoch)
            _write_sync(h5, event_rows)
            required_aligned = all(
                signal in watch_group
                and watch_group[signal].attrs.get("alignment_status") != "unavailable"
                and np.all(np.isfinite(watch_group[signal]["aligned_time/value"][...]))
                for signal in REQUIRED_WATCH
            )
            anchors_present = np.isfinite(clap_start)
            status = "valid" if requested_status == "valid" and not missing and required_aligned and anchors_present else "incomplete"
            h5.attrs["status"] = status
            h5.flush()
        validate_capture(partial)
        os.replace(partial, target)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise
    session_json = session_dir / "session.json"
    index = json.loads(session_json.read_text(encoding="utf-8")) if session_json.exists() else {}
    captures = [item for item in index.get("captures", []) if int(item.get("capture_index", -1)) != capture_index]
    captures.append({
        "capture_index": capture_index,
        "hdf5_file": filename,
        "raw_data_dir": f"capture{capture_index}",
        "status": status,
    })
    captures.sort(key=lambda item: item["capture_index"])
    expected = list(range(1, len(captures) + 1))
    if [item["capture_index"] for item in captures] != expected:
        target.unlink(missing_ok=True)
        raise RuntimeError("Capture 编号必须从 01 连续递增")
    clean_index = {
        "session_id": session_id,
        "capture_storage": {
            "format": "hdf5", "schema_name": SCHEMA_NAME, "schema_version": SCHEMA_VERSION,
            "filename_template": "<session_id>_capture_NN.h5",
        },
        "captures": captures,
    }
    index_partial = session_dir / "session.json.partial"
    index_partial.write_text(json.dumps(clean_index, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(index_partial, session_json)
    return target
