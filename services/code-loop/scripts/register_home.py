#!/usr/bin/env python3
"""register_home.py — 把本服务根写入机器本地钉扎文件（~/.code-loop/service.home）。

薄 skill 的 shim 在 walk-up 找不到 services/ 时（skill 被拷贝到无 services/ 的
检出）读取该钉扎兜底。写入前校验服务根 marker，marker 缺失拒绝写入（fail-closed）。

用法：
  python register_home.py                    # 钉扎本脚本所在服务，写默认路径
  python register_home.py --root <服务根> --pin <钉扎路径>
输出 JSON；失败 exit 1。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_PIN = Path.home() / ".code-loop" / "service.home"


class RegisterError(RuntimeError):
    pass


def register(pin_path=None, root=None):
    """校验服务根并写入钉扎文件；返回结果 dict。测试注入 pin_path/root。"""
    root = Path(root) if root else Path(__file__).resolve().parents[1]
    if not (root / "scripts" / "control_plane.py").is_file():
        raise RegisterError(
            f"not a code-loop service root (missing scripts/control_plane.py): {root}")
    pin_path = Path(pin_path) if pin_path else DEFAULT_PIN
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(root.resolve())
    pin_path.write_text(resolved + "\n", encoding="utf-8")
    return {"ok": True, "pin": str(pin_path), "root": resolved}


def main(argv=None):
    p = argparse.ArgumentParser(prog="register_home.py")
    p.add_argument("--root", help="service root (default: this script's service)")
    p.add_argument("--pin", help=f"pin file path (default: {DEFAULT_PIN})")
    args = p.parse_args(argv)
    try:
        result = register(pin_path=args.pin, root=args.root)
    except Exception as exc:
        print(json.dumps({"ok": False, "code": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
