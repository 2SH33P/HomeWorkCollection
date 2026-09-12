#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键更新（跨平台，最稳）：双击本文件或在项目目录运行  python update.py
拉取 GitHub 最新代码 -> 自动切到 main 分支 -> 提示重启服务。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PUB = "https://github.com/2SH33P/HomeWorkCollection.git"


def git(args, timeout=180):
    try:
        return subprocess.run(["git", "-C", ROOT] + args, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        return None
    except Exception as e:
        print("[错误] 执行 git 失败:", e)
        return None


def out_of(r):
    return ((r.stdout or "") + (r.stderr or "")).strip()


def main():
    print("=" * 46)
    print("  错题收集工具 - 一键更新")
    print("=" * 46)
    r = git(["--version"])
    if r is None or r.returncode != 0:
        print("[错误] 系统里找不到 git 命令。")
        print("       请安装 Git for Windows，或直接到")
        print("       https://github.com/2SH33P/HomeWorkCollection")
        print("       下载新版覆盖本目录。")
        return 1

    print("[1/3] 拉取远程代码…")
    r = git(["fetch", "origin", "main"])
    if r is None or r.returncode != 0:
        print("      直连远程失败，改用公开地址重试…")
        r = git(["fetch", PUB, "main"])
        if r is None or r.returncode != 0:
            print("[错误] 拉取失败：", out_of(r)[-200:])
            print("       国内网络请在代理软件的全局/TUN 模式下重试，")
            print("       或在网页「设置 → 更新代理」填代理后点“立即更新”。")
            return 1

    print("[2/3] 切换到 main 分支…")
    git(["checkout", "-B", "main", "FETCH_HEAD"])       # 兼容本地旧分支叫 master 的情况
    r = git(["reset", "--hard", "FETCH_HEAD"])
    if r is None or r.returncode != 0:
        print("[错误] 对齐版本失败：", out_of(r)[-200:])
        return 1

    print("[3/3] 完成，当前版本：")
    r = git(["log", "--oneline", "-1"])
    print("      " + out_of(r))
    print()
    print(">>> 请重启服务：关掉旧的黑窗口，重新双击 start.bat；")
    print("    然后刷新浏览器页面（手机上重新打开页面）。")
    return 0


if __name__ == "__main__":
    code = main()
    try:
        input("\n按回车键关闭…")
    except EOFError:
        pass
    sys.exit(code)
