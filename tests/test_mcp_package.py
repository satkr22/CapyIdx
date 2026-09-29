from __future__ import annotations

import importlib

import pytest


def test_mcp_package_lazy_exports_and_missing_attribute() -> None:
    module = importlib.import_module("capyidx.mcp")

    assert module.Runtime.__name__ == "Runtime"
    assert module.build_runtime.__name__ == "build_runtime"
    assert module.Tool.__name__ == "Tool"
    assert module.TOOLS_BY_NAME["lookup_symbol"].name == "lookup_symbol"
    with pytest.raises(AttributeError, match="has no attribute"):
        module.not_a_public_mcp_attribute


def test_mcp_main_module_defines_entrypoint() -> None:
    module = importlib.import_module("capyidx.mcp.__main__")
    assert callable(module.main)
