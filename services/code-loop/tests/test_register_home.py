#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_register_home.py — 钉扎写入脚本的门禁测试（服务拆分，2026-09-10）。

锁死契约：register 把服务根绝对路径写入钉扎文件（默认 ~/.code-loop/service.home），
写入前校验服务根 marker（<root>/scripts/control_plane.py）；marker 缺失（文件被
挪动/残缺部署）时拒绝写入；钉扎父目录不存在则自动创建（首次安装零手工步骤）。
"""
import tempfile
import unittest
from pathlib import Path


import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import register_home  # noqa: E402


class TmpTree:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        return Path(self._tmp.name)

    def __exit__(self, *a):
        self._tmp.cleanup()


def make_service(root: Path) -> Path:
    """搭一个最小服务根（含 marker），返回根目录。"""
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "control_plane.py").write_text("# marker\n", encoding="utf-8")
    return root


class TestRegisterHome(unittest.TestCase):
    def test_writes_absolute_service_root(self):
        """正常路径：把服务根绝对路径写入钉扎文件。"""
        with TmpTree() as t:
            svc = make_service(t / "svc-root")
            pin = t / "home" / "service.home"
            result = register_home.register(pin_path=pin, root=svc)
            self.assertTrue(result["ok"])
            self.assertEqual(Path(pin.read_text(encoding="utf-8").strip()).resolve(),
                             svc.resolve())
            self.assertEqual(result["root"], str(svc.resolve()))

    def test_rejects_when_marker_missing(self):
        """服务 marker 缺失（残缺部署）→ 拒绝写入，钉扎文件不被创建。"""
        with TmpTree() as t:
            broken = t / "broken"
            broken.mkdir()
            pin = t / "home" / "service.home"
            with self.assertRaises(register_home.RegisterError):
                register_home.register(pin_path=pin, root=broken)
            self.assertFalse(pin.exists())

    def test_creates_missing_home_dir(self):
        """钉扎文件父目录不存在 → 自动创建。"""
        with TmpTree() as t:
            svc = make_service(t / "svc")
            pin = t / "deep" / "nested" / "service.home"
            result = register_home.register(pin_path=pin, root=svc)
            self.assertTrue(result["ok"])

    def test_cli_main_writes_pin(self):
        """main() 契约：exit 0 且钉扎文件落盘（注入临时 pin 与 root）。"""
        with TmpTree() as t:
            svc = make_service(t / "svc")
            pin = t / "home" / "service.home"
            rc = register_home.main(["--pin", str(pin), "--root", str(svc)])
            self.assertEqual(rc, 0)
            self.assertEqual(Path(pin.read_text(encoding="utf-8").strip()).resolve(),
                             svc.resolve())

    def test_real_service_root_self_register_valid(self):
        """回归：本服务真实根自检通过（脚本与 CLI 同目录共存）。"""
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "scripts" / "control_plane.py").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
