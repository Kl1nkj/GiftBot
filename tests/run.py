"""Запуск тестов без pytest: python tests/run.py"""
import importlib.util
import sys
import traceback
from pathlib import Path

failed = 0
for path in sorted(Path(__file__).parent.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in dir(module):
        if name.startswith("test_"):
            try:
                getattr(module, name)()
                print(f"ok   {path.stem}.{name}")
            except Exception:
                failed += 1
                print(f"FAIL {path.stem}.{name}")
                traceback.print_exc()
sys.exit(1 if failed else 0)
