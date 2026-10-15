# -*- coding: utf-8 -*-
"""_sync_form_to_tanks.py —— kdocs-cli 版液位回写（读 A 表 → 写各站罐表 C/D/F）

背景（2026-10-15 根治定案）：
  表单「液位仪数据采集表」的真实落点是多维表 A 表（.dbt），不是落表 .ksheet。
  原回写脚本 airscript_liquid_level_writeback.js 读的是落表 cmOCi38pohLr（永远空），
  且 AirScript 2.0 运行时根本不支持读 .dbt ⇒ 改由本脚本（kdocs-cli）直读 A 表。

数据源：A 表「液位仪数据采集表」
  file_id = HjR51pWq9xMTdtJwgEPmxx5SUw1a2WVPc , sheet_id = 2
  用 `dbsheet list-records` 读取全部提交记录。

映射：
  站点(J)  → 优化表 file_id（OPT）
  罐号(L)  → 罐表 sheetId（endswith 匹配 '🛢️ 东站1#罐'，sheetId>=3）
  日期(K)  → 罐表 A 列行号（日期序列号匹配）

写（列 0-based）：
  C(2) 试枪回罐(L)  ← 表单「试枪回罐(L)」（字符串，转浮点）
  D(3) 实际库存(L)  ← 表单「实际库存(L)」
  F(5) 交班温度(℃)  ← 表单「交班温度(℃)」
  规则：
    · 表单该列为空/None → 不写该列（不误清空）
    · 幂等：目标同值跳过
    · 危险闸门：D/F 源值 0 且目标非零真值 → 跳过（不抹真值）；C 源 0 是常态放行
    · 去重：同(站|罐|日期) 后提交者胜
    · 写 API `sheet update-range-data` 分片 ≤100

本机 / 云端通吃：
  kdocs-cli.exe 路径优先取环境变量 KDOCS_CLI（云端 GitHub Actions 指仓库 bin/），
  默认回退项目内 bin/kdocs-cli.exe。

用法：
  python scripts/_sync_form_to_tanks.py --dry              # 只读预览（不写）
  python scripts/_sync_form_to_tanks.py                     # 实写全部
  python scripts/_sync_form_to_tanks.py --stations 秦岭,白河
  python scripts/_sync_form_to_tanks.py --since 3           # 只处理最近 3 天的提交
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KDOCS = os.environ.get("KDOCS_CLI") or os.path.join(os.path.dirname(HERE), "bin", "kdocs-cli.exe")
KDOCS_TOKEN = (os.environ.get("KDOCS_TOKEN") or "").strip()  # 云端 CI 无 keychain，走 --token

# A 表（表单真实落点）
A_FILE = "HjR51pWq9xMTdtJwgEPmxx5SUw1a2WVPc"
A_SHEET = 2

# 各站优化表 file_id（写入目标，与 sales_sync / backfill 完全一致）
OPT = {
    "秦岭": "cjtgYCHSQnXk", "宁陕": "cicYzlWSz42i", "洋县": "cgsMLS7EDSgK",
    "汉中": "ck57MuopxMGq", "富平": "csj1JZvB1mXk", "韩城": "crsUIwTN1ddF",
    "富县": "cn5F4fFtcW3O", "南沙": "crAzwpzEpIt9", "华山": "cmfB737g3GTQ",
    "白河": "cjnF7ztNQSjk", "旬阳": "caekiQMG3Y1b", "略阳": "cdp2sa8yXIU3",
    "玉华宫": "cqCjwvRElbGF", "照金": "ctuoOPC4jYCE", "天汉水城": "ceSbFX0AoyL0",
}
SKIP_HINTS = ("工作台", "使用说明", "收油统计", "损溢启动", "月度损溢", "全站损溢汇总")
LO, HI = 43000, 47000  # 合法业务日期序列范围（2026 前后）
# ★ 液位仪表单 10-14 才正式启用；8-25（秦岭）/9-15（南沙）是当年测试记录，写进罐表会污染真实台账 ⇒ 排除。
MIN_SER = 46296  # 2026-10-01，留补录余地，同时挡掉测试数据


def out(s):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(s)


def cli(args, timeout=240):
    """调 kdocs-cli，返回 (parsed_json|None, err_text)。含 --file 大 JSON + 花括号抽取 + 升级横幅过滤。"""
    import tempfile
    new_args, tmp = [], None
    for a in args:
        if len(a) > 2000 and a.lstrip().startswith(("{", "[")):
            fd, tmp = tempfile.mkstemp(suffix=".json", text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(a)
            new_args += ["--file", tmp]
        else:
            new_args.append(a)
    try:
        try:
            cmd = [KDOCS] + (["--token", KDOCS_TOKEN] if KDOCS_TOKEN else []) + new_args + ["--output", "raw"]
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except Exception as e:
            return None, "spawn: %r" % e
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    txt = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    i = txt.find("{")
    if i < 0:
        return None, "no-json: " + txt[:300]
    depth, instr, esc, j = 0, False, False, i
    while j < len(txt):
        ch = txt[j]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        j += 1
    try:
        return json.loads(txt[i:j + 1]), err
    except Exception as e:
        return None, "parse: %s | %s" % (e, txt[:300])


def to_serial(v):
    """字符串 → Excel 日期序列号；非法返回 None。兼容 YYYY/M/D 与 YYYY-M-D 与纯序列号。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        n = float(s)
        if LO <= n <= HI:
            return int(n)
    except ValueError:
        pass
    m = re.match(r"^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        n = (d - datetime.date(1899, 12, 30)).days
        return n if LO <= n <= HI else None
    return None


def num(v):
    """→ 浮点；None/空 → None（区别于 0，用于「表单没填就不写」）。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def cell_val(c):
    """rangeData 单元格 → 文本值（originalCellValue → understandableType.value → cellText）。"""
    if not c:
        return None
    v = c.get("originalCellValue")
    if v is None:
        v = (c.get("understandableType") or {}).get("value")
    if v is None:
        v = c.get("cellText")
    return None if v is None or str(v).strip() == "" else str(v).strip()


def list_records():
    """读 A 表全部记录，返回 [ {station,dateStr,tank,C,D,F,submit} ]。"""
    j, err = cli(["dbsheet", "list-records", json.dumps({"file_id": A_FILE, "sheet_id": A_SHEET})])
    if not j:
        raise RuntimeError("list-records 失败: " + err)
    # 兼容两种返回层级
    recs = ((j.get("data") or {}).get("detail") or {}).get("records")
    if recs is None:
        recs = ((j.get("data") or {}).get("data") or {}).get("records") or []
    rows = []
    for r in recs:
        f = r.get("fields", {}) or {}
        st = str(f.get("站点", "") or "").strip()
        dt = str(f.get("日期", "") or "").strip()
        tk = str(f.get("罐号", "") or "").strip()
        if not st or not dt or not tk:
            continue
        rows.append({
            "station": st, "dateStr": dt, "tank": tk,
            "C": num(f.get("试枪回罐(L)")),
            "D": num(f.get("实际库存(L)")),
            "F": num(f.get("交班温度(℃)")),
            "submit": str(f.get("提交时间", "") or ""),
        })
    return rows


def dedup(rows):
    """同(站|罐|日期) 后提交者胜：按提交时间正序，后出现的覆盖先出现的。"""
    # 提交时间 "YYYY/MM/DD HH:MM:SS" 字典序 = 时间序；缺时间的排最前（会被有时间的覆盖）
    rows = sorted(rows, key=lambda r: r["submit"])
    m = {}
    for r in rows:
        m[(r["station"], r["tank"], r["dateStr"])] = r
    return list(m.values())


def find_tank_sheet(fid, tank):
    """站 file_id + 罐号(东站1#罐) → 罐表 sheetId；找不到返回 None。"""
    j, err = cli(["sheet", "get-sheets-info", json.dumps({"file_id": fid})])
    if not j:
        raise RuntimeError("get-sheets-info 失败 %s: %s" % (fid, err))
    for s in j["data"]["detail"]["sheetsInfo"]:
        name = s.get("sheetName") or ""
        sid = s.get("sheetId") or 0
        if sid >= 3 and not any(h in name for h in SKIP_HINTS) and name.endswith(tank):
            return sid
    return None


def read_range(fid, ws, r0, r1, c0, c1):
    """读一个矩形区，返回 {(row,col): text}（空格不占位）。"""
    j, err = cli(["sheet", "get-range-data", json.dumps({
        "file_id": fid, "worksheet_id": ws,
        "range": {"rowFrom": r0, "rowTo": r1, "colFrom": c0, "colTo": c1},
    })])
    if not j or j.get("code") != 0:
        raise RuntimeError("get-range-data 失败 %s ws=%s: %s" % (fid, ws, err or (j or {}).get("message")))
    m = {}
    for c in j["data"]["detail"]["rangeData"] or []:
        r, col_ = c.get("originRow"), c.get("originCol")
        if r is not None and col_ is not None:
            m[(r, col_)] = cell_val(c)
    return m


def sync(args):
    since = args.since
    only = set(args.stations) if args.stations else None
    rows = dedup(list_records())

    # 日期过滤（--since N：只处理最近 N 天的提交）；并排除启用日前的测试记录
    if since:
        today = datetime.date.today()
        cutoff = today - datetime.timedelta(days=since)
        rows = [r for r in rows
                if (to_serial(r["dateStr"]) is not None
                    and to_serial(r["dateStr"]) >= (cutoff - datetime.date(1899, 12, 30)).days)]
    rows = [r for r in rows if (to_serial(r["dateStr"]) or 0) >= MIN_SER]

    out("=" * 96)
    out("液位回写（A 表 → 罐表 C/D/F）  %s   记录 %d 条" %
        ("DRY-RUN（只读）" if args.dry else "实写", len(rows)))
    out("=" * 96)

    # 预取各站 sheet 映射（缓存，避免重复 get-sheets-info）
    tank_sheet = {}
    for st in sorted(set(r["station"] for r in rows)):
        if st not in OPT:
            out("  ✘ 未知站点: %s" % st)
            continue
        if only and st not in only:
            continue
        tank_sheet[st] = {}

    stats = dict(rows=0, matched=0, changed=0, fill=0, overwrite=0, danger=0,
                 no_sheet=0, no_row=0, nosrc=0, written=0)
    detail = []

    # 缓存：同一罐表只读一次 A 列日期映射；同一目标行只读一次 C/D/F
    a_cache = {}       # (fid, ws) -> {serial: row}
    rowval_cache = {}  # (fid, ws, row) -> {col: num}

    for r in rows:
        st, tank, ds = r["station"], r["tank"], r["dateStr"]
        if st not in OPT:
            continue
        if only and st not in only:
            continue
        ser = to_serial(ds)
        if ser is None:
            stats["nosrc"] += 1
            out("  ✘ %s %s 日期无法解析: %s" % (st, tank, ds))
            continue
        fid = OPT[st]
        # 罐表 sheetId（缓存）
        if tank not in tank_sheet[st]:
            try:
                tank_sheet[st][tank] = find_tank_sheet(fid, tank)
            except Exception as e:
                out("  ✘ %s %s 查罐表失败: %s" % (st, tank, e))
                continue
        ws = tank_sheet[st][tank]
        if ws is None:
            stats["no_sheet"] += 1
            out("  ✘ %s %s 无对应罐表（可能罐号选错，如南北站选了东站）" % (st, tank))
            continue
        # A 列日期映射（缓存）
        if (fid, ws) not in a_cache:
            try:
                g = read_range(fid, ws, 4, 374, 0, 0)
            except Exception as e:
                out("  ✘ %s %s 读A列失败: %s" % (st, tank, e))
                a_cache[(fid, ws)] = {}
                continue
            m = {}
            for (rr, cc), v in g.items():
                s = to_serial(v)
                if s is not None and s not in m:
                    m[s] = rr
            a_cache[(fid, ws)] = m
        row = a_cache[(fid, ws)].get(ser)
        if row is None:
            stats["no_row"] += 1
            out("  ✘ %s %s %s 罐表无此日期行" % (st, tank, ds))
            continue
        stats["matched"] += 1

        # 目标行当前 C/D/F（缓存，一次读 C..F 四列）
        if (fid, ws, row) not in rowval_cache:
            try:
                g = read_range(fid, ws, row, row, 2, 5)
            except Exception:
                g = {}
            rowval_cache[(fid, ws, row)] = {cc: (num(v) if v is not None else None)
                                            for (rr, cc), v in g.items()}
        cur = rowval_cache[(fid, ws, row)]

        # 组装写入 op（仅写表单非空列；幂等；危险闸门）
        ops = []
        for ci, want in ((2, r["C"]), (3, r["D"]), (5, r["F"])):
            if want is None:
                continue  # 表单没填该列 → 不写
            c0 = cur.get(ci)  # 目标行该列为空时稀疏字典无此 key → None
            if c0 is not None and abs(want - c0) < 1e-9:
                continue  # 同值，幂等跳过
            if ci >= 3 and want == 0 and c0 is not None and c0 != 0:
                stats["danger"] += 1
                out("  ⚠ %s %s %s 列%s 源0/目标%.2f 危险，跳过" % (st, tank, ds, "DF"[ci - 3], c0))
                continue
            ops.append({"opType": "formula", "rowFrom": row, "rowTo": row,
                        "colFrom": ci, "colTo": ci, "formula": str(want)})
            stats["changed"] += 1
            if c0 in (None, 0.0):
                stats["fill"] += 1
            else:
                stats["overwrite"] += 1

        if not ops:
            stats["rows"] += 1
            continue
        stats["rows"] += 1
        detail.append((st, tank, ds, row, [(o["colFrom"], o["formula"]) for o in ops]))
        if args.dry:
            continue
        # 实写（分片 ≤100）
        n = 0
        for k in range(0, len(ops), 100):
            payload = json.dumps({"file_id": fid, "worksheet_id": ws,
                                  "rangeData": ops[k:k + 100]}, ensure_ascii=False)
            j, err = cli(["sheet", "update-range-data", payload])
            if not j or j.get("code") != 0:
                out("  ✘ WRITE_FAIL %s %s %s: %s" % (st, tank, ds, (j or {}).get("message") or err or "unknown"))
            else:
                n += min(100, len(ops) - k)
            time.sleep(0.3)
        stats["written"] += n

    # 打印明细
    colname = {2: "C", 3: "D", 5: "F"}
    for st, tank, ds, row, cols in detail:
        tag = "[dry]" if args.dry else "[写]"
        cs = " ".join("%s=%s" % (colname[o[0]], o[1]) for o in cols)
        out("  %s %-5s %-10s %s r%d  %s" % (tag, st, tank, ds, row + 1, cs))

    out("-" * 96)
    out("合计: 记录%d 匹配%d 变更格%d(填充%d/覆盖%d/危险%d) 跳过无表%d/无行%d/无源%d 写入%d" %
        (stats["rows"], stats["matched"], stats["changed"], stats["fill"], stats["overwrite"],
         stats["danger"], stats["no_sheet"], stats["no_row"], stats["nosrc"], stats["written"]))
    if stats["danger"]:
        out("!! 已阻止 %d 格「源0抹目标非零真值」的危险写入 !!" % stats["danger"])
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--stations")
    ap.add_argument("--since", type=int, default=None, help="只处理最近 N 天的提交")
    a = ap.parse_args()
    return sync(a)


if __name__ == "__main__":
    sys.exit(main())
