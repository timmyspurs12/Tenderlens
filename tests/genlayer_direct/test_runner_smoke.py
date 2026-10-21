"""Smoke the official in-memory Direct Mode deploy/read/write path only."""
from pathlib import Path


CONTRACT = Path(__file__).parent / "contracts" / "storage.py"


def test_runtime_storage_descriptor_probe(direct_vm):
    import inspect
    import importlib
    import pkgutil
    from gltest.direct.loader import load_contract_class

    contract_cls = load_contract_class(CONTRACT, direct_vm)
    generate = importlib.import_module("genlayer.py.storage._internal.generate")
    package = importlib.import_module("genlayer.py.storage._internal")
    modules = sorted(module.name for module in pkgutil.iter_modules(package.__path__))
    api = {}
    for name, value in vars(generate).items():
        if callable(value) and any(part in name.lower() for part in ("build", "type", "desc", "record")):
            try:
                api[name] = str(inspect.signature(value))
            except (TypeError, ValueError):
                api[name] = type(value).__name__
    diagnostic = (
        f"generate_file={generate.__file__!r}; modules={modules!r}; "
        f"generate_api={api!r}; names={sorted(vars(generate))!r}; "
        f"contract_class={contract_cls!r}; class_desc={getattr(contract_cls, '__type_desc__', None)!r}"
    )
    assert hasattr(generate, "_BuilderCtx") and hasattr(generate, "_storage_build"), diagnostic


def test_direct_mode_deploy_read_and_write(direct_deploy):
    storage = direct_deploy(str(CONTRACT), "initial")

    assert storage.get_value() == "initial"
    storage.set_value("updated")
    assert storage.get_value() == "updated"
