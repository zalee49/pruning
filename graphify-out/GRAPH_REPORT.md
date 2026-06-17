# Graph Report - .  (2026-06-14)

## Corpus Check
- Corpus is ~3,367 words - fits in a single context window. You may not need a graph.

## Summary
- 49 nodes · 68 edges · 9 communities (8 shown, 1 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 1 edges (avg confidence: 0.85)
- Token cost: 34,686 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]

## God Nodes (most connected - your core abstractions)
1. `prune()` - 10 edges
2. `_write_avi()` - 5 edges
3. `_extract_probe_string()` - 5 edges
4. `_classify_probe()` - 5 edges
5. `prune() exclusion pipeline` - 5 edges
6. `_is_3d()` - 4 edges
7. `Dataset` - 4 edges
8. `_frame_count()` - 4 edges
9. `_probe_string()` - 4 edges
10. `_find_dicoms()` - 4 edges

## Surprising Connections (you probably didn't know these)
- `3D Render Filter Plan (tabled)` --references--> `_is_3d() (3D/volumetric check)`  [EXTRACTED]
  3D_RENDER_FILTER_PLAN.md → AGENTS.md
- `3D renders exported as 2D US Multi-frame (known gap)` --conceptually_related_to--> `_is_3d() (3D/volumetric check)`  [EXTRACTED]
  3D_RENDER_FILTER_PLAN.md → AGENTS.md
- `_is_3d() (3D/volumetric check)` --references--> `SequenceOfUltrasoundRegions (0018,6011)`  [EXTRACTED]
  AGENTS.md → 3D_RENDER_FILTER_PLAN.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **prune() ordered exclusion checks** — prune__prune, prune__is_3d, prune__classify_probe, prune__min_frames [EXTRACTED 1.00]
- **32-frame cross-repo contract** — prune__min_frames, data, stage_avis [EXTRACTED 1.00]
- **Pruning to de-id to classifier flow** — pruning, dicom_deidentification, view_classifier [EXTRACTED 1.00]

## Communities (9 total, 1 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.24
Nodes (9): Color-space double-conversion guard, _write_avi() (AVI writer), main(), Dataset, Path, Convert all probe-excluded and unknown-probe DICOM clips from a pruning manifest, Decode ds.pixel_array and write an MJPG AVI, handling raw Philips DICOMs correct, _write_avi() (+1 more)

### Community 1 - "Community 1"
Cohesion: 0.29
Nodes (7): src/data.py (N_FRAMES * SAMPLE_PERIOD), Ordered exclusion rules (first match wins), Probe check ordering invariant, _classify_probe() (probe classifier), _MIN_FRAMES = 32 (cross-repo contract), prune() exclusion pipeline, integration/stage_avis.py (decode-based safety net)

### Community 2 - "Community 2"
Cohesion: 0.33
Nodes (7): DataElement, _classify_probe(), _extract_probe_string(), _probe_string(), Dataset, Return (action, reason) for the probe in this DICOM.      action  : "keep" | "ex, Extract a plain probe model string from a TransducerData DataElement.      Trans

### Community 3 - "Community 3"
Cohesion: 0.60
Nodes (5): 3D Render Filter Plan (tabled), 3D renders exported as 2D US Multi-frame (known gap), DERIVED + no-ultrasound-regions discriminator, SequenceOfUltrasoundRegions (0018,6011), _is_3d() (3D/volumetric check)

### Community 4 - "Community 4"
Cohesion: 0.40
Nodes (5): dicom-deid conda env (Python 3.11), DICOM DEIDENTIFICATION (de-id stage), Pruning (Stage 0 pre-filter), Three-repo pipeline flow, View Classifier (downstream classifier)

### Community 5 - "Community 5"
Cohesion: 0.40
Nodes (4): Header-first, fail-conservatively convention, _frame_count(), Prune a directory of DICOM files before de-identification.  Reads DICOM headers, Source frame count from NumberOfFrames (0028,0008).      Absent tag means a sing

### Community 6 - "Community 6"
Cohesion: 0.40
Nodes (5): _find_dicoms(), _link_or_copy(), Path, Walk input_dir recursively; return all files (assumed DICOM)., Hardlink src -> dst; fall back to copy if they're on different drives.

### Community 7 - "Community 7"
Cohesion: 0.67
Nodes (3): main(), prune(), Scan input_dir, copy/link approved files to output_dir, write manifest.      Ret

## Knowledge Gaps
- **7 isolated node(s):** `Dataset`, `DataElement`, `DICOM DEIDENTIFICATION (de-id stage)`, `View Classifier (downstream classifier)`, `dicom-deid conda env (Python 3.11)` (+2 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Pruning (Stage 0 pre-filter)` connect `Community 4` to `Community 0`, `Community 5`?**
  _High betweenness centrality (0.294) - this node is a cross-community bridge._
- **Why does `prune() exclusion pipeline` connect `Community 1` to `Community 3`, `Community 5`?**
  _High betweenness centrality (0.282) - this node is a cross-community bridge._
- **Why does `pruning_manifest.json` connect `Community 0` to `Community 5`?**
  _High betweenness centrality (0.136) - this node is a cross-community bridge._
- **What connects `Dataset`, `Convert all probe-excluded and unknown-probe DICOM clips from a pruning manifest`, `Decode ds.pixel_array and write an MJPG AVI, handling raw Philips DICOMs correct` to the rest of the system?**
  _21 weakly-connected nodes found - possible documentation gaps or missing edges._