#!/usr/bin/env python3
"""
rm-render converter - Stage 1 + 2
Renders reMarkable documents from the rmfakecloud sync15 blob store to PDF.

Stage 1: Unannotated PDFs -- copy through unchanged.
Stage 2: Annotated PDFs -- overlay .rm strokes onto each PDF page.
Stage 3: Pure .rm notebooks -- render strokes onto a blank canvas.

Read-only access to sync data. Writes only to output_dir.
"""

import io
import json
import re
import shutil
import sys
from pathlib import Path

import logging
import warnings

# Suppress benign warnings from rmscene and pypdf
logging.getLogger("rmscene").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="Some data has not been read")

import cairosvg
import pypdf
from pypdf import Transformation
from pypdf.errors import PdfReadWarning
warnings.filterwarnings("ignore", category=PdfReadWarning)
from rmscene import read_tree
from rmc.exporters.svg import tree_to_svg, PAGE_WIDTH_PT, PAGE_HEIGHT_PT


# cairosvg scales SVG user units (treated as CSS px at 96dpi) to PDF pt (72dpi)
CAIRO_SCALE = 72.0 / 96.0

VERBOSE = False        # set to True via --verbose flag
MAX_PDF_BYTES = 10 * 1024 * 1024  # skip PDFs larger than this; set via --max-pdf-mb


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------

def rm_blob_to_svg(rm_path: Path, timeout: int = 30) -> str:
    """Convert a .rm blob to SVG text using the rmc Python API.
    Raises TimeoutError if conversion takes longer than timeout seconds."""
    import concurrent.futures
    def _convert():
        with open(rm_path, "rb") as f:
            tree = read_tree(f)
        out = io.StringIO()
        tree_to_svg(tree, out)
        return out.getvalue()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_convert)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"rmc timed out after {timeout}s on {rm_path.name}")


def parse_viewbox(svg_text: str):
    """Return (vb_x, vb_y, vb_w, vb_h) from an SVG viewBox attribute."""
    m = re.search(r'viewBox=["\']([\d.\-e]+ [\d.\-e]+ [\d.\-e]+ [\d.\-e]+)', svg_text)
    if not m:
        raise ValueError("No viewBox found in SVG")
    return tuple(map(float, m.group(1).split()))


def compute_overlay_transform(vb_x: float, vb_h: float,
                              bg_w: float, bg_h: float):
    """
    Return (s, tx, ty) for Transformation().scale(s, s).translate(tx, ty).

    rmc outputs stroke coordinates in PDF point units, centered at x=0:
        x range: -bg_w/2 .. +bg_w/2
        y range: 0 (top) .. bg_h (bottom)

    cairosvg applies a 0.75 scale and y-flip. The inverse scale is 4/3.
    The formulas simplify to:
        s  = 4/3
        tx = bg_w/2 + vb_x
        ty = bg_h - vb_h
    """
    s  = 4.0 / 3.0
    tx = bg_w / 2.0 + vb_x
    ty = bg_h - vb_h
    return s, tx, ty


def overlay_stroke_onto_page(bg_page: pypdf.PageObject,
                              rm_path: Path) -> tuple:
    """
    Overlay strokes from rm_path onto bg_page.
    Returns (bg_page, None) on success, (bg_page, error_str) if rmc fails.
    The page is returned unmodified on failure.
    """
    try:
        svg_text = rm_blob_to_svg(rm_path)
        vb_x, _vb_y, _vb_w, vb_h = parse_viewbox(svg_text)
        stroke_pdf_bytes = cairosvg.svg2pdf(bytestring=svg_text.encode())
        bg_w = float(bg_page.mediabox.width)
        bg_h = float(bg_page.mediabox.height)
        s, tx, ty = compute_overlay_transform(vb_x, vb_h, bg_w, bg_h)
        stroke_reader = pypdf.PdfReader(io.BytesIO(stroke_pdf_bytes))
        stroke_page = stroke_reader.pages[0]
        bg_page.merge_transformed_page(
            stroke_page,
            Transformation().scale(s, s).translate(tx, ty)
        )
        return bg_page, None
    except Exception as e:
        return bg_page, str(e)


def make_blank_page(width_pt: float = PAGE_WIDTH_PT,
                    height_pt: float = PAGE_HEIGHT_PT) -> pypdf.PageObject:
    """Create a blank white PDF page of the given size."""
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=width_pt, height=height_pt)
    return writer.pages[0]


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------

def output_is_fresh(out_path: Path, source_mtimes) -> bool:
    """Return True if out_path exists and is newer than all source mtimes."""
    if not out_path.exists():
        return False
    out_mtime = out_path.stat().st_mtime
    return all(out_mtime >= mtime for mtime in source_mtimes)


# ---------------------------------------------------------------------------
# Path helpers (unchanged from stage 1)
# ---------------------------------------------------------------------------

def fractional_index_sort_key(idx_str):
    return tuple(ord(c) - ord('a') for c in idx_str)


def load_json_blob(sync_dir, hash_val):
    path = sync_dir / hash_val
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, PermissionError):
        return None


def safe_filename(name):
    for ch in '/\\:*?"<>|\n\r\t':
        name = name.replace(ch, '_')
    while '  ' in name:
        name = name.replace('  ', ' ')
    return name.strip()


def build_folder_map(docs, sync_dir):
    folders = {}
    for doc in docs:
        mf = next((f for f in doc["Files"] if f["EntryName"].endswith(".metadata")), None)
        if not mf:
            continue
        meta = load_json_blob(sync_dir, mf["Hash"])
        if not meta:
            continue
        if meta.get("type") == "CollectionType":
            uuid = mf["EntryName"].replace(".metadata", "")
            folders[uuid] = (meta.get("visibleName", uuid), meta.get("parent", ""))
    return folders


def resolve_folder_path(folder_map, parent_uuid, max_depth=10):
    from pathlib import PurePosixPath
    segments = []
    current = parent_uuid
    depth = 0
    while current and current in folder_map and current != "trash" and depth < max_depth:
        name, parent = folder_map[current]
        segments.append(safe_filename(name))
        current = parent
        depth += 1
    segments.reverse()
    return PurePosixPath(*segments) if segments else PurePosixPath("")


def get_output_path(output_dir, folder_map, parent, name):
    clean_name = name[:-4] if name.lower().endswith(".pdf") else name
    safe_name = safe_filename(clean_name) + ".pdf"
    if parent and parent in folder_map:
        folder_path = resolve_folder_path(folder_map, parent)
        return output_dir / str(folder_path) / safe_name
    return output_dir / safe_name


# ---------------------------------------------------------------------------
# Document processing
# ---------------------------------------------------------------------------

def process_document(doc, sync_dir, output_dir, folder_map):
    files_by_name = {f["EntryName"]: f for f in doc["Files"]}

    mf = next((f for f in doc["Files"] if f["EntryName"].endswith(".metadata")), None)
    if not mf:
        return None, "no metadata file"

    uuid = mf["EntryName"].replace(".metadata", "")
    meta = load_json_blob(sync_dir, mf["Hash"])
    if not meta:
        return None, "could not load metadata"

    if meta.get("type") == "CollectionType":
        return None, "folder, skipped"

    name = meta.get("visibleName", uuid)
    parent = meta.get("parent", "")

    if parent == "trash":
        return None, "skipped (in trash)"

    if name.startswith("._"):
        return None, "skipped (macOS resource fork)"

    cf = files_by_name.get(f"{uuid}.content")
    if not cf:
        return None, "no content file"

    content = load_json_blob(sync_dir, cf["Hash"])
    if not content:
        return None, "could not load content"

    file_type = content.get("fileType", "")

    # Resolve active pages in display order
    cpages = content.get("cPages", {})
    raw_pages = cpages.get("pages", [])
    active_pages = [p for p in raw_pages
                    if not (p.get("deleted", {}).get("value", 0) == 1)]
    active_pages.sort(key=lambda p: fractional_index_sort_key(
        p.get("idx", {}).get("value", "")))

    out_path = get_output_path(output_dir, folder_map, parent, name)

    # --- Stage 3: pure .rm notebook (no PDF background) ---
    if file_type == "notebook" or (file_type == "" and not files_by_name.get(f"{uuid}.pdf")):
        rm_blobs = []
        for page in active_pages:
            page_id = page.get("id", "")
            rm_key = f"{uuid}/{page_id}.rm"
            rm_entry = files_by_name.get(rm_key)
            if rm_entry:
                rm_blobs.append(sync_dir / rm_entry["Hash"])

        if not rm_blobs:
            return None, "skipped (notebook with no .rm pages)"

        total_rm_size = sum(b.stat().st_size for b in rm_blobs if b.exists())
        if total_rm_size > MAX_PDF_BYTES:
            return None, f"skipped (notebook too large: {total_rm_size/1024/1024:.1f}MB > {MAX_PDF_BYTES/1024/1024:.0f}MB)"

        source_mtimes = [b.stat().st_mtime for b in rm_blobs if b.exists()]
        if output_is_fresh(out_path, source_mtimes):
            return out_path, "up to date, skipped"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        writer = pypdf.PdfWriter()
        page_errors = []
        for rm_blob in rm_blobs:
            if not rm_blob.exists():
                continue
            blank = make_blank_page()
            _, err = overlay_stroke_onto_page(blank, rm_blob)
            if err:
                page_errors.append(f"{rm_blob.name}: {err}")
            writer.add_page(blank)

        if page_errors:
            print(f"  WARN: {len(page_errors)} page(s) failed overlay: {page_errors[0]}", flush=True)

        with open(out_path, "wb") as f:
            writer.write(f)
        return out_path, "rendered (notebook)"

    # --- Stage 1 + 2: PDF document ---
    if file_type != "pdf":
        return None, f"skipped (file_type={file_type!r})"

    pdf_entry = files_by_name.get(f"{uuid}.pdf")
    if not pdf_entry:
        return None, "no pdf blob found"

    pdf_blob = sync_dir / pdf_entry["Hash"]
    if not pdf_blob.exists():
        return None, "pdf blob missing from sync dir"

    with open(pdf_blob, "rb") as f:
        if f.read(4) != b"%PDF":
            return None, "blob is not a PDF (skipped)"

    pdf_size = pdf_blob.stat().st_size
    if pdf_size > MAX_PDF_BYTES:
        return None, f"skipped (PDF too large: {pdf_size/1024/1024:.1f}MB > {MAX_PDF_BYTES/1024/1024:.0f}MB)"

    # Find which pages have .rm annotations
    annotated_page_ids = {}
    for page in active_pages:
        page_id = page.get("id", "")
        rm_key = f"{uuid}/{page_id}.rm"
        rm_entry = files_by_name.get(rm_key)
        if rm_entry:
            annotated_page_ids[page_id] = sync_dir / rm_entry["Hash"]

    # Stage 1: no annotations -- copy through
    if not annotated_page_ids:
        source_mtimes = [pdf_blob.stat().st_mtime]
        if output_is_fresh(out_path, source_mtimes):
            return out_path, "up to date, skipped"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf_blob, out_path)
        return out_path, "copied"

    # Stage 2: has annotations -- build overlaid PDF
    rm_mtimes = [b.stat().st_mtime for b in annotated_page_ids.values() if b.exists()]
    source_mtimes = [pdf_blob.stat().st_mtime] + rm_mtimes
    if output_is_fresh(out_path, source_mtimes):
        return out_path, "up to date, skipped"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    import time as _time
    _log = lambda msg: print(f"    {msg}", flush=True) if VERBOSE else None

    _t = _time.time()
    writer = pypdf.PdfWriter(str(pdf_blob), incremental=True)
    _log(f"PdfWriter init: {_time.time()-_t:.2f}s  ({len(writer.pages)} pages, {len(annotated_page_ids)} annotated)")

    # Map PDF page index (redir value) to active page record
    redir_to_page = {}
    for page in active_pages:
        redir_val = page.get("redir", {})
        if isinstance(redir_val, dict):
            idx = redir_val.get("value")
        else:
            idx = redir_val
        if idx is not None:
            redir_to_page[int(idx)] = page

    n_overlaid = 0
    for pdf_idx in range(len(writer.pages)):
        page_record = redir_to_page.get(pdf_idx)
        if page_record is None:
            continue
        page_id = page_record.get("id", "")
        rm_blob = annotated_page_ids.get(page_id)
        if rm_blob and rm_blob.exists():
            _tp = _time.time()
            _, err = overlay_stroke_onto_page(writer.pages[pdf_idx], rm_blob)
            if err:
                _log(f"  page {pdf_idx}: WARN skipped overlay: {err}")
                print(f"  WARN page {pdf_idx}: {err}", flush=True)
            else:
                n_overlaid += 1
                _log(f"  page {pdf_idx}: overlay {_time.time()-_tp:.2f}s")

    _t = _time.time()
    writer.write(str(out_path))
    _log(f"write: {_time.time()-_t:.2f}s  ({n_overlaid} pages overlaid)")
    return out_path, "overlaid"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Render reMarkable documents to PDF")
    parser.add_argument("data_dir",   help="Path containing .tree and sync/ (read-only)")
    parser.add_argument("output_dir", help="Where rendered PDFs are written")
    parser.add_argument("--only",     help="Process only this UUID (for testing)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-page timing")
    parser.add_argument("--max-pdf-mb", type=float, default=10.0,
                        help="Skip PDFs larger than this size in MB (default: 10)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    sync_dir = data_dir / "sync"

    for path, label in [(data_dir / ".tree", ".tree"), (sync_dir, "sync/")]:
        if not path.exists():
            print(f"ERROR: {label} not found at {path}")
            sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    global VERBOSE, MAX_PDF_BYTES
    VERBOSE = args.verbose
    MAX_PDF_BYTES = int(args.max_pdf_mb * 1024 * 1024)

    tree = json.load(open(data_dir / ".tree"))
    docs = tree.get("Docs", [])
    folder_map = build_folder_map(docs, sync_dir)

    print(f"Documents: {len(docs)}  Folders: {len(folder_map)}")
    print(f"Output:    {output_dir}")
    print()

    counts = {"copied": 0, "overlaid": 0, "rendered (notebook)": 0,
              "up to date, skipped": 0, "skipped": 0, "error": 0}

    if args.only:
        docs = [d for d in docs if any(
            f["EntryName"] == f"{args.only}.metadata" for f in d["Files"]
        )]
        if not docs:
            print(f"ERROR: UUID {args.only} not found in .tree")
            sys.exit(1)

    import time
    n_total = len(sorted_docs := sorted(docs, key=lambda d: next(
        (f["EntryName"] for f in d["Files"] if f["EntryName"].endswith(".metadata")), ""
    )))

    for i, doc in enumerate(sorted_docs, 1):
        uuid = next(
            (f["EntryName"].replace(".metadata", "") for f in doc["Files"]
             if f["EntryName"].endswith(".metadata")), "unknown"
        )
        mf = next((f for f in doc["Files"] if f["EntryName"].endswith(".metadata")), None)
        if mf:
            meta = load_json_blob(sync_dir, mf["Hash"])
            name = meta.get("visibleName", uuid) if meta else uuid
            parent = meta.get("parent", "") if meta else ""
        else:
            name = uuid
            parent = ""

        folder_path = resolve_folder_path(folder_map, parent)
        display_path = str(folder_path / name) if str(folder_path) else name

        print(f"[{i}/{n_total}] {display_path}", flush=True)
        t0 = time.time()
        try:
            out_path, status = process_document(doc, sync_dir, output_dir, folder_map)
        except Exception as e:
            import traceback
            elapsed = time.time() - t0
            print(f"  ERR ({elapsed:.1f}s): {e}")
            print(traceback.format_exc())
            counts["error"] += 1
            continue
        elapsed = time.time() - t0

        if status in ("copied", "overlaid", "rendered (notebook)"):
            print(f"  OK  [{status}] ({elapsed:.1f}s)  {out_path.relative_to(output_dir)}")
            counts[status] += 1
        elif status == "up to date, skipped":
            print(f"  -- up to date ({elapsed:.1f}s)")
            counts["up to date, skipped"] += 1
        elif "skipped" in status or status == "folder, skipped":
            print(f"  -- skipped: {status}")
            counts["skipped"] += 1
        else:
            print(f"  ERR ({elapsed:.1f}s): {status}")
            counts["error"] += 1

    print()
    print(f"Copied:       {counts['copied']}")
    print(f"Overlaid:     {counts['overlaid']}")
    print(f"Notebooks:    {counts['rendered (notebook)']}")
    print(f"Up to date:   {counts['up to date, skipped']}")
    print(f"Skipped:      {counts['skipped']}")
    print(f"Errors:       {counts['error']}")


if __name__ == "__main__":
    main()