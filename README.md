# rm-render

Renders reMarkable tablet documents synced via rmfakecloud to PDFs with correct
page ordering and annotation overlays, served via a local nginx web interface.

## Quick install

```bash
# Prerequisites
sudo apt install inotify-tools
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone anywhere and install
git clone https://github.com/txoof/rm-render.git ~/src/rm-render
cd ~/src/rm-render
./install.sh
```

`install.sh` will detect your rmfakecloud directory, deploy the nginx
configuration, install the systemd service, and run the first render.
After installation, documents are available at `http://<your-server>/`.

The repo can be cloned anywhere. `install.sh` installs `watch.sh` to
`~/.local/bin/rm-render-watch` so the service does not depend on the
repo location after installation.

## Prerequisites

- Ubuntu (tested on 26.04 LTS)
- Docker + Docker Compose with an existing rmfakecloud installation
- rmfakecloud configured with sync15 enabled (`sync15: true` in user profile)
- `inotify-tools` -- `sudo apt install inotify-tools`
- `uv` -- https://astral.sh/uv

## How it works

When the reMarkable tablet syncs to rmfakecloud, the sync15 blob store is
updated. rm-render watches for these changes via inotify, reads the blob
chain directly, and renders changed documents to PDF in `rendered/`. An
nginx container serves `rendered/` as a browseable file index on port 80.

Three document types are handled:

- **Unannotated PDF** -- original PDF blob copied directly
- **Annotated PDF** -- original PDF with `.rm` annotation strokes overlaid per page, links preserved
- **Notebook** -- `.rm` strokes rendered onto a blank canvas

## Updating

```bash
cd ~/src/rm-render
git pull
./install.sh
```

`install.sh` is idempotent -- it only updates files that have changed and
restarts services as needed.

## Uninstalling

```bash
cd ~/src/rm-render
./install.sh --uninstall
```

This will stop and remove the systemd service, remove the installed watch
script from `~/.local/bin/`, remove the nginx deploy files, and stop the
nginx container. You will be asked whether to delete the `rendered/`
directory and its PDFs. The repo itself is not removed.

## Version

```bash
./install.sh --version
```

## Converter options

```
rm-render <data-dir> <output-dir> [options]

  --only UUID       Process only this document UUID (for testing)
  --verbose, -v     Show per-page timing detail
  --max-pdf-mb N    Skip PDFs larger than N MB (default: 10)
  --workers N, -j N Parallel workers (default: half of CPU cores)
```

## Directory layout

```
<rmfakecloud-root>/
    data/
        users/<username>/
            sync/           # content-addressable blob store (read-only)
    rendered/               # written by rm-render, served by nginx
        Folder/
            Document.pdf
    docker-compose.yml      # existing rmfakecloud Compose file
    docker-compose.override.yml  # adds nginx (from deploy/)
    nginx.conf              # nginx config (from deploy/)
    index.html              # file browser UI (from deploy/)
```

```
<repo>/                     # clone anywhere
    install.sh              # installation and update script
    watch.sh                # inotify watcher, installed to ~/.local/bin/rm-render-watch
    rm-render@.service      # systemd unit template
    src/rm_render/
        convert.py          # document converter
    deploy/
        docker-compose.override.yml
        nginx.conf
        index.html
    docs/
        OVERLAY_NOTES.md    # coordinate system derivation notes
```

## Systemd service

rm-render uses a systemd template unit (`rm-render@.service`). The `@` means
it can be instantiated for any user. The instance name sets both the user the
service runs as and the home directory used to locate paths.

```bash
# Install and enable for the current user
sudo cp rm-render@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rm-render@$USER
sudo systemctl start rm-render@$USER
```

The service expects:
- `watch.sh` at `/home/<user>/src/rm-render/watch.sh`
- rmfakecloud data at `/home/<user>/remotehomes/remarkable/`

If your paths differ, edit the `ExecStart` line in the service file before
installing, or use a systemd drop-in override:

```bash
# Override paths without editing the service file
sudo systemctl edit rm-render@$USER
```

Add to the override:
```ini
[Service]
ExecStart=
ExecStart=/bin/bash /custom/path/to/watch.sh /custom/path/to/rmfakecloud
```

The `ExecStart=` blank line clears the original value before setting the new one.

## Logs and status

```bash
# Service status
systemctl status rm-render@$USER

# Live logs
journalctl -u rm-render@$USER -f

# Restart after config change
sudo systemctl restart rm-render@$USER
```

## Limitations

- Pen colors introduced in reMarkable firmware >= 3.x may not render if
  unsupported by `rmc` -- affected pages are skipped with a warning
- PDF internal links are preserved; annotation links are not rendered
- PDFs larger than `--max-pdf-mb` (default 10MB) are skipped
- epub files are not supported
- Write-back to the tablet is out of scope

## Coordinate system notes

See `docs/OVERLAY_NOTES.md` for a full derivation of the stroke overlay
transform. Short version: rmc outputs stroke coordinates in PDF point units
centered at x=0, cairosvg applies a 0.75 scale factor, and the correct
inverse transform is `s=4/3, tx=bg_w/2+vb_x, ty=bg_h-vb_h`.