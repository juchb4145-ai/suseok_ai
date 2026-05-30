import builtins
import importlib
import sys


def test_core_api_imports_without_pyqt(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("PyQt5"):
            raise AssertionError(f"Core API imported PyQt module: {name}")
        return original_import(name, globals, locals, fromlist, level)

    for module_name in list(sys.modules):
        if module_name == "trading_app.api" or module_name.startswith("trading_app."):
            sys.modules.pop(module_name, None)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    module = importlib.import_module("trading_app.api")

    assert module.app.title == "Trading Core API"


def test_core_storage_and_engine_do_not_import_kiwoom_client():
    for path in ["storage/db.py", "trading/engine.py", "trading/strategy/conditions.py", "trading/strategy/candidates.py"]:
        source = open(path, encoding="utf-8").read()
        assert "from kiwoom.client" not in source
        assert "import kiwoom.client" not in source
