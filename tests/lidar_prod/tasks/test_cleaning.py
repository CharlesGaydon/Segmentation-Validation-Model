import os.path as osp
import tempfile

import pytest

from lidar_prod.tasks.cleaning import Cleaner
from lidar_prod.tasks.utils import get_las_data_from_las, pdal_read_las_array
from tests.conftest import check_las_format_versions_and_srs, check_las_invariance

SRC_LAS_SUBSET_PATH = "tests/files/870000_6618000.subset.postIA.las"
SRC_LAS_EPSG = "2154"
LAS_SUBSET_FILE_VEGETATION = "tests/files/436000_6478000.subset.postIA.las"


@pytest.mark.parametrize("extra_dims", ([], ""))
def test_cleaning_no_extra_dims(extra_dims):
    cl = Cleaner(extra_dims=extra_dims)

    with tempfile.TemporaryDirectory() as td:
        clean_las_path = osp.join(td, "no_extra_dims.las")
        cl.run(SRC_LAS_SUBSET_PATH, clean_las_path, SRC_LAS_EPSG)
        check_las_invariance(SRC_LAS_SUBSET_PATH, clean_las_path, SRC_LAS_EPSG)
        a, _ = pdal_read_las_array(clean_las_path, SRC_LAS_EPSG)
        las_dimensions = a.dtype.fields.keys()
        # Check that key dims were cleaned out
        assert all(dim not in las_dimensions for dim in ["building", "entropy"])
        check_las_format_versions_and_srs(clean_las_path, epsg=SRC_LAS_EPSG)


def test_cleaning_float_extra_dim():
    cl = Cleaner(extra_dims="entropy=float")
    with tempfile.TemporaryDirectory() as td:
        clean_las_path = osp.join(td, "float_extra_dim.las")
        cl.run(SRC_LAS_SUBSET_PATH, clean_las_path, SRC_LAS_EPSG)
        check_las_invariance(SRC_LAS_SUBSET_PATH, clean_las_path, SRC_LAS_EPSG)
        a, _ = pdal_read_las_array(clean_las_path, SRC_LAS_EPSG)
        las_dimensions = a.dtype.fields.keys()
        assert "entropy" in las_dimensions
        assert "building" not in las_dimensions
        check_las_format_versions_and_srs(clean_las_path, epsg=SRC_LAS_EPSG)


def test_cleaning_two_float_extra_dims_and_one_fantasy_dim():
    d1 = "entropy"
    d2 = "building"
    d3 = "i_do_not_exist_but_no_error_incurs"
    extra_dims = [f"{d1}=float", f"{d2}=float", f"{d3}=float"]
    cl = Cleaner(extra_dims=extra_dims)
    with tempfile.TemporaryDirectory() as td:
        clean_las_path = osp.join(td, "float_extra_dim_and_fantasy.las")
        cl.run(SRC_LAS_SUBSET_PATH, clean_las_path, SRC_LAS_EPSG)
        check_las_invariance(SRC_LAS_SUBSET_PATH, clean_las_path, SRC_LAS_EPSG)
        out_a, _ = pdal_read_las_array(clean_las_path, SRC_LAS_EPSG)
        assert d1 in out_a.dtype.fields.keys()
        assert d2 in out_a.dtype.fields.keys()
        assert d3 not in out_a.dtype.fields.keys()
        check_las_format_versions_and_srs(clean_las_path, epsg=SRC_LAS_EPSG)


@pytest.mark.parametrize("extra_dims", ("", "entropy=float", "building=float"))
def test_pdal_cleaning_format(extra_dims):
    cl = Cleaner(extra_dims=extra_dims)
    with tempfile.TemporaryDirectory() as td:
        clean_las_path = osp.join(td, "float_extra_dim.las")
        cl.run(SRC_LAS_SUBSET_PATH, clean_las_path, SRC_LAS_EPSG)
        check_las_format_versions_and_srs(clean_las_path, SRC_LAS_EPSG)


@pytest.mark.parametrize(
    "extra_dims, expected",
    [
        ("", []),
        ("entropy=float", "entropy=float"),
        (["entropy=float", "building=float"], "entropy=float,building=float"),
    ],
)
def test_pdal_cleaning_get_extra_dims_as_str(extra_dims, expected):
    cleaner = Cleaner(extra_dims=extra_dims)
    assert cleaner.get_extra_dims_as_str() == expected


@pytest.mark.parametrize(
    "extra_dims, expected",
    [
        (
            "all",
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
                "unclassified",
            ],
        ),
        (
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
                "unclassified",
            ],
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
                "unclassified",
            ],
        ),
        (
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
            ],
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
            ],
        ),
        ("", []),
        (
            ["PredictedClassification", "entropy"],
            ["entropy", "PredictedClassification"],
        ),
    ],
)
def test_laspy_cleaning_remove_dimensions(extra_dims, expected):
    las_data = get_las_data_from_las(LAS_SUBSET_FILE_VEGETATION)
    cleaner = Cleaner(extra_dims=extra_dims)
    cleaner.remove_dimensions(las_data)
    assert [dim for dim in las_data.point_format.extra_dimension_names] == expected


@pytest.mark.parametrize(
    "extra_dims, expected",
    [
        (
            [],
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
                "unclassified",
            ],
        ),
        (
            ["dimenplus1=int32"],
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
                "unclassified",
                "dimenplus1",
            ],
        ),
        (
            ["dimenplus1"],
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
                "unclassified",
            ],
        ),
        (
            ["dimenplus1=int32", "dimenplus2=float"],
            [
                "entropy",
                "PredictedClassification",
                "lasting_above",
                "bridge",
                "water",
                "building",
                "vegetation",
                "ground",
                "unclassified",
                "dimenplus1",
                "dimenplus2",
            ],
        ),
    ],
)
def test_laspy_cleaning_add_dimensions(extra_dims, expected):
    las_data = get_las_data_from_las(LAS_SUBSET_FILE_VEGETATION)
    cleaner = Cleaner(extra_dims=extra_dims)
    cleaner.add_dimensions(las_data)
    assert [dim for dim in las_data.point_format.extra_dimension_names] == expected
