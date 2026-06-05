"""
Prune a directory of DICOM files before de-identification.

Reads DICOM headers only (stop_before_pixels=True) and excludes:
  - 3D/volumetric datasets (Enhanced US Volume SOP class, ImageType "3D"/"VOLUME",
    or RegionSpatialFormat >= 3 in SequenceOfUltrasoundRegions)
  - Non-TEE probes identified via TransducerData (0018,5010):
      excluded: linear probes (L-series), epiaortic x7-2
      kept:     x8-2t, x7-2t

Usage:
    python prune.py --input <DicomDir> --output <PrunedDir> [--unknown-probe-action exclude|include]
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pydicom

# SOPClassUID for Enhanced US Volume Storage (3D/4D volumes)
_ENHANCED_US_VOLUME_UID = "1.2.840.10008.5.1.4.1.1.6.2"

# ImageType values that indicate a volumetric/reconstructed dataset
_VOLUME_IMAGE_TYPE_KEYWORDS = {"3D", "VOLUME"}

# RegionSpatialFormat codes >= 3 indicate volumetric region (3D, 3D+M-mode, etc.)
_VOLUMETRIC_SPATIAL_FORMAT_THRESHOLD = 3


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


def prune(input_dir: Path, output_dir: Path, unknown_action: str) -> int:
    """
    Scan input_dir, copy/link approved files to output_dir, write manifest.

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
        "excluded_3d": 0,
        "excluded_probe": 0,
        "excluded_unknown": 0,
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
                "reason": "unreadable",
            })
            counts["excluded_unreadable"] += 1
            continue

        probe = _probe_string(ds)
        is_3d = _is_3d(ds)

        if is_3d:
            action, reason = "exclude", "3d_volume"
            counts["excluded_3d"] += 1
        else:
            action, reason = _classify_probe(ds, unknown_action)
            if action == "keep":
                counts["kept"] += 1
            elif reason == "unknown_probe":
                counts["excluded_unknown"] += 1
            else:
                counts["excluded_probe"] += 1

        records.append({
            "relative_path": rel.as_posix(),
            "action": action,
            "probe": probe,
            "is_3d": is_3d,
            "reason": reason,
        })

        if action == "keep":
            _link_or_copy(path, output_dir / rel)

        # Warn on unknowns so the user can extend the probe rules
        if reason == "unknown_probe":
            label = "kept (unknown)" if action == "keep" else "excluded (unknown)"
            print(f"WARN: {rel.as_posix()} — TransducerData={probe!r} not recognised; {label}", file=sys.stderr)

    total = len(records)
    summary = {
        "total": total,
        "kept": counts["kept"],
        "excluded_3d": counts["excluded_3d"],
        "excluded_probe": counts["excluded_probe"],
        "excluded_unknown": counts["excluded_unknown"],
        "excluded_unreadable": counts["excluded_unreadable"],
    }

    manifest = {"summary": summary, "records": records}
    manifest_path = output_dir / "pruning_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(
        f"Pruning complete: {counts['kept']}/{total} kept "
        f"({counts['excluded_3d']} 3D, {counts['excluded_probe']} probe, "
        f"{counts['excluded_unknown']} unknown-probe excluded, "
        f"{counts['excluded_unreadable']} unreadable). "
        f"Manifest: {manifest_path}"
    )

    return 0 if counts["kept"] > 0 else 1


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
    args = parser.parse_args()

    if not args.input.is_dir():
        print(f"ERROR: --input is not a directory: {args.input}", file=sys.stderr)
        sys.exit(2)

    sys.exit(prune(args.input, args.output, args.unknown_probe_action))


if __name__ == "__main__":
    main()
