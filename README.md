<div align="center">

# Semantic Segmentation production - Fusion Module

<a href="https://pytorch.org/get-started/locally/"><img alt="PyTorch" src="https://img.shields.io/badge/PyTorch-ee4c2c?logo=pytorch&logoColor=white"></a>
<a href="https://pytorchlightning.ai/"><img alt="Lightning" src="https://img.shields.io/badge/-Lightning-792ee5?logo=pytorchlightning&logoColor=white"></a>
<a href="https://hydra.cc/"><img alt="Config: Hydra" src="https://img.shields.io/badge/Config-Hydra-89b8cd"></a>

[![](https://shields.io/badge/-Lightning--Hydra--Template-017F2F?style=flat&logo=github&labelColor=303030)](https://github.com/ashleve/lightning-hydra-template)
</div>
<br><br>

## Description
### Context
The Lidar HD project ambitions to map France in 3D using 10 pulse/m² aerial Lidar. The data will be openly available, including a semantic segmentation with a minimal number of classes: ground, vegetation, buildings, vehicles, bridges, others.

A simple geometric rule-based semantic segmentation algorithm was applied on 160km² of Lidar data in three areas, to identify its buildings. An audit of the resulting classification showed a large number of false positive. A thorough inspection and labelling was performed to evaluate the quality of this classification, with an identification of its false positive and false negative. At larger scale, this kind of human inspection would be intractable, and more powerful methods are needed to validate the quality of the segmentation before its diffusion.

We therefore develop a production module which augments rules-based semantic segmentation algorithms with deep learning neural network predictions and a public building vector database. 

Components are:

- `application.py`: Fuse together rules-based classification, deep learning building probabilities, and building database, highlighting area of uncertainty for a final human inspection.
- `optimization.py`: Multi-objective hyperparameter optimization of the bulding validation decision thresholds.

### Process

The end goal is to edit the input (rules-based) classification as much as we confidently can, and to highlight remaining areas of uncertainty for human inspection.

**Input**: point cloud that went through a first geometric algorithm that identified `candidates building points` based on geometric rules (e.g. plane surfaces, above 1.5m of the ground, etc.), and for which a semantic segmentation model produced a point-level probability of being a building. The default name for this extra dimension is `building`. You can leverage this [package for aerial lidar deep learning segmentation](https://github.com/IGNF/lidar-deep-segmentation).

#### A) Building Validation

Goal: Confirm or refute groups of candidate building points when possible, mark them as unsure elsewise.

1) Clustering of _candidate buildings points_ into connected components.
2) Point-level decision
   1) Identification of points with ambiguous probability: `high entropy` if entropy $\geq$ E1 
   2) Identification of points that are `overlayed` by a building vector from the database.
   3) Decision at the point-level based on probabilities : 
      1) `confirmed` if:
         1) p$\geq$`C1`, or
         2) `overlayed` and p$\geq$ (`C1` * `Cr`), where `Cr` is a relaxation factor that reduces the confidence we require to confirm when a point overlayed by a building vector. 
      2) `refuted` if (1-p)$\geq$`R1`
3) Group-level decision :
    1) Uncertain due to high entropy: if proportion of `high entropy` points $\geq$ `E2`
    2) Confirmation: if proportion of `confirmed` points $\geq$ `C2` OR if proportion of `overlayed` points $\geq$ `O1`
    3) Refutation: if proportion of `refuted` points $\geq$ `R2` AND proportion of `overlayed` points < `O1`
    4) Uncertainty: elsewise (this is a safeguard: uncertain groups are supposed to be already captured via their entropy)
4) Update of the point cloud classification

Decision thresholds `E1`, `E2` , `C1`, `C2`, `R1`, `R2`, `O1` are chosen via a multi-objective hyperparameter optimization that aims to maximize automation, precision, and recall of the decisions. Right now we have automation=91%, precision=98.5%, recall=98.1% on a validation dataset. Illustration comes from older version.

![](assets/img/LidarBati-BuildingValidationM7.1V2.0.png)

#### B) Building Completion

Goal: Confirm points that were too isolated to make up a group but have high-enough probability nevertheless (e.g. walls)

Among  _candidate buildings points_ that have not been clustered in previous step due, identify those which nevertheless meet the requirement to be `confirmed`.
Cluster them together with previously confirmed building points in a relaxed, vertical fashion (higher tolerance, XY plan).
For each cluster, if some points were confirmed, the others are considered to belong to the same building, and are 
therefore confirmed as well.

![](assets/img/LidarBati-BuildingCompletion.png)


#### C) Building Identification

Goal: Highlight potential buildings that were missed by the rule-based algorithm, for human inspection. 

Among points that were **not** _candidate buildings points_ identify those which meet the requirement to be `confirmed`, and cluster them.

This clustering defines a LAS extra dimensions (`Group`) which indexes newly found cluster that may be some missed buildings.

![](assets/img/LidarBati-BuildingIdentification.png)


## Usage

### Install dependencies

```yaml
# clone project
git clone https://github.com/IGNF/lidar-prod-quality-control
cd lidar-prod-quality-control

# install conda
https://www.anaconda.com/products/individual


# create conda environment (you may need to run lines manually as conda may not activate properly from bash script)
source bash/setup_environment/setup_env.sh

# install postgis to request building database
sudo apt-get install postgis

# activate using
conda activate lidar_prod
```

### Use application as a package

To run the module from anywhere, you can install as a package in a your virtual environment.

```bash
# activate an env matching ./bash/setup_env.sh requirements.
conda activate lidar_prod

# install the package
pip install --upgrade https://github.com/IGNF/lidar-prod-quality-control/tarball/prod  # from github directly, using production branch
pip install -e .  # from local sources
```

To run the module as a package, you will need a source cloud point in LAS format with an additional channel containing predicted building probabilities. The name of this channel is specified by `config.data_format.las_dimensions.ai_building_proba`.

To run using default configurations of the installed package, use
```bash
python -m lidar_prod.run paths.src_las=[/path/to/file.las]
```

You can override the yaml file with flags `--config-path` and `--config-name`. You can also override specific parameters. By default, results are saved to a `./outputs/` folder, but this can be overriden with `paths.output_dir` parameter. See [hydra documentation](https://hydra.cc/docs/next/tutorials/basic/your_first_app/config_file/) for reference on overriding syntax.

To print default configuration run `python -m lidar_prod.run -h`. For pretty colors, run `python -m lidar_prod.run print_config=true`.

### Run sequentialy on multiple files

Hydra supports running the python script with several different values for a parameter via a `--multiruns`|`-m` flag and values separated by a comma.

```bash
python -m lidar_prod.run --multiruns paths.src_las=[file_1.las],[file_2.las],[file_3.las]
```

## Development

### Use application from source

Simply run from python sources directly
```bash
# activate an env matching ./bash/setup_env.sh requirements.
conda activate lidar_prod
python lidar_prod/run.py paths.src_las=[/path/to/file.las]
```

### Optimization and evaluation of building validation decision thresholds

Run a multi-objectives hyperparameters optimization of the decision thresholds, to maximize recall and precision directly while also maximizing automation. For this, you need a set of LAS with 1) a channel with predicted building probability, 2) a classification with labels that distinguish false positive, false negative, and true positive from a rules-based building classification.

```bash
conda activate lidar_prod
python lidar_prod/run.py +task=optimize building_validation.optimization.todo='prepare+optimize+evaluate+update' building_validation.optimization.paths.input_las_dir=[path/to/labelled/val/dataset/] building_validation.optimization.paths.results_output_dir=[path/to/save/results] 
```
Nota: to run on a single file during development, add a `+building_validation.optimization.debug=true` flag to the command line.

Optimized decision threshold will be pickled inside the results directory.

To evaluate the optimized module on a test set, change input las folder, and rerun. You need to specify that no optimization is required using the `todo` params. You also need to give the path to the pickled decision trheshold.

```bash
conda activate lidar_prod
python lidar_prod/run.py +task=optimize building_validation.optimization.todo='prepare+evaluate+update' building_validation.optimization.paths.input_las_dir=[path/to/labelled/test/dataset/] building_validation.optimization.paths.results_output_dir=[path/to/save/results] building_validation.optimization.paths.building_validation_thresholds_pickle=[path/to/optimized_thresholds.pickle]
```

### CICD and versions

New features are staged in the `dev` branch, and CICD workflow is run when a pull requets to merge is created.
In Actions, check the output of a full evaluation on a single LAS to spot potential regression. The app is also run 
on a subset of a LAS, which can be visually inspected before merging - there can always be surprises.

Package version follows semantic versionning conventions and is defined in `setup.py`. 

Releases are generated when new high-level functionnality are implemented (e.g. a new step in the production process), with a documentation role. Production-ready code is fast-forwarded in the `prod` branch when needed. 