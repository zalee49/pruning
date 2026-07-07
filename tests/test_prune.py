"""Tests for prune.py's 3D-exclusion logic.

Run with the dicom-deid conda env, which has pydicom + pytest:
    conda run --no-capture-output -n dicom-deid pytest
"""

import sys
from pathlib import Path

import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prune import _ENHANCED_US_VOLUME_UID, _US_MULTIFRAME_UID, _is_3d, _is_rendered_3d


def _dataset(*, sop_class: str | None = None, image_type: list[str] | None = None, regions: list[dict] | None = None) -> pydicom.Dataset:
    """Build a minimal synthetic dataset with just the tags _is_3d/_is_rendered_3d read."""
    ds = pydicom.Dataset()
    if sop_class is not None:
        ds.SOPClassUID = sop_class
    if image_type is not None:
        ds.ImageType = image_type
    if regions is not None:
        seq = []
        for r in regions:
            item = pydicom.Dataset()
            item.RegionSpatialFormat = r.get("spatial_format", 1)
            seq.append(item)
        ds.SequenceOfUltrasoundRegions = seq
    return ds


# --- _is_3d: existing signals (locking in current behavior) ---


def test_is_3d_enhanced_us_volume_sop_class():
    ds = _dataset(sop_class=_ENHANCED_US_VOLUME_UID)
    assert _is_3d(ds) is True


def test_is_3d_image_type_3d():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["DERIVED", "PRIMARY", "3D"])
    assert _is_3d(ds) is True


def test_is_3d_image_type_volume():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["DERIVED", "PRIMARY", "VOLUME"])
    assert _is_3d(ds) is True


def test_is_3d_volumetric_region_spatial_format():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["ORIGINAL", "PRIMARY"], regions=[{"spatial_format": 3}])
    assert _is_3d(ds) is True


def test_is_3d_plain_2d_clip_is_false():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["ORIGINAL", "PRIMARY"], regions=[{"spatial_format": 1}])
    assert _is_3d(ds) is False


# --- _is_rendered_3d: the new rule ---


def test_rendered_3d_derived_no_regions_sequence_caught():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["DERIVED", "PRIMARY", "CARDIOLOGY"])
    assert _is_rendered_3d(ds) is True


def test_rendered_3d_derived_empty_regions_sequence_caught():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["DERIVED", "PRIMARY", "CARDIOLOGY"], regions=[])
    assert _is_rendered_3d(ds) is True


def test_original_2d_clip_no_regions_is_safe():
    # ORIGINAL (not DERIVED) with no regions must not be flagged.
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["ORIGINAL", "PRIMARY"])
    assert _is_rendered_3d(ds) is False


def test_derived_2d_cine_with_one_region_is_safe():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID, image_type=["DERIVED", "PRIMARY", "CARDIOLOGY"], regions=[{"spatial_format": 1}])
    assert _is_rendered_3d(ds) is False


def test_derived_xplane_with_two_regions_is_safe():
    ds = _dataset(
        sop_class=_US_MULTIFRAME_UID,
        image_type=["DERIVED", "PRIMARY", "CARDIOLOGY"],
        regions=[{"spatial_format": 1}, {"spatial_format": 1}],
    )
    assert _is_rendered_3d(ds) is False


def test_enhanced_us_volume_sop_class_is_not_rendered_3d():
    # That's _is_3d's job, not _is_rendered_3d's.
    ds = _dataset(sop_class=_ENHANCED_US_VOLUME_UID, image_type=["DERIVED", "PRIMARY"])
    assert _is_rendered_3d(ds) is False


def test_non_us_multiframe_sop_class_is_safe():
    ds = _dataset(sop_class="1.2.840.10008.5.1.4.1.1.7", image_type=["DERIVED", "PRIMARY"])
    assert _is_rendered_3d(ds) is False


def test_missing_sop_class_is_safe():
    ds = _dataset(image_type=["DERIVED", "PRIMARY"])
    assert _is_rendered_3d(ds) is False


def test_missing_image_type_is_safe():
    ds = _dataset(sop_class=_US_MULTIFRAME_UID)
    assert _is_rendered_3d(ds) is False
