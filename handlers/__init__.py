
import importlib
import pkgutil
from pathlib import Path

_PACKAGE = __name__
_PACKAGE_DIR = Path(__file__).resolve().parent

for module_info in sorted(pkgutil.iter_modules([str(_PACKAGE_DIR)]), key=lambda item: item.name):
    if module_info.name in {"__init__", "messages", "registry"}:
        continue
    importlib.import_module(f"{_PACKAGE}.{module_info.name}")
