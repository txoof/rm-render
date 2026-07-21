# rm-render

Converts reMarkable tablet documents synced via rmfakecloud (sync15) to
correctly-ordered PDFs, served via a local nginx web interface.

## Problem

rmfakecloud's web UI displays document pages in edit order rather than
page order. This project renders documents to PDF with correct page
ordering and serves them via a minimal web interface accessible over VPN.

## Design principles

- Read-only access to rmfakecloud sync data -- originals are never modified
- Minimal additional services -- one nginx container added to existing Compose stack
- Low overhead file watching via inotify -- no polling, no cron
- Simple update path -- no custom Docker images, no compiled code

## Requirements

### Host system

- Ubuntu (tested on 26.04 LTS)
- Docker + Docker Compose (existing rmfakecloud installation)
- `inotify-tools` (`sudo apt install inotify-tools`)
- `uv` (https://astral.sh/uv)

### Python dependencies (managed by uv)

- `rmc` >= 0.3.0 -- converts v6 .rm files to SVG
- `pypdf` >= 6.14.2 -- PDF page assembly and annotation overlay
- `cairosvg` >= 2.9.0 -- SVG to PDF conversion
- `rmscene` >= 0.6.1 -- reads reMarkable .rm scene format

### Existing infrastructure

- rmfakecloud running via Docker Compose with sync15 enabled
- Sync data at a known path (default: `/home/txoof/remotehomes/remarkable/data`)
- User profile with `sync15: true`

## Directory layout

```
/home/txoof/remotehomes/remarkable/
    data/                        # existing rmfakecloud data, never modified
        users/txoof/
            .tree                # sync15 document index (watched by inotifywait)
            .userprofile
            sync/                # content-addressable blob store
    rendered/                    # written by rm-render, served by nginx
        Folder Name/
            Document Name.pdf
        Document Name.pdf
    docker-compose.yml           # existing rmfakecloud Compose file
    docker-compose.override.yml  # adds nginx service (from deploy/)
    nginx.conf                   # nginx config (from deploy/)
```

```
/home/txoof/src/rm-render/       # this repository
    README.md
    pyproject.toml               # uv project file
    src/
        rm_render/
            __init__.py
            convert.py           # main converter script
    deploy/
        docker-compose.override.yml  # copy alongside rmfakecloud docker-compose.yml
        nginx.conf                   # copy alongside rmfakecloud docker-compose.yml
    docs/
        OVERLAY_NOTES.md         # coordinate system research notes
    watch.sh                     # inotifywait watcher (started by systemd) [planned]
    rm-render.service            # systemd unit file [planned]
    install.sh                   # installation script [planned]
```

## Components

### convert.py

Reads `.tree`, resolves blob hashes, assembles PDFs in correct page order,
writes to `rendered/`. Skips documents that have not changed since last run.
Never reads from or writes to `data/`.

Handles three document types:

- **Pure notebook** -- pages rendered from `.rm` via `rmc`, assembled in
  fractional index order
- **Annotated PDF** -- original PDF reloaded incrementally, `.rm` annotation
  layer stamped on top via `rmc` where present, links preserved
- **Unannotated PDF** -- original PDF blob copied directly, no conversion

Usage:

```bash
uv run python -m rm_render.convert <data-dir> <output-dir> [options]

Options:
  --only UUID       Process only this document UUID (for testing)
  --verbose, -v     Show per-page timing detail
  --max-pdf-mb N    Skip PDFs larger than N MB (default: 10)
  --workers N, -j N Parallel workers (default: half of CPU cores)
```

### deploy/docker-compose.override.yml

Copy to the same directory as the rmfakecloud `docker-compose.yml`. Docker
Compose automatically merges it. Adds an nginx container serving `rendered/`
on port 80. Updating rmfakecloud's `docker-compose.yml` does not affect this
override file.

### watch.sh (planned)

Wraps `inotifywait` in a loop watching `.tree` for changes. On change,
calls `rm-render`. Blocks with zero CPU when idle.

### rm-render.service (planned)

systemd unit that starts `watch.sh` on boot, restarts on failure.

### install.sh (planned)

Installation script that sets up the full stack from scratch.

## Manual installation

```bash
# Install Python dependencies
cd /home/txoof/src/rm-render
uv sync

# Create rendered output directory
mkdir -p /home/txoof/remotehomes/remarkable/rendered

# Copy deploy files
cp deploy/docker-compose.override.yml /home/txoof/remotehomes/remarkable/
cp deploy/nginx.conf /home/txoof/remotehomes/remarkable/

# Start nginx
cd /home/txoof/remotehomes/remarkable
docker compose up -d rendered

# Run converter
uv run rm-render \
    /home/txoof/remotehomes/remarkable/data/users/txoof \
    /home/txoof/remotehomes/remarkable/rendered/
```

## Updating

```bash
cd /home/txoof/src/rm-render
git pull
uv sync
```

No Docker image rebuilds required.

## Page ordering

reMarkable sync15 stores page order as fractional index strings (e.g. `ba`,
`bab`, `bc`) in a CRDT log. Pages are sorted by these strings to recover
correct display order. For annotated PDFs, each page also carries a `redir`
value pointing to the original PDF page index used as background.

Pages marked `deleted: 1` are excluded.

## Limitations

- `.rm` annotation rendering via `rmc` does not support all pen colors
  introduced in firmware >= 3.x (unknown color IDs are skipped with a warning)
- Internal PDF links from the original PDF are preserved; links added as
  reMarkable annotations are not rendered
- PDFs larger than `--max-pdf-mb` (default 10MB) are skipped
- Write-back to the tablet is out of scope for this tool

## Coordinate system notes

See `docs/OVERLAY_NOTES.md` for a full derivation of the stroke overlay
transform. Short version: rmc outputs stroke coordinates in PDF point units
centered at x=0, cairosvg applies a 0.75 scale factor, and the correct
inverse transform is `s=4/3, tx=bg_w/2+vb_x, ty=bg_h-vb_h`.