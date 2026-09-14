#!/usr/bin/env python3
"""Small standalone EFT log monitor.

Examples:
    python raid_monitor.py --log-dir "C:/.../Escape from Tarkov/Logs"
    python raid_monitor.py --log-dir ./logs --raid-sound sounds/raid_starting.mp3
    python raid_monitor.py --progress-json progress.json --tasks-json tasks.json --list-open-tasks

The live monitor reads only newly appended log records. It does not modify EFT
logs or send task updates back to Tarkov Tracker.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.request import Request, urlopen

LOG_RECORD_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}(?: [+-]\d{2}:\d{2})?\|")
LOG_FOLDER = re.compile(r"log_\d+\.\d+\.\d+_\d+-\d+-\d+$")


@dataclass
class LogRecord:
    message: str
    payload: dict[str, Any] | None


def newest_log_folder(log_root: Path) -> Path:
    """Return the newest EFT log_* folder, or log_root when it is already one."""
    if LOG_FOLDER.match(log_root.name):
        return log_root
    folders = [path for path in log_root.iterdir() if path.is_dir() and LOG_FOLDER.match(path.name)]
    if not folders:
        return log_root
    return max(folders, key=lambda path: path.stat().st_mtime)


def parse_record(lines: list[str]) -> LogRecord:
    text = "".join(lines).strip()
    first_line = lines[0].rstrip("\r\n")
    separator = first_line.find("|")
    message = first_line[separator + 1 :] if separator >= 0 else first_line

    json_start = text.find("{")
    payload = None
    if json_start >= 0:
        try:
            decoded = json.loads(text[json_start:])
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            pass
    return LogRecord(message=message, payload=payload)


def follow_file(path: Path, poll_seconds: float = 0.25) -> Iterator[LogRecord]:
    """Yield complete records appended to a file, starting at its current end."""
    position = path.stat().st_size
    pending: list[str] = []

    while True:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                while True:
                    line = handle.readline()
                    if line:
                        position = handle.tell()
                        if LOG_RECORD_START.match(line):
                            if pending:
                                yield parse_record(pending)
                            pending = [line]
                        elif pending:
                            pending.append(line)
                        continue
                    time.sleep(poll_seconds)
                    try:
                        if path.stat().st_size < position:
                            position = 0
                            pending = []
                            break
                    except FileNotFoundError:
                        break
        except FileNotFoundError:
            time.sleep(1)


def play_sound(path: Path | None, key: str) -> None:
    """Play a sound without adding a Python audio dependency."""
    if path is None:
        print(f"SOUND: {key} (pass --{key.replace('_', '-').replace('raid-starting', 'raid-sound').replace('match-found', 'match-sound')} to play a file)")
        return
    if not path.is_file():
        print(f"Sound file does not exist: {path}", file=sys.stderr)
        return

    system = platform.system()
    if system == "Darwin" and shutil.which("afplay"):
        subprocess.Popen(["afplay", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif shutil.which("ffplay"):
        subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(f"No supported audio player found for {path}", file=sys.stderr)


def task_id_from_record(record: LogRecord) -> str | None:
    message = record.payload.get("message") if record.payload else None
    template_id = message.get("templateId") if isinstance(message, dict) else None
    return template_id.split(" ", 1)[0] if isinstance(template_id, str) else None


def task_status_from_record(record: LogRecord) -> str | None:
    message = record.payload.get("message") if record.payload else None
    message_type = message.get("type") if isinstance(message, dict) else None
    return {10: "started", 11: "failed", 12: "finished"}.get(message_type)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_progress(url: str, token: str) -> dict[str, Any]:
    request = Request(url.rstrip("/") + "/progress", headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Tracker progress response must be a JSON object")
    return value


def open_tasks(progress: dict[str, Any], definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join task definitions with Tracker progress and return unfinished tasks."""
    data = progress.get("data", progress)
    progress_by_id = {
        item.get("id"): item
        for item in data.get("tasksProgress", [])
        if isinstance(item, dict) and item.get("id")
    }
    result = []
    for task in definitions:
        task_id = task.get("id")
        status = progress_by_id.get(task_id)
        if status is None or not any(status.get(flag, False) for flag in ("complete", "failed", "invalid")):
            result.append(task)
    return result


def print_open_tasks(progress: dict[str, Any], definitions: list[dict[str, Any]]) -> None:
    for task in open_tasks(progress, definitions):
        print(f"{task.get('id', '?')}: {task.get('name', task.get('title', 'unnamed task'))}")


def monitor(log_dir: Path, raid_sound: Path | None, match_sound: Path | None) -> None:
    folder = newest_log_folder(log_dir)
    application = folder / "application.log"
    notifications = folder / "notifications.log"
    print(f"Watching {folder}")

    records: queue.Queue[LogRecord] = queue.Queue()

    def read_file(path: Path) -> None:
        for record in follow_file(path):
            records.put(record)

    for path in (application, notifications):
        threading.Thread(target=read_file, args=(path,), daemon=True).start()

    game_start_seen = False

    while True:
        record = records.get()
        if "TRACE-NetworkGameCreate profileStatus" in record.message:
            print("MATCH FOUND")
            play_sound(match_sound, "match_found")
        if "GameStarting" in record.message:
            game_start_seen = True
            print("RAID STARTING")
            play_sound(raid_sound, "raid_starting")
        if "GameStarted" in record.message:
            print("RAID STARTED")
            if not game_start_seen:
                print("RAID START SOUND FALLBACK")
                play_sound(raid_sound, "raid_starting")
            game_start_seen = False
        status = task_status_from_record(record)
        task_id = task_id_from_record(record)
        if status and task_id:
            print(f"TASK {status.upper()}: {task_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read EFT logs for raid audio events and task notifications.")
    parser.add_argument("--log-dir", type=Path, help="EFT Logs directory or a log_YYYY.MM.DD_H-mm-ss folder")
    parser.add_argument("--raid-sound", type=Path, help="MP3/WAV file for raid starting")
    parser.add_argument("--match-sound", type=Path, help="MP3/WAV file for match found")
    parser.add_argument("--progress-json", type=Path, help="Saved TarkovTracker /progress response")
    parser.add_argument("--tasks-json", type=Path, help="JSON array of task definitions")
    parser.add_argument("--tracker-url", help="TarkovTracker API base URL, used with --token")
    parser.add_argument("--token", help="TarkovTracker API token")
    parser.add_argument("--list-open-tasks", action="store_true", help="Print open tasks and exit")
    args = parser.parse_args()

    if args.list_open_tasks:
        if args.progress_json:
            progress = load_json(args.progress_json)
        elif args.tracker_url and args.token:
            progress = get_progress(args.tracker_url, args.token)
        else:
            parser.error("--list-open-tasks requires --progress-json or --tracker-url with --token")
        if not args.tasks_json:
            parser.error("--list-open-tasks also requires --tasks-json")
        definitions = load_json(args.tasks_json)
        task_list = definitions.get("tasks", definitions) if isinstance(definitions, dict) else definitions
        if not isinstance(task_list, list):
            parser.error("--tasks-json must contain a JSON array or an object with a 'tasks' array")
        print_open_tasks(progress, task_list)
        return 0

    if not args.log_dir:
        parser.error("live monitoring requires --log-dir")
    try:
        monitor(args.log_dir, args.raid_sound, args.match_sound)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
