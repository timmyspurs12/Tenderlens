"""Smoke the official in-memory Direct Mode deploy/read/write path only."""
from pathlib import Path


CONTRACT = Path(__file__).parent / "contracts" / "storage.py"


def test_runtime_storage_descriptor_probe(direct_vm):
    import importlib
    from gltest.direct.loader import load_contract_class

    contract_cls = load_contract_class(CONTRACT, direct_vm)
    storage = importlib.import_module("genlayer.py.storage")
    generate = importlib.import_module("genlayer.py.storage._internal.generate")
    type_desc = generate._storage_build(contract_cls, {})
    instance = type_desc.get(direct_vm._storage.get_store_slot(storage.ROOT_SLOT_ID), 0)
    diagnostic = (
        f"type_desc_type={type(type_desc)!r}; props={getattr(type_desc, 'props', None)!r}; "
        f"instance_dict={getattr(instance, '__dict__', None)!r}"
    )
    assert hasattr(instance, "__type_desc__"), diagnostic
    assert "value" in instance.__type_desc__.props, diagnostic


def test_direct_mode_deploy_read_and_write(direct_deploy):
    storage = direct_deploy(str(CONTRACT), "initial")

    assert storage.get_value() == "initial"
    storage.set_value("updated")
    assert storage.get_value() == "updated"
