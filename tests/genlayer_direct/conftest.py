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
import sys

import gltest.direct.loader as direct_loader
import gltest.direct.vm as direct_vm


if version("genlayer-test") != "0.30.0rc2" or version("genlayer-py") != "0.19.0rc2":
    raise RuntimeError("Direct Mode compatibility shim requires the exact locked GenLayer toolchain")


def _install_legacy_storage_imports():
    """Expose the current SDK storage package at the path gltest 0.30 imports."""
    storage = import_module("genlayer.py.storage")
    internal = import_module("genlayer.py.storage._internal")
    generate = import_module("genlayer.py.storage._internal.generate")

    # The pinned Direct Mode loader imports these legacy names in
    # _allocate_contract. This SDK release moved the implementation beneath
    # genlayer.py; without aliases, its ImportError fallback constructs the
    # contract directly and bypasses the generated storage type descriptor.
    sys.modules.setdefault("genlayer.storage", storage)
    sys.modules.setdefault("genlayer.storage._internal", internal)
    sys.modules.setdefault("genlayer.storage._internal.generate", generate)
    genlayer = import_module("genlayer")
    if not hasattr(genlayer, "storage"):
        genlayer.storage = storage


def _runtime_calldata():
    _install_legacy_storage_imports()
    return import_module("genlayer.py.calldata")


def _runtime_address():
    return import_module("genlayer.py.types").Address


def _runtime_address_u256():
    sdk_types = import_module("genlayer.py.types")
    return sdk_types.Address, sdk_types.u256


direct_loader.import_calldata = _runtime_calldata
direct_loader.import_address = _runtime_address
direct_vm.import_address_u256 = _runtime_address_u256
