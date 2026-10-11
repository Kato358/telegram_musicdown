#!/usr/bin/env python3
"""Windows 测试运行脚本：一键跑起 telegram-musicdown。

用法（在本目录）：
    python run.py            # 等价 run：缺依赖自动装、缺前端产物自动构建，然后启动
    python run.py run        # 启动服务（默认 http://127.0.0.1:8787）
    python run.py build      # 只构建前端（web/dist）
    python run.py test       # pytest
    python run.py lint       # ruff + mypy
    python run.py deps       # 只安装依赖（含 dev 组）

环境变量（可选，均可被外部覆盖）：
    TGM_WEB_HOST  默认 127.0.0.1（本机免密；改 0.0.0.0 必须设 TGM_WEB_LOGIN_SECRET）
    TGM_WEB_PORT  默认 8787
    TGM_BASE_DIR  默认脚本所在目录（数据布局：downloads/ data/sessions/ data/logs/ data/temp/；
                  可用 config.yaml 的 save/session/temp_directory 或同名 TGM_* env 改为绝对路径）

与 Linux/Docker 的 run.sh 对应；Docker 部署见 docker-compose.yml。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 发行包名 → 导入名（用于探测是否已安装）
IMPORT_NAMES = {
    "pyyaml": "yaml",
    "kurigram": "pyrogram",  # Kurigram 是 Pyrogram 分支，沿用上游导入名
}

# tgcrypto：Kurigram 加解密加速（等价于 kurigram[fast] 里的那一项），非必需。
# Python 3.13 常无预编译 wheel、需 MSVC 才能装，因此单独作为「尽力安装」的可选依赖：
# 失败只告警，不阻断启动。
OPTIONAL_REQS = ["tgcrypto"]

# Windows 控制台默认 GBK，中文路径/日志会 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def _log(msg: str) -> None:
    print(f"[run] {msg}", flush=True)


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    """执行子命令；继承 stdio，返回退出码。"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    _log("$ " + " ".join(cmd))
    try:  # noqa: S603  命令均由本脚本构造，非外部输入
        return subprocess.call(cmd, cwd=cwd or ROOT, env=env)
    except FileNotFoundError as e:
        _log(f"命令不存在：{cmd[0]}（{e}）")
        return 127


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def _requirements(dev: bool = False) -> list[str]:
    """从 pyproject 取核心依赖；dev=True 追加 dev 组（pytest/ruff/mypy/httpx）。"""
    data = _pyproject()
    reqs = list(data["project"].get("dependencies", []))
    if dev:
        reqs.extend(data.get("dependency-groups", {}).get("dev", []))
    return reqs


def _missing(reqs: list[str]) -> list[str]:
    """返回尚未安装的依赖（按导入名探测；无法映射的按包名探测）。"""
    out = []
    for req in reqs:
        pkg = req.split("[")[0].split(">")[0].split("=")[0].split("<")[0].strip().lower()
        mod = IMPORT_NAMES.get(pkg, pkg.replace("-", "_"))
        if importlib.util.find_spec(mod) is None:
            out.append(req)
    return out


def _pip_install(reqs: list[str], only_binary: bool = False) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if only_binary:
        cmd.append("--only-binary=:all:")  # 无预编译 wheel 立即失败，不触发本地编译
    return _run([*cmd, *reqs]) == 0


def install_deps(dev: bool = False) -> bool:
    """安装核心依赖（失败即中止）；随后尽力安装可选依赖（失败仅告警）。"""
    miss = _missing(_requirements(dev))
    if miss:
        _log("安装缺失依赖：" + ", ".join(miss))
        if not _pip_install(miss):
            return False
    else:
        _log("核心依赖齐备")
    opt = _missing(OPTIONAL_REQS) if not dev else []
    if opt:
        _log("尝试安装可选加速依赖：" + ", ".join(opt) + "（无预编译包则跳过）")
        if not _pip_install(opt, only_binary=True):
            _log("可选依赖 " + ", ".join(opt) + " 不可用，跳过（Kurigram 仍正常，速度略慢）")
    return True


def build_frontend(force: bool = False) -> bool:
    """前端产物缺失（或 force）时构建；已存在则跳过。"""
    dist = ROOT / "web" / "dist" / "index.html"
    if dist.exists() and not force:
        _log(f"前端产物已存在：{dist.relative_to(ROOT)}")
        return True
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm or not shutil.which("node"):
        _log("未找到 node/npm，跳过前端构建（仅 API 可用，Web 界面无静态资源）")
        return False
    web = ROOT / "web"
    _log("构建前端（npm install + npm run build）…")
    if not (web / "node_modules").exists() and _run(
        [npm, "install", "--no-audit", "--no-fund"], cwd=web
    ):
        _log("npm install 失败")
        return False
    if _run([npm, "run", "build"], cwd=web):
        _log("前端构建失败")
        return False
    _log("前端构建完成")
    return True


def start() -> int:
    # 绑定 0.0.0.0 却没人给口令时兜底的口令：只够防误触，必须让用户看见这件事
    default_secret = "123"  # noqa: S105  见上方注释：兜底口令，仅防误触
    os.environ.setdefault("TGM_WEB_HOST", "0.0.0.0")  # noqa: S104  公网面板默认地址，无口令会在下方中止
    os.environ.setdefault("TGM_WEB_PORT", "8787")
    os.environ.setdefault("TGM_WEB_LOGIN_SECRET", default_secret)
    os.environ.setdefault("TGM_BASE_DIR", str(ROOT))

    host, port = os.environ["TGM_WEB_HOST"], os.environ["TGM_WEB_PORT"]
    if host in ("0.0.0.0", "::") and not os.environ.get("TGM_WEB_LOGIN_SECRET"):  # noqa: S104
        _log("绑定 0.0.0.0 必须设置 TGM_WEB_LOGIN_SECRET（FR-WEB-02 强制密码），已中止")
        return 1

    _log(f"启动 telegram-musicdown → http://{host}:{port}")
    if os.environ.get("TGM_WEB_LOGIN_SECRET") == default_secret:
        _log(f"警告：未显式设置 TGM_WEB_LOGIN_SECRET，正使用默认口令 {default_secret!r}；"
             f"监听 {host} 时同网段任何人都能登录，请改掉它")
    _log(f"数据目录 TGM_BASE_DIR={os.environ['TGM_BASE_DIR']}")
    _log("首次使用：浏览器打开上面的地址，在 /setup 向导内配置 api_id/api_hash 并登录 Telegram")
    _log("Ctrl+C 停止")
    return _run([sys.executable, "-m", "app"])


def _cmd_build() -> int:
    return 0 if build_frontend(force=True) else 1


def _cmd_test() -> int:
    return _run([sys.executable, "-m", "pytest", "-q"])


def _cmd_lint() -> int:
    if _run([sys.executable, "-m", "ruff", "check", "."]):
        return 1
    return _run([sys.executable, "-m", "mypy"])


def _cmd_deps() -> int:
    return 0 if install_deps(dev=True) else 1


COMMANDS = {
    "run": start,
    "build": _cmd_build,
    "test": _cmd_test,
    "lint": _cmd_lint,
    "deps": _cmd_deps,
}

USAGE = "用法：python run.py [run|build|test|lint|deps]"


def _prepare(cmd: str) -> bool:
    """命令前置：版本检查 + 依赖/前端准备。"""
    if sys.version_info < (3, 11):
        _log(f"需要 Python ≥ 3.11，当前 {sys.version.split()[0]}")
        return False
    if cmd in ("run", "build"):
        if not install_deps():
            return False
        if cmd == "run":
            build_frontend()  # 构建失败不阻断：API 仍可用
    elif cmd in ("test", "lint") and not install_deps(dev=True):
        return False
    return True


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "run").lower()
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd not in COMMANDS:
        _log(f"未知命令：{cmd}")
        print(USAGE)
        return 2
    if not _prepare(cmd):
        return 1
    try:
        return COMMANDS[cmd]()
    except KeyboardInterrupt:
        _log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv))
