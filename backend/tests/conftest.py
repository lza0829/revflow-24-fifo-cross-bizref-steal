"""测试隔离：每个测试模块独立 SQLite 库，并按模块声明的 SEED_FILE 装载夹具。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("FIXTURES_DIR", str(ROOT / "fixtures"))

import app.db as appdb  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def isolated_db(request):
    data_dir = ROOT / "backend" / f"data_test_{request.module.__name__.replace('.', '_')}"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    appdb.DATA_DIR = data_dir
    appdb.DB_PATH = data_dir / "app.db"
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["SEED_FILE"] = getattr(request.module, "SEED_FILE", "seed.json")
    yield
    shutil.rmtree(data_dir, ignore_errors=True)
