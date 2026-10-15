# -*- coding: utf-8 -*-
"""_form_report.py —— 液位仪表单提交日报（读 A 表 .dbt，替代旧的落表监控）

背景（2026-10-15）：表单真实落点是 A 表（.dbt），旧监控读落表 .ksheet 永远 0 条误报。
本脚本用 `dbsheet list-records` 直读 A 表，输出中文日报。

输出：
  总提交数 / 今日提交数 / 各站今日分布 / 近 7 日每日分布 /
  空记录（无站点或罐号）/ 同站同日同罐重复提交 / 从未提交的站。

用法：python scripts/_form_report.py
"""
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KDOCS = os.environ.get("KDOCS_CLI") or os.path.join(os.path.dirname(HERE), "bin", "kdocs-cli.exe")
KDOCS_TOKEN = (os.environ.get("KDOCS_TOKEN") or "").strip()  # 云端 CI 无 keychain，走 --token
A_FILE = "HjR51pWq9xMTdtJwgEPmxx5SUw1a2WVPc"
A_SHEET = 2

ALL_STATIONS = ["秦岭", "宁陕", "洋县", "汉中", "富平", "韩城", "富县",
                "南沙", "华山", "白河", "旬阳", "略阳", "玉华宫", "照金", "天汉水城"]


def out(s):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(s)


def cli(args, timeout=240):
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


def main():
    j, err = cli(["dbsheet", "list-records", json.dumps({"file_id": A_FILE, "sheet_id": A_SHEET})])
    if not j:
        out("✘ 读 A 表失败: " + err)
        return 1
    recs = ((j.get("data") or {}).get("detail") or {}).get("records")
    if recs is None:
        recs = ((j.get("data") or {}).get("data") or {}).get("records") or []

    today = datetime.date.today().strftime("%Y/%m/%d")
    total = len(recs)
    today_rows = []
    empty = 0
    dup = {}
    day_cnt = {}
    station_cnt = {}

    for r in recs:
        f = r.get("fields", {}) or {}
        st = str(f.get("站点", "") or "").strip()
        dt = str(f.get("日期", "") or "").strip()
        tk = str(f.get("罐号", "") or "").strip()
        submit = str(f.get("提交时间", "") or "").strip()
        # 提交日期（提交时间前缀 YYYY/MM/DD）
        sd = submit[:10] if submit else dt
        if not st or not tk:
            empty += 1
            continue
        day_cnt[sd] = day_cnt.get(sd, 0) + 1
        if st in ALL_STATIONS:
            station_cnt[st] = station_cnt.get(st, 0) + 1
        if dt == today:
            today_rows.append((st, tk, submit))
        # 同站同日同罐重复（多值才告警）
        key = (st, dt, tk)
        dup.setdefault(key, []).append(submit)

    out("=" * 60)
    out("液位仪表单提交日报  %s" % today)
    out("=" * 60)
    out("A 表总提交数：%d（含空记录 %d 条）" % (total, empty))
    out("今日（%s）提交：%d 条" % (today, len(today_rows)))
    out("")
    out("— 今日各站分布 —")
    today_by_st = {}
    for st, tk, sub in today_rows:
        today_by_st.setdefault(st, []).append(tk)
    if today_by_st:
        for st in sorted(today_by_st):
            out("  %-6s %2d 罐：%s" % (st, len(today_by_st[st]), "、".join(today_by_st[st])))
    else:
        out("  （今日暂无提交）")
    out("")
    out("— 近 7 日每日提交数 —")
    for d in sorted(day_cnt)[-7:]:
        out("  %s  %d 条" % (d, day_cnt[d]))
    out("")
    # 重复提交告警（同站同日同罐 >=2 条且值不同）
    out("— 重复提交（同站同日同罐 ≥2 条）—")
    dup_hit = {k: v for k, v in dup.items() if len(v) >= 2}
    if dup_hit:
        for (st, dt, tk), subs in dup_hit.items():
            out("  %s %s %s × %d 次" % (st, dt, tk, len(subs)))
    else:
        out("  无")
    out("")
    # 从未提交的站
    never = [s for s in ALL_STATIONS if s not in station_cnt]
    out("— 从未提交的站（%d）—" % len(never))
    out("  " + ("、".join(never) if never else "无"))
    out("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
