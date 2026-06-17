# Plan: filter out 3D / rendered-volume clips that slip past pruning

**Status:** Tabled (investigation complete, not yet implemented).
**Date:** 2026-06-14

## Problem

Some 3D / volume-rendered TEE clips are making it all the way through the
pipeline (de-id → stage → classify). `prune.py`'s `_is_3d()` is not catching
them.

`_is_3d()` ([prune.py](prune.py)) checks three header signals:

1. SOPClassUID == Enhanced US Volume Storage (`1.2.840.10008.5.1.4.1.1.6.2`)
2. ImageType contains `3D` or `VOLUME`
3. RegionSpatialFormat ≥ 3 inside SequenceOfUltrasoundRegions (0018,6011)

These work for clips the scanner tags as volumetric, but they miss
**3D volumes that the scanner exports as ordinary 2D US Multi-frame images.**

## What we confirmed

Inspected one slipped-through clip:

- Flat AVI: `Z:\DICOM Research\prunedavi_flat\PSE_1b5985b2a60ce75f__2.25.195536980597243143459166247771012321187.avi`
- Source DICOM: `Z:\DICOM Research\prunedavi\PSE_1b5985b2a60ce75f\2.25.195536980597243143459166247771012321187.dcm`

Headers:

| Tag | Value |
|---|---|
| SOPClassUID | `1.2.840.10008.5.1.4.1.1.3.1` (ordinary US Multi-frame) |
| ImageType | `DERIVED\PRIMARY\CARDIOLOGY` (no `3D`/`VOLUME` token) |
| SequenceOfUltrasoundRegions | **ABSENT** |
| NumberOfFrames | 76 |
| TransducerData | `X8_2t` (legit TEE probe) |
| Manufacturer / Model | Philips Medical Systems / EPIQ CVx |

Extracting the middle frame confirmed visually that it is a **3D Zoom volume
render** (on-screen text "3D Zoom", "2D/3D", "XRES 3"; rendered cube of the
mitral valve). None of the three `_is_3d()` signals fire on it.

## Discriminator we landed on

A real scan-converted 2D acquisition always carries at least one ultrasound
region (sector geometry). A rendered volume does not. Across this patient's
192 clips:

- Every `ORIGINAL` clip has ≥1 ultrasound region.
- `DERIVED` 2D cines have 1–12 regions.
- **20 clips are `DERIVED` with 0 ultrasound regions** — the rendered
  3D / MPR exports, including the reported one.

**Proposed rule:** exclude US Multi-frame clips that are `DERIVED` **and** have
an absent/empty `SequenceOfUltrasoundRegions` (0018,6011).

## Plan when we pick this back up

1. **Validate before committing.** The rule was checked against one patient
   only. Sweep all 20 suspects here (and a couple of other patients), extract a
   frame from each, and confirm none are legitimate 2D clips before we start
   dropping on this rule.
2. **Implement in `prune.py`.** Add the `DERIVED + no-ultrasound-regions`
   signal as a new exclusion. Give it its own manifest reason / counter
   (e.g. `excluded_3d_rendered`, or fold into `excluded_3d`) so it stays
   auditable in `pruning_manifest.json`.
3. **One-off cleanup of already-staged data.** The fix only affects future
   runs. `Z:\DICOM Research\prunedavi_flat` already contains these renders
   (~20 per patient in the sample), so a separate cleanup pass over the
   existing staged AVIs will be needed.

## Open questions / risks

- Is `DERIVED + 0 regions` ever true for a legitimate 2D clip we want to keep?
  (Step 1 above is meant to answer this.) M-mode and other non-sector modes
  are the most likely false-positive sources to check.
- Whether to gate the rule on Philips/EPIQ specifically, or apply it across all
  manufacturers. Only Philips data was examined.
