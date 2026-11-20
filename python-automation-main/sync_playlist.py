"""Fetch a JSON playlist from your configured server."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

FIELDS = ("model", "title", "page_url", "stream_url", "thumbnail")


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    source = os.environ.get("PLAYLIST_SOURCE_URL", "").strip()
    if not source:
        raise ValueError("Set the PLAYLIST_SOURCE_URL repository variable to your server JSON endpoint.")
    if urlsplit(source).scheme not in ("http", "https"):
        raise ValueError("The source must be an HTTP or HTTPS JSON endpoint.")
    request = Request(source, headers={"Accept": "application/json", "User-Agent": "PlaylistSync/1.0"})
    with urlopen(request, timeout=45) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError("The server must return a JSON array of playlist entries.")
    entries = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Entry {index} must be an object.")
        entry = {key: item.get(key, "") for key in FIELDS}
        if not all(isinstance(value, str) for value in entry.values()):
            raise ValueError(f"Entry {index}: playlist fields must be strings.")
        if not entry["title"].strip() or not entry["stream_url"].strip():
            raise ValueError(f"Entry {index} needs title and stream_url.")
        if urlsplit(entry["stream_url"]).scheme not in ("http", "https"):
            raise ValueError(f"Entry {index} has an invalid stream URL.")
        entries.append(entry)
    now = datetime.now(timezone.utc)
    india = timezone(timedelta(hours=5, minutes=30))
    folder = Path(__file__).resolve().parent
    status = {
        "message": "Server playlist refreshed successfully!",
        "last_updated_utc": now.isoformat(timespec="seconds"),
        "last_updated_ist": now.astimezone(india).isoformat(timespec="seconds"),
        "refresh_interval_hours": 5,
        "playlist_file": "playlist.json",
        "playlist_entries": len(entries),
    }
    save_json(folder / "playlist.json", entries)
    save_json(folder / "demo-status.json", status)
    print(f"Saved playlist.json with {len(entries)} entries.")


if __name__ == "__main__":
    main()
