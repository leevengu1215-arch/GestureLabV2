#!/usr/bin/env python3
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8766


def ensure_hdf5_dependencies():
    try:
        import h5py  # noqa: F401
        import numpy  # noqa: F401
        return True
    except ImportError:
        print("[Gesture Lab] 正在安装 HDF5 组件（h5py、numpy）...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "-r", str(APP_DIR / "requirements-hdf5.txt")],
            check=False,
        )
        return result.returncode == 0


def ready():
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/status", timeout=0.5) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def main():
    os.chdir(APP_DIR)
    if not ensure_hdf5_dependencies():
        input("HDF5 组件安装失败。请检查网络后按 Return 关闭...")
        return 1

    env = os.environ.copy()
    env["GESTURE_LAB_PORT"] = str(PORT)
    url = f"http://{HOST}:{PORT}/"
    print(f"[Gesture Lab] 正在启动：{url}")
    print("[Gesture Lab] 实验期间请保持此窗口打开。")
    process = subprocess.Popen([sys.executable, str(APP_DIR / "app.py")], env=env)

    for _ in range(80):
        if ready():
            webbrowser.open(url)
            return process.wait()
        if process.poll() is not None:
            input("启动失败，按 Return 关闭...")
            return 1
        time.sleep(0.25)

    process.terminate()
    input("启动超时，按 Return 关闭...")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
