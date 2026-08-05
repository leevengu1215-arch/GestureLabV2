#!/usr/bin/env python3
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath


APP_DIR = Path(__file__).resolve().parent
UPDATE_PATTERN = "GestureLab_Update_*.zip"
PROTECTED_ROOTS = {"sessions", "backups"}
PORT_RANGE = range(8765, 8796)


def gesture_lab_is_running():
    running = []
    for port in PORT_RANGE:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=0.12
            ) as response:
                if response.status == 200 and b'"ok": true' in response.read():
                    running.append(port)
        except (OSError, urllib.error.URLError):
            continue
    return running


def choose_update_zip(argument):
    if argument:
        candidate = Path(argument).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"Update package not found: {candidate}")
        return candidate
    candidates = sorted(
        APP_DIR.glob(UPDATE_PATTERN), key=lambda path: path.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise FileNotFoundError(
            f"No {UPDATE_PATTERN} file was found beside the updater."
        )
    return candidates[0]


def safe_payload_path(member_name, payload_root):
    path = PurePosixPath(member_name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe path in update package: {member_name}")
    if not path.parts or path.parts[0] != payload_root:
        return None
    relative = PurePosixPath(*path.parts[1:])
    if not relative.parts:
        return None
    if relative.parts[0].lower() in PROTECTED_ROOTS:
        raise ValueError(f"Update package tried to modify protected data: {relative}")
    return Path(*relative.parts)


def restore_backup(backup_dir, overwritten, created):
    for target in reversed(created):
        if target.exists() and target.is_file():
            target.unlink()
    for relative in overwritten:
        source = backup_dir / relative
        target = APP_DIR / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def apply_update(update_zip):
    with zipfile.ZipFile(update_zip) as archive:
        try:
            manifest = json.loads(archive.read("update_manifest.json").decode("utf-8"))
        except KeyError as exc:
            raise ValueError("Missing update_manifest.json in update package.") from exc
        if manifest.get("format") != 1:
            raise ValueError("Unsupported update package format.")
        version = str(manifest.get("version", "unknown")).strip() or "unknown"
        payload_root = str(manifest.get("payload", "payload")).strip("/") or "payload"
        files = []
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = safe_payload_path(member.filename, payload_root)
            if relative is not None:
                files.append((member, relative))
        if not files:
            raise ValueError("The update package contains no payload files.")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = APP_DIR / "backups" / f"{stamp}_{version}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        overwritten = []
        created = []

        try:
            with tempfile.TemporaryDirectory(prefix="gesture_lab_update_") as temp_name:
                temp_dir = Path(temp_name)
                for member, relative in files:
                    extracted = temp_dir / relative
                    extracted.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, extracted.open("wb") as target_file:
                        shutil.copyfileobj(source, target_file)

                for _, relative in files:
                    source = temp_dir / relative
                    target = APP_DIR / relative
                    if target.exists():
                        if not target.is_file():
                            raise ValueError(f"Cannot replace non-file path: {relative}")
                        backup_target = backup_dir / relative
                        backup_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup_target)
                        overwritten.append(relative)
                    else:
                        created.append(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)

            result = {
                "version": version,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "package": update_zip.name,
                "file_count": len(files),
                "backup": str(backup_dir),
            }
            (APP_DIR / "update_status.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (backup_dir / "update_result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            restore_backup(backup_dir, overwritten, created)
            raise

    applied_dir = APP_DIR / "backups" / "applied_updates"
    applied_dir.mkdir(parents=True, exist_ok=True)
    destination = applied_dir / update_zip.name
    if destination.exists():
        destination = applied_dir / f"{update_zip.stem}_{int(time.time())}.zip"
    shutil.move(str(update_zip), destination)
    return version, len(files), backup_dir


def main():
    running_ports = gesture_lab_is_running()
    if running_ports:
        print(
            "Gesture Lab is still running on port(s): "
            + ", ".join(map(str, running_ports))
        )
        print("Close the Gesture Lab command window, then run the updater again.")
        return 2

    try:
        update_zip = choose_update_zip(sys.argv[1] if len(sys.argv) > 1 else "")
        print(f"Applying update: {update_zip.name}")
        version, file_count, backup_dir = apply_update(update_zip)
    except Exception as exc:
        print(f"Update failed: {exc}")
        return 1

    print(f"Update complete: {version}")
    print(f"Updated files: {file_count}")
    print(f"Backup: {backup_dir}")
    print("Experiment data in sessions was not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
