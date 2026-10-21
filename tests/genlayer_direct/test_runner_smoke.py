"""Smoke the official in-memory Direct Mode deploy/read/write path only."""
from pathlib import Path


CONTRACT = Path(__file__).parent / "contracts" / "storage.py"


def test_runtime_storage_descriptor_probe(direct_vm):
    from gltest.direct.loader import load_contract_class

    contract_cls = load_contract_class(CONTRACT, direct_vm)
    from genlayer.storage import ROOT_SLOT_ID
    from genlayer.storage._internal.generate import _BuilderCtx, _storage_build

    type_desc = _storage_build(_BuilderCtx.empty(), contract_cls)
    slot = direct_vm._storage.get_store_slot(ROOT_SLOT_ID)
    instance = type_desc.get(slot, 0)
    diagnostic = (
        f"type_desc_type={type(type_desc)!r}; "
        f"type_desc_attrs={[name for name in dir(type_desc) if not name.startswith('_')]!r}; "
        f"props={getattr(type_desc, 'props', None)!r}; "
        f"contract_class_desc={getattr(contract_cls, '__type_desc__', None)!r}; "
        f"instance_dict={getattr(instance, '__dict__', None)!r}"
    )
    assert hasattr(instance, "__type_desc__"), diagnostic


def test_direct_mode_deploy_read_and_write(direct_deploy):
    storage = direct_deploy(str(CONTRACT), "initial")

    assert storage.get_value() == "initial"
    storage.set_value("updated")
    assert storage.get_value() == "updated"
