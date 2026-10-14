#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_daily.py — 总客户端驾驶舱 每日自动同步编排器（纯 Python，不依赖 MCP）。

职责：
  1. 由 Agent（自动化）先用 kdocs MCP 工具把每个站的原始数据抓取并存盘为
     raw_<站名>_ws<N>.txt / .json（格式即 kdocs get_typed_value 返回体）。
  2. 本脚本负责「抓取之后」的全部确定性处理：
       --station <站名> : 运行 process_station.py 产出 <站名>.json
       --build           : 运行 build_all_data.py 汇总为 all_data.json
       --deploy          : 运行 deploy.py 推送到 GitHub Pages
       (无参数)           : 依次执行 全部站 process + build + deploy

设计要点（避免上下文爆炸）：
  Agent 在自动化里「一站一站」地抓取+process，本脚本只做本地磁盘 IO 与计算，
  不把任何 kdocs 原始载荷放进对话上下文。
"""
import os
import sys
import json
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACT = os.path.join(HERE, "_extract")
# ★ 2026-10-14 云端化：优先用「当前正在运行的解释器」（GitHub Actions 上是 runner 的 python），
#   本地跑时等价于原 Windows Python；如需强制指定可用环境变量 PY_EXE 覆盖。
PY = (os.environ.get("PY_EXE") or sys.executable
      or r"C:\Users\DELL\.workbuddy\binaries\python\versions\3.13.12\python.exe")

# 15 站 名称 -> kdocs file_id。file_id 仅供抓取阶段（Agent/MCP）使用；
# 本脚本处理阶段只认「站名 + _extract 下的 raw_<站名>_ws<N> 文件」。
STATIONS = [
    ("秦岭",   "cjtgYCHSQnXk"),
    ("宁陕",   "cicYzlWSz42i"),
    ("洋县",   "cgsMLS7EDSgK"),
    ("汉中",   "ck57MuopxMGq"),
    ("富平",   "csj1JZvB1mXk"),
    ("韩城",   "crsUIwTN1ddF"),
    ("富县",   "cn5F4fFtcW3O"),
    ("南沙",   "crAzwpzEpIt9"),
    ("华山",   "cmfB737g3GTQ"),
    ("白河",   "cjnF7ztNQSjk"),
    ("旬阳",   "caekiQMG3Y1b"),
    ("略阳",   "cdp2sa8yXIU3"),
    ("玉华宫", "cqCjwvRElbGF"),
    ("照金",   "ctuoOPC4jYCE"),
    ("天汉水城", "ceSbFX0AoyL0"),
]

TANK_WS = list(range(3, 15))  # ws3..ws14 = 12 个罐表


def run(py_script, args, tolerant=False):
    cmd = [PY, os.path.join(EXTRACT, py_script)] + [str(a) for a in args]
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=EXTRACT, capture_output=True, text=True, encoding="utf-8")
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    if r.returncode != 0:
        if tolerant:
            print("  (步骤 %s 返回非0，已容忍，继续)" % py_script)
            return r
        raise RuntimeError("script failed: %s\n%s" % (py_script, r.stderr))
    return r


def process_station(name):
    ws2 = os.path.join(EXTRACT, "raw_%s_ws2.json" % name)
    if not os.path.exists(ws2):
        # 兜底：部分旧抓取用 .txt
        ws2_alt = os.path.join(EXTRACT, "raw_%s_ws2.txt" % name)
        ws2 = ws2_alt if os.path.exists(ws2_alt) else None
    if not ws2:
        print("  ! skip %s: 缺少 ws2 原始文件" % name)
        return False
    tank_paths = []
    for w in TANK_WS:
        p = os.path.join(EXTRACT, "raw_%s_ws%d.txt" % (name, w))
        if not os.path.exists(p):
            p = os.path.join(EXTRACT, "raw_%s_ws%d.json" % (name, w))
        if os.path.exists(p):
            tank_paths.append(p)
        else:
            print("  ! warn %s: 缺少 ws%d" % (name, w))
    if not tank_paths:
        print("  ! skip %s: 无任何罐表原始文件" % name)
        return False
    run("process_station.py", [name, ws2] + tank_paths)
    return True


def cmd_clean():
    """预处理：剥离各站罐表未来日期(>TODAY)的 D~I 实体列脏数据。
    无论云端重抓带回多少垃圾，本步保证进入 process 的 raw 已干净，
    从而 dashboard 永久免疫未来数据污染（与 process_station 的 TODAY 过滤双保险）。"""
    print("=== clean future garbage from raw tank files ===")
    run("clean_future_rows.py", [])


def cmd_station(name):
    print("=== process station:", name, "===")
    ok = process_station(name)
    print("  done" if ok else "  SKIPPED")


def cmd_build():
    print("=== build all_data.json ===")
    run("build_all_data.py", [])


def cmd_deploy():
    print("=== deploy to GitHub Pages ===")
    # deploy.py 位于 dashboard/ 根目录（与 sync_daily.py 同级），不在 _extract/ 下
    dep = os.path.join(HERE, "deploy.py")
    if not os.path.exists(dep):
        print("  ! deploy.py 未找到:", dep)
        return
    # tolerant：缺少凭据时 deploy.py 会退出非0，这里容忍并提示（不影响前序步骤）
    cmd = [PY, dep]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, encoding="utf-8")
    if r.stdout:
        sys.stdout.write(r.stdout)
    if r.stderr:
        sys.stderr.write(r.stderr)
    if r.returncode != 0:
        print("  (deploy 返回非0，已容忍，继续)")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("--all", "all"):
        # 第一步：剥离各站罐表未来日期脏数据（免疫云端重抓污染）
        cmd_clean()
        for name, _ in STATIONS:
            cmd_station(name)
        cmd_build()
        cmd_deploy()
        return
    if args[0] == "--clean":
        cmd_clean()
        return
    # ★ 2026-10-14 云端化新增：只做「清洗 + 全站 process」，不 build/deploy。
    #   供 GitHub Actions 在「汇总」与「发布」之间插入体检关卡，取数失败时不推空数据上线。
    if args[0] == "--process-all":
        cmd_clean()
        for name, _ in STATIONS:
            cmd_station(name)
        return
    if args[0] == "--station" and len(args) >= 2:
        cmd_clean()
        cmd_station(args[1])
        return
    if args[0] == "--build":
        cmd_build()
        return
    if args[0] == "--deploy":
        cmd_deploy()
        return
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
