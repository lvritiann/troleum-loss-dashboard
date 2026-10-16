# -*- coding: utf-8 -*-
"""_check_unload_health.py —— 每日自动体检：罐表卸油量 vs 收油统计表。

背景（2026-10-16）：新 V20 写入收油统计表 K 列带「#罐」后缀，
罐表 G/H/I 的 SUMIFS 匹配不上 ⇒ 静默返 0，卸油量/卸油后温/进货三列全丢，
且**不报任何错**。没有自动体检就只能靠人工发现。

体检逻辑（每站）：
  ① 读收油统计表全部记录，按 (日期序列, 罐号归一化) 聚合 H 列 ⇒ 应卸总量
  ② 读 12 个罐表 G 列，按 (日期序列, A3) 聚合 ⇒ 实际取到总量
  ③ 逐 (日期, 罐) 比对：
       · 应卸 > 0 且实际 = 0        ⇒ KEY_MISMATCH（键不匹配，最危险）
       · |实际 − 应卸| > 容差        ⇒ MISMATCH（数量不符）
       · 应卸 = 0 且实际 > 0         ⇒ ORPHAN（罐表有值但源无记录）
  ④ K 列写法分布：带 #罐 的记录数（新增即预警）
  ⑤ 收油统计表新鲜度：最后记录日期（超过 3 天未更新即预警）

用法：
  python scripts/_check_unload_health.py              # 全部 15 站
  python scripts/_check_unload_health.py --stations 南沙,韩城
  python scripts/_check_unload_health.py --days 30    # 只查最近 30 天
  python scripts/_check_unload_health.py --fix        # 体检 + 自动修复键不匹配
  退出码：0 = 健康；1 = 有告警；2 = 执行异常
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

TOL = 1.0          # 升，比对容差
FRESH_DAYS = 3     # 收油统计表超过 N 天未更新即预警
SALES_DAYS = 2     # 罐表 B 列（销售）超过 N 天未更新即预警


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
    if v is None or v == '':
        return 0.0
    try:
        return float(str(v).strip().replace(',', '').replace('，', ''))
    except Exception:
        return 0.0


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stations', help='逗号分隔，只查指定站')
    ap.add_argument('--days', type=int, default=14,
                    help='只查最近 N 天（默认 14，避开历史遗留噪音）')
    ap.add_argument('--fix', action='store_true', help='体检后自动修复键不匹配')
    a = ap.parse_args()

    only = [x.strip() for x in a.stations.split(',')] if a.stations else None
    sts = [st for st in OPT if (only is None or st in only)]
    cutoff = None
    if a.days:
        cutoff = (date.today() - EPOCH).days - a.days

    out('=' * 106)
    out('卸油数据每日体检（罐表 G 列 vs 收油统计表 H 列）    %s' % date.today().isoformat())
    out('=' * 106)

    alerts = []
    for st in sts:
        fid = OPT[st]
        ss = sheets_info(fid)
        if not ss:
            out('%-8s ✘ sheets_info 失败' % st)
            alerts.append((st, 'READ_FAIL', 'sheets_info 失败'))
            continue
        sid2 = None
        for x in ss:
            if '收油' in (x.get('sheetName') or ''):
                sid2 = x['sheetId']
                break
        if sid2 is None:
            out('%-8s · 无收油统计表（该站不适用本体检）' % st)
            continue
        g2 = read_grid(fid, sid2, 0, 1199, 0, 12)
        # 源：按 (日期, 罐号归一化) 聚合
        src = {}
        nkey = 0
        lastts = None
        for r in range(4, 1199):
            c = g2.get((r, 0))
            if not c:
                continue
            ts = ser(c)
            if not ts:
                continue
            lastts = ts if lastts is None or ts > lastts else lastts
            tk = s(g2.get((r, 10)))
            if not tk:
                continue
            if tk.endswith('#罐'):
                nkey += 1
            e = src.setdefault((ts, tk.replace('#罐', '')), 0.0)
            src[(ts, tk.replace('#罐', ''))] = e + num(g2.get((r, 7)))
        if cutoff:
            src = {k: v for k, v in src.items() if k[0] >= cutoff}

        # 罐表：按 (日期, A3) 聚合 G 列 + A3 健康检查
        got = {}
        a3_seen = {}
        a3_bad = []
        last_b = None       # 罐表 B 列（销售）最后有值日期
        for x in ss:
            nm = x.get('sheetName') or ''
            if '罐' not in nm:
                continue
            # ★ A3 必须读 A..B 两列（colTo 要 > colFrom，单格边界写法会返空）
            h = read_grid(fid, x['sheetId'], 2, 2, 0, 1)
            a3 = s(h.get((2, 0)))
            # A3 健康：空 ⇒ 该罐永远取不到；重复 ⇒ 多表抢同一键、卸油量双算
            if not a3:
                a3_bad.append(('EMPTY', nm, x['sheetId'], a3))
            elif a3 in a3_seen:
                a3_bad.append(('DUP', nm, x['sheetId'], a3))
            else:
                a3_seen[a3] = nm
            if not a3:
                continue
            g = read_grid(fid, x['sheetId'], 4, 373, 0, 6)
            for r in range(4, 374):
                ts = ser(g.get((r, 0)))
                if not ts:
                    continue
                # ★ B 列（💰当日销售）新鲜度：外部写入（日报 sales_sync）
                if num(g.get((r, 1))) != 0 and (last_b is None or ts > last_b):
                    last_b = ts
                v = num(g.get((r, 6)))
                if v == 0:
                    continue
                got[(ts, a3)] = got.get((ts, a3), 0.0) + v
        if cutoff:
            got = {k: v for k, v in got.items() if k[0] >= cutoff}

        # 比对（聚焦近期，避开历史遗留噪音）
        # 关键：只有「K 列确实带 #罐」才算 KEY_MISMATCH；
        #       不带 #罐 却取不到 ⇒ 多半是时间范围不重叠（源表起点晚于罐表），不算键问题。
        src_has_hash = set()
        for r in range(4, 1199):
            c = g2.get((r, 0))
            if not c:
                continue
            ts = ser(c)
            if not ts:
                continue
            tk = s(g2.get((r, 10)))
            if tk.endswith('#罐'):
                src_has_hash.add((ts, tk.replace('#罐', '')))

        km = mm = orph = nooverlap = 0
        det = []
        for (ts, tk), v in sorted(src.items()):
            gv = got.get((ts, tk), 0.0)
            if v > 0 and gv == 0:
                if (ts, tk) in src_has_hash:
                    km += 1
                    det.append(('KEY', ts, tk, v, gv))
                else:
                    nooverlap += 1
            elif abs(gv - v) > TOL:
                mm += 1
                det.append(('DIFF', ts, tk, v, gv))
        for (ts, tk), v in sorted(got.items()):
            if (ts, tk) not in src:
                orph += 1
                det.append(('ORPH', ts, tk, 0.0, v))

        flags = []
        if nkey:
            flags.append('K列带#罐 %d 条' % nkey)
        if lastts is not None:
            gap = (date.today() - EPOCH).days - lastts
            if gap > FRESH_DAYS:
                flags.append('收油表 %d 天未更新(最后%s)' % (gap, ds(lastts)))
        if km:
            flags.append('键不匹配 %d 处' % km)
        if mm:
            flags.append('数量不符 %d 处' % mm)
        if orph:
            flags.append('罐表有值源无记录 %d 处' % orph)
        if nooverlap:
            flags.append('时间不重叠 %d 处(非键问题)' % nooverlap)
        if a3_bad:
            flags.append('A3异常 %d 处(%s)' % (
                len(a3_bad), ','.join('%s:%s' % (k, v) for k, v, _, _ in a3_bad)))
        # ★ 销售列（B）新鲜度：外部写入，停摆无自动发现 ⇒ 这里兜底
        if last_b is None:
            flags.append('B列(销售)全空')
        else:
            gap_b = (date.today() - EPOCH).days - last_b
            if gap_b > SALES_DAYS:
                flags.append('B列(销售) %d 天未更新(最后%s)' % (gap_b, ds(last_b)))

        if flags:
            out('%-8s ⚠ %s' % (st, '；'.join(flags)))
            for kind, ts, tk, v, gv in det[:6]:
                out('           [%s] %s %-5s 应卸%10.2f 实取%10.2f' % (kind, ds(ts), tk, v, gv))
            if len(det) > 6:
                out('           …另 %d 处' % (len(det) - 6))
            for f in flags:
                if '键不匹配' in f:
                    alerts.append((st, 'KEY_MISMATCH', '%d 处' % km))
                elif '数量不符' in f:
                    alerts.append((st, 'MISMATCH', '%d 处' % mm))
                elif '未更新' in f:
                    alerts.append((st, 'STALE', f))
                elif '罐表有值源无记录' in f:
                    alerts.append((st, 'ORPHAN', '%d 处' % orph))
                elif 'A3异常' in f:
                    alerts.append((st, 'A3_BAD', f.split('(')[1].rstrip(')')))
                elif 'B列(销售)' in f:
                    alerts.append((st, 'SALES_STALE', f))
            # A3 异常明细
            for kind, nm, sid, a3 in a3_bad:
                if kind == 'DUP':
                    other = a3_seen.get(a3, '?')
                    out('           [A3重复] ws=%s %s 的 A3=%r 与 %s 相同 ⇒ 卸油量双算' % (
                        sid, nm[:16], a3, other[:16]))
                else:
                    out('           [A3为空] ws=%s %s ⇒ $A5&$A$3 只剩日期，永远取不到卸油量' % (
                        sid, nm[:16]))
            if a.fix and km:
                out('           → 自动修复（调用 _fix_tank_ghi_keyfmt.py）…')
                r = subprocess.run(
                    [sys.executable, os.path.join(HERE, '_fix_tank_ghi_keyfmt.py'),
                     '--stations', st],
                    capture_output=True, timeout=1800)
                tail = (r.stdout or b'').decode('utf-8', 'replace').strip().splitlines()
                for line in tail[-3:]:
                    out('             %s' % line)
        else:
            out('%-8s ✔ 健康（源%d条/罐表%d组，最后记录 %s）' % (
                st, len(src), len(got), ds(lastts) if lastts else '—'))

    out('')
    out('=' * 106)
    # 退出码语义（供云端定时任务判断）：
    #   1 = 有「数据错误类」告警（键不匹配/数量不符/A3异常/销售列停更）⇒ 需立即处理
    #   0 = 健康，或仅有「业务未录入类」提示（收油表未更新/孤立值）⇒ 打印即可
    CRITICAL = ('KEY_MISMATCH', 'MISMATCH', 'A3_BAD', 'SALES_STALE')
    crit = [x for x in alerts if x[1] in CRITICAL]
    soft = [x for x in alerts if x[1] not in CRITICAL]
    if alerts:
        out('体检结论：%d 项告警（其中数据错误类 %d 项）' % (len(alerts), len(crit)))
        for st, kind, msg in alerts:
            mark = '★' if kind in CRITICAL else ' '
            out('  %s %-9s %-14s %s' % (mark, st, kind, msg))
        out('')
        out('修复命令：python scripts/_fix_tank_ghi_keyfmt.py            # 键不匹配')
        out('          python scripts/_fix_tank_a3.py                     # A3 异常（先确认）')
        out('          python scripts/_sync_sales_to_tanks.py             # 销售列停更')
        out('=' * 106)
        return 1 if crit else 0
    out('体检结论：全部 %d 站健康 ✔' % len(sts))
    out('=' * 106)
    return 0


if __name__ == '__main__':
    sys.exit(main())
