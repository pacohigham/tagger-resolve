# Tagger for Resolve

AI video metadata tagging for DaVinci Resolve Studio. Cross-platform (macOS, Windows, Linux).

## How it works

1. Drop video files into a watch folder.
2. Tagger extracts frames, builds a 5760x4320 grid with timecode overlays and a metadata header strip, and sends it to the Tagger Render server.
3. Claude returns structured metadata (description, tags, scene, shot, primary subject).
4. Metadata is queued locally in SQLite. When DaVinci Resolve Studio is open with a project, the flush worker writes metadata into matching Media Pool clips via the scripting API.

Resolve does not need to be open during analysis. Process overnight, arrive in the morning to a fully tagged Media Pool.

## Requirements

- DaVinci Resolve **Studio** 19.0.2 or later (the free version blocks external scripting)
- Python 3.9+
- ffmpeg / ffprobe on PATH or in a standard location
- A Tagger license key + hardware ID (purchase at tagger.mov)

## Setup

```bash
pip install -r requirements.txt
```

In Resolve: Preferences > General > "External scripting using" -> **Local**.

## Run

```bash
python src/main.py
```

The tray icon appears. Right-click for status, pause/resume, settings, and quit.

## Metadata mapping

| Tagger output | Resolve field |
|---|---|
| tags (list) | Keyword (comma-separated; auto-creates keyword bins) |
| description | Description |
| scene | Scene |
| shot_type | Shot |
| primary_subject | Comments |
| primary_action, transcript, version, timestamp | Third-party metadata namespace |

## Project layout

```
src/
  config.py              # config loader, app dir, paths
  resolve_connector.py   # cross-platform DaVinciResolveScript discovery
  metadata_queue.py      # SQLite queue
  resolve_writer.py      # SetMetadata + clip matching
  frame_extractor.py     # ffmpeg + opencv + Pillow grid
  claude_analyzer.py     # Render proxy client
  video_tagger.py        # extract -> analyze -> enqueue
  watcher.py             # watchdog folder monitor with stability debounce
  flush_worker.py        # background queue -> Resolve writer
  main.py                # pystray tray entry point
```
