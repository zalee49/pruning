"""
Prune a directory of DICOM files before de-identification.

Reads DICOM headers only (stop_before_pixels=True) and excludes:
  - 3D/volumetric datasets (Enhanced US Volume SOP class, ImageType "3D"/"VOLUME",
    or RegionSpatialFormat >= 3 in SequenceOfUltrasoundRegions)
  - Rendered 3D volumes disguised as ordinary 2D clips: US Multi-frame SOP class,
    ImageType containing "DERIVED", and an absent/empty SequenceOfUltrasoundRegions
    (see 3D_RENDER_FILTER_PLAN.md). Pass --no-render-3d-filter to disable this rule.
  - Non-TEE probes identified via TransducerData (0018,5010):
      excluded: linear probes (L-series), epiaortic x7-2
      kept:     x8-2t, x7-2t
  - Clips with fewer than 32 source frames (NumberOfFrames < 32), too short for
    the classifier.

Approved clips that contain two side-by-side ultrasound panels (x-plane/biplane,
or color-compare) are split into two single-panel DICOMs based on
SequenceOfUltrasoundRegions (0018,6011); both halves are written to the output.
Pass --no-split to disable this and hard-link dual-pane clips whole instead.

Usage:
    python prune.py --input <DicomDir> --output <PrunedDir> [--unknown-probe-action exclude|include] [--no-split] [--no-render-3d-filter]
"""

import argparse
import copy
import json
import os
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

# SOPClassUID for Enhanced US Volume Storage (3D/4D volumes)
_ENHANCED_US_VOLUME_UID = "1.2.840.10008.5.1.4.1.1.6.2"

# SOPClassUID for ordinary (non-enhanced) US Multi-frame Image Storage — the
# SOP class Philips uses to export rendered 3D Zoom/MPR volumes disguised as
# 2D clips (see _is_rendered_3d).
_US_MULTIFRAME_UID = "1.2.840.10008.5.1.4.1.1.3.1"

# ImageType values that indicate a volumetric/reconstructed dataset
_VOLUME_IMAGE_TYPE_KEYWORDS = {"3D", "VOLUME"}

# RegionSpatialFormat codes >= 3 indicate volumetric region (3D, 3D+M-mode, etc.)
_VOLUMETRIC_SPATIAL_FORMAT_THRESHOLD = 3

# Minimum source frames a clip needs to be usable by the TEE view classifier.
# = N_FRAMES * SAMPLE_PERIOD in the View Classifier (src/data.py). Kept in sync
# manually with data.py and integration/stage_avis.py (MIN_FRAMES).
_MIN_FRAMES = 32

# JPEG-based transfer syntaxes where pydicom/Pillow automatically decodes to
# RGB during pixel_array even though PhotometricInterpretation still says YBR.
# Applying convert_color_space a second time produces a green tint. Same trap
# documented in export_excluded_avis.py and in the DICOM DEID repo's pipeline.py.
_JPEG_SYNTAXES = {
    "1.2.840.10008.1.2.4.50",  # JPEG Baseline (most Philips clips)
    "1.2.840.10008.1.2.4.51",  # JPEG Extended
}
_YBR_SPACES = {"YBR_FULL", "YBR_FULL_422", "YBR_PARTIAL_422", "YBR_PARTIAL_420"}

# RegionSpatialFormat: 1 = 2D region (candidate imaging panel)
_REGION_SPATIAL_FORMAT_2D = 1

# RegionDataType values that indicate an actual imaging panel (as opposed to a
# spectral/PW/CW Doppler trace, an ECG/physio strip, or an M-mode strip):
# 1 = Tissue, 2 = Color Flow.
_IMAGE_REGION_DATA_TYPES = {1, 2}

# A region must span at least this fraction of the frame's width and height to
# count as a real imaging panel rather than a small annotation/residual region.
_MIN_PANEL_FRACTION = 0.25

# Two regions are treated as belonging to the same panel if their x-ranges
# overlap by more than this many pixels (e.g. a Color Flow ROI drawn on top of
# a Tissue region in a single-pane color clip).
_PANEL_OVERLAP_TOLERANCE_PX = 10


def _is_3d(ds: pydicom.Dataset) -> bool:
    """Return True if any DICOM signal indicates a 3D/volumetric dataset."""
    # 1. SOP class check (most definitive)
    sop = ds.get((0x0008, 0x0016))
    if sop is not None and str(sop.value).strip() == _ENHANCED_US_VOLUME_UID:
        return True

    # 2. ImageType keyword check — pydicom 2.x always returns MultiValue for CS,
    #    but guard against an older single-string form just in case.
    image_type = ds.get((0x0008, 0x0008))
    if image_type is not None:
        val = image_type.value
        elements = [val] if isinstance(val, str) else val
        for element in elements:
            if str(element).upper() in _VOLUME_IMAGE_TYPE_KEYWORDS:
                return True

    # 3. RegionSpatialFormat inside SequenceOfUltrasoundRegions
    seq_elem = ds.get((0x0018, 0x6011))
    if seq_elem is not None:
        for item in seq_elem.value:
            try:
                fmt = int(item[(0x0018, 0x6012)].value)
                if fmt >= _VOLUMETRIC_SPATIAL_FORMAT_THRESHOLD:
                    return True
            except (KeyError, AttributeError, TypeError, ValueError):
                continue

    return False


def _is_rendered_3d(ds: pydicom.Dataset) -> bool:
    """Return True if ds is a 3D volume render disguised as a 2D US Multi-frame clip.

    Philips exports some 3D Zoom / MPR volume renders as ordinary US
    Multi-frame Image Storage with ImageType containing DERIVED and no
    SequenceOfUltrasoundRegions at all — a real scan-converted 2D acquisition
    always carries at least one ultrasound region. See 3D_RENDER_FILTER_PLAN.md.
    """
    sop = ds.get((0x0008, 0x0016))
    if sop is None or str(sop.value).strip() != _US_MULTIFRAME_UID:
        return False

    image_type = ds.get((0x0008, 0x0008))
    if image_type is None:
        return False
    val = image_type.value
    elements = [val] if isinstance(val, str) else val
    if not any(str(element).upper() == "DERIVED" for element in elements):
        return False

    seq_elem = ds.get((0x0018, 0x6011))
    return seq_elem is None or not seq_elem.value


class _Panel:
    """A group of x-overlapping ultrasound regions treated as one imaging panel."""

    __slots__ = ("x0", "x1", "items")

    def __init__(self, x0: int, x1: int, items: list):
        self.x0 = x0
        self.x1 = x1
        self.items = items


def _region_bounds(item) -> tuple[int, int, int, int] | None:
    """Return (x0, x1, y0, y1) for a SequenceOfUltrasoundRegions item, or None
    if any location tag is missing/unparseable."""
    try:
        x0 = int(item[(0x0018, 0x6018)].value)  # RegionLocationMinX0
        y0 = int(item[(0x0018, 0x601A)].value)  # RegionLocationMinY0
        x1 = int(item[(0x0018, 0x601C)].value)  # RegionLocationMaxX1
        y1 = int(item[(0x0018, 0x601E)].value)  # RegionLocationMaxY1
    except (KeyError, AttributeError, TypeError, ValueError):
        return None
    return x0, x1, y0, y1


def _detect_dual_pane(ds: pydicom.Dataset) -> list[_Panel] | None:
    """Return two left-to-right _Panel objects if ds contains exactly two
    horizontally disjoint imaging panels (x-plane/biplane or color-compare),
    else None.

    Regions are first filtered to genuine imaging panels (2D, Tissue or Color
    Flow, large enough to be a real panel), then grouped by x-overlap so that
    a single-pane color clip's overlapping Tissue + Color-Flow-ROI regions
    are treated as one panel, not two.
    """
    seq_elem = ds.get((0x0018, 0x6011))
    if seq_elem is None:
        return None

    columns = ds.get((0x0028, 0x0011))
    rows = ds.get((0x0028, 0x0010))
    if columns is None or rows is None:
        return None
    try:
        columns = int(columns.value)
        rows = int(rows.value)
    except (TypeError, ValueError):
        return None
    if columns <= 0 or rows <= 0:
        return None

    min_width = _MIN_PANEL_FRACTION * columns
    min_height = _MIN_PANEL_FRACTION * rows

    candidates = []
    for item in seq_elem.value:
        try:
            fmt = int(item[(0x0018, 0x6012)].value)  # RegionSpatialFormat
            data_type = int(item[(0x0018, 0x6014)].value)  # RegionDataType
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
        if fmt != _REGION_SPATIAL_FORMAT_2D or data_type not in _IMAGE_REGION_DATA_TYPES:
            continue
        bounds = _region_bounds(item)
        if bounds is None:
            continue
        x0, x1, y0, y1 = bounds
        if (x1 - x0) < min_width or (y1 - y0) < min_height:
            continue
        candidates.append((x0, x1, item))

    if len(candidates) < 2:
        return None

    candidates.sort(key=lambda c: c[0])

    panels: list[_Panel] = []
    for x0, x1, item in candidates:
        if panels and x0 <= panels[-1].x1 - _PANEL_OVERLAP_TOLERANCE_PX:
            panel = panels[-1]
            panel.x1 = max(panel.x1, x1)
            panel.items.append(item)
        else:
            panels.append(_Panel(x0, x1, [item]))

    if len(panels) != 2:
        return None

    return panels


def _frame_count(ds: pydicom.Dataset) -> int:
    """Source frame count from NumberOfFrames (0028,0008).

    Absent tag means a single-frame image (1 frame). Unparseable values are
    treated as 0 so the clip is excluded conservatively.
    """
    elem = ds.get((0x0028, 0x0008))
    if elem is None:
        return 1
    try:
        return int(elem.value)
    except (TypeError, ValueError):
        return 0


def _extract_probe_string(tag_elem: pydicom.dataelem.DataElement) -> str:
    """Extract a plain probe model string from a TransducerData DataElement.

    TransducerData (0018,5010) has VR LO. pydicom may return either a plain str
    or a MultiValue list when the field has multiple values. We take the first
    element to get the probe model string.
    """
    val = tag_elem.value
    if isinstance(val, str):
        return val.strip()
    # MultiValue or similar sequence — take first non-empty element
    try:
        for item in val:
            s = str(item).strip()
            if s:
                return s
    except TypeError:
        pass
    return str(val).strip()


def _classify_probe(ds: pydicom.Dataset, unknown_action: str) -> tuple[str, str]:
    """
    Return (action, reason) for the probe in this DICOM.

    action  : "keep" | "exclude"   (always one of these two — never "include")
    reason  : "probe_match" | "linear_probe" | "epiaortic_probe" | "unknown_probe"
    """
    tag_elem = ds.get((0x0018, 0x5010))
    if tag_elem is None:
        return ("keep" if unknown_action == "include" else "exclude"), "unknown_probe"

    raw = _extract_probe_string(tag_elem)
    if not raw:
        return ("keep" if unknown_action == "include" else "exclude"), "unknown_probe"

    # Normalize to lowercase with hyphens — Philips stores "X8_2t" or "X8-2t"
    # depending on system version; treat them identically.
    norm = raw.lower().replace("_", "-")

    # TEE probes to keep (check before any exclusion rule)
    if "x8-2t" in norm or "x7-2t" in norm:
        return "keep", "probe_match"

    # Epiaortic x7-2 (must come after x7-2t check — but the keep check above
    # already handles x7-2t, so any remaining "x7-2" hit here is the epiaortic)
    if re.search(r"x7-2(?!t)", norm):
        return "exclude", "epiaortic_probe"

    # Linear probes: L followed by a digit (e.g. l12-4, l15-7, l9-3, l4-12)
    if re.search(r"\bl\d", norm):
        return "exclude", "linear_probe"

    return ("keep" if unknown_action == "include" else "exclude"), "unknown_probe"


def _probe_string(ds: pydicom.Dataset) -> str | None:
    tag_elem = ds.get((0x0018, 0x5010))
    if tag_elem is None:
        return None
    raw = _extract_probe_string(tag_elem)
    return raw if raw else None


def _find_dicoms(input_dir: Path) -> list[Path]:
    """Walk input_dir recursively; return all files (assumed DICOM)."""
    paths = []

    def _on_error(exc: OSError) -> None:
        print(f"WARN: cannot read directory {exc.filename}: {exc.strerror}", file=sys.stderr)

    for dirpath, _, filenames in os.walk(input_dir, onerror=_on_error):
        for fname in filenames:
            paths.append(Path(dirpath) / fname)
    return sorted(paths)


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink src -> dst; fall back to copy if they're on different drives."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _decode_rgb_frames(ds: pydicom.Dataset) -> np.ndarray:
    """Decode ds.pixel_array to a normalized (N, H, W, C) uint8 array.

    C is 3 for color clips, 1 for grayscale. Applies the same JPEG-already-RGB
    guard as export_excluded_avis.py._write_avi to avoid a double YBR->RGB
    conversion (green tint) on JPEG-compressed transfer syntaxes.
    """
    pixels = ds.pixel_array  # (N,H,W,3) | (H,W,3) | (N,H,W) | (H,W)

    if pixels.ndim == 2:
        pixels = pixels[np.newaxis, :, :, np.newaxis]
    elif pixels.ndim == 3:
        if pixels.shape[-1] == 3:
            pixels = pixels[np.newaxis]
        else:
            pixels = pixels[:, :, :, np.newaxis]
    # else already (N, H, W, 3)

    if pixels.shape[-1] == 3:
        photometric = str(ds.get("PhotometricInterpretation", "RGB"))
        transfer_syntax = str(getattr(ds.file_meta, "TransferSyntaxUID", ""))
        already_rgb = transfer_syntax in _JPEG_SYNTAXES
        if not already_rgb and photometric in _YBR_SPACES:
            from pydicom.pixels import convert_color_space
            pixels = convert_color_space(pixels, photometric, "RGB")

    if pixels.dtype != np.uint8:
        pixels = pixels.astype(np.uint8)

    return np.ascontiguousarray(pixels)


def _rewrite_regions(half: pydicom.Dataset, panel: _Panel) -> None:
    """Replace SequenceOfUltrasoundRegions with only this panel's regions,
    with x-coordinates shifted to be relative to the crop origin (panel.x0)."""
    new_items = []
    for src_item in panel.items:
        item = copy.deepcopy(src_item)
        item[(0x0018, 0x6018)].value = int(item[(0x0018, 0x6018)].value) - panel.x0  # MinX0
        item[(0x0018, 0x601C)].value = int(item[(0x0018, 0x601C)].value) - panel.x0  # MaxX1
        new_items.append(item)
    half[(0x0018, 0x6011)].value = new_items


def _make_half_dataset(ds_full: pydicom.Dataset, frames: np.ndarray, panel: _Panel) -> pydicom.Dataset:
    """Build one split-panel DICOM dataset, cropped to panel.x0:panel.x1+1."""
    half = copy.deepcopy(ds_full)

    sub = np.ascontiguousarray(frames[:, :, panel.x0 : panel.x1 + 1, :])
    frame_count, height, width, channels = sub.shape

    half.Columns = width
    half.Rows = height
    half.NumberOfFrames = frame_count
    half.SamplesPerPixel = channels
    half.BitsAllocated = 8
    half.BitsStored = 8
    half.HighBit = 7
    half.PixelRepresentation = 0
    if channels == 3:
        half.PhotometricInterpretation = "RGB"
        half.PlanarConfiguration = 0
    else:
        half.PhotometricInterpretation = "MONOCHROME2"
        if "PlanarConfiguration" in half:
            del half.PlanarConfiguration

    half.PixelData = sub.tobytes()
    half["PixelData"].is_undefined_length = False

    _rewrite_regions(half, panel)

    new_uid = generate_uid()
    half.SOPInstanceUID = new_uid
    half.file_meta.MediaStorageSOPInstanceUID = new_uid
    half.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    return half


def _split_and_write(src_path: Path, panels: list[_Panel], output_dir: Path, rel: Path) -> list[str]:
    """Decode src_path, split into panels, write both halves under output_dir.

    Returns the two output relative paths (posix) in left-to-right order.
    Raises on any failure — caller is responsible for a fallback action.
    """
    ds_full = pydicom.dcmread(str(src_path))
    frames = _decode_rgb_frames(ds_full)

    outputs = []
    for panel, tag in zip(panels, ("L", "R")):
        half = _make_half_dataset(ds_full, frames, panel)
        out_rel = rel.parent / f"{rel.stem}__{tag}{rel.suffix}"
        dst = output_dir / out_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        half.save_as(dst, enforce_file_format=True)
        outputs.append(out_rel.as_posix())

    return outputs


def prune(
    input_dir: Path,
    output_dir: Path,
    unknown_action: str,
    split_enabled: bool = True,
    render_3d_filter_enabled: bool = True,
) -> int:
    """
    Scan input_dir, copy/link approved files to output_dir, write manifest.

    Approved clips with two side-by-side imaging panels are split into two
    single-panel DICOMs (unless split_enabled is False, in which case they are
    hard-linked whole like any other approved clip).

    Returns an exit code: 0 = files kept, 1 = all excluded, 2 = no input files.
    """
    # Resolve both paths so relative_to() works correctly even if --input
    # contains a symlink component (os.walk follows symlinks by default).
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()

    dicoms = _find_dicoms(input_dir)
    if not dicoms:
        print(f"ERROR: No files found under {input_dir}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)

    records = []
    counts = {
        "kept": 0,
        "split": 0,
        "excluded_3d": 0,
        "excluded_3d_rendered": 0,
        "excluded_probe": 0,
        "excluded_unknown": 0,
        "excluded_frames": 0,
        "excluded_unreadable": 0,
    }

    for path in dicoms:
        rel = path.relative_to(input_dir)

        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        except Exception as exc:
            print(f"WARN: could not read {rel.as_posix()} ({exc}); skipping", file=sys.stderr)
            records.append({
                "relative_path": rel.as_posix(),
                "action": "exclude",
                "probe": None,
                "is_3d": False,
                "n_frames": None,
                "reason": "unreadable",
            })
            counts["excluded_unreadable"] += 1
            continue

        probe = _probe_string(ds)
        is_3d = _is_3d(ds)
        n_frames = _frame_count(ds)

        if is_3d:
            action, reason = "exclude", "3d_volume"
            counts["excluded_3d"] += 1
        elif render_3d_filter_enabled and _is_rendered_3d(ds):
            action, reason = "exclude", "3d_rendered"
            counts["excluded_3d_rendered"] += 1
        else:
            action, reason = _classify_probe(ds, unknown_action)
            if action == "keep" and n_frames < _MIN_FRAMES:
                action, reason = "exclude", "too_few_frames"
                counts["excluded_frames"] += 1
            elif action != "keep":
                if reason == "unknown_probe":
                    counts["excluded_unknown"] += 1
                else:
                    counts["excluded_probe"] += 1

        record = {
            "relative_path": rel.as_posix(),
            "action": action,
            "probe": probe,
            "is_3d": is_3d,
            "n_frames": n_frames,
            "reason": reason,
        }

        if action == "keep":
            panels = _detect_dual_pane(ds) if split_enabled else None
            if panels is not None:
                try:
                    outputs = _split_and_write(path, panels, output_dir, rel)
                    record["action"] = "split"
                    record["reason"] = "dual_pane"
                    record["outputs"] = outputs
                    counts["split"] += 1
                except Exception as exc:
                    print(f"WARN: split failed for {rel.as_posix()} ({exc}); keeping whole", file=sys.stderr)
                    record["reason"] = "split_failed"
                    _link_or_copy(path, output_dir / rel)
                    counts["kept"] += 1
            else:
                _link_or_copy(path, output_dir / rel)
                counts["kept"] += 1

        records.append(record)

        # Warn on unknowns so the user can extend the probe rules
        if reason == "unknown_probe":
            label = "kept (unknown)" if action == "keep" else "excluded (unknown)"
            print(f"WARN: {rel.as_posix()} — TransducerData={probe!r} not recognised; {label}", file=sys.stderr)

    total = len(records)
    summary = {
        "total": total,
        "kept": counts["kept"],
        "split": counts["split"],
        "excluded_3d": counts["excluded_3d"],
        "excluded_3d_rendered": counts["excluded_3d_rendered"],
        "excluded_probe": counts["excluded_probe"],
        "excluded_unknown": counts["excluded_unknown"],
        "excluded_frames": counts["excluded_frames"],
        "excluded_unreadable": counts["excluded_unreadable"],
    }

    manifest = {"summary": summary, "records": records}
    manifest_path = output_dir / "pruning_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(
        f"Pruning complete: {counts['kept']}/{total} kept, {counts['split']} split "
        f"({counts['excluded_3d']} 3D, {counts['excluded_3d_rendered']} rendered-3D, "
        f"{counts['excluded_probe']} probe, "
        f"{counts['excluded_unknown']} unknown-probe, "
        f"{counts['excluded_frames']} too-few-frames excluded, "
        f"{counts['excluded_unreadable']} unreadable). "
        f"Manifest: {manifest_path}"
    )

    return 0 if (counts["kept"] + counts["split"]) > 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input",  required=True, type=Path, metavar="DIR", help="Raw DICOM input directory (walked recursively)")
    parser.add_argument("--output", required=True, type=Path, metavar="DIR", help="Output directory for approved DICOMs + pruning_manifest.json")
    parser.add_argument(
        "--unknown-probe-action",
        choices=["exclude", "include"],
        default="exclude",
        help="What to do when TransducerData is absent or unrecognised (default: exclude)",
    )
    parser.add_argument(
        "--no-split",
        action="store_true",
        help="Disable dual-pane splitting; dual-pane clips are hard-linked whole (default: split)",
    )
    parser.add_argument(
        "--no-render-3d-filter",
        action="store_true",
        help="Disable the DERIVED + no-ultrasound-regions rendered-3D exclusion (default: enabled)",
    )
    args = parser.parse_args()

    if not args.input.is_dir():
        print(f"ERROR: --input is not a directory: {args.input}", file=sys.stderr)
        sys.exit(2)

    sys.exit(prune(
        args.input,
        args.output,
        args.unknown_probe_action,
        split_enabled=not args.no_split,
        render_3d_filter_enabled=not args.no_render_3d_filter,
    ))


if __name__ == "__main__":
    main()
