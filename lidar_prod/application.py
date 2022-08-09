import logging
import os
from typing import Callable
from tempfile import TemporaryDirectory
import hydra
from omegaconf import DictConfig
from lidar_prod.tasks.building_completion import BuildingCompletor
from lidar_prod.tasks.cleaning import Cleaner

from lidar_prod.commons import commons
from lidar_prod.tasks.building_validation import BuildingValidator
from lidar_prod.tasks.building_identification import BuildingIdentifier
from lidar_prod.tasks.basic_identification import BasicIdentifier

from lidar_prod.tasks.utils import get_las_data_from_las, save_las_data_to_las

log = logging.getLogger(__name__)


@commons.eval_time
def apply(config: DictConfig):
    """
    Augment rule-based classification of a point cloud with deep learning
    probabilities and vector building database.

    Args:
        config (DictConfig): Hydra config passed from run.py

    """
    processed_file_list = []
    for src_las_path in get_list_las_path_from_src(config.paths.src_las):
        target_las_path = os.path.join(config.paths.output_dir, os.path.basename(src_las_path))
        processed_file_list.append(process_one_file(config, src_las_path, target_las_path))
    return processed_file_list


@commons.eval_time
def applying(config: DictConfig, logic: Callable):
    for src_las_path in get_list_las_path_from_src(config.paths.src_las):
        target_las_path = os.path.join(config.paths.output_dir, os.path.basename(src_las_path))
        logic(config, src_las_path, target_las_path)


def get_list_las_path_from_src(src_path: str):
    """get a list of las from a path.
    If the path is a single file, that file will be the only one in the returned list
    if the path is a directory, all the .las will be in the returned list"""
    # src_path is a unique file
    if os.path.isfile(src_path):
        return [src_path]

    # src_path is a directory
    if os.path.isdir(src_path):
        src_las_path = []
        for (root, _, files) in os.walk(src_path):
            for file in files:
                _, file_extension = os.path.splitext(file)
                if file_extension.lower() != ".las":    # only LAS files are selected (the extension might be in uppercase)
                    continue
                src_las_path.append(os.path.join(root, file))
        return src_las_path


@commons.eval_time
def detect_vegetation_unclassified(config, src_las_path: str, dest_las_path: str = None):

    log.info(f"Detecting on {src_las_path}")
    data_format = config["data_format"]
    las_data = get_las_data_from_las(src_las_path)

    # detect vegetation
    vegetation_identifier = BasicIdentifier(
        config["vegetation_identification"]["vegetation_threshold"],
        data_format.las_dimensions.ai_vegetation_proba,
        data_format.las_dimensions.ai_vegetation_unclassified_groups,
        data_format.codes.vegetation,
        data_format
    )
    vegetation_identifier.identify(las_data)

    # detect unclassified
    unclassified_identifier = BasicIdentifier(
        config["vegetation_identification"]["unclassified_threshold"],
        data_format.las_dimensions.ai_unclassified_proba,
        data_format.las_dimensions.ai_vegetation_unclassified_groups,
        data_format.codes.unclassified,
        data_format
    )
    unclassified_identifier.identify(las_data)

    # keeping only the wanted dimensions for the result las
    cleaner = hydra.utils.instantiate(data_format.cleaning.output)
    cleaner.remove_dimensions(las_data)
    save_las_data_to_las(dest_las_path, las_data)


@commons.eval_time
def just_clean(config, src_las_path: str, dest_las_path: str = None):
    log.info(f"Cleaning {src_las_path}")
    data_format = config["data_format"]
    las_data = get_las_data_from_las(src_las_path)

    # remove unwanted dimensions
    cleaner = hydra.utils.instantiate(data_format.cleaning.input)
    cleaner.remove_dimensions(las_data)

    # save points array to the target
    save_las_data_to_las(dest_las_path, las_data)


@commons.eval_time
def process_one_file(config: DictConfig, src_las_path: str, dest_las_path: str = None):
    """call every desired step to process a las
    Args:
        src_las_path: the path of the source las
        dest_las_path: the path to save the result (optional)
    """
    log.info(f"Processing {src_las_path}")
    with TemporaryDirectory() as td:
        # Temporary LAS file for intermediary results.
        tmp_las_path = os.path.join(td, os.path.basename(src_las_path))

        # Removes unnecessary input dimensions to reduce memory usage
        cl: Cleaner = hydra.utils.instantiate(config.data_format.cleaning.input)
        cl.run(src_las_path, tmp_las_path)

        # Validate buildings (unsure/confirmed/refuted) on a per-group basis.
        bv: BuildingValidator = hydra.utils.instantiate(
            config.building_validation.application
        )
        bv.run(tmp_las_path, tmp_las_path)

        # Complete buildings with non-candidates that were nevertheless confirmed
        bc: BuildingCompletor = hydra.utils.instantiate(config.building_completion)
        bc.run(tmp_las_path, tmp_las_path)

        # Define groups of confirmed building points among non-candidates
        bi: BuildingIdentifier = hydra.utils.instantiate(config.building_identification)
        bi.run(tmp_las_path, tmp_las_path)

        # Remove unnecessary intermediary dimensions
        cl: Cleaner = hydra.utils.instantiate(config.data_format.cleaning.output)
        cl.run(tmp_las_path, dest_las_path)

    return dest_las_path
