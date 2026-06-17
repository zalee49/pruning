# AGENTS.md

Agent guidance for the `zalee49/pruning` repository.

## What this project is

The **Stage 0 pre-filter** for the TEE view-classification pipeline. It scans a
directory of raw DICOM files (headers only) and drops clips the downstream
classifier can't use, *before* de-identification runs. It is the upstream-most
step of a three-repo flow:

```
Pruning (this repo)          → DICOM DEIDENTIFICATION → View Classifier
prune raw DICOM by header      de-identify pixels +     stage AVIs → classify
                               export AVI
```

Two standalone CLI scripts, no package, no shared module.

## Repository layout

```
prune.py                    # the filter: walk raw DICOM, link approved files, write manifest
export_excluded_avis.py     # QA tool: render probe-excluded clips to AVI for visual review
3D_RENDER_FILTER_PLAN.md    # tabled plan for a future 3D-render exclusion rule
```

## Environment setup

No build, no test suite, no linter. The code uses 3.10+ type syntax
(`tuple[str, str]`, `str | None`), so run it under a Python ≥3.10 env that has
`pydicom` (and `opencv-python` + `numpy` for the exporter). In this workspace
that is the sibling **`dicom-deid`** conda env (Python 3.11):

```
conda run --no-capture-output -n dicom-deid python prune.py \
  --input  <RawDicomDir> \
  --output <PrunedDir> \
  [--unknown-probe-action exclude|include]   # default: exclude

conda run --no-capture-output -n dicom-deid python export_excluded_avis.py \
  --manifest <PrunedDir>/pruning_manifest.json \
  --input    <RawDicomDir> \
  --output   <InspectionAviDir>
```

`prune.py` exit codes are meaningful to the pipeline orchestrator:
**0** = files kept, **1** = all excluded, **2** = no input files.

## What the scripts do

- **`prune.py`** — walks `--input` recursively, reads each DICOM with
  `stop_before_pixels=True`, and hard-links (or copies, across drives) approved
  files into `--output` preserving relative paths, plus a `pruning_manifest.json`
  recording the action/reason for **every** file.
- **`export_excluded_avis.py`** — reads a `pruning_manifest.json` and converts
  the *probe-excluded* clips (`linear_probe`, `epiaortic_probe`, `unknown_probe`)
  to AVI so a human can confirm the probe rules were right. Output AVIs are
  **raw pixels with no PHI redaction** — inspection only, not for distribution.

## Exclusion rules (the core logic)

`prune()` applies these in order; the first match wins and is recorded in the
manifest `reason`:

1. **Unreadable** — `dcmread` raised (`reason: unreadable`).
2. **3D / volumetric** (`_is_3d`, `reason: 3d_volume`) — any of: SOPClassUID ==
   Enhanced US Volume Storage `1.2.840.10008.5.1.4.1.1.6.2`; `3D`/`VOLUME` in
   ImageType; or RegionSpatialFormat ≥ 3 in SequenceOfUltrasoundRegions.
3. **Probe** (`_classify_probe`) — keeps TEE probes **x8-2t / x7-2t**; excludes
   linear `L\d` probes (`linear_probe`) and epiaortic **x7-2** (`epiaortic_probe`).
   TransducerData absent/unrecognised → `unknown_probe`, kept or excluded per
   `--unknown-probe-action`.
4. **Too few frames** (`reason: too_few_frames`) — `NumberOfFrames < _MIN_FRAMES`.

## Invariants that are easy to break

- **`_MIN_FRAMES = 32` is a cross-repo contract.** It must equal
  `N_FRAMES * SAMPLE_PERIOD` in the View Classifier's `src/data.py`, and stay in
  sync with `MIN_FRAMES` in that repo's `integration/stage_avis.py`. This is an
  early header-based pre-filter (`NumberOfFrames`); `stage_avis.py` keeps a
  separate decode-based check as the safety net because `CAP_PROP_FRAME_COUNT` /
  `NumberOfFrames` can over-report for MJPG.
- **Probe ordering matters.** The x7-2t *keep* check must run before the x7-2
  epiaortic *exclude* check; the regex `x7-2(?!t)` depends on that. Probe model
  strings are normalised lowercase with `_`→`-` because Philips writes both
  `X8_2t` and `X8-2t`.
- **Known gap — 3D renders exported as 2D.** Philips exports 3D Zoom / volume
  renders as ordinary US Multi-frame images (`...1.1.3.1`, ImageType
  `DERIVED\PRIMARY\CARDIOLOGY`, no SequenceOfUltrasoundRegions), so `_is_3d`
  does **not** catch them and they pass. Investigation, the proposed
  `DERIVED + no-ultrasound-regions` discriminator, and the validation/cleanup
  plan live in [3D_RENDER_FILTER_PLAN.md](3D_RENDER_FILTER_PLAN.md). Tabled, not
  yet implemented.
- **Color-space double-conversion (exporter).** `export_excluded_avis.py` skips
  `convert_color_space` for JPEG transfer syntaxes (`...4.50`/`...4.51`) because
  pydicom/Pillow already decodes those to RGB even though
  PhotometricInterpretation still says YBR; converting again produces a green
  tint. Preserve this guard when touching `_write_avi`.

## Conventions

- Both scripts are header-first where possible (`stop_before_pixels=True`),
  fail conservatively (unparseable counts/values → exclude), and never abort the
  whole run on one bad file — they record the failure in the manifest/log and
  continue.
- The manifest is the source of truth for what happened and is consumed by
  `export_excluded_avis.py`; keep its `records` shape (`relative_path`, `action`,
  `probe`, `is_3d`, `n_frames`, `reason`) and `summary` counters in sync if you
  add an exclusion category.
