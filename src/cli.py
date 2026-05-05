# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""CLI command handlers for Tagger for Resolve.

Each cmd_* function takes config/queue/args and returns an int exit code.
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

from batch_client import (
    BatchClient, BatchSubmitError, CreditsExhaustedError,
)
from claude_analyzer import ClaudeAnalyzer, test_proxy_connection
from config import Config, app_dir
from frame_extractor import FrameExtractor, test_ffmpeg
from metadata_merge import merge_technical_metadata
from metadata_queue import MetadataQueue
from resolve_connector import describe as describe_resolve
from video_tagger import process_video, DemoExhaustedError
from flush_worker import FlushWorker
from dialogs import confirm
from resolve_helpers import TAGGER_NATIVE_FIELDS, save_and_reload_project

logger = logging.getLogger(__name__)


def build_analyzer(cfg: Config) -> ClaudeAnalyzer:
    return ClaudeAnalyzer(
        proxy_url=cfg.proxy_url,
        license_key=cfg.license_key,
        hardware_id=cfg.effective_hardware_id,
        description_length=cfg.description_length,
    )


def cmd_validate(cfg: Config, version: str) -> int:
    print(f"Tagger for Resolve {version}")
    print("ffmpeg/ffprobe:", "OK" if test_ffmpeg() else "MISSING")
    print("proxy:         ", "OK" if test_proxy_connection(cfg.proxy_url) else "FAIL")
    try:
        from braw_extractor import is_braw_available
        print("BRAW SDK:      ", "OK" if is_braw_available() else "not installed (.braw files will be skipped)")
    except Exception as e:
        print(f"BRAW SDK:       ERROR ({e})")
    info = describe_resolve()
    print("resolve:")
    for k, v in info.items():
        print(f"  {k}: {v}")
    print("watch_folder: ", cfg.watch_folder or "(unset)")
    print("license_key:  ", "subscription" if cfg.is_subscription else ("set" if cfg.license_key else "(unset)"))
    print("hardware_id:  ", "(not needed)" if cfg.is_subscription else (cfg.hardware_id or "(unset)"))
    return 0


def cmd_status(queue: MetadataQueue) -> int:
    counts = queue.counts_by_status()
    if not counts:
        print("queue empty")
    else:
        for status, n in sorted(counts.items()):
            print(f"  {status:8s}  {n}")
    return 0


def cmd_process_file(cfg: Config, queue: MetadataQueue, path: str, version: str) -> int:
    if not Path(path).exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1
    analyzer = build_analyzer(cfg)
    try:
        ok = process_video(path, analyzer, queue, tagger_version=version, cfg=cfg)
    except DemoExhaustedError as e:
        print(f"DEMO LIMIT: {e}", file=sys.stderr)
        return 2
    print("queued" if ok else "failed")
    return 0 if ok else 1


def cmd_flush_once(queue: MetadataQueue) -> int:
    worker = FlushWorker(queue)
    worker._tick()
    print(worker.status())
    return 0


def cmd_batch(cfg: Config, queue: MetadataQueue, paths: list[str], version: str) -> int:
    if not paths:
        print("no files supplied", file=sys.stderr)
        return 1

    items: list[tuple[str, str, str, float, str, dict]] = []
    temp_dirs: list[str] = []
    print(f"Extracting frames for {len(paths)} files...")
    for i, path in enumerate(paths, 1):
        if not Path(path).exists():
            print(f"  [{i}/{len(paths)}] SKIP (missing): {path}")
            continue
        name = Path(path).name
        duration = FrameExtractor.get_duration(path)
        if duration is None:
            print(f"  [{i}/{len(paths)}] SKIP (no duration): {name}")
            continue
        stitched, td = FrameExtractor.extract_and_stitch(path)
        if not stitched:
            print(f"  [{i}/{len(paths)}] SKIP (stitch failed): {name}")
            if td:
                temp_dirs.append(td)
            continue
        b64 = BatchClient.encode_image(stitched)
        info = FrameExtractor._get_video_info(path) or {}
        custom_id = f"job-{i:04d}"
        items.append((custom_id, b64, name, duration, path, info))
        if td:
            temp_dirs.append(td)
        cam = f"{info.get('camera_make','?')} / {info.get('camera_model','?')}"
        print(f"  [{i}/{len(paths)}] stitched: {name} ({len(b64)//1024} KB)  cam={cam}")

    if not items:
        print("no files to submit", file=sys.stderr)
        return 1

    bc = BatchClient(
        proxy_url=cfg.proxy_url,
        license_key=cfg.license_key,
        hardware_id=cfg.effective_hardware_id,
        description_length=cfg.description_length,
    )

    print(f"\nSubmitting batch of {len(items)} items...")
    try:
        sub = bc.submit([(cid, b64, n) for (cid, b64, n, _, _, _) in items])
    except CreditsExhaustedError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    except BatchSubmitError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    print(f"  batch_id={sub.batch_id}")
    print(f"  pre-deducted={sub.credits_pre_deducted} balance_after={sub.credits_remaining}")

    print("\nWaiting for results (poll every 10s)...")
    def progress(st):
        c = st.counts
        print(
            f"  status={st.processing_status} "
            f"processing={c.get('processing',0)} "
            f"succeeded={c.get('succeeded',0)} errored={c.get('errored',0)}"
        )

    try:
        res = bc.wait_for(sub.batch_id, poll_interval=10.0, progress_cb=progress)
    except TimeoutError as e:
        print(f"TIMEOUT: {e}", file=sys.stderr)
        return 3

    print(f"\nBatch ended: succeeded={res.succeeded} failed={res.failed} "
          f"refunded={res.credits_refunded} balance={res.credits_remaining}")

    by_id = {cid: (n, d, p, info) for (cid, _, n, d, p, info) in items}
    enqueued = 0
    for r in res.results:
        if r.status != "succeeded" or not r.metadata:
            print(f"  [{r.custom_id}] {r.status}: {r.error or '(no metadata)'}")
            continue
        meta = dict(r.metadata)
        name, dur, path, info = by_id[r.custom_id]
        merge_technical_metadata(meta, info, tagger_version=version, schema_version=bc.schema_version)
        queue.enqueue(path, meta, duration_s=dur)
        enqueued += 1
        print(f"  [{r.custom_id}] OK: {name}")

    for td in temp_dirs:
        FrameExtractor.cleanup_frames([], td)

    print(f"\nQueued {enqueued} items for Resolve write.")
    print("Run --flush-once to push them into the open Resolve project.")
    return 0


def cmd_apply_cached(queue: MetadataQueue) -> int:
    from resolve_connector import get_current_project
    from resolve_writer import (
        _walk_clips, _clip_filename, _clip_duration_seconds,
        write_metadata_to_clip,
    )

    project = get_current_project()
    if project is None:
        print("No Resolve project is open.", file=sys.stderr)
        return 1

    project_name = project.GetName()
    cached_names = queue.list_written_filenames()
    if not cached_names:
        print("No cached metadata in database.")
        return 0

    clips = list(_walk_clips(project.GetMediaPool().GetRootFolder()))
    cached_lower = {n.lower() for n in cached_names}
    applied = 0
    skipped = 0

    for clip in clips:
        name = _clip_filename(clip)
        if not name or name.lower() not in cached_lower:
            continue

        existing = clip.GetMetadata() or {}
        if existing.get("Keywords"):
            skipped += 1
            continue

        dur = _clip_duration_seconds(clip)
        row = queue.lookup_written(name, dur)
        if row is None:
            continue

        ok, msg = write_metadata_to_clip(clip, row["metadata"])
        if ok:
            applied += 1
            print(f"  Applied: {name}")
        else:
            print(f"  Failed:  {name}: {msg}")

    if applied:
        save_and_reload_project(project_name)
    print(f"\nApplied cached tags to {applied} clips, skipped {skipped} (already tagged).")
    return 0


def cmd_clear_metadata() -> None:
    from resolve_connector import get_current_project
    from resolve_writer import _walk_clips

    project = get_current_project()
    if project is None:
        logger.warning("Clear Metadata: no Resolve project is open")
        return

    project_name = project.GetName()
    clips = list(_walk_clips(project.GetMediaPool().GetRootFolder()))
    tagged = sum(
        1 for c in clips
        if any((c.GetMetadata() or {}).get(f) for f in TAGGER_NATIVE_FIELDS)
        or (c.GetThirdPartyMetadata() or {})
    )

    confirmed = confirm(
        title="Clear Tagger Metadata",
        message=(
            f"Clear all Tagger metadata from {tagged} clips "
            f"in \"{project_name}\"? This cannot be undone."
        ),
        confirm_label="Clear Metadata",
    )
    if not confirmed:
        logger.info("Clear Metadata: cancelled by user")
        return

    nc = tc = 0
    for c in clips:
        existing = c.GetMetadata() or {}
        for f in TAGGER_NATIVE_FIELDS:
            if existing.get(f):
                if c.SetMetadata(f, ""):
                    nc += 1
        third = c.GetThirdPartyMetadata() or {}
        if third:
            c.SetThirdPartyMetadata({k: "" for k in third})
            tc += len(third)

    logger.info(f"Cleared {nc} native + {tc} third-party fields from {len(clips)} clips")
    save_and_reload_project(project_name)


def cmd_uninstall() -> int:
    app_path = Path("/Applications/Tagger for Resolve.app")
    data_dir = app_dir()

    has_keychain = False
    try:
        import keyring
        has_keychain = bool(keyring.get_password("TaggerForResolve", "license_key"))
    except Exception:
        pass

    found = []
    if app_path.exists():
        found.append(str(app_path))
    if data_dir.exists():
        found.append(str(data_dir))
    if has_keychain:
        found.append("License key from Keychain")

    if not found:
        print("Tagger for Resolve is not installed.")
        return 0

    print("This will remove:")
    for p in found:
        print(f"  {p}")
    answer = input("\nProceed? [y/N] ").strip().lower()
    if answer != "y":
        print("Cancelled.")
        return 0

    if app_path.exists():
        shutil.rmtree(app_path)
        print(f"  Removed {app_path}")
    if data_dir.exists():
        shutil.rmtree(data_dir)
        print(f"  Removed {data_dir}")

    try:
        import keyring
        keyring.delete_password("TaggerForResolve", "license_key")
        print("  Removed license key from Keychain")
    except Exception:
        pass

    print("Uninstall complete.")
    return 0
