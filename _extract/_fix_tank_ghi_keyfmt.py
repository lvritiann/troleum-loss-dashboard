# -*- coding: utf-8 -*-
"""_fix_tank_ghi_keyfmt.py —— 方案B：罐表 G/H/I 公式兼容「裸号」与「#罐」两种匹配键。

背景（2026-10-16 定案）：
  罐表 G/H/I 三列从「收油统计表」取数，匹配键 = 日期序列 & 罐号：
      罐表 G5 = SUMIFS(收油统计表!H:H, 收油统计表!M:M, $A5&$A$3)
      罐表 A3 = 裸号「南1」
      收油统计表 M = $A&$K
  新 V20 录入后 K 列写成「南1#罐」（带 #罐 后缀）⇒ 键成 46310南1#罐
  ⇒ 与罐表要的 46310南1 对不上 ⇒ SUMIFS 静默返 0，卸油量/卸油后温/进货三列全丢。
  受影响 3 站：韩城(6条,9/16起)、南沙(2条,10/15起)、天汉水城(2条,10/15起)。

方案B（不改收油统计表 K 列 —— 那是卸油软件写入的主链路，改了会被覆盖回去）：
  改罐表 G/H/I 公式，同时兼容两种键。

  G(卸油升) 与 I(进货) 是累加量 ⇒ 两个 SUMIFS **相加**：
      =IFERROR(SUMIFS(…H:H,…M:M,$A5&$A$3)
              +SUMIFS(…H:H,…M:M,$A5&$A$3&"#罐"),0)
  H(卸油后温) 是**平均值** ⇒ 不能相加，用 IF+COUNTIFS「优先带#罐，否则裸号」：
      =IFERROR(IF(COUNTIFS(…M:M,$A5&$A$3&"#罐")>0,
                  AVERAGEIFS(…I:I,…M:M,$A5&$A$3&"#罐"),
                  AVERAGEIFS(…I:I,…M:M,$A5&$A$3)),0)

范围控制（默认定向，最小改动）：
  · 默认只改「已确认受影响的罐表行」= 收油统计表里 K 列带 #罐 的 (日期,罐号) 对应的罐表行
  · `--full` 改受影响**罐表**的整列（行 5~374），该罐将来任何 #罐 数据都能取到
  · `--all-rows` 仅配合 --full：连未受影响的罐表也整列改（15 站全量，慎用）

用法：
  python scripts/_fix_tank_ghi_keyfmt.py --dry
  python scripts/_fix_tank_ghi_keyfmt.py                 # 定向修受影响的行
  python scripts/_fix_tank_ghi_keyfmt.py --full          # 受影响罐表整列改
  python scripts/_fix_tank_ghi_keyfmt.py --stations 南沙 --dry
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
KDOCS = os.environ.get("KDOCS_CLI") or os.path.join(os.path.dirname(HERE), "bin", "kdocs-cli.exe")
KDOCS_TOKEN = (os.environ.get("KDOCS_TOKEN") or "").strip()
EPOCH = date(1899, 12, 30)

OPT = {
    "秦岭": "cjtgYCHSQnXk", "宁陕": "cicYzlWSz42i", "洋县": "cgsMLS7EDSgK",
    "汉中": "ck57MuopxMGq", "富平": "csj1JZvB1mXk", "韩城": "crsUIwTN1ddF",
    "富县": "cn5F4fFtcW3O", "南沙": "crAzwpzEpIt9", "华山": "cmfB737g3GTQ",
    "白河": "cjnF7ztNQSjk", "旬阳": "caekiQMG3Y1b", "略阳": "cdp2sa8yXIU3",
    "玉华宫": "cqCjwvRElbGF", "照金": "ctuoOPC4jYCE", "天汉水城": "ceSbFX0AoyL0",
}

ROW0, ROW1 = 4, 373          # 0-based 数据行 5~374
SRC = "'⛽ 收油统计表'"
COL_G, COL_H, COL_I = 6, 7, 8   # 0-based


def out(s):
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    print(s)


def cli(args, timeout=240):
    cmd = [KDOCS]
    if KDOCS_TOKEN:
        cmd += ["--token", KDOCS_TOKEN]
    cmd += args
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    txt = (p.stdout or b'').decode('utf-8', 'replace')
    i = txt.find('{')
    if i < 0:
        return None, 'no-json: ' + txt[:200]
    depth, instr, esc, j = 0, False, False, i
    while j < len(txt):
        ch = txt[j]
        if instr:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
        j += 1
    try:
        return json.loads(txt[i:j + 1]), None
    except Exception as e:
        return None, 'parse: %r' % e


def sheets_info(fid, retry=3):
    """带重试 —— 单次瞬时失败曾被误判成「无收油统计表」。"""
    for _ in range(retry):
        j, err = cli(['sheet', 'get-sheets-info', json.dumps({'file_id': fid})])
        if j and j.get('code') == 0 and j['data']['detail'].get('sheetsInfo'):
            return j['data']['detail']['sheetsInfo']
        time.sleep(0.4)
    return []


def read_grid(fid, ws, r0, r1, c0, c1):
    j, err = cli(['sheet', 'get-range-data', json.dumps(
        {'file_id': fid, 'worksheet_id': ws,
         'range': {'rowFrom': r0, 'rowTo': r1, 'colFrom': c0, 'colTo': c1}})])
    if not j or j.get('code') != 0:
        return {}
    g = {}
    for c in (j['data']['detail'].get('rangeData') or []):
        r, col = c.get('originRow'), c.get('originCol')
        if r is None or col is None:
            continue
        g[(r, col)] = c.get('cellText')
    return g


def s(v):
    return '' if v is None else str(v).strip()


def ser(v):
    if v is None or v == '':
        return None
    t = str(v).strip()
    try:
        n = float(t)
        return int(n) if 43000 <= n <= 47000 else None
    except ValueError:
        pass
    m = re.match(r'^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', t)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    n = (d - EPOCH).days
    return n if 43000 <= n <= 47000 else None


def ds(n):
    return (EPOCH + timedelta(days=n)).isoformat()


# ---------------- 新公式 ----------------
def f_g(r):
    return ('=IFERROR(SUMIFS({s}!$H:$H,{s}!$M:$M,$A{r}&$A$3)'
            '+SUMIFS({s}!$H:$H,{s}!$M:$M,$A{r}&$A$3&"#罐"),0)').format(s=SRC, r=r)


def f_i(r):
    return ('=IFERROR(SUMIFS({s}!$J:$J,{s}!$M:$M,$A{r}&$A$3)'
            '+SUMIFS({s}!$J:$J,{s}!$M:$M,$A{r}&$A$3&"#罐"),0)').format(s=SRC, r=r)


def f_h(r):
    return ('=IFERROR(IF(COUNTIFS({s}!$M:$M,$A{r}&$A$3&"#罐")>0,'
            'AVERAGEIFS({s}!$I:$I,{s}!$M:$M,$A{r}&$A$3&"#罐"),'
            'AVERAGEIFS({s}!$I:$I,{s}!$M:$M,$A{r}&$A$3)),0)').format(s=SRC, r=r)


def is_new(f):
    """已是新公式（含双 SUMIFS 或 COUNTIFS）？"""
    f = f or ''
    return '&"#罐"' in f


def is_old(f):
    f = f or ''
    return 'SUMIFS' in f and not is_new(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true', help='只预览不写')
    ap.add_argument('--full', action='store_true', help='受影响罐表整列改（行5~374）')
    ap.add_argument('--all-rows', action='store_true', help='配合 --full：未受影响的罐表也整列改')
    ap.add_argument('--stations', help='逗号分隔，只跑指定站')
    a = ap.parse_args()

    only = [x.strip() for x in a.stations.split(',')] if a.stations else None
    sts = [st for st in OPT if (only is None or st in only)]

    out('=' * 100)
    out('方案B：罐表 G/H/I 公式兼容「裸号 / #罐」两种匹配键' +
        ('  [dry-run 预览]' if a.dry else '  [实写]'))
    out('模式：%s' % ('整列(行5~374)' if a.full else '定向(仅受影响的行)') +
        ('  + 未受影响罐表' if a.all_rows else ''))
    out('=' * 100)

    total_ops = 0
    total_written = 0
    affected_tanks = []
    problems = []

    for st in sts:
        fid = OPT[st]
        ss = sheets_info(fid)
        if not ss:
            out('%-8s ✘ sheets_info 失败（重试3次仍空）' % st)
            problems.append((st, 'sheets_info 失败'))
            continue
        sid2 = None
        for x in ss:
            if '收油' in (x.get('sheetName') or ''):
                sid2 = x['sheetId']
                break
        if sid2 is None:
            out('%-8s · 无收油统计表（跳过）' % st)
            continue
        g2 = read_grid(fid, sid2, 0, 1199, 0, 12)
        # 收油统计表里带 #罐 的记录 → (日期序列, 罐号裸号)
        hit = {}
        for r in range(4, 1199):
            c = g2.get((r, 0))
            if not c:
                continue
            ts = ser(c)
            if not ts:
                continue
            tk = s(g2.get((r, 10)))
            if not tk.endswith('#罐'):
                continue
            hit.setdefault((ts, tk[:-2]), []).append(r + 1)   # 去掉「#罐」2 字符

        if not hit:
            out('%-8s ✔ K 列无 #罐 后缀，公式无需改' % st)
            continue

        # 该站罐表
        tank_sheets = [x for x in ss if '罐' in (x.get('sheetName') or '')]
        # 建 A3 → sheetId
        a3map = {}
        for x in tank_sheets:
            h = read_grid(fid, x['sheetId'], 2, 2, 0, 0)
            a3 = s(h.get((2, 0)))
            if a3:
                a3map[a3] = x

        out('\n%-8s 收油统计表有 %d 条 K 列带 #罐 ⇒ 受影响罐/日期：' % (st, sum(len(v) for v in hit.values())))
        for (ts, tk), rows in sorted(hit.items(), key=lambda z: (z[0][0], z[0][1])):
            out('    %s  %-6s（源表行 %s）' % (ds(ts), tk, ','.join(str(x) for x in rows[:4])))

        ops = []          # [(worksheet_id, op)] —— 必须按 sheet 分组写
        for (ts, tk), rows in sorted(hit.items()):
            x = a3map.get(tk)
            if not x:
                out('    ✘ 罐号 %r 在 12 个罐表里找不到对应 A3（该站 A3=%s）⇒ 跳过' % (
                    tk, '/'.join(sorted(a3map))))
                problems.append((st, '罐号 %r 无对应罐表' % tk))
                continue
            sid = x['sheetId']
            nm = x['sheetName']
            g = read_grid(fid, sid, ROW0, ROW1, 0, COL_I)
            # 找 A 列日期 = ts 的行
            tgt = None
            for r in range(ROW0, ROW1 + 1):
                if ser(g.get((r, 0))) == ts:
                    tgt = r
                    break
            if tgt is None:
                out('    ✘ %s 罐表无 %s 行（A 列无此日期）⇒ 跳过' % (nm, ds(ts)))
                problems.append((st, '%s 无 %s 行' % (nm, ds(ts))))
                continue
            rowno = tgt + 1
            rng = range(ROW0, ROW1 + 1) if a.full else [tgt]
            for r in rng:
                for ci, fn in ((COL_G, f_g), (COL_H, f_h), (COL_I, f_i)):
                    ops.append((sid, {"opType": "formula", "rowFrom": r, "rowTo": r,
                                      "colFrom": ci, "colTo": ci, "formula": fn(r + 1)}))
            affected_tanks.append((st, nm, sid, tk, ts, rowno, a.full))
            out('    ✔ %s (ws=%s) %s → %s 的 G/H/I' % (
                nm, sid, ds(ts), '整列行5~374' if a.full else '第%d行' % rowno))

        if not ops:
            continue
        total_ops += len(ops)
        out('    ⇒ 待写 %d 格（分布在 %d 个罐表）' % (
            len(ops), len(set(o[0] for o in ops))))
        if a.dry:
            sg = [o for _, o in ops if o['colFrom'] == COL_G][:1]
            sh = [o for _, o in ops if o['colFrom'] == COL_H][:1]
            if sg:
                out('       G 新公式(r%d)：%s' % (sg[0]['rowFrom'] + 1, sg[0]['formula']))
            if sh:
                out('       H 新公式(r%d)：%s' % (sh[0]['rowFrom'] + 1, sh[0]['formula']))
            continue
        # 实写：按 worksheet_id 分组，组内分片 ≤100
        n = 0
        by_ws = {}
        for sid, o in ops:
            by_ws.setdefault(sid, []).append(o)
        for sid, ol in by_ws.items():
            for k in range(0, len(ol), 100):
                payload = json.dumps({"file_id": fid, "worksheet_id": sid,
                                      "rangeData": ol[k:k + 100]}, ensure_ascii=False)
                j, err = cli(["sheet", "update-range-data", payload])
                if not j or j.get('code') != 0:
                    out('    ✘ WRITE_FAIL %s ws=%s: %s' % (
                        st, sid, (j or {}).get('message') or err or 'unknown'))
                    problems.append((st, 'WRITE_FAIL ws=%s' % sid))
                else:
                    n += min(100, len(ol) - k)
                time.sleep(0.3)
        total_written += n
        out('    ✔ 写入 %d 格' % n)

    out('')
    out('=' * 100)
    out('合计：受影响罐表 %d 个，待写/已写 %d 格' % (len(set((t[1], t[2]) for t in affected_tanks)), total_ops))
    if not a.dry:
        out('实际写入 %d 格' % total_written)
    if problems:
        out('!! 异常 %d 项：' % len(problems))
        for st, msg in problems:
            out('   %-8s %s' % (st, msg))
    out('=' * 100)
    return 0


if __name__ == '__main__':
    sys.exit(main())
