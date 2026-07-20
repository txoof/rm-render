#!/usr/bin/env python3
"""
rm-render converter - Stage 1
Copies unannotated PDFs from the rmfakecloud sync15 blob store to the
rendered output directory with human-readable names and folder structure.

Read-only access to sync data. Writes only to output_dir.
"""

import json
import shutil
import sys
from pathlib import Path


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
    """Replace characters that are problematic in filenames."""
    for ch in '/\\:*?"<>|\n\r\t':
        name = name.replace(ch, '_')
    # Collapse multiple spaces/underscores introduced by replacements
    while '  ' in name:
        name = name.replace('  ', ' ')
    return name.strip()


def build_folder_map(docs, sync_dir):
    """Return {uuid: (visible_name, parent_uuid)} for all CollectionType entries."""
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
    """Walk parent chain to build full folder path. Returns a Path of folder segments."""
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
    result = PurePosixPath(*segments) if segments else PurePosixPath("")
    return result


def get_output_path(output_dir, folder_map, parent, name):
    """Resolve the output path for a document given its parent UUID."""
    from pathlib import Path
    clean_name = name[:-4] if name.lower().endswith(".pdf") else name
    safe_name = safe_filename(clean_name) + ".pdf"
    if parent and parent in folder_map:
        folder_path = resolve_folder_path(folder_map, parent)
        return output_dir / str(folder_path) / safe_name
    return output_dir / safe_name


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

    # Skip items in trash
    if parent == "trash":
        return None, "skipped (in trash)"

    # Skip macOS resource fork documents
    if name.startswith("._"):
        return None, "skipped (macOS resource fork)"

    cf = files_by_name.get(f"{uuid}.content")
    if not cf:
        return None, "no content file"

    content = load_json_blob(sync_dir, cf["Hash"])
    if not content:
        return None, "could not load content"

    file_type = content.get("fileType", "")

    # Stage 1: handle unannotated PDFs only
    if file_type != "pdf":
        return None, f"skipped (file_type={file_type}, not handled in stage 1)"

    cpages = content.get("cPages", {})
    raw_pages = cpages.get("pages", [])
    active_pages = [p for p in raw_pages if not (p.get("deleted", {}).get("value", 0) == 1)]
    annotated = sum(1 for p in active_pages if f"{uuid}/{p.get('id','')}.rm" in files_by_name)

    if annotated > 0:
        return None, f"skipped (annotated pdf, not handled in stage 1)"

    # Find the PDF blob
    pdf_entry = files_by_name.get(f"{uuid}.pdf")
    if not pdf_entry:
        return None, "no pdf blob found"

    pdf_blob = sync_dir / pdf_entry["Hash"]
    if not pdf_blob.exists():
        return None, "pdf blob missing from sync dir"

    # Verify PDF magic bytes -- skip macOS resource forks and other non-PDF blobs
    with open(pdf_blob, "rb") as f:
        header = f.read(4)
    if header != b"%PDF":
        return None, "blob is not a PDF (skipped)"

    out_path = get_output_path(output_dir, folder_map, parent, name)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Skip if output is already up to date
    if out_path.exists() and out_path.stat().st_mtime >= pdf_blob.stat().st_mtime:
        return out_path, "up to date, skipped"

    shutil.copyfile(pdf_blob, out_path)
    return out_path, "copied"


def main():
    if len(sys.argv) < 3:
        print("Usage: convert.py <data-dir> <output-dir>")
        print("  data-dir:   contains .tree and sync/ (read-only)")
        print("  output-dir: where rendered PDFs are written")
        sys.exit(1)

    data_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    sync_dir = data_dir / "sync"

    for path, label in [(data_dir / ".tree", ".tree"), (sync_dir, "sync/")]:
        if not path.exists():
            print(f"ERROR: {label} not found at {path}")
            sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    tree = json.load(open(data_dir / ".tree"))
    docs = tree.get("Docs", [])
    folder_map = build_folder_map(docs, sync_dir)

    print(f"Documents: {len(docs)}  Folders: {len(folder_map)}")
    print(f"Output:    {output_dir}")
    print()

    counts = {"copied": 0, "up to date, skipped": 0, "skipped": 0, "error": 0}

    for doc in sorted(docs, key=lambda d: next(
        (f["EntryName"] for f in d["Files"] if f["EntryName"].endswith(".metadata")), ""
    )):
        out_path, status = process_document(doc, sync_dir, output_dir, folder_map)
        name = next(
            (f["EntryName"].replace(".metadata","") for f in doc["Files"]
             if f["EntryName"].endswith(".metadata")), "unknown"
        )

        if status == "copied":
            print(f"  OK  {out_path.relative_to(output_dir)}")
            counts["copied"] += 1
        elif status == "up to date, skipped":
            counts["up to date, skipped"] += 1
        elif "skipped" in status or status == "folder, skipped":
            counts["skipped"] += 1
        else:
            print(f"  ERR {name}: {status}")
            counts["error"] += 1

    print()
    print(f"Copied:       {counts['copied']}")
    print(f"Up to date:   {counts['up to date, skipped']}")
    print(f"Skipped:      {counts['skipped']}")
    print(f"Errors:       {counts['error']}")


if __name__ == "__main__":
    main()