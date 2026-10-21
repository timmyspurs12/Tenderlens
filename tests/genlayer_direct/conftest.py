"""Narrow compatibility shim for the pinned upstream Direct Mode runner.

Testing Suite v0.30.0-rc.2 reports ``unexpected end of memory`` because its
message injector can fail to import the current GenVM v0.6 SDK modules and
silently leave fd 0 untouched. The runtime modules live under
``genlayer.py``. Patch only the two injector lookups, after the runner has added
its versioned SDK path and before each contract is imported. This does not
change application code or claim target-network compatibility.

Upstream tracking: https://github.com/genlayerlabs/genlayer-testing-suite/issues/113
"""
from __future__ import annotations

from importlib import import_module
from importlib.metadata import version

import gltest.direct.loader as direct_loader
import gltest.direct.vm as direct_vm


if version("genlayer-test") != "0.30.0rc2" or version("genlayer-py") != "0.19.0rc2":
    raise RuntimeError("Direct Mode compatibility shim requires the exact locked GenLayer toolchain")


def _runtime_calldata():
    return import_module("genlayer.py.calldata")


def _runtime_address():
    return import_module("genlayer.py.types").Address


def _runtime_address_u256():
    sdk_types = import_module("genlayer.py.types")
    return sdk_types.Address, sdk_types.u256


direct_loader.import_calldata = _runtime_calldata
direct_loader.import_address = _runtime_address
direct_vm.import_address_u256 = _runtime_address_u256
