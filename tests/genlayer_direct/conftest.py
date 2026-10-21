"""Narrow compatibility shim for the pinned upstream Direct Mode runner.

The runner's message injector and WASI codec look under legacy SDK paths,
while the pinned GenVM v0.6 SDK modules live under ``genlayer.py``. Its contract
allocator also expects a removed ``_BuilderCtx`` API; adapt that seam to the
current stdlib's real ``_storage_build`` API. This preserves generated
descriptors and real contract execution; it does not change application code
or claim target-network compatibility.

Upstream tracking: https://github.com/genlayerlabs/genlayer-testing-suite/issues/113
"""
from __future__ import annotations

from importlib import import_module
from importlib.metadata import version

import gltest.direct.loader as direct_loader
import gltest.direct.vm as direct_vm
import gltest.direct.wasi_mock as direct_wasi_mock


if version("genlayer-test") != "0.30.0rc2" or version("genlayer-py") != "0.19.0rc2":
    raise RuntimeError("Direct Mode compatibility shim requires the exact locked GenLayer toolchain")


def _allocate_contract_current_runtime(contract_cls, vm, *args, **kwargs):
    """Allocate through the storage builder API shipped with this runtime."""
    storage = import_module("genlayer.py.storage")
    generate = import_module("genlayer.py.storage._internal.generate")

    # gltest 0.30.0rc2 expects the removed _BuilderCtx API. In the pinned
    # py-genlayer stdlib, _storage_build takes (contract_cls, generics_map).
    # Use its actual TypeDesc and instance factory so generated field setters
    # receive the descriptor the runtime expects; never synthesize metadata.
    type_desc = generate._storage_build(contract_cls, {})
    slot = vm._storage.get_store_slot(storage.ROOT_SLOT_ID)
    instance = type_desc.get(slot, 0)

    generated_class = getattr(type_desc, "cls", None)
    init_owner = generated_class if generated_class is not None else contract_cls
    init = getattr(init_owner, "__init__", None)
    if init is not None:
        init = getattr(init, generate.ORIGINAL_INIT_ATTR, init)
        init(instance, *args, **kwargs)
    return instance


direct_loader._allocate_contract = _allocate_contract_current_runtime


def _runtime_calldata():
    return import_module("genlayer.py.calldata")


def _runtime_address():
    return import_module("genlayer.py.types").Address


def _runtime_address_u256():
    sdk_types = import_module("genlayer.py.types")
    return sdk_types.Address, sdk_types.u256


direct_loader.import_calldata = _runtime_calldata
direct_wasi_mock.import_calldata = _runtime_calldata
direct_loader.import_address = _runtime_address
direct_vm.import_address_u256 = _runtime_address_u256
