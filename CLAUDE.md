# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Photo Curator is a single-file PyQt5 desktop application for curating large photo collections (1500+ photos) down to a target count (default 33) through iterative rounds of yes/no selection. The UI is in Korean.

## Commands

```bash
# Run the app
python photo_curator.py

# Build standalone binary with PyInstaller (single-file, no console)
# Produces dist/PhotoCurator (binary) and dist/PhotoCurator.app (macOS bundle)
pyinstaller PhotoCurator.spec

# Install dependencies (only PyQt5 is needed at runtime)
pip install PyQt5
```

The project uses `uv` for environment management (`.venv` exists). The `pyproject.toml` declares Python >=3.13 but lists no dependencies (PyQt5 is installed separately).

## Architecture

The entire application lives in `photo_curator.py` (~1478 lines). There are no tests, no linting config, and no CI.

### Key classes (all in `photo_curator.py`):

- **`PhotoCurator(QMainWindow)`** — Main window with a 3-page `QStackedWidget`: setup (page 0), curator (page 1), results (page 2). Manages round-based curation state, session persistence, and all UI logic.
- **`ZoomableImageView(QGraphicsView)`** — Single-photo viewer with mouse wheel zoom, drag-pan, fit-to-view, and original-size modes.
- **`FullImageLoader(QThread)`** — Background thread for loading a single full-resolution image; emits `finished(path, pixmap)`. Previous loader is cancelled before starting a new one.
- **`ThumbLoaderWorker(QThread)`** — Background thread that batch-loads thumbnails and emits `thumb_ready(path, small_pixmap, grid_pixmap)` signals.
- **`ThumbnailCache`** — LRU cache (OrderedDict, max 1080 entries) for thumbnail pixmaps. Two instances exist: `thumb_cache` (80px queue thumbnails) and `grid_thumb_cache` (400px grid thumbnails).
- **`QueueThumbnail(QLabel)`** — Bottom filmstrip thumbnail items with current/selected state rendering via QPainter.
- **`ClickableThumbnail(QLabel)`** — Grid view thumbnail items with click/double-click signals.
- **`DropZoneList(QListWidget)`** — Drag-and-drop zone for adding photo folders/files on the setup page.

### Data flow

1. Setup page collects source folders/files into `self.source_entries` (list of `{type, path}` dicts)
2. `_collect_all_photos()` scans sources (optionally recursive) into `self.all_photos`
3. Each round operates on `self.current_round_photos`; selections go into `self.selected_photos`
4. When a round ends, selected photos become the next round's input until count <= target
5. Session state is auto-saved to `~/.photo_curator_session.json` on every navigation action

### State variables on PhotoCurator

- `source_entries`: list of source folder/file dicts
- `all_photos` / `current_round_photos` / `selected_photos`: photo path lists
- `current_index`: position in current round
- `round_number` / `target_count`: curation progress
- `round_history`: list of `{round, input_count, selected_count}` for summary
- `current_view_mode`: `VIEW_SINGLE` (0) or `VIEW_GRID` (1)

### Thumbnail preloading strategy

`_preload_all_thumbs(center_idx)` loads all uncached thumbnails sorted by proximity to the current position (nearest first). The previous `ThumbLoaderWorker` is cancelled before starting a new one. Full-resolution images are loaded on-demand by `FullImageLoader` when navigating to a photo.

### Supported image formats

`{.jpg, .jpeg, .png, .bmp, .tiff, .tif, .webp, .heic, .heif}`