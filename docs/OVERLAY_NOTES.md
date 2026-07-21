# Stroke Overlay Research Notes

## What works

### Stage 1: Unannotated PDF copy-through
`convert.py` correctly copies unannotated PDFs from the sync15 blob store to a
rendered output directory with correct folder hierarchy and human-readable filenames.
51 PDFs rendered correctly. No issues.

### Stroke rendering (stroke-only, no background)
`rmc -f rm -t svg` then `cairosvg.svg2pdf()` produces correct stroke-only PDFs.
All strokes render. The warning "Some data has not been read" is benign on fully
synced files -- it was masking a sync problem earlier, not an rmc bug.

rmc exits with code 1 even on success when this warning fires. Check for output
file existence rather than exit code.

rmc does NOT support `/dev/stdout` as an output path -- it hangs. Always write to
a named temp file.

### Stage 2: Annotated PDF overlay (SOLVED -- v5)

The correct overlay transform is:

```python
CAIRO_SCALE = 72.0 / 96.0   # cairosvg maps SVG user units (CSS px) -> PDF pt

# Parse viewBox from rmc SVG output
vb_x, vb_y, vb_w, vb_h = parse_viewbox(svg_text)

# Convert SVG -> stroke PDF via cairosvg (applies CAIRO_SCALE = 0.75 uniformly)
stroke_pdf_bytes = cairosvg.svg2pdf(bytestring=svg_text.encode())

# Compute transform
s  = 4 / 3              # exact inverse of cairosvg's 0.75 factor
tx = bg_w / 2 + vb_x   # shift viewBox origin to PDF left edge
ty = bg_h - vb_h        # shift viewBox top to PDF top (accounting for y-flip)

# Merge using pypdf
out_page.merge_transformed_page(
    stroke_page,
    Transformation().scale(s, s).translate(tx, ty)
)
```

`Transformation().scale(s, s).translate(tx, ty)` means:
`out_x = in_x * s + tx`, `out_y = in_y * s + ty`

Verified on page 6 of '26 Bullet Journal at 3375x4500px render.
Residual error ~5-14px, consistent with stylus drawing precision. No further
correction is possible without sub-pixel ground-truth calibration marks.

## Coordinate system model (CONFIRMED)

### rmc SVG output
- Source: `/home/txoof/src/rm-render/.venv/lib/python3.10/site-packages/rmc/exporters/svg.py`
- `SCALE = 72.0 / 226` is used for stroke widths but NOT for point coordinates
- Point coordinates in the SVG are in **PDF point units**, centered at x=0
- x=0 is the horizontal center of the PDF; range is `-bg_w/2 .. +bg_w/2`
- y=0 is the **top** of the PDF; range is `0 .. bg_h` (y increases downward)
- viewBox is set to the **bounding box of strokes** -- not the full PDF dimensions
- For pages where strokes don't reach all edges, viewBox will be smaller than PDF

### cairosvg PDF output
- cairosvg treats SVG user units as CSS pixels (96 dpi) and converts to PDF pt (72 dpi)
- Applies a uniform scale of `72/96 = 0.75` to all coordinates
- Applies a y-flip (SVG y=0 top -> PDF y=stroke_pdf_h; SVG y=vb_h -> PDF y=0)
- Stroke PDF size: `vb_w * 0.75` x `vb_h * 0.75` pt
- The inverse scale to recover PDF pt from stroke PDF coords is `4/3`

### Background PDF
- BuJo test PDF: 1620 x 2160pt
- PDF coordinates: origin at **bottom-left**

### margins field in .content
- `margins: 125` is the left toolbar/margin width in screen pixels
- This affects where the PDF is displayed on screen (UI only)
- Stroke coordinates are recorded in PDF coordinate space, NOT screen coordinate space
- The margin does NOT need to be corrected in the overlay transform

### Transform derivation
Given:
- `vb_x` = viewBox x origin (negative, since x is centered at 0)
- `vb_h` = viewBox height (in SVG user units = PDF pt)
- `bg_w`, `bg_h` = background PDF dimensions in pt

The PDF left edge is at SVG x = `-bg_w/2`. After cairosvg:
  `stroke_x = (-bg_w/2 - vb_x) * 0.75`

We want `stroke_x * s + tx = 0` (PDF left -> bg left), so:
  `tx = -stroke_x * s = (bg_w/2 + vb_x) * 0.75 * (4/3) = bg_w/2 + vb_x`

The PDF top is at SVG y=0. After cairosvg y-flip:
  `stroke_y = vb_h * 0.75 = stroke_pdf_h`

We want `stroke_y * s + ty = bg_h` (PDF top -> bg top), so:
  `ty = bg_h - stroke_pdf_h * s = bg_h - vb_h * 0.75 * (4/3) = bg_h - vb_h`

## Test document

- Document: `'26 Bullet Journal`
- UUID: `e7292a79-1934-40ac-8840-52d3601ceb91`
- PDF blob: `4ae5d026e09b8e69d4dc3ae76db5fcfebaf2e72cb9646b0b00176e4212a8f244`
- PDF size: 1620 x 2160pt
- Test page: page 6 (0-indexed: page index 5)
- Page 6 rm blob: `0528a763b58b9ea1843b6cae38a24938863314361aecb0197ca2f60bf7236c2d`
- Page 6 annotations: diagonal corner-to-corner lines, box around "The Bullet" text,
  box around QR code
- Page 6 SVG cached at: `/tmp/p6_strokes.svg` (regenerate if needed)

## What did NOT work and why

### Screen-coordinate approach
All attempts to map rm coordinates through screen pixels failed. The rm coordinate
system is NOT in screen pixels. Strokes are recorded in PDF point space directly.
Trying to account for the 125px margin in the transform produced wildly wrong results.

### Scaling by stroke PDF dimensions (v3/v4)
`s = bg_w / stroke_pdf_w` almost works but fails because the viewBox aspect ratio
differs from the PDF aspect ratio when strokes don't reach all four PDF corners.
This produces a y overflow (stroke content runs ~26pt past the bottom of the PDF)
visible as systematic downward displacement at the bottom of the page.

### Large-scale transforms (s=4.83)
Attempts to map from screen canvas coordinates produced transforms with
s~4.83, tx~-2100, ty~-5700 which placed all strokes completely off-page.

## Key files

- Stage 1+2 converter: `/home/txoof/src/rm-render/convert.py` (stage 1 only so far)
- Overlay prototype: `/home/txoof/src/rm-render/overlay.py`
- Inspection script: `/home/txoof/src/rm-render/inspect_tree.py`
- Page order checker: `/home/txoof/src/rm-render/check_page_order.py`
- Test output: `/home/txoof/src/rm-render/test_output/`
- uv project: `/home/txoof/src/rm-render/`

## Sync / environment notes

- Sync15 blob store: `/home/txoof/remotehomes/remarkable/data/users/txoof/sync/`
- Tree index: `/home/txoof/remotehomes/remarkable/data/users/txoof/.tree`
- The tablet lost auth in mid-July 2026; re-pairing fixed sync
- Always use `sudo /home/txoof/.local/bin/uv run --project /home/txoof/src/rm-render`
  (sudo doesn't have uv in PATH)
- test output directory: `/home/txoof/src/rm-render/test_output/`