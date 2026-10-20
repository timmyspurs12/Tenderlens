# Milestone 3 — environment gate and implementation status

**Status: blocked before contract implementation.** No contract, Direct Mode test, evaluation-submission API, or lifecycle adapter has been added. This avoids shipping an uncompiled contract, treating simulations as consensus, or accepting transaction/finality claims without a trusted status source.

## Repository/environment discovery

- Checkout: `arena/fa6458cf-tenderlens`, starting commit `ce919fd`; existing WSGI/SQLite API and prior tests are present and unchanged.
- Host Python is CPython 3.11.2. `python3.12`, Docker, GenLayer CLI, and the `genlayer` Python module are absent.
- No `GENLAYER*`, RPC, wallet/private-key, `TENDERLENS*`, or database configuration variables are present in the environment. No network or deployment was selected or attempted.
- `uv==0.8.17` was installed into `/tmp/tenderlens-uv-bootstrap` using pip's `--target`, not into the system Python. `uv python install 3.12.11 --install-dir /home/user/.local/share/uv/python` failed downloading the official standalone CPython release from GitHub because the redirected `objects.githubusercontent.com` TLS connection failed (`UnknownIssuer`, then TLS handshake EOF). No 3.12 interpreter or project venv was created. The local restriction permits `github.com` and `codeload.github.com`, but not the release-asset host; the source tarball route lacks the system development headers needed for a reliable standalone build. The existing system Python was not altered.

## Official tooling checked

- Official Direct Mode/Studio Mode docs: <https://docs.genlayer.com/api-references/genlayer-test>. They specify Python >=3.12; Direct Mode requires no Docker/network; Studio Mode requires GenLayer Studio (Docker).
- The official `genlayerlabs/genlayer-testing-suite` `pyproject.toml` states `genlayer-test==0.29.2` upstream and a supported `genlayer-py>=0.18.0,<0.20.0` range. PyPI metadata was checked: `genlayer-test 0.29.2` and `genlayer-py 0.18.0` both require Python >=3.12. These are candidate compatible pins, not installed/tested in this checkout.
- Official contract docs require non-deterministic work inside `gl.eq_principle.*`, prohibit storage access inside the nondeterministic closure, and require validators independently verify the substance of the leader's result: <https://docs.genlayer.com/developers/intelligent-contracts/first-intelligent-contract> and <https://docs.genlayer.com/developers/intelligent-contracts/equivalence-principle>.
- The apparent legacy Direct Mode URL `/developers/intelligent-contracts/testing/direct-mode` returned 404; the current testing-suite API reference above is used instead.

## Why lifecycle work is also blocked

A durable evaluation submitter must persist the returned transaction identifier before returning a submission response, then resume tracking through a supported GenLayer lifecycle/status API. Without the compatible SDK/runtime (and, for Studio Mode, a Studio endpoint), there is no verified submission response shape, retry/idempotency behavior, protocol-status enum, execution-result source, or finalization confirmation to safely implement. A local fake adapter or client-supplied state would violate the milestone's trust boundary. No evaluation route is exposed as a placeholder.

## Next action

Provide a supported CPython 3.12+ runtime in the environment, or permit access to the official standalone Python release-asset host; then create a repository-local virtual environment, install the candidate pins, verify the actual installed GenLayer APIs, write contract Direct Mode tests, and only then add the database migration and worker against verified transaction interfaces. Studio integration/deployment remains a separate step requiring explicit network selection and authorization. Do not expose submissions to the browser before an authenticated BFF/session is in place.
