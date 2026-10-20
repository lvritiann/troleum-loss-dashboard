#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_kdocs.py — 用 kdocs-cli 替代不可用的 mcp__kdocs__sheet.get_typed_value，
把每个站的 13 个工作表抓取并重建为 process_station.py 所需的 canonical raw 文件：

  raw_<站>_ws2.json   (worksheet_id=2, 收油统计表, 范围 A1:M700)
  raw_<站>_ws<N>.txt  (worksheet_id=3..14, 12 个罐表, 范围 A1:N400)

canonical 格式（与历史 MCP 抓取一致）：
  {"code":0,"message":"成功","data":{"typedValues":[ {"type":..,"value":..}, ... ]}}
  其中 typedValues 为「逐行、行内从左到右」的稠密平铺数组，空单元格以
  {"type":"string","value":""} 占位（process_station 靠它做行定界与列索引）。

kdocs-cli get-range-data 返回的是稀疏 cells（detail.rangeData），每项带
rowFrom/colFrom 坐标与 understandableType{type,value}。本脚本据此重建稠密数组。

用法：
  python fetch_kdocs.py [站名]      # 不传站名则抓全部 15 站
"""
import json
import subprocess
import sys
import os
import time
import re
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
# ★ 2026-10-14 云端化：CLI 路径与令牌支持环境变量覆盖（GitHub Actions 无本地 keychain）。
#   KDOCS_CLI  ：kdocs-cli.exe 路径（云端指向仓库内 bin/kdocs-cli.exe）
#   KDOCS_TOKEN：212 字符 kdocs API 令牌；有值时每次调用显式带 --token（CI 无 keychain，必须走这条）
#   两者都不设时，行为与原来完全一致（本地开发不受影响）。
KDOCS = os.environ.get("KDOCS_CLI") or r"C:\Users\DELL\.local\bin\kdocs-cli.exe"
KDOCS_TOKEN = (os.environ.get("KDOCS_TOKEN") or "").strip()

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

# ★ 2026-10-20 修：WS2 原为 699 行，截断了超出部分 ⇒ 页面卸油数据大面积滞后
#   （华山实际 1149 行、南沙 920 行，700 行只覆盖到 6/14 / 8/03）。
#   收油统计表是长期累积表，行数只增不减 ⇒ 上限必须留足余量。
#   2026-10-20 实测各站最大尾行 = 华山 1148（0-based），设 1300 留余量。
WS2_RANGE = (1299, 12)  # A1:M1300 -> rowTo=1299, colTo=12
TANK_RANGE = (399, 13)  # A1:N400 -> rowTo=399, colTo=13


def fetch_once(file_id, ws, rowTo, colTo):
    """单次抓取；返回 detail.rangeData（可能为空列表）。

    注意：直接把 JSON 作为命令行参数传入（不落盘临时文件），
    以避免触发沙箱 safe-delete 钩子（批量删除会阻断进程）。
    """
    payload = {
        "file_id": file_id,
        "worksheet_id": ws,
        "range": {"rowFrom": 0, "rowTo": rowTo, "colFrom": 0, "colTo": colTo},
    }
    arg = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    cmd = [KDOCS]
    if KDOCS_TOKEN:
        cmd += ["--token", KDOCS_TOKEN]
    cmd += ["sheet", "get-range-data", arg]
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    out = (r.stdout or "").strip()
    if not out:
        err = (r.stderr or "").strip()
        raise RuntimeError("empty output; stderr=%s" % err[:300])
    d = json.loads(out)
    if d.get("code") != 0:
        raise RuntimeError("kdocs code=%s msg=%s" % (d.get("code"), d.get("message")))
    det = (d.get("data") or {}).get("detail") or {}
    return det.get("rangeData") or []


def fetch_with_retry(file_id, ws, rowTo, colTo):
    last = None
    for attempt in range(4):  # 首次 + 最多 3 次重试
        try:
            rd = fetch_once(file_id, ws, rowTo, colTo)
            return rd
        except Exception as e:
            msg = str(e)
            last = e
            if "429" in msg or "限频" in msg or "熔断" in msg:
                wait = 30
                m = re.search(r"(\d+)", msg)
                if m:
                    wait = min(max(int(m.group(1)), 5), 120)
                print("    [429] ws%d 第%d次重试，等待 %ds" % (ws, attempt + 1, wait))
                time.sleep(wait)
                continue
            # 其它错误：重试 1 次（可能瞬时网络问题）
            if attempt < 1:
                time.sleep(3)
                continue
            raise
    raise last or RuntimeError("fetch failed ws%d" % ws)


def reconstruct(rd, rowTo, colTo):
    rows = rowTo + 1
    cols = colTo + 1
    empty = {"type": "string", "value": ""}
    grid = [[dict(empty) for _ in range(cols)] for _ in range(rows)]
    for c in rd:
        rf = c.get("rowFrom", 0)
        cf = c.get("colFrom", 0)
        if 0 <= rf < rows and 0 <= cf < cols:
            ut = c.get("understandableType")
            if isinstance(ut, dict) and "type" in ut:
                v = ut.get("value")
                grid[rf][cf] = {"type": ut["type"], "value": v}
    flat = []
    for row in grid:
        flat.extend(row)
    return flat


def fetch_station(name, file_id, log):
    ok = True
    # ws2 收油统计表
    try:
        rd = fetch_with_retry(file_id, 2, *WS2_RANGE)
        flat = reconstruct(rd, *WS2_RANGE)
        out = os.path.join(HERE, "raw_%s_ws2.json" % name)
        json.dump({"code": 0, "message": "成功", "data": {"typedValues": flat}},
                  open(out, "w", encoding="utf-8"), ensure_ascii=False)
        log.append("  ws2(收油): %d cells" % len(flat))
    except Exception as e:
        ok = False
        log.append("  ws2(收油) FAIL: %s" % e)
    # ws3..ws14 罐表
    for ws in range(3, 15):
        try:
            rd = fetch_with_retry(file_id, ws, *TANK_RANGE)
            flat = reconstruct(rd, *TANK_RANGE)
            out = os.path.join(HERE, "raw_%s_ws%d.txt" % (name, ws))
            json.dump({"code": 0, "message": "成功", "data": {"typedValues": flat}},
                      open(out, "w", encoding="utf-8"), ensure_ascii=False)
            log.append("  ws%d(罐): %d cells" % (ws, len(flat)))
        except Exception as e:
            ok = False
            log.append("  ws%d(罐) FAIL: %s" % (ws, e))
        time.sleep(0.35)  # 温和限速，降低 429 概率
    return ok


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    stations = [s for s in STATIONS if (only is None or s[0] == only)]
    if only and not stations:
        print("未知站名:", only)
        sys.exit(2)
    print("开始抓取 %d 个站" % len(stations))
    all_ok = True
    for name, fid in stations:
        log = []
        t0 = time.time()
        try:
            ok = fetch_station(name, fid, log)
        except Exception as e:
            ok = False
            log.append("  !! 整站异常: %s" % e)
            traceback.print_exc()
        dt = time.time() - t0
        all_ok = all_ok and ok
        print("[%s] %s 用时 %.1fs" % ("OK" if ok else "FAIL", name, dt))
        for line in log:
            print(line)
    print("全部完成, 整体:%s" % ("OK" if all_ok else "PARTIAL_FAIL"))
    sys.exit(0)


if __name__ == "__main__":
    main()
