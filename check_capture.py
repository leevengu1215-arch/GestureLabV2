#!/usr/bin/env python3
"""Validate one complete xgr.capture 2.0 session before delivery."""

import json
import sys
from pathlib import Path

import h5py

from capture_hdf5_v2 import validate_capture


def main():
    if len(sys.argv) < 2:
        print("用法：python check_capture.py <session_id>")
        return 2
    app_dir = Path(__file__).resolve().parent
    session_id = sys.argv[1]
    session_dir = app_dir / "sessions" / session_id
    index_path = session_dir / "session.json"
    if not index_path.exists():
        print(f"失败：找不到 {index_path}")
        return 1
    index = json.loads(index_path.read_text(encoding="utf-8"))
    captures = index.get("captures", [])
    if not captures:
        print("失败：Session 中没有 Capture")
        return 1
    expected = list(range(1, len(captures) + 1))
    actual = [int(item.get("capture_index", -1)) for item in captures]
    if actual != expected:
        print(f"失败：Capture 编号必须连续，当前为 {actual}")
        return 1
    errors = []
    for item in captures:
        capture_index = int(item["capture_index"])
        expected_name = f"{session_id}_capture_{capture_index:02d}.h5"
        path = session_dir / expected_name
        try:
            if item.get("hdf5_file") != expected_name:
                raise RuntimeError("session.json 文件名不一致")
            validate_capture(path)
            with h5py.File(path, "r") as h5:
                if h5.attrs.get("status") != "valid":
                    raise RuntimeError(f"状态为 {h5.attrs.get('status')}，不是 valid")
                for signal in ("accel", "gyroscope"):
                    if signal not in h5["watch"]:
                        raise RuntimeError(f"缺少 /watch/{signal}")
                    if h5[f"watch/{signal}"].attrs.get("alignment_status") == "unavailable":
                        raise RuntimeError(f"/watch/{signal} 尚未对齐")
            print(f"通过：{expected_name}")
        except Exception as exc:
            errors.append(f"{expected_name}: {exc}")
    if errors:
        print("\n验收失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"\nSession {session_id} 验收通过，可进入标注流程。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
