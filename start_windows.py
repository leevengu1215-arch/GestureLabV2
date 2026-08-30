#!/usr/bin/env python3
import os
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
START_PORT = 8766
MAX_PORT = 8795


def get_gesture_lab_status(port):
    try:
        with urllib.request.urlopen(
            f"http://{HOST}:{port}/api/status", timeout=0.5
        ) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def is_gesture_lab(port):
    status = get_gesture_lab_status(port)
    return bool(status and status.get("ok"))


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
        except OSError:
            return False
    return True


def choose_port():
    if is_gesture_lab(START_PORT):
        return START_PORT, True
    for port in range(START_PORT, MAX_PORT + 1):
        if port_is_free(port):
            return port, False
    raise RuntimeError(f"No available port between {START_PORT} and {MAX_PORT}.")


def wait_until_ready(port, process, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_gesture_lab(port):
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def main():
    os.chdir(APP_DIR)
    port, already_running = choose_port()
    url = f"http://{HOST}:{port}/"

    if already_running:
        print(f"[Gesture Lab] Already running: {url}")
        webbrowser.open(url)
        return 0

    env = os.environ.copy()
    env["GESTURE_LAB_PORT"] = str(port)
    print(f"[Gesture Lab] Starting local server: {url}")
    print("[Gesture Lab] Keep this window open while using the console.")
    process = subprocess.Popen([sys.executable, str(APP_DIR / "app.py")], env=env)

    if not wait_until_ready(port, process):
        print("[Gesture Lab] The server did not start successfully.")
        print("[Gesture Lab] Check the error message above, then try again.")
        if process.poll() is None:
            process.terminate()
        input("Press Enter to close...")
        return 1

    print(f"[Gesture Lab] Ready. Opening browser: {url}")
    webbrowser.open(url)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
