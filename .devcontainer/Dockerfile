# Official Microsoft Dev Container Python image (verified published variant: 3.12-bookworm).
# Runtime minor is fixed at Python 3.12; security patch releases follow the maintained image tag.
FROM mcr.microsoft.com/devcontainers/python:3.12-bookworm

# uv is only the environment/lockfile manager. GenLayer dependencies are installed in .venv.
ARG UV_VERSION=0.13.3
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
