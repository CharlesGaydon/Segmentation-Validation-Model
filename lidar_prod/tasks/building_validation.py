from dataclasses import dataclass
from typing import Union
import logging
import os
import os.path as osp
from typing import Dict, Optional
import subprocess
import numpy as np
import pdal
from tempfile import TemporaryDirectory
from tempfile import mkdtemp
import shutil
import geopandas
from tqdm import tqdm
from lidar_prod.tasks.utils import (
    BDUniConnectionParams,
    get_integer_bbox,
    get_pdal_reader,
    get_pdal_writer,
    split_idx_by_dim,
    get_pipeline
)

log = logging.getLogger(__name__)


@dataclass
class BuildingValidationClusterInfo:
    """Elements needed to confirm, refute, or be uncertain about a cluster of candidate building points."""

    probabilities: np.ndarray
    overlays: np.ndarray
    entropies: np.ndarray

    # target is based on corrected labels - only needed for optimization of decision thresholds
    target: Optional[int] = None


class BuildingValidator:
    """Logic of building validation.

    The candidate building points identified with a rule-based algorithm are cluster together.
    The BDUni building vectors are overlayed on the points clouds, and points that fall under a vector are flagged.
    Then, classification dim is updated on a per-group basis, based on both AI probabilities and BDUni flag.

    See `README.md` for the detailed process.
    """

    def __init__(
        self,
        shp_path: str = None,
        bd_uni_connection_params=None,
        cluster=None,
        bd_uni_request=None,
        data_format=None,
        thresholds=None,
        use_final_classification_codes: bool = True,
    ):
        self.shp_path = shp_path
        self.bd_uni_connection_params = bd_uni_connection_params
        self.cluster = cluster
        self.bd_uni_request = bd_uni_request
        self.use_final_classification_codes = use_final_classification_codes
        self.thresholds = thresholds  # default values
        self.data_format = data_format
        # For easier access
        self.codes = data_format.codes.building
        self.candidate_buildings_codes = data_format.codes.building.candidates
        self.pipeline: pdal.pipeline.Pipeline = None
        self.setup()

    def setup(self):
        """Setup. Defines useful variables."""

        self.detailed_to_final_map: dict = {
            detailed: final for detailed, final in self.codes.detailed_to_final
        }

    def run(
        self,
        input_values: Union[str, pdal.pipeline.Pipeline],
        target_las_path: str = None,
    ) -> str:
        """Runs application.

        Transforms cloud at `input_values` following building validation logic,
        and saves it to `target_las_path`

        Args:
            input_values (str| pdal.pipeline.Pipeline): path or pipeline to input LAS file with a building probability channel
            target_las_path (str): path for saving updated LAS file.

        Returns:
            str: returns `target_las_path`

        """
        self.pipeline = get_pipeline(input_values)
        with TemporaryDirectory() as td:
            log.info(
                "Preparation : Clustering of candidates buildings & Requesting BDUni"
            )
            if type(input_values) == str:
                log.info(f"Applying Building Validation to file \n{input_values}")
                temp_f = osp.join(td, osp.basename(input_values))
            else:
                temp_f = ""
            self.prepare(input_values, temp_f)
            log.info("Using AI and Databases to update cloud Classification")
            self.update()
        return target_las_path

    def prepare(self, input_values: Union[str, pdal.pipeline.Pipeline], prepared_las_path: str, save_result: bool = False) -> None:
        f"""
        Prepare las for later decision process. .
        1. Cluster candidates points, in a new `{self.data_format.las_dimensions.ClusterID_candidate_building}`
        dimension where the index of clusters starts at 1 (0 means no cluster).
        2. Identify points overlayed by a BD Uni building, in a new
        `{self.data_format.las_dimensions.uni_db_overlay}` dimension (0/1 flag).

        In the process is created a new dimensions which identifies candidate buildings (0/1 flag)
        `{self.data_format.las_dimensions.candidate_buildings_flag}`, to ignore them in later
        buildings identification.

        Dimension classification should not be modified here, as optimization step needs to
        do this step once before testing multiple decision parameters on the same prepared data.

        Args:
            input_values (str| pdal.pipeline.Pipeline): path or pipeline to input LAS file with a building probability channel
            target_las_path (str): path for saving prepared LAS file.
            save_result (bool): True to save a las instead of propagating a pipeline

        """

        dim_candidate_flag = self.data_format.las_dimensions.candidate_buildings_flag
        dim_cluster_id_pdal = self.data_format.las_dimensions.cluster_id
        dim_cluster_id_candidates = (
            self.data_format.las_dimensions.ClusterID_candidate_building
        )
        dim_overlay = self.data_format.las_dimensions.uni_db_overlay

        self.pipeline = get_pipeline(input_values)
        # Identify candidates buildings points with a boolean flag
        self.pipeline |= pdal.Filter.ferry(dimensions=f"=>{dim_candidate_flag}")
        _is_candidate_building = (
            "("
            + " || ".join(
                f"Classification == {int(candidate_code)}"
                for candidate_code in self.candidate_buildings_codes
            )
            + ")"
        )
        self.pipeline |= pdal.Filter.assign(
            value=f"{dim_candidate_flag} = 1 WHERE {_is_candidate_building}"
        )
        # Cluster candidates buildings points. This creates a ClusterID dimension (int)
        # in which unclustered points have index 0.
        self.pipeline |= pdal.Filter.cluster(
            min_points=self.cluster.min_points,
            tolerance=self.cluster.tolerance,
            where=f"{dim_candidate_flag} == 1",
        )

        # Copy ClusterID into a new dim and reset it to 0 to avoid conflict with later tasks.
        self.pipeline |= pdal.Filter.ferry(
            dimensions=f"{dim_cluster_id_pdal}=>{dim_cluster_id_candidates}"
        )
        self.pipeline |= pdal.Filter.assign(value=f"{dim_cluster_id_pdal} = 0")
        self.pipeline.execute()
        bbox = get_integer_bbox(self.pipeline, buffer=self.bd_uni_request.buffer)

        self.pipeline |= pdal.Filter.ferry(dimensions=f"=>{dim_overlay}")

        if self.shp_path:
            temp_dirpath = None     # no need for a temporay directory to add the shapefile in it, we already have the shapefile
            _shp_p = self.shp_path
            gdf = geopandas.read_file(_shp_p)
            buildings_in_bd_topo = not len(gdf) == 0    # check if there arebuildings in the shp

        else:
            temp_dirpath = mkdtemp()
            # TODO: extract coordinates from LAS directly using pdal.
            # Request BDUni to get a shapefile of the known buildings in the LAS
            _shp_p = os.path.join(temp_dirpath, "temp.shp")
            buildings_in_bd_topo = request_bd_uni_for_building_shapefile(
                self.bd_uni_connection_params, _shp_p, bbox
            )

        # Create overlay dim
        # If there are some buildings in the database, create a BDTopoOverlay boolean
        # dimension to reflect it.

        if buildings_in_bd_topo:
            self.pipeline |= pdal.Filter.overlay(
                column="PRESENCE", datasource=_shp_p, dimension=dim_overlay
            )

        if save_result:
            self.pipeline |= get_pdal_writer(prepared_las_path)
            os.makedirs(osp.dirname(prepared_las_path), exist_ok=True)
        self.pipeline.execute()

        if temp_dirpath:
            shutil.rmtree(temp_dirpath)

    def update(self, src_las_path: str = None, target_las_path: str = None) -> None:
        """Updates point cloud classification channel."""
        if src_las_path:
            self.pipeline = pdal.Pipeline()
            self.pipeline |= get_pdal_reader(src_las_path)
            self.pipeline.execute()

        las = self.pipeline.arrays[0]

        # 1) Map all points to a single "not_building" class
        # to be sure that they will all be modified.

        dim_clf = self.data_format.las_dimensions.classification
        dim_flag = self.data_format.las_dimensions.candidate_buildings_flag
        candidates_mask = las[dim_flag] == 1
        las[dim_clf][candidates_mask] = self.codes.final.not_building

        # 2) Decide at the group-level
        # TODO: check if this can be moved somewhere else. WARNING: use_final_classification_codes may be modified in
        # an unsafe manner during optimization. Consider using a setter that will change decision_func alongside.

        # Decide level of details of classification codes
        decision_func = self._make_detailed_group_decision
        if self.use_final_classification_codes:
            decision_func = self._make_group_decision

        # Get the index of points of each cluster
        # Remove unclustered group that have ClusterID = 0 (i.e. the first "group")
        cluster_id_dim = las[
            self.data_format.las_dimensions.ClusterID_candidate_building
        ]
        split_idx = split_idx_by_dim(cluster_id_dim)
        split_idx = split_idx[1:]

        # Iterate over groups and update their classification
        for pts_idx in tqdm(
            split_idx, desc="Update cluster classification", unit="clusters"
        ):
            infos = self._extract_cluster_info_by_idx(las, pts_idx)
            las[dim_clf][pts_idx] = decision_func(infos)

        self.pipeline = pdal.Pipeline(arrays=[las])

        if target_las_path:
            self.pipeline = get_pdal_writer(target_las_path).pipeline(las)
            os.makedirs(osp.dirname(target_las_path), exist_ok=True)
            self.pipeline.execute()

    def _extract_cluster_info_by_idx(
        self, las: np.ndarray, pts_idx: np.ndarray
    ) -> BuildingValidationClusterInfo:
        """Extracts all necessary information to make a decision based on points indices.

        Args:
            las (np.ndarray): point cloud of interest
            pts_idx (np.ndarray): indices of points in considered clusters

        Returns:
            BuildingValidationClusterInfo: data necessary to make a decision at cluster level.

        """
        pts = las[pts_idx]
        probabilities = pts[self.data_format.las_dimensions.ai_building_proba]
        overlays = pts[self.data_format.las_dimensions.uni_db_overlay]
        entropies = pts[self.data_format.las_dimensions.entropy]
        targets = pts[self.data_format.las_dimensions.classification]
        return BuildingValidationClusterInfo(
            probabilities, overlays, entropies, targets
        )

    def _make_group_decision(self, *args, **kwargs) -> int:
        f"""Wrapper to simplify decision codes during LAS update.
        Signature follows the one of {self._make_detailed_group_decision.__name__}
        Returns:
            int: final classification code for the considered group.
        """
        detailed_code = self._make_detailed_group_decision(*args, **kwargs)
        return self.detailed_to_final_map[detailed_code]

    def _make_detailed_group_decision(
        self, infos: BuildingValidationClusterInfo
    ) -> int:
        """Decision process at the cluster level.

        Confirm or refute candidate building groups based on fraction of confirmed/refuted points and
        on fraction of points overlayed by a building vector in BDUni.

        See Readme for details of this group-level decision process.

        Args:
            infos (BuildngValidationClusterInfo): arrays describing the cluster of candidate builiding points

        Returns:
            int: detailed classification code for the considered group.

        """
        # HIGH ENTROPY

        high_entropy = (
            np.mean(infos.entropies >= self.thresholds.min_entropy_uncertainty)
            >= self.thresholds.min_frac_entropy_uncertain
        )

        # CONFIRMATION - threshold is relaxed under BDUni
        p_heq_threshold = (
            infos.probabilities >= self.thresholds.min_confidence_confirmation
        )

        relaxed_threshold = (
            self.thresholds.min_confidence_confirmation
            * self.thresholds.min_frac_confirmation_factor_if_bd_uni_overlay
        )
        p_heq_relaxed_threshold = infos.probabilities >= relaxed_threshold

        ia_confirmed_flag = np.logical_or(
            p_heq_threshold, np.logical_and(infos.overlays, p_heq_relaxed_threshold)
        )

        ia_confirmed = (
            np.mean(ia_confirmed_flag) >= self.thresholds.min_frac_confirmation
        )

        # REFUTATION
        ia_refuted = (
            np.mean(
                (1 - infos.probabilities) >= self.thresholds.min_confidence_refutation
            )
            >= self.thresholds.min_frac_refutation
        )
        uni_overlayed = (
            np.mean(infos.overlays) >= self.thresholds.min_uni_db_overlay_frac
        )

        if high_entropy:
            return self.codes.detailed.unsure_by_entropy
        if ia_refuted:
            if uni_overlayed:
                return self.codes.detailed.ia_refuted_but_under_db_uni
            return self.codes.detailed.ia_refuted
        if ia_confirmed:
            if uni_overlayed:
                return self.codes.detailed.both_confirmed
            return self.codes.detailed.ia_confirmed_only
        if uni_overlayed:
            return self.codes.detailed.db_overlayed_only
        return self.codes.detailed.both_unsure


def request_bd_uni_for_building_shapefile(
    bd_params: BDUniConnectionParams,
    shapefile_path: str,
    bbox: Dict[str, int],
):
    """BD Uni request.

    Create a shapefile with non destructed building on the area of interest
    and saves it.
    Also add a "PRESENCE" column filled with 1 for later use by pdal.

    """
    Lambert_93_SRID = 2154
    sql_request = f'SELECT \
        st_setsrid(batiment.geometrie,{Lambert_93_SRID}) AS geometry, \
        1 as presence \
        FROM batiment \
        WHERE batiment.geometrie \
            && \
        ST_MakeEnvelope({bbox["x_min"]}, {bbox["y_min"]}, {bbox["x_max"]}, {bbox["y_max"]}, {Lambert_93_SRID}) \
        and \
        not gcms_detruit'
    cmd = [
        "pgsql2shp",
        "-f",
        shapefile_path,
        "-h",
        bd_params.host,
        "-u",
        bd_params.user,
        "-P",
        bd_params.pwd,
        bd_params.bd_name,
        sql_request,
    ]
    # This call may yield
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as e:
        # In empty zones, pgsql2shp does not create a shapefile
        if (
            e.output
            == b"Initializing... \nERROR: Could not determine table metadata (empty table)\n"
        ):
            return False
        # Error can be due to something else entirely, like
        # an inability to translate host name to an address.
        # e.g. "could not translate host name "serveurbdudiff.ign.fr" to address: System error"
        raise e
    except ConnectionRefusedError as e:
        log.error(
            "ConnectionRefusedError when requesting BDUni.  \
            This means that the Database cannot be accessed (e.g. due to vpn/proxy reasons, \
            or bad credentials)"
        )
        raise e

    # read & write to avoid unnacepted 3D shapefile format.
    gdf = geopandas.read_file(shapefile_path)
    gdf[["PRESENCE", "geometry"]].to_file(shapefile_path)

    return True


@dataclass
class thresholds:
    """The decision thresholds for a cluser-level decisions."""

    min_confidence_confirmation: float
    min_frac_confirmation: float
    min_frac_confirmation_factor_if_bd_uni_overlay: float
    min_uni_db_overlay_frac: float
    min_confidence_refutation: float
    min_frac_refutation: float
    min_entropy_uncertainty: float
    min_frac_entropy_uncertain: float
