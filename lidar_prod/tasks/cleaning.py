import logging
import os
import os.path as osp
from typing import Iterable, Optional, Union

import laspy
import pdal

from lidar_prod.tasks.utils import get_pdal_reader, get_pdal_writer

log = logging.getLogger(__name__)


class Cleaner:
    """Keep only necessary extra dimensions channels."""

    def __init__(self, extra_dims: Optional[Union[Iterable[str], str]]):
        """Format extra_dims parameter from config.

        Args:
            extra_dims (Optional[Union[Iterable[str], str]]): each dim should have format dim_name:pdal_type.
            If a string, used directly; if an iterable, dimensions are joined together.

        """
        # turn a listconfig into a 'normal' list
        self.extra_dims = [extra_dims] if isinstance(extra_dims, str) else [dimension for dimension in extra_dims]

        # creating a dict where key = dimension's name and value = diemnsion's type
        #  if no "=type" in extra_dims then value = None
        self.extra_dims_as_dict = dict()
        for extra_dim in self.extra_dims:
            if len(extra_dim.split("=")) == 2:
                self.extra_dims_as_dict[extra_dim.split("=")[0]] = extra_dim.split("=")[1]
            else:
                self.extra_dims_as_dict[extra_dim] = None

    def get_extra_dims_as_str(self):
        """'stringify' the extra_dims list and return it, or an empty list if there is no extra dims"""
        return_str = ",".join(self.extra_dims)
        return return_str if return_str else []

    def run(self, src_las_path: str, target_las_path: str):
        """Clean out LAS extra dimensions.

        Args:
            src_las_path (str): input LAS path
            target_las_path (str): output LAS path, with specified extra dims.
        """
        pipeline = pdal.Pipeline()
        pipeline |= get_pdal_reader(src_las_path)
        pipeline |= get_pdal_writer(target_las_path, extra_dims=self.get_extra_dims_as_str())
        os.makedirs(osp.dirname(target_las_path), exist_ok=True)
        pipeline.execute()
        log.info(f"Saved to {target_las_path}")

    def remove_dimensions(self, las_data: laspy.lasdata.LasData):
        """remove dimension from (laspy) data"""
        # if we want to keep all dimension, we do nothing
        if self.extra_dims == ["all"]:
            return

        # selecting dimensions to remove
        dimension_to_remove = []
        for dimension in las_data.point_format.extra_dimension_names:
            if dimension not in self.extra_dims_as_dict:
                dimension_to_remove.append(dimension)

        # case: 0 dimension to remove
        if not dimension_to_remove:
            return

        # case: 1 dimension to remove
        if len(dimension_to_remove) == 1:
            las_data.remove_extra_dim(dimension_to_remove[0])
            return

        # case: 2+ dimensions to remove
        las_data.remove_extra_dims(dimension_to_remove)

    def add_dimensions(self, las_data: laspy.lasdata.LasData):
        """Add the dimensions that exist in self.extra_dimensions but not in las data"""
        # selecting dimensions to add
        dimensions_to_add = []
        for dimension, type in self.extra_dims_as_dict.items():
            if not type:  # we only add the dimensions we know the type of
                log.warning(f"{dimension} has no type and thus is not added as a column.")
                continue

            if dimension not in las_data.point_format.extra_dimension_names:
                dimensions_to_add.append(dimension)

        # adding dimensions
        # case: 0 dimension to add
        if len(dimensions_to_add) == 0:
            return

        # case: 1 dimension to add
        if len(dimensions_to_add) == 1:
            las_data.add_extra_dim(
                laspy.ExtraBytesParams(
                    dimensions_to_add[0],
                    type=self.extra_dims_as_dict[dimensions_to_add[0]],
                )
            )
            return

        # case: 2+ dimensions to add
        extra_bytes_list = []
        for dimension in dimensions_to_add:
            extra_bytes_list.append(laspy.ExtraBytesParams(dimension, type=self.extra_dims_as_dict[dimension]))
        las_data.add_extra_dims(extra_bytes_list)
