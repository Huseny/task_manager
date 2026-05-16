#!/usr/bin/env python3
"""
validate_package.py 自动更新包装脚本

每次调用前自动检测远程仓库中 validate_package.py 是否有更新:
- 有更新则同步到本地后执行
- 无更新则直接执行
所有参数透传给 validate_package.py

用法与 validate_package.py 完全一致:
  python run_validate.py /path/to/TASK-001
  python run_validate.py TASK-001 --repair
  python run_validate.py TASK-001 --convert-legacy
"""

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================== 配置区域 ====================
# GitLab（优先）
GITLAB_URL = "https://gitlab.mindflow.com.cn"
PROJECT_ID = "5736"
BRANCH = "main"

# GitHub（备用）
GITHUB_REPO = "ForgetH3r/repo_task_validate"
GITHUB_BRANCH = "main"
# ==============================================================================

REMOTE_FILE_PATH = "validate_package.py"
IS_WINDOWS = sys.platform == "win32"

# The local dir should be the current folder
LOCAL_DIR = Path(__file__).parent.resolve()
LOCAL_FILE = LOCAL_DIR / "validate_package.py"


def _load_dotenv(path=None):
    """Load simple KEY=VALUE pairs from a .env file into os.environ if not already set."""
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(LOCAL_DIR / ".env")
        candidates.append(LOCAL_DIR.parent / ".env")

    for p in candidates:
        try:
            if not p or not p.exists():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
            return
        except Exception:
            continue


# Try to load a .env file from the project before reading tokens
_load_dotenv()

# Tokens are read from environment variables (optionally via .env)
GITLAB_TOKEN = os.environ.get("VALIDATOR_GITLAB_TOKEN", "")
GITHUB_TOKEN = os.environ.get("VALIDATOR_GITHUB_TOKEN", "")


GITLAB_MAX_RETRIES = 3


def _gitlab_get(url, params=None, raw=False):
    """GitLab API 请求，最多重试 GITLAB_MAX_RETRIES 次后抛出异常"""
    headers = {"PRIVATE-TOKEN": GITLAB_TOKEN}
    last_err = None
    for attempt in range(1, GITLAB_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, headers=headers, params=params, timeout=15, verify=False
            )
            resp.raise_for_status()
            return resp.content if raw else resp.json()
        except Exception as e:
            last_err = e
            if attempt < GITLAB_MAX_RETRIES:
                print(f"  [GitLab] 第{attempt}次请求失败，3秒后重试... ({e})")
                time.sleep(3)
    raise last_err


def _github_get(url, params=None, raw=False):
    """GitHub API 请求，最多重试 3 次后抛出异常"""
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3.raw" if raw else "application/vnd.github.v3+json",
    }
    last_err = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            return resp.content if raw else resp.json()
        except Exception as e:
            last_err = e
            if attempt < 3:
                print(f"  [GitHub] 第{attempt}次请求失败，3秒后重试... ({e})")
                time.sleep(3)
    raise last_err


def _gitlab_get_sha256():
    """从 GitLab 获取远程文件 content_sha256"""
    encoded = quote(REMOTE_FILE_PATH, safe="")
    url = f"{GITLAB_URL}/api/v4/projects/{quote(str(PROJECT_ID), safe='')}/repository/files/{encoded}"
    meta = _gitlab_get(url, {"ref": BRANCH})
    return meta.get("content_sha256", "")


def _gitlab_download():
    """从 GitLab 下载文件原始内容"""
    encoded = quote(REMOTE_FILE_PATH, safe="")
    url = f"{GITLAB_URL}/api/v4/projects/{quote(str(PROJECT_ID), safe='')}/repository/files/{encoded}/raw"
    return _gitlab_get(url, {"ref": BRANCH}, raw=True)


def _github_get_sha256():
    """从 GitHub 获取远程文件 SHA-256（下载内容后本地计算）"""
    content = _github_download()
    return hashlib.sha256(content).hexdigest(), content


def _github_download():
    """从 GitHub 下载文件原始内容"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{REMOTE_FILE_PATH}"
    return _github_get(url, {"ref": GITHUB_BRANCH}, raw=True)


def get_remote_info():
    """获取远程文件的 sha256 和内容，优先 GitLab，失败回退 GitHub

    Returns: (sha256, content_or_None, source_name)
      - 仅检查阶段: content_or_None 为 None
      - 需要下载时再单独调用 download_remote_file
    """
    # Try GitHub first, then fall back to GitLab
    try:
        print("  [GitHub] 正在连接...")
        sha, content = _github_get_sha256()
        return sha, content, "GitHub"
    except Exception as e:
        print(f"  [GitHub] 不可用({e})，尝试 GitLab...")

    try:
        print("  [GitLab] 正在连接...")
        sha = _gitlab_get_sha256()
        return sha, None, "GitLab"
    except Exception as e:
        print(f"  [GitLab] 不可用({e})，无法获取远程文件")
        raise


def download_remote_file(source):
    """根据来源下载文件"""
    if source == "GitHub":
        return _github_download()
    if source == "GitLab":
        return _gitlab_download()
    # Default to GitHub
    return _github_download()


def _get_real_user():
    """获取实际用户（sudo 场景下从 SUDO_USER 取，否则取当前用户）"""
    return os.environ.get("SUDO_USER") or os.getlogin()


def _write_file(content, dest):
    """写入文件，先尝试普通权限，失败则回退 sudo"""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except PermissionError:
        print("  [权限] 普通写入失败，尝试 sudo...")
        real_user = _get_real_user()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            subprocess.run(["sudo", "mkdir", "-p", str(dest.parent)], check=True)
            subprocess.run(["sudo", "cp", tmp_path, str(dest)], check=True)
            subprocess.run(["sudo", "chown", f"{real_user}:{real_user}", str(dest.parent)], check=True)
            subprocess.run(["sudo", "chown", f"{real_user}:{real_user}", str(dest)], check=True)
        finally:
            os.unlink(tmp_path)


def get_local_sha256():
    """计算本地 validate_package.py 的 SHA-256"""
    if not LOCAL_FILE.is_file():
        return ""
    return hashlib.sha256(LOCAL_FILE.read_bytes()).hexdigest()


def sync_if_needed():
    """比对远程与本地版本，有差异则更新（优先 GitLab，失败回退 GitHub）"""
    print("[AutoUpdate] 检查 validate_package.py 是否有更新...")

    remote_sha, cached_content, source = get_remote_info()
    local_sha = get_local_sha256()

    if remote_sha and remote_sha == local_sha:
        print(f"[AutoUpdate] 本地已是最新版本（来源: {source}），无需更新")
        return

    if not remote_sha:
        print("[AutoUpdate] 无法获取远程版本信息，跳过更新检查")
        return

    reason = "本地文件不存在" if not local_sha else "检测到远程更新"
    print(f"[AutoUpdate] {reason}，正在从 {source} 同步...")

    content = cached_content if cached_content is not None else download_remote_file(source)
    _write_file(content, LOCAL_FILE)
    print(f"[AutoUpdate] 已更新: {LOCAL_FILE}")


def main():
    # 1. 检查并同步
    try:
        sync_if_needed()
    except Exception as e:
        print(f"[AutoUpdate] 更新检查失败({e})，使用本地版本继续执行")

    print()

    # 2. 本地文件必须存在才能执行
    if not LOCAL_FILE.is_file():
        print(f"错误: {LOCAL_FILE} 不存在且无法从远程获取")
        return 1

    # 3. 透传参数，执行 validate_package.py
    result = subprocess.run(
        [sys.executable, str(LOCAL_FILE)] + sys.argv[1:],
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
