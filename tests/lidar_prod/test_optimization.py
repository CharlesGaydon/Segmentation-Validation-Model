import os
import hydra
import numpy as np

import pdal
import os.path as osp
import tempfile

from lidar_prod.tasks.building_validation_optimization import (
    BuildingValidationOptimizer,
)
from tests.conftest import get_a_format_preserving_pdal_pipeline, pdal_read_las_array


IN_F = "tests/files/870000_6618000.subset.postIA.las"


def test_optimize_on_subset(default_hydra_cfg):
    default_hydra_cfg.paths.src_las = IN_F
    # TODO: ignore the warning here.
    with tempfile.TemporaryDirectory() as td:
        # Copy this file into a clean, temporary dir, to be sure that it is the only one considered
        # during optimization even if we add new files in its folde later.
        # In ClassificationCorrected we put a corrected version of the classification
        # We copy it into Classification channel to use it in this test.
        ops = [pdal.Filter.assign(value=f"Classification = ClassificationCorrected")]
        in_f_isolated_copy = osp.join(td, osp.basename(IN_F))
        pipeline = get_a_format_preserving_pdal_pipeline(IN_F, in_f_isolated_copy, ops)
        pipeline.execute()

        default_hydra_cfg.building_validation.optimization.paths.input_las_dir = td
        default_hydra_cfg.building_validation.optimization.paths.results_output_dir = td

        # Check that a full optimization run can pass successfully
        bvo: BuildingValidationOptimizer = hydra.utils.instantiate(
            default_hydra_cfg.building_validation.optimization
        )
        bvo.run()

        # Assert that an prepared and an updated file are generated in td
        assert os.path.isfile(osp.join(td, "prepared", osp.basename(IN_F)))
        out_f = osp.join(td, "updated", osp.basename(IN_F))
        assert os.path.isfile(out_f)

        # Check the output of the evaluate method. Note that it uses the
        # prepared data obtained from the full run just above
        metrics_dict = bvo.evaluate()
        assert metrics_dict["groups_count"] == 15
        assert metrics_dict["group_no_buildings"] == 0.4
        assert metrics_dict["p_auto"] == 1.0
        assert metrics_dict["p_auto"] == 1.0
        assert metrics_dict["recall"] == 1.0
        assert metrics_dict["precision"] == 1.0

        # Update with final codes and check if they are the right ones.
        bvo.bv.use_final_classification_codes = True
        bvo.update()
        assert os.path.isfile(out_f)
        arr = pdal_read_las_array(out_f)
        # Check that we have either 1/2 (ground/unclassified), or one of
        # the final classification code of the module. here there are no unsure groups
        # so we have only 4 codes.
        final_codes = default_hydra_cfg.data_format.codes.building.final
        expected_codes = {1, 2, final_codes.building, final_codes.not_building}
        actual_codes = {*np.unique(arr["Classification"])}
        assert expected_codes == actual_codes


# We may want to use a large las if we manage to download it. we cannot version it with git.
# Here are the perfs for reference, on
# /home/CGaydon/repositories/Validation_Module/lidar-prod-quality-control/inputs/evaluation_las/V0.5_792000_6272000.las

# """
# groups_count=1493
# group_unsure=0.00402
# group_no_buildings=0.149
# group_building=0.847
# p_auto=0.889
# p_unsure=0.111
# p_refute=0.0924
# p_confirm=0.797
# a_refute=0.899
# a_confirm=0.976
# precision=0.98
# recall=0.99
# Confusion Matrix
# [[   2    1    3]
# [  74  124   25]
# [  89   13 1162]]
# Confusion Matrix (normalized)
# [[0.333 0.167 0.5  ]
# [0.332 0.556 0.112]
# [0.07  0.01  0.919]]
# """

# REFERENCE_METRICS = {"precision": 0.98, "recall":0.98,"p_auto":0.889,}
