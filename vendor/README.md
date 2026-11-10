# Vendored wheels

`saaf-0.1.0.dev0-py3-none-any.whl` is Saaf (India-first PII masking,
Apache-2.0) built from `packages/saaf-core` at the commit pinned in
`pyproject.toml`. It has no runtime dependencies.

The Docker image installs Saaf from this file, checked against its SHA-256 in
the `Dockerfile`, so building the image needs no `git` and no access to
GitHub. Before, pip cloned the whole Saaf repository during the build, and a
slow or interrupted connection failed it ("could not fetch … from promisor
remote").

The wheel builds reproducibly. To check it against the source, or to update it
after changing the Saaf pin, run `vendor/build-saaf.sh` (or `--update`).

`pip install -e .` on a development machine still installs Saaf from the git
URL in `pyproject.toml`. If GitHub is slow or unreachable, install the wheel
first and skip the dependency resolution for it:

```bash
pip install vendor/saaf-0.1.0.dev0-py3-none-any.whl
pip install -e ".[dev]" --no-deps && pip install -r requirements.lock --no-deps
```
