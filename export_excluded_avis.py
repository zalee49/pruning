"""
Convert all probe-excluded and unknown-probe DICOM clips from a pruning manifest
to AVI files for visual inspection. Also supports rendering dual-pane split
outputs (action == "split") so both halves can be eyeballed for correct
cropping and color.

PHI WARNING: output AVIs are raw pixels with no banner redaction — for
inspection only, not for distribution.

Usage:
    python export_excluded_avis.py --manifest <pruning_manifest.json>
                                   --input    <original DICOM root>
                                   --output   <folder to write AVIs>
                                   [--pruned-output <PrunedDir> ]  # to also render split halves
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pydicom

# JPEG-based transfer syntaxes where pydicom/Pillow automatically decodes to
# RGB during pixel_array even though PhotometricInterpretation still says YBR.
# Applying convert_color_space a second time produces the green tint.
_JPEG_SYNTAXES = {
    "1.2.840.10008.1.2.4.50",  # JPEG Baseline (most Philips clips)
    "1.2.840.10008.1.2.4.51",  # JPEG Extended
}
_YBR_SPACES = {"YBR_FULL", "YBR_FULL_422", "YBR_PARTIAL_422", "YBR_PARTIAL_420"}

INSPECT_REASONS = {"linear_probe", "epiaortic_probe", "unknown_probe"}


def _write_avi(ds: pydicom.Dataset, dst: Path) -> int:
    """Decode ds.pixel_array and write an MJPG AVI, handling raw Philips DICOMs correctly."""
    pixels = ds.pixel_array  # (N,H,W,3) | (H,W,3) | (N,H,W) | (H,W)

    # Normalize to (N, H, W, 3)
    if pixels.ndim == 2:
        pixels = np.stack([pixels] * 3, axis=-1)[np.newaxis]
    elif pixels.ndim == 3:
        if pixels.shape[-1] == 3:
            pixels = pixels[np.newaxis]
        else:
            pixels = np.stack([pixels] * 3, axis=-1)
    # else already (N, H, W, 3)

    n_frames, h, w, _ = pixels.shape

    photometric = str(ds.get("PhotometricInterpretation", "RGB"))
    transfer_syntax = str(getattr(ds.file_meta, "TransferSyntaxUID", ""))

    # For JPEG-compressed DICOMs, pydicom/Pillow decodes to RGB internally.
    # The PhotometricInterpretation tag still says YBR, but a second conversion
    # would produce wrong colors. Skip it when the transfer syntax tells us
    # decompression already happened.
    already_rgb = transfer_syntax in _JPEG_SYNTAXES
    if not already_rgb and photometric in _YBR_SPACES:
        from pydicom.pixels import convert_color_space
        pixels = convert_color_space(pixels, photometric, "RGB")

    fps = float(ds.get("CineRate", 0) or ds.get("RecommendedDisplayFrameRate", 0) or 30)

    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(dst), fourcc, fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"VideoWriter could not open {dst}")

    try:
        for frame in pixels:
            if frame.dtype != np.uint8:
                frame = frame.astype(np.uint8)
            writer.write(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
    finally:
        writer.release()

    return n_frames


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", required=True, type=Path, help="pruning_manifest.json produced by prune.py")
    p.add_argument("--input",    required=True, type=Path, help="original DICOM root (same --input used for prune.py)")
    p.add_argument("--output",   required=True, type=Path, help="directory to write AVIs (organised by reason)")
    p.add_argument(
        "--pruned-output", type=Path, default=None,
        help="Pruned output directory (same --output used for prune.py); if given, "
             "also renders both halves of every dual-pane split record for QA",
    )
    args = p.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    records = [r for r in manifest["records"] if r["action"] == "exclude" and r["reason"] in INSPECT_REASONS]

    jobs = [(rec, args.input / Path(rec["relative_path"]), rec["reason"], Path(rec["relative_path"]).name) for rec in records]

    if args.pruned_output is not None:
        for rec in manifest["records"]:
            if rec["action"] != "split":
                continue
            for out_rel in rec["outputs"]:
                jobs.append((rec, args.pruned_output / Path(out_rel), "split", Path(out_rel).name))

    if not jobs:
        print("No probe-excluded, unknown-probe, or split files found in manifest.")
        sys.exit(0)

    print(f"Converting {len(jobs)} clips to AVI...")
    ok = skipped = failed = 0

    for rec, src, reason, stem in jobs:
        probe = (rec["probe"] or "no_transducer_data").replace("/", "_").replace("\\", "_")
        dst   = args.output / reason / f"{stem}__{probe}.avi"
        dst.parent.mkdir(parents=True, exist_ok=True)

        if not src.exists():
            print(f"  SKIP (not found): {src}")
            skipped += 1
            continue

        try:
            ds = pydicom.dcmread(str(src))
            n_frames = _write_avi(ds, dst)
            print(f"  OK  [{reason}] {src.name}  ({n_frames} frames)  probe={rec['probe']!r}")
            ok += 1
        except Exception as exc:
            print(f"  ERR [{reason}] {src.name}: {exc}")
            failed += 1

    print(f"\nDone: {ok} converted, {skipped} skipped (missing), {failed} failed.")
    print(f"AVIs written to: {args.output}")


if __name__ == "__main__":
    main()
