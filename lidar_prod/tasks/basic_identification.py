"""
Takes vegetation probabilities as input, and defines vegetation

"""
import logging
from typing import Union

import numpy as np
import laspy

log = logging.getLogger(__name__)


class IoU:
    """contains an IoU and its associated values """
    true_positive: int
    false_negative: int
    false_positive: int
    iou: float

    def __init__(self, true_positive: int, false_negative: int, false_positive: int):
        self.true_positive = true_positive
        self.false_negative = false_negative
        self.false_positive = false_positive
        self.iou = true_positive / (true_positive + false_negative + false_positive)

    def __str__(self):
        return "IoU: {:0.3f} |  true positive: {:,} | false negative: {:,} | false positive: {:,}"\
            .format(self.iou, self.true_positive, self.false_negative, self.false_positive)

    @staticmethod
    def combine_iou(iou_list: list):
        """combine several IoUs to return an average/total IoU"""
        return IoU(
            sum(iou.true_positive for iou in iou_list),
            sum(iou.false_negative for iou in iou_list),
            sum(iou.false_positive for iou in iou_list)
            )

    @staticmethod
    def iou_by_mask(mask_to_evaluate: np.ndarray, truth_mask: np.ndarray):
        """ return an IoU from a mask we want to evaluate and a mask containing the truth"""
        true_positive = np.count_nonzero(np.logical_and(truth_mask, mask_to_evaluate))
        false_negative = np.count_nonzero(np.logical_and(truth_mask, ~mask_to_evaluate))
        false_positive = np.count_nonzero(np.logical_and(~truth_mask, mask_to_evaluate))
        return IoU(true_positive, false_negative, false_positive)


class BasicIdentifier:
    def __init__(
            self,
            threshold: float,
            proba_column: str,
            result_column: str,
            result_code: int,
            evaluate_iou: bool = False,
            truth_column: str = None,
            truth_result_code: Union[int, list] = None):
        """
        BasicIdentifier set all points with a value from a column above a threshold to another value in another column

        threshold: above the threshold, a point is set
        proba_column: the column the treshold is compared against
        result_column: the column to store the result
        result_code: the value the point will be set to
        evaluate_iou: True if we want to evaluate the IoU of that selection
        truth_column: if we want to evaluate the IoU, this is the column with the real results to compare againt
        truth_result_code: if we want to evaluate the IoU, this is/are the code(s) of the "truth".
                            Can be an int of a list of int, if we want an IoU but truth_result_code is not provided then result_code
                            is used instead
        """
        self.threshold = threshold
        self.proba_column = proba_column
        self.result_column = result_column
        self.result_code = result_code
        self.evaluate_iou = evaluate_iou
        self.truth_column = truth_column
        self.truth_result_code = truth_result_code if truth_result_code else result_code

    def identify(self, las_data: laspy.lasdata.LasData):

        # if the result column doesn't exist, we add it
        if self.result_column not in [dim for dim in las_data.point_format.extra_dimension_names]:
            las_data.add_extra_dim(laspy.ExtraBytesParams(name=self.result_column, type="uint32"))

        # get the mask listing the points above the threshold
        threshold_mask = las_data.points[self.proba_column] >= self.threshold

        # set the selected points to the wanted value
        las_data.points[self.result_column][threshold_mask] = self.result_code

        # calculate ious if necessary
        if self.evaluate_iou:
            if isinstance(self.truth_result_code, int):
                truth_mask = las_data.points[self.truth_column] == self.truth_result_code
            else:   # if not an int, truth_mask should be a list
                truth_mask = np.isin(las_data.points[self.truth_column], self.truth_result_code)
            self.iou = IoU.iou_by_mask(threshold_mask, truth_mask)
