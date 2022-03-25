from dataclasses import dataclass
import functools
from glob import glob
import logging
import os
import pickle
import numpy as np
from sklearn.metrics import confusion_matrix
from typing import Any, Dict, List
import optuna
from tqdm import tqdm
import os.path as osp
import laspy

from lidar_prod.tasks.building_validation import (
    BuildingValidator,
    BuildingValidationClusterInfo,
    thresholds,
)
from lidar_prod.tasks.utils import split_idx_by_dim

log = logging.getLogger(__name__)


def constraints_func(trial):
    return trial.user_attrs["constraint"]


class BuildingValidationOptimizer:
    """Optimization logic for the BuildingValidation decision thresholds."""

    def __init__(
        self,
        todo: str,
        paths: Dict[str, str],
        building_validator: BuildingValidator,
        study: optuna.Study,
        design: Any,
        labels_from_20211001_building_val: Any,
        use_final_classification_codes: bool = False,
        debug=False,
    ):
        self.debug = debug
        self.todo = todo
        self.paths = paths
        self.bv = building_validator
        self.study = study
        self.design = design
        self.labels_from_20211001_building_val = labels_from_20211001_building_val
        self.use_final_classification_codes = use_final_classification_codes
        self.setup()

    def run(self):
        """Run decision threshold optimization."""
        if "prepare" in self.todo:
            self.prepare()
        if "optimize" in self.todo:
            self.optimize()
        if "evaluate" in self.todo:
            self.evaluate()
        if "update" in self.todo:
            self.update()

    def setup(self):
        """Setup step.

        Setup a few attributes and override BuildingValidator classification
        codes to adapt to those of the optimization dataset.

        """
        self.las_filepaths = glob(osp.join(self.paths.input_las_dir, "*.las"))
        self.las_filepaths = sorted(self.las_filepaths)
        if self.debug:
            self.las_filepaths = self.las_filepaths[:1]
        os.makedirs(self.paths.prepared_las_dir, exist_ok=True)
        self.prepared_las_filepaths = [
            osp.join(self.paths.prepared_las_dir, osp.basename(f))
            for f in self.las_filepaths
        ]
        os.makedirs(self.paths.updated_las_dir, exist_ok=True)
        self.out_las_filepaths = [
            osp.join(self.paths.updated_las_dir, osp.basename(f))
            for f in self.las_filepaths
        ]

        self.bv.candidate_buildings_codes = (
            self.labels_from_20211001_building_val.codes.true_positives
            + self.labels_from_20211001_building_val.codes.false_positives
        )
        self.bv.use_final_classification_codes = self.use_final_classification_codes
        self.design.confusion_matrix_order = [
            self.bv.codes.final.unsure,
            self.bv.codes.final.not_building,
            self.bv.codes.final.building,
        ]

    def prepare(self):
        """Preparation step.

        Cluster clouds and cross with building vector database, then
        extract cluster-level information.

        """
        clusters = []
        for in_f, out_f in tqdm(
            zip(self.las_filepaths, self.prepared_las_filepaths),
            desc="Preparation.",
            total=len(self.las_filepaths),
            unit="tiles",
        ):
            self.bv.prepare(in_f, out_f)
            clusters += self._extract_clusters_from_las(out_f)
        self._dump_clusters(clusters)

    def optimize(self):
        """Optimization step"""
        clusters = self._load_clusters()
        objective = functools.partial(self._objective, clusters=clusters)
        self.study.optimize(objective, n_trials=self.design.n_trials)
        best_rules = self._select_best_rules(self.study)
        log.info(f"Best_trial thresholds: \n{best_rules}")
        self._dump_best_rules(best_rules)

    def evaluate(self):
        """Evaluation step

        Returns:
            dict: a name: value dict of metrics.

        """
        clusters = self._load_clusters()
        self.bv._set_thresholds_from_pickle(
            self.paths.building_validation_thresholds_pickle
        )
        decisions = np.array([self.bv._make_group_decision(c) for c in clusters])
        mts_gt = np.array([c.target for c in clusters])
        metrics_dict = self._evaluate_decisions(mts_gt, decisions)
        log.info(f"\n Results:\n{self._get_results_logs_str(metrics_dict)}")
        return metrics_dict

    def update(self):
        """Update step.

        Update point cloud classification using optimized decision thresholds.

        """
        log.info(f"Updated las will be saved in {self.paths.results_output_dir}")
        self.bv._set_thresholds_from_pickle(
            self.paths.building_validation_thresholds_pickle
        )
        for prep_f, out_f in tqdm(
            zip(self.prepared_las_filepaths, self.out_las_filepaths),
            total=len(self.prepared_las_filepaths),
            desc="Update.",
            unit="tiles",
        ):
            self.bv.update(prep_f, out_f)
            log.info(f"Saved to {out_f}")

    def _extract_clusters_from_las(
        self, prepared_las: str
    ) -> List[BuildingValidationClusterInfo]:
        """Extract a cluster information object  in a prepared LAS.

        Args:
            prepared_las (str): path to LAS prepared for building validation.

        Returns:
            List[BuildingValidationClusterInfo]: cluster information for each cluster of candidate buildings
        """
        las = laspy.read(prepared_las)
        dim_cluster_id = las[
            self.bv.data_format.las_dimensions.ClusterID_candidate_building
        ]
        dim_classification = las[self.bv.data_format.las_dimensions.classification]

        split_idx = split_idx_by_dim(dim_cluster_id)
        # removes unclustered group that have ClusterID = 0
        START_IDX_OF_CLUSTERS = 1
        split_idx = split_idx[START_IDX_OF_CLUSTERS:]
        clusters = []
        for pts_idx in tqdm(
            split_idx, desc="Extract cluster info from LAS", unit="clusters"
        ):
            infos: BuildingValidationClusterInfo = self.bv._extract_cluster_info_by_idx(
                las, pts_idx
            )
            infos.target = self._define_MTS_ground_truth_flag(
                dim_classification[pts_idx]
            )
            clusters += [infos]
        return clusters

    def _define_MTS_ground_truth_flag(self, targets) -> int:
        """Based on the fraction of confirmed building points, set the nature of the shape or declare an ambiguous case"""
        tp_frac = np.mean(
            np.isin(
                targets,
                self.labels_from_20211001_building_val.codes.true_positives,
            )
        )
        if tp_frac >= self.labels_from_20211001_building_val.min_frac.true_positives:
            return self.bv.codes.final.building
        elif tp_frac < self.labels_from_20211001_building_val.min_frac.false_positives:
            return self.bv.codes.final.not_building
        return self.bv.codes.final.unsure

    def _compute_penalty(self, auto, precision, recall):
        """Positive float indicative a solution violates the constraint of minimal auto/precision/metrics"""
        penalty = 0
        if precision < self.design.constraints.min_precision_constraint:
            penalty += self.design.constraints.min_precision_constraint - precision
        if recall < self.design.constraints.min_recall_constraint:
            penalty += self.design.constraints.min_recall_constraint - recall
        if auto < self.design.constraints.min_automation_constraint:
            penalty += self.design.constraints.min_automation_constraint - auto
        return [penalty]

    def _objective(self, trial, clusters: List[BuildingValidationClusterInfo] = None):
        """Objective function for optuna optimization.
        Use prepared list to access group-level probas and targets.

        Args:
            trial: optuna trial
            clusters (List[BuildngValidationClusterInfo], optional): _description_. Defaults to None.

        Returns:
            float, float, float: automatisation, precision, recall
        """
        params = {
            "min_confidence_confirmation": trial.suggest_float(
                "min_confidence_confirmation", 0.0, 1.0
            ),
            "min_frac_confirmation": trial.suggest_float(
                "min_frac_confirmation", 0.0, 1.0
            ),
            "min_confidence_refutation": trial.suggest_float(
                "min_confidence_refutation", 0.0, 1.0
            ),
            "min_frac_refutation": trial.suggest_float("min_frac_refutation", 0.0, 1.0),
            "min_uni_db_overlay_frac": trial.suggest_float(
                "min_uni_db_overlay_frac", 0.5, 1.0
            ),
            "min_frac_confirmation_factor_if_bd_uni_overlay": trial.suggest_float(
                "min_frac_confirmation_factor_if_bd_uni_overlay", 0.5, 1.0
            ),
            "min_entropy_uncertainty": trial.suggest_float(
                "min_entropy_uncertainty", 0.5, 1.0
            ),
            "min_frac_entropy_uncertain": trial.suggest_float(
                "min_frac_entropy_uncertain", 0.33, 1.0
            ),
        }
        self.bv.thresholds = thresholds(**params)
        decisions = np.array([self.bv._make_group_decision(c) for c in clusters])
        mts_gt = np.array([c.target for c in clusters])
        metrics_dict = self._evaluate_decisions(mts_gt, decisions)

        # WARNING: order should always be automation, precision, recall
        values = (
            metrics_dict[self.design.metrics.proportion_of_automated_decisions],
            metrics_dict[self.design.metrics.precision],
            metrics_dict[self.design.metrics.recall],
        )
        auto, precision, recall = (
            value if not np.isnan(value) else 0 for value in values
        )

        # This enables constrained optimization
        trial.set_user_attr(
            "constraint", self._compute_penalty(auto, precision, recall)
        )
        return auto, precision, recall

    def _select_best_rules(self, study):
        """Find the trial that meets constraints and that maximizes automation."""
        trials = sorted(study.best_trials, key=lambda x: x.values[0], reverse=True)
        TRIALS_BELOW_ZERO_ARE_VALID = 0
        respect_constraints = [
            s
            for s in trials
            if s.user_attrs["constraint"][0] <= TRIALS_BELOW_ZERO_ARE_VALID
        ]
        try:
            best = respect_constraints[0]
        except Exception:
            log.warning(
                "No trial respecting constraints - returning best metrics-products."
            )
            trials = sorted(
                study.best_trials, key=lambda x: np.product(x.values), reverse=True
            )
            best = trials[0]
        best_rules = thresholds(**best.params)
        return best_rules

    def _dump_best_rules(self, best_trial_params):
        with open(self.paths.building_validation_thresholds_pickle, "wb") as f:
            pickle.dump(best_trial_params, f)
            log.info(
                f"Pickled best params to {self.paths.building_validation_thresholds_pickle}"
            )

    def _dump_clusters(self, clusters):
        with open(self.paths.group_info_pickle_path, "wb") as f:
            pickle.dump(clusters, f)
            log.info(f"Pickled groups to {self.paths.group_info_pickle_path}")

    def _load_clusters(self):
        with open(self.paths.group_info_pickle_path, "rb") as f:
            clusters = pickle.load(f)
            log.info(f"Loading pickled groups from {self.paths.group_info_pickle_path}")
        return clusters

    def _evaluate_decisions(self, mts_gt, ia_decision):
        """Evaluate confirmation and refutation decisions.

        Get dict of metrics to evaluate how good module decisions were in reference to ground truths.
        Targets: U=Unsure, N=No (not a building), Y=Yes (building)
        PRedictions : U=Unsure, C=Confirmation, R=Refutation
        Confusion Matrix :
                predictions
                [Uu Ur Uc]
        target  [Nu Nr Nc]
                [Yu Yr Yc]

        Maximization criteria:
        Proportion of each decision among total of candidate groups.
        We want to maximize it.

        Accuracies:
        Confirmation/Refutation Accuracy.
        Accurate decision if either "unsure" or the same as the label.

        Quality
        Precision and Recall, assuming perfect posterior decision for unsure predictions.
        Only candidate shapes with known ground truths are considered (ambiguous labels are ignored).
        Precision : (Yu + Yc) / (Yu + Yc + Nc)
        Recall : (Yu + Yc) / (Yu + Yn + Yc)
        """
        metrics_dict = dict()

        # VECTORS INFOS
        num_shapes = len(ia_decision)
        metrics_dict.update({self.design.metrics.groups_count: num_shapes})

        cm = confusion_matrix(
            mts_gt,
            ia_decision,
            labels=self.design.confusion_matrix_order,
            normalize=None,
        )
        metrics_dict.update({self.design.metrics.confusion_matrix_no_norm: cm.copy()})

        # CRITERIA
        cm = confusion_matrix(
            mts_gt,
            ia_decision,
            labels=self.design.confusion_matrix_order,
            normalize="all",
        )
        P_MTS_U, P_MTS_N, P_MTS_C = cm.sum(axis=1)
        metrics_dict.update(
            {
                self.design.metrics.group_unsure: P_MTS_U,
                self.design.metrics.group_no_buildings: P_MTS_N,
                self.design.metrics.group_building: P_MTS_C,
            }
        )
        P_IA_u, P_IA_r, P_IA_c = cm.sum(axis=0)
        PAD = P_IA_c + P_IA_r
        metrics_dict.update(
            {
                self.design.metrics.proportion_of_automated_decisions: PAD,
                self.design.metrics.proportion_of_uncertainty: P_IA_u,
                self.design.metrics.proportion_of_refutation: P_IA_r,
                self.design.metrics.proportion_of_confirmation: P_IA_c,
            }
        )

        # ACCURACIES
        cm = confusion_matrix(
            mts_gt,
            ia_decision,
            labels=self.design.confusion_matrix_order,
            normalize="pred",
        )
        RA = cm[1, 1]
        CA = cm[2, 2]
        metrics_dict.update(
            {
                self.design.metrics.refutation_accuracy: RA,
                self.design.metrics.confirmation_accuracy: CA,
            }
        )

        # NORMALIZED CM
        cm = confusion_matrix(
            mts_gt,
            ia_decision,
            labels=self.design.confusion_matrix_order,
            normalize="true",
        )
        metrics_dict.update({self.design.metrics.confusion_matrix_norm: cm.copy()})

        # QUALITY
        non_ambiguous_idx = mts_gt != self.bv.codes.final.unsure
        ia_decision = ia_decision[non_ambiguous_idx]
        mts_gt = mts_gt[non_ambiguous_idx]
        cm = confusion_matrix(
            mts_gt,
            ia_decision,
            labels=self.design.confusion_matrix_order,
            normalize="all",
        )
        final_true_positives = cm[2, 0] + cm[2, 2]  # Yu + Yc
        final_false_positives = cm[1, 2]  # Nc
        precision = final_true_positives / (
            final_true_positives + final_false_positives
        )  #  (Yu + Yc) / (Yu + Yc + Nc)

        positives = cm[2, :].sum()
        recall = final_true_positives / positives  # (Yu + Yc) / (Yu + Yn + Yc)

        metrics_dict.update(
            {
                self.design.metrics.precision: precision,
                self.design.metrics.recall: recall,
            }
        )

        return metrics_dict

    def _get_results_logs_str(self, metrics_dict: dict):
        """Format all metrics as a str for logging."""
        results_logs = "\n".join(
            f"{name}={value:{'' if type(value) is int else '.3'}}"
            for name, value in metrics_dict.items()
            if name
            not in [
                self.design.metrics.confusion_matrix_norm,
                self.design.metrics.confusion_matrix_no_norm,
            ]
        )
        results_logs = (
            results_logs
            + "\nConfusion Matrix\n"
            + str(metrics_dict[self.design.metrics.confusion_matrix_no_norm].round(3))
            + "\nConfusion Matrix (normalized)\n"
            + str(metrics_dict[self.design.metrics.confusion_matrix_norm].round(3))
        )
        return results_logs
