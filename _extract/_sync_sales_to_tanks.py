# -*- coding: utf-8 -*-
"""_sync_sales_to_tanks.py —— 本地版「日报泵码 → 罐表 B 列（当日销售）」同步。

背景（2026-10-16）：
  原实现是 AirScript 脚本 `airscript_sales_sync_ksdrive.js`，部署在秦岭共享脚本编辑器
  script_id=`V2-6oQRf1Wmpo1TpO9t9t0ktW`。**该槽位现已被 `airscript_unload_record_sync.js`
  （收油统计同步）占用** ⇒ 原 sales_sync 不再运行 ⇒ 8 站罐表 B 列停在 2026-09-13。
  故改由本脚本（kdocs-cli）本地执行，口径与原脚本**逐条对齐**。

数据流（与原脚本一致）：
  ① 各站《日报月报表》-「日泵码数明细」
       左半区(西/南站)：B=枪号(0-based 1), F=当日泵码数(5)
       右半区(东/北站)：K=枪号(10),     O=当日泵码数(14)
       A 列 = 日号（会计月窗口：上月26 → 本月25），块内表头2行 + 数据行
       ★ F/O 是"期末−上期"的增量，数据源以负数存储 ⇒ 取绝对值
  ② 《枪罐对照表》(csgZJjI1Pu30)：sheet 名形如「N-站名」
       左半区 B=枪号(1) D=罐(3)  → SW
       右半区 G=枪号(6) I=罐(8)  → EN
  ③ 聚合键 = `SW|N#罐` / `EN|N#罐`（v6 分侧，避免东西同号罐碰撞）
  ④ 写各站优化表罐表 B 列：`🛢️ 南站/西站N#罐` → SW；`🛢️ 北站/东站N#罐` → EN
     按 A 列日期定位行；窗口内写真实销量

安全设计（相比原脚本收紧）：
  · **默认只写「有销量」的日期**（不主动写 0），避免误清历史
  · 单枪日销量 > 50000 升 → 跳过并告警（期末泵码被清空的典型症状）
  · 枪有量但无映射 → **报错停下**，不静默丢数
  · 目标 B 列已有非零值且与新值差异 > 1 升 → 计入"覆盖"，dry-run 可先看

用法：
  python scripts/_sync_sales_to_tanks.py --dry
  python scripts/_sync_sales_to_tanks.py --stations 秦岭,富平
  python scripts/_sync_sales_to_tanks.py --since 2026-09-26
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

# 站 → (优化表 lossUrl, 日报月报表 pumpUrl)
STATIONS = [
    ("秦岭", "cjtgYCHSQnXk", "cej6LMW9Ryxb"),
    ("宁陕", "cicYzlWSz42i", "cnkSQhv80kqh"),
    ("洋县", "cgsMLS7EDSgK", "cs1aQHh3UQgU"),
    ("汉中", "ck57MuopxMGq", "cqeH3YSYzeIk"),
    ("富平", "csj1JZvB1mXk", "cpEvp4E1cv1w"),
    ("韩城", "crsUIwTN1ddF", "cimZvfXHlHNH"),
    ("富县", "cn5F4fFtcW3O", "cr6TNWmG9pcd"),
    ("南沙", "crAzwpzEpIt9", "ciaWmXVEI49Y"),
    ("华山", "cmfB737g3GTQ", "ccd84k0yXxVQ"),
    ("白河", "cjnF7ztNQSjk", "cb8FVAHtfJhM"),
    ("旬阳", "caekiQMG3Y1b", "ceo20kfrxvWe"),
    ("略阳", "cdp2sa8yXIU3", "coNQuJ09gRie"),
    ("玉华宫", "cqCjwvRElbGF", "cqMATKbeVMS0"),
    ("照金", "ctuoOPC4jYCE", "cbZksg8CtFrP"),
    ("天汉水城", "ceSbFX0AoyL0", "clJ5LexpQ1Qy"),
]
MAP_FID = "csgZJjI1Pu30"       # 《枪罐对照表》
MAX_DAILY_GUN_L = 50000        # 单枪日销量上限（升）
TANK_SHEET_RE = re.compile(r'🛢️\s*(南站|北站|东站|西站)\s*(\d+#罐)')


def out(s):
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
    depth, j = 0, i
    while j < len(txt):
        if txt[j] == '{':
            depth += 1
        elif txt[j] == '}':
            depth -= 1
            if depth == 0:
                break
        j += 1
    try:
        return json.loads(txt[i:j + 1]), None
    except Exception as e:
        return None, 'parse: %r' % e


def sheets_info(fid, retry=3):
    """带重试 —— 单次瞬时失败曾被误判成「无此表」。"""
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


def num(v):
    t = s(v).replace(',', '').replace('，', '')
    if not t:
        return 0.0
    try:
        return float(t)
    except ValueError:
        return 0.0


def excel_serial(d):
    return (d - EPOCH).days


def serial_to_date(n):
    return EPOCH + timedelta(days=n)


def acct_window(ref):
    """会计月窗口 = 上月26日 ~ 本月25日（含 ref 的那一个）。"""
    if ref.day >= 26:
        start = date(ref.year, ref.month, 26)
        m, y = ref.month + 1, ref.year
        if m > 12:
            m, y = 1, y + 1
        end = date(y, m, 25)
    else:
        m, y = ref.month - 1, ref.year
        if m < 1:
            m, y = 12, y - 1
        start = date(y, m, 26)
        end = date(ref.year, ref.month, 25)
    return start, end


def daynum_to_date(dn, win_start):
    """日号 → 日期。26..31 → 窗口起始月；1..25 → 下一个月。非法日（如9/31）返回 None。"""
    try:
        if dn >= 26:
            return date(win_start.year, win_start.month, dn)
        m, y = win_start.month + 1, win_start.year
        if m > 12:
            m, y = 1, y + 1
        return date(y, m, dn)
    except ValueError:
        return None


def load_gun_to_tank():
    """读《枪罐对照表》→ {站名: {'SW': {枪:罐}, 'EN': {...}}}"""
    out('[1] 读《枪罐对照表》%s …' % MAP_FID)
    m = {}
    for x in sheets_info(MAP_FID):
        nm = x.get('sheetName') or ''
        if '-' not in nm:
            continue
        st = nm.split('-', 1)[1].strip()
        if not st:
            continue
        g = read_grid(MAP_FID, x['sheetId'], 0, 79, 0, 9)
        sw, en = {}, {}
        for r in range(1, 80):
            gl, tl = s(g.get((r, 1))), s(g.get((r, 3)))
            gr, tr = s(g.get((r, 6))), s(g.get((r, 8)))
            if gl and tl and not gl.startswith('枪号'):
                sw[gl] = tl
            if gr and tr and not gr.startswith('枪号'):
                en[gr] = tr
        m[st] = {'SW': sw, 'EN': en}
    out('    共 %d 个站: %s' % (len(m), ', '.join(sorted(m))))
    return m


def collect_sales(pump_fid, win_start, win_end, max_serial, g2t, st):
    """聚合该站 日号→(日期序列, side|罐) 的当日销量。返回 (tankSales, warn)"""
    sid = None
    for x in sheets_info(pump_fid):
        nm = x.get('sheetName') or ''
        if '泵码' in nm and '明细' in nm:
            sid = x['sheetId']
            break
    if sid is None:
        return None, ['日报表无「日泵码数明细」sheet']
    g = read_grid(pump_fid, sid, 0, 1399, 0, 15)
    tank_sales = {}
    warn = []
    over = {}
    unmapped = {}
    cur = None
    skip = 0
    for r in range(0, 1400):
        a = s(g.get((r, 0)))
        try:
            fv = float(a)
            isnum = 1 <= fv <= 31 and fv == int(fv)
        except ValueError:
            isnum = False
        if isnum:
            cur = int(fv)
            skip = 2
            continue
        if cur is None:
            continue
        if skip > 0:
            skip -= 1
            continue
        d = daynum_to_date(cur, win_start)
        if d is None:
            continue
        ds = excel_serial(d)
        if not (excel_serial(win_start) <= ds <= excel_serial(win_end)):
            continue
        if ds > max_serial:      # v7：只到昨日
            continue
        for side, gc, qc, table in (('SW', 1, 5, g2t['SW']), ('EN', 10, 14, g2t['EN'])):
            gun = s(g.get((r, gc)))
            q = abs(num(g.get((r, qc))))
            if not gun or q <= 0:
                continue
            if q > MAX_DAILY_GUN_L:
                over['%s:%s' % (side, gun)] = over.get('%s:%s' % (side, gun), 0) + 1
                continue
            tank = table.get(gun)
            if not tank:
                unmapped['%s:%s' % (side, gun)] = unmapped.get('%s:%s' % (side, gun), 0.0) + q
                continue
            key = '%s|%s' % (side, tank)
            tank_sales.setdefault(ds, {})
            tank_sales[ds][key] = tank_sales[ds].get(key, 0.0) + q
    if unmapped:
        tot = sum(unmapped.values())
        return None, ['★ 未配置「枪→罐」映射且有销量: %s 合计 %.0f 升 ⇒ 按原脚本规则停止该站' % (
            ', '.join(sorted(unmapped)), tot)]
    if over:
        warn.append('超限枪已跳过: %s' % ', '.join('%s×%d' % (k, v) for k, v in sorted(over.items())))
    return tank_sales, warn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--stations', help='逗号分隔')
    ap.add_argument('--since', help='起始日期 YYYY-MM-DD（默认=会计月窗口起点）')
    a = ap.parse_args()

    only = [x.strip() for x in a.stations.split(',')] if a.stations else None

    ref = date.today() - timedelta(days=1)          # 昨日（脚本同步的是昨日销售）
    win_start, win_end = acct_window(ref)
    max_serial = excel_serial(ref)
    out('=' * 100)
    out('日报泵码 → 罐表 B 列（当日销售）同步' + ('   [dry-run 预览]' if a.dry else '   [实写]'))
    out('  会计月窗口: %s ~ %s   基准(昨日): %s   最晚写入日: %s' % (
        win_start, win_end, ref, ref))
    if a.since:
        win_start = date.fromisoformat(a.since)
        out('  ★ --since 覆盖窗口起点: %s' % win_start)
    out('=' * 100)

    g2t_all = load_gun_to_tank()

    stat = {'rec': 0, 'fill': 0, 'over': 0, 'skip': 0, 'written': 0, 'nocs': 0}
    detail_lines = []
    problems = []

    for st, loss_fid, pump_fid in STATIONS:
        if only and st not in only:
            continue
        m = g2t_all.get(st)
        if not m:
            out('\n%-8s ✘ 《枪罐对照表》缺该站 sheet' % st)
            problems.append((st, '对照表缺站'))
            continue
        out('\n%-8s 聚合中 …' % st)
        ts_map, warn = collect_sales(pump_fid, win_start, win_end, max_serial, m, st)
        for w in warn:
            out('        ⚠ %s' % w)
        if ts_map is None:
            out('        ✘ %s' % (warn[0] if warn else '聚合失败'))
            problems.append((st, warn[0] if warn else '聚合失败'))
            continue
        if not ts_map:
            out('        · 窗口内无销量数据')
            stat['nocs'] += 1
            continue

        # 罐表清单
        ss = sheets_info(loss_fid)
        tanks = []
        for x in ss:
            mm = TANK_SHEET_RE.search(x.get('sheetName') or '')
            if not mm:
                continue
            side = 'SW' if mm.group(1) in ('南站', '西站') else 'EN'
            tanks.append({'name': x.get('sheetName'), 'sid': x['sheetId'],
                          'side': side, 'tank': mm.group(2)})
        if not tanks:
            out('        ✘ 优化表未找到罐 sheet' % ())
            problems.append((st, '优化表无罐sheet'))
            continue

        ops = []      # (sheetId, op)
        for tk in tanks:
            key = '%s|%s' % (tk['side'], tk['tank'])
            # 该罐在窗口内的所有日期销量
            vals = {ds: v[key] for ds, v in ts_map.items() if key in v}
            if not vals:
                continue
            g = read_grid(loss_fid, tk['sid'], 0, 379, 0, 1)
            row_of = {}
            for r in range(4, 375):
                c = s(g.get((r, 0)))
                if not c or c == '汇总':
                    continue
                mm = re.match(r'^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', c)
                if mm:
                    try:
                        d = date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
                        row_of[excel_serial(d)] = r
                    except ValueError:
                        pass
            gb = read_grid(loss_fid, tk['sid'], 0, 379, 1, 1)
            for ds in sorted(vals):
                if ds not in row_of:
                    continue
                r = row_of[ds]
                want = round(vals[ds], 2)
                cur = num(gb.get((r, 1)))
                stat['rec'] += 1
                if cur and abs(cur - want) > 1:
                    stat['over'] += 1
                    tag = '覆盖'
                elif cur == 0:
                    stat['fill'] += 1
                    tag = '填充'
                else:
                    stat['skip'] += 1
                    continue          # 同值，幂等跳过
                ops.append((tk['sid'], {"opType": "formula", "rowFrom": r, "rowTo": r,
                                        "colFrom": 1, "colTo": 1, "formula": str(want)}))
                if len(detail_lines) < 400:
                    detail_lines.append('  [%s] %-6s %-9s %s r%d  旧=%s 新=%.2f' % (
                        'dry' if a.dry else '写', st, tk['name'][-6:],
                        serial_to_date(ds).isoformat(), r + 1,
                        ('%.2f' % cur) if cur else '空', want))

        if not ops:
            out('        · 无需变更（全部同值或已齐）')
            continue
        out('        ⇒ 待写 %d 格（%d 个罐）' % (len(ops), len(set(o[0] for o in ops))))
        if a.dry:
            continue
        n = 0
        by = {}
        for sid, o in ops:
            by.setdefault(sid, []).append(o)
        for sid, ol in by.items():
            for k in range(0, len(ol), 100):
                payload = json.dumps({"file_id": loss_fid, "worksheet_id": sid,
                                      "rangeData": ol[k:k + 100]}, ensure_ascii=False)
                j, err = cli(["sheet", "update-range-data", payload])
                if not j or j.get('code') != 0:
                    out('        ✘ WRITE_FAIL ws=%s: %s' % (
                        sid, (j or {}).get('message') or err or 'unknown'))
                    problems.append((st, 'WRITE_FAIL ws=%s' % sid))
                else:
                    n += min(100, len(ol) - k)
                time.sleep(0.3)
        stat['written'] += n
        out('        ✔ 写入 %d 格' % n)

    out('')
    if detail_lines:
        out('  明细（前 %d 条）:' % len(detail_lines))
        for x in detail_lines:
            out(x)
        out('')
    out('=' * 100)
    out('合计: 候选%d  填充%d  覆盖%d  幂等跳过%d  无销量站%d  写入%d' % (
        stat['rec'], stat['fill'], stat['over'], stat['skip'], stat['nocs'], stat['written']))
    if problems:
        out('!! 异常 %d 项:' % len(problems))
        for st, msg in problems:
            out('   %-8s %s' % (st, msg))
    out('=' * 100)
    return 0


if __name__ == '__main__':
    sys.exit(main())
