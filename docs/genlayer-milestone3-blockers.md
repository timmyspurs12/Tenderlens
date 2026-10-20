# GenLayer environment discovery: Milestone 3A

## Runtime status

The local Agent Mode host remains CPython 3.11.2, with no `python3.12`, Docker, or GenLayer CLI. `uv==0.13.3` is available only from a temporary `pip --target` install, not system-wide. Attempts to download managed CPython 3.12.15 from `github.com/astral-sh/python-build-standalone` failed on its redirect to `objects.githubusercontent.com` (`UnknownIssuer`). No local Python 3.12 environment was created, and no package has been installed into the existing Python environment.

A reproducible GitHub Codespaces configuration is now checked in under `.devcontainer/`. Its base image is the published Microsoft Dev Container Python tag `mcr.microsoft.com/devcontainers/python:3.12-bookworm`, verified as a listed variant in the official `devcontainers/images` repository. The `postCreateCommand` asserts CPython 3.12, reports pip and GenLayer versions, and checks that the Direct Mode runner exists. The image/container itself was **not built or launched** in this local environment, and the Codespaces checks have not run.

## GenLayer dependency compatibility finding

The official testing-suite GitHub `main` resolves to signed commit `343e3a358f9e235a93b49c60721ce7676585ff07`. Its `pyproject.toml` declares package version `0.29.2`, requires Python >=3.12, and accepts `genlayer-py>=0.18.0,<0.20.0`. The exact PyPI release `genlayer-test==0.29.2`, however, publishes metadata `genlayer-py>=0.13.0,<0.17.0`. This was checked directly from the PyPI JSON for release 0.29.2. Therefore the PyPI distribution and requested `genlayer-py==0.18.0` are incompatible; forcing either would conceal a dependency conflict.

`requirements-genlayer.in` instead pins the official source at that immutable signed Git commit (which declares the required 0.29.2 package version) together with `genlayer-py==0.18.0` and `pytest==9.1.1`. The transitive dependency lock with artifact hashes was successfully resolved for Python 3.12 using `uv pip compile` into `requirements-genlayer.lock.txt`. This is a **lock resolution**, not evidence that the packages were installed or that tests passed. uv warned that the Python 3.12 interpreter was unavailable locally; it used Python 3.11.2 to build dependency metadata while resolving the requested target.

## Official references checked

- [Official GenLayer Testing Suite docs (Direct Mode and Studio Mode)](https://docs.genlayer.com/api-references/genlayer-test): Direct Mode needs Python >=3.12 but no Docker/network; Studio Mode requires Docker.
- [Official testing-suite source at the pinned commit](https://github.com/genlayerlabs/genlayer-testing-suite/tree/343e3a358f9e235a93b49c60721ce7676585ff07).
- [Pinned commit API/signature metadata](https://api.github.com/repos/genlayerlabs/genlayer-testing-suite/commits/343e3a358f9e235a93b49c60721ce7676585ff07).
- [PyPI metadata for `genlayer-test` 0.29.2](https://pypi.org/pypi/genlayer-test/0.29.2/json).
- [Official Microsoft Dev Container Python image source](https://github.com/devcontainers/images/tree/main/src/python): lists published variant `3.12-bookworm`; a runtime pull/digest was not available from this host.

## GitHub Actions validation

The workflow now has a separate `GenLayer Direct Mode` job. It provisions Python 3.12 and Node 22.14.0, installs pinned uv, and calls `.devcontainer/setup.sh --genlayer-only` so the same dependency locks and installed-package/source/runner checks are reused without repeating frontend installation. It then runs `tests/genlayer_direct/test_runner_smoke.py` using the official pytest plugin fixtures and a small storage contract based on the pinned suite's published Direct Mode example. This smoke fixture validates only Direct Mode contract load/read/write; it is not a procurement assessment or real consensus.

The GitHub Actions job must execute successfully before the runtime is considered verified. No wallet secrets, network credentials, or deployment configuration are required. No Studio Mode, deployment, transaction, or consensus flow is part of this job.
