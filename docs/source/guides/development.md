# Developer's guide

## Code versionning

Package version follows semantic versionning conventions and is defined in `setup.py`.

Releases are generated when new high-level functionnality are implemented (e.g. a new step in the production process), with a documentation role. Production-ready code is fast-forwarded in the `prod` branch when needed to match the `main` branch.

## Tests

Tests can be run in an activated environment with.

```bash
conda activate lidar_prod
python -m pytest
```

One test depends on a large, non-versionned file (665MB), which is accessible from the self-hosted action runner, but not publicly available at the moment. The absence of the file makes the test xfail so that it is not required for local development.

## Continuous Integration (CI)

New features are developped in ad-hoc branches (e.g. `refactor-database-query`), and merged in the `dev` branch. When ready, `dev` can be merged in `main`.

CI tests are run for pull request to merge on either `dev` or `main` branches, and on pushes to `dev`, `main`, and `prod`. The CI workflow builds a docker image, runs linting, and tests the code.

## Continuous Delivery (CD)

When the event is a push and not a merge request, this means that there was either a direct push to `dev`|`main`|`prod` or that a merge request was accepted. In this case, if the CI workflow passes, the docker image is tagged with the branch name, resulting in e.g. a `lidar_prod:prod` image that is up to date with the branch content. See [../tutorials/use.md] for how to leverage such image to run the app.

Additionnaly, pushes on the `main` branch build this library documentation, which is hosted on Github pages.
