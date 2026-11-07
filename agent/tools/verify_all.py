"""#48 L0 聚合验证入口（只读，不改任何源文件）。

一条命令跑完以下 4 项，汇总成 PASS/FAIL 列表与总退出码：
  1) agent/main.py --check
  2) .workbuddy/artifacts/31-verify.py
  3) git diff --check
  4) git status --short

用法（用带 maafw 的解释器）：
    python agent\tools\verify_all.py

主 agent 改完代码后直接跑这一条即可；总退出码 0 = 全 PASS，1 = 有 FAIL。
只读：只发起子进程与打印，不写、不改任何源文件 / 配置 / 模板。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "agent" / "main.py"
VERIFY = ROOT / ".workbuddy" / "artifacts" / "31-verify.py"
PY = sys.executable

CHECKS = [
    ("main.py --check", [PY, str(MAIN), "--check"]),
    ("31-verify.py", [PY, str(VERIFY)]),
    ("git diff --check", ["git", "diff", "--check"]),
    ("git status --short", ["git", "status", "--short"]),
]


def main() -> int:
    print("=" * 70)
    print("#48 L0 聚合验证（只读）  cwd =", ROOT)
    print("=" * 70)
    results = []
    for name, cmd in CHECKS:
        print(f"\n--- {name} ---")
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"  [FAIL] 无法执行: {exc}")
            results.append((name, False))
            continue
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        lines = [ln for ln in out.splitlines() if ln.strip()]
        ok = p.returncode == 0
        results.append((name, ok))
        if ok:
            for ln in lines[-6:]:
                print("   ", ln)
            if not lines:
                print("    (无输出 / 无空白错误)")
        else:
            for ln in lines:                      # 失败时打印全部，便于排查
                print("   ", ln)
            print(f"    [exit {p.returncode}]")

    print("\n" + "=" * 70)
    all_ok = True
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        all_ok = all_ok and ok
    print("=" * 70)
    n_pass = sum(1 for _, o in results if o)
    print(f"总计: {n_pass}/{len(results)} PASS  总退出码: {0 if all_ok else 1}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
