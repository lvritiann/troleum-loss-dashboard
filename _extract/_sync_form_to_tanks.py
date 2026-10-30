# -*- coding: utf-8 -*-
"""_sync_form_to_tanks.py —— kdocs-cli 版液位回写（读 A 表 → 写各站罐表 C/D/E/F）

背景（2026-10-15 根治定案）：
  表单「液位仪数据采集表」的真实落点是多维表 A 表（.dbt），不是落表 .ksheet。
  原回写脚本 airscript_liquid_level_writeback.js 读的是落表 cmOCi38pohLr（永远空），
  且 AirScript 2.0 运行时根本不支持读 .dbt ⇒ 改由本脚本（kdocs-cli）直读 A 表。

数据源：A 表「液位仪数据采集表」
  file_id = HjR51pWq9xMTdtJwgEPmxx5SUw1a2WVPc , sheet_id = 2
  用 `dbsheet list-records` 读取全部提交记录（★ 必须能取全 712 条，见 list_records 注释）。

映射：
  站点(J)  → 优化表 file_id（OPT）；站点名先归一（华山北/华山南站 → 华山）
  罐号(L)  → 罐表 sheetId（endswith 匹配 '🛢️ 东站1#罐'，sheetId>=3）
             直配失败且本站罐表前缀集合只有「南站/北站」时，东站x#罐 → 南站x#罐（兜底，仅华山误选场景，
             每次兜底都进告警清单；西站x#罐一律不猜）
  日期(K)  → 罐表 A 列行号（日期序列号匹配）

写（列 0-based，只写 C..F，**绝不碰 M 列公式**）：
  C(2) 试枪回罐(L)  ← 表单「试枪回罐(L)」（字符串，转浮点）
  D(3) 实际库存(L)  ← 表单「实际库存(L)」
  E(4) 密度         ← ①该罐最后一次有效密度（_diag_lv/oil_density_map.json）
                      ②无历史值 ⇒ 按罐表 B3(row=2,col=1) 油品取兜底常量
                      ③油品也识别不出 ⇒ 不写
  F(5) 交班温度(℃)  ← 表单「交班温度(℃)」
  规则：
    · 表单该列为空/None → 不写该列（不误清空）
    · 幂等：目标同值跳过
    · 危险闸门：D/F 源值 0 且目标非零真值 → 跳过（不抹真值）；C 源 0 是常态放行
    · 物理不可信：D<=0 视为缺测，不写（不拿 0 覆盖任何值）
    · E 只填空值，不覆盖已有非空值（已有实测密度不能被标定值抹掉）
    · 去重：同(站|罐表|日期) 后提交者胜，逐列取「最后一个有效值」
    · 写 API `sheet update-range-data` 分片 ≤100 格，写后逐行回读校验
    · MIN_SER=46296（2026-10-01）挡掉 8/25、9/15 的历史测试记录

本机 / 云端通吃：
  kdocs-cli.exe 路径优先取环境变量 KDOCS_CLI（云端 GitHub Actions 指仓库 bin/），
  默认回退项目内 bin/kdocs-cli.exe。

用法：
  python scripts/_sync_form_to_tanks.py --dry              # 只读预览（不写）
  python scripts/_sync_form_to_tanks.py                     # 实写全部
  python scripts/_sync_form_to_tanks.py --stations 秦岭,白河
  python scripts/_sync_form_to_tanks.py --since 3           # 只处理最近 3 天的提交
  python scripts/_sync_form_to_tanks.py --dry --report _lv_dry_report.json

★ 双通道（2026-10-20 加，--channel 默认 cli，不加该参数时行为与本文件锁版完全一致）：
  python scripts/_sync_form_to_tanks.py --channel airscript --handshake        # 零写入握手（只验版本）
  python scripts/_sync_form_to_tanks.py --channel airscript --dry --batch 2    # 批2 只读演练
  python scripts/_sync_form_to_tanks.py --channel airscript --fill-only        # 首轮保守实写（两批）
  python scripts/_sync_form_to_tanks.py --channel airscript --as-request-out req.json   # 只落请求体不发
  python scripts/_sync_form_to_tanks.py --channel airscript --as-response-in resp.json  # 回放回执验断言
  AirScript 通道下 Python 只做表单侧（读 A 表 / 站点归一 / 罐号→罐表 / 去重融合），
  罐表侧（找行/读数/闸门/写入/回读）交给云端槽位 B 脚本，一轮只花 2 次 HTTP，
  不消耗 kdocs-cli 日配额。退出码 5/6/7/8 见 AS_* 常量（6/7/8 应回落 --channel cli 重跑）。
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
ROOT = os.path.dirname(HERE)
KDOCS = os.environ.get("KDOCS_CLI") or os.path.join(ROOT, "bin", "kdocs-cli.exe")
KDOCS_TOKEN = (os.environ.get("KDOCS_TOKEN") or "").strip()  # 云端 CI 无 keychain，走 --token

# A 表（表单真实落点）
A_FILE = "HjR51pWq9xMTdtJwgEPmxx5SUw1a2WVPc"
A_SHEET = 2

# 最近一次 list-records 的取数统计（表头打印用，便于一眼确认翻页是否取全）
_LAST_FETCH = {"fetched": 0, "parsed": 0, "dropped": 0}

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

# ★ 分页：实测 page_size=1000 一次可取全 712 条（服务端取完后不再返回 offset 游标）；
#   默认 page_size=100 只回 100 条 ⇒ 旧版只处理第 1 页，709 条里 626 条从未落表。
PAGE_SIZE = 1000        # 首选：一次取全
PAGE_SIZE_SAFE = 100    # 服务端若拒收 1000 则回退分页（游标形如 "B5"，非数字）
MAX_PAGES = 50          # 防御死循环
MAX_CELLS_PER_WRITE = 100

# ★ 站点名归一：表单下拉里华山被拆成三个选项，实际都是同一个站的 12 张罐表。
STATION_ALIAS = {"华山北": "华山", "华山南站": "华山"}

# ★ E 列兜底密度（kg/m³）：罐无历史实测密度时按 B3 油品给标定值。
#   注意：oil_density_map.json 的 oil_density 段 0#柴油 记的是 825.0（历史实测中位数），
#   与本表 830 不一致，本表按定稿取 830（若需改回 825 只动这里一处）。
# ★ 兜底密度（kg/m³）—— 口径混杂，逐条注明，禁止日后"统一"时改错：
#   0#柴油   830  末值众数（20/56 罐），可信
#   92#汽油  740  末值众数（15/39）
#   95#汽油  745  末值众数（7/26）
#   -20#柴油 815  ⚠ 参考值：来自 map 的 uniq 加权众数，但实测仅 1 个样本且该样本值为 830
#                 ⇒ 无可靠依据，待实测替换
#   -10#柴油 822  ⚠ 行业参考值，非实测（冬季牌号密度略低于 0#，介于 0#=830 与 -20#=815 之间）
#   -35#柴油 810  ⚠ 行业参考值，非实测（比 -20# 更低）
#   E 列不参与 M~P 损溢公式，仅台账参考；参考值写错不影响损溢计算，但影响观感与历史连续性。
FALLBACK_DENSITY = {
    "0#柴油": 830.0,
    "-10#柴油": 822.0,
    "-20#柴油": 815.0,
    "-35#柴油": 810.0,
    "92#汽油": 740.0,
    "95#汽油": 745.0,
}

# ★ 只认这几个「牌号 + 大类」组合；未登记的一律不猜（team-lead 2026-10-20 定稿）。
#   背景：`-10#柴油`/`-35#柴油` 以前被静默归成 0#柴油(830) —— 而且因为「识别成功」，
#   连 no_density 告警都不会出，写错了没人知道。11 月北方站换 -10# 是常态。
OIL_GRADES = {
    ("0", "柴油"): "0#柴油",
    ("-10", "柴油"): "-10#柴油",
    ("-20", "柴油"): "-20#柴油",
    ("-35", "柴油"): "-35#柴油",
    ("92", "汽油"): "92#汽油",
    ("95", "汽油"): "95#汽油",
}
# E 列密度合理区间（kg/m³）：汽油 720~780，柴油 810~860 ⇒ 放宽容差后 600~1200，超出视为脏值
E_MIN, E_MAX = 600.0, 1200.0
# 按油品类别收紧：汽油/柴油密度不可能互换，跨类即视为脏值（实测踩到过）
DENSITY_RANGE = {"汽油": (700.0, 790.0), "柴油": (800.0, 870.0)}

# 罐表行内列位（0-based）
COL_TAG = {2: "C", 3: "D", 4: "E", 5: "F"}
COL_DATE = 0     # A 列
COL_OIL = 1      # B 列（油品在 B3 = row 2）
ROW_OIL = 2      # B3
A_ROW_FROM, A_ROW_TO = 4, 500   # A 列日期扫描区（Excel A5..A501）；旧值 374 零余量，
                                # 实测 187 张罐表里有 3 张最后非空行正好 374（QA P3-5）

# ★ 物理合理性闸门（规范第 6 条的加严版）：只拦"一眼不可能"的值，全部进告警清单待人工核对。
#   实测依据：15 站单罐实际库存在 6792~47192 L 区间；试枪回罐 0~150 L；交班温度 10~30℃。
D_MAX = 100000          # 单罐库存上限(L)，留 2 倍余量；实测最大值 47192
C_MAX = 20000           # 试枪回罐上限(L)
F_MIN, F_MAX = -30.0, 60.0
#   与"目标现值非零"相比超出该倍数 ⇒ 疑似多打/少打一位。
#   实测拦下的真实脏值：白河 南站1#罐 2026/10/16 目标 30021.2 / 表单 300212（多打一位，10 倍）。
#   倍数带取 0.2~5 而非更紧，是为了不误拦华山北站2#罐 10/16 这类真实补录（17283.5 → 36024.8 = 2.08 倍）。
RATIO_LO, RATIO_HI = 0.2, 5.0

VERBOSE = bool(os.environ.get("FORM_SYNC_VERBOSE"))
_WARN = []          # 告警清单（结尾统一打印 + 可落 JSON）

# 瞬时限流重试（429002）；日配额 429001 不在重试范围
RETRY_CODES = (429002,)
RETRY_ATTEMPTS = 3
RETRY_WAIT = 3.0

# ★ 配额类错误白名单（只认 code，**不看文案**）。
#   P2-2：旧实现用「msg 里含『上限』/『限制』」判配额，而 page_size=1000 被拒的文案本身就是
#   「page_size 超出上限，最大 100」⇒ 被误判成配额 ⇒ 直接中止，100 分页兜底永远走不到。
QUOTA_CODES = (429001, 429002)

# 退出码约定（供 CI 判成败，dashboard/.github/workflows/liquid.yml 靠它）
EXIT_OK = 0
EXIT_WRITE_FAIL = 1        # 写分片被拒（重跑本批通常有效）
EXIT_ABORT = 2             # 读表失败 / 配额耗尽，未写入
EXIT_UNVERIFIED = 3        # 写了但回读没验住（重跑通常无效，必须人工核对罐表真实值）
EXIT_BOTH = 4              # 写被拒 + 未验住**同时存在**（先核对 unverified_rows，再重跑补写失败部分）


def warn(kind, msg):
    _WARN.append({"kind": kind, "msg": msg})


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


def cli_retry(args, timeout=240, attempts=RETRY_ATTEMPTS):
    """只对**瞬时限流**(429002)退避重试；429001 是当日配额，重试无意义 ⇒ 直接抛给上层。

    实测：连续调用会返回 code=429002「频繁触发调用限制」；日配额耗尽返回 429001。
    """
    last_j, last_err = None, ""
    for k in range(attempts):
        j, err = cli(args, timeout)
        code = int((j or {}).get("code") or 0)
        last_j, last_err = j, err
        if j is not None and code not in RETRY_CODES:
            return j, err
        if k < attempts - 1:
            if VERBOSE:
                out("[retry] code=%s，%.0fs 后重试（%d/%d）" % (code, RETRY_WAIT * (k + 1), k + 1, attempts))
            time.sleep(RETRY_WAIT * (k + 1))
    return last_j, last_err


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
        try:                                    # 防 2026/02/30 这类非法日期把整个同步崩掉
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        n = (d - datetime.date(1899, 12, 30)).days
        return n if LO <= n <= HI else None
    return None


def num(v):
    """→ 浮点；None/空/非数字 → None（区别于 0，用于「表单没填就不写」）。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt(v):
    """浮点 → 写单元格用的文本（整数值不留 .0）。"""
    if v is None:
        return ""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return ("%.4f" % v).rstrip("0").rstrip(".")


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


def norm_station(s):
    """站点名归一（华山北/华山南站 → 华山）。"""
    return STATION_ALIAS.get(s, s)


def norm_oil(v):
    r"""B3 油品文本 → `FALLBACK_DENSITY` 的键；**未登记牌号一律返回 None（不猜）**。

    识别规则：正则取「牌号」(`-?\d+#`，兼容 `N号`)，再看大类是汽油还是柴油，
    两者都必须在 `OIL_GRADES` 里登记过才算成功。
      已登记：0#柴油 / -20#柴油 / 92#汽油 / 95#汽油（判定结果与旧版完全一致）
      不猜：`-10#`、`-35#`、`98#`、「10号柴油」、只写「柴油」不写牌号 等
    返回 None 时由调用方按「① 该罐历史密度 carry-forward → ② 都没有则不写 E + no_density 告警」处理。
    """
    s = re.sub(r"\s+", "", str(v or "")).replace("－", "-").replace("—", "-").replace("　", "")
    if not s:
        return None
    m = re.search(r"(-?\d+)\s*#", s) or re.search(r"(-?\d+)\s*号", s)
    kind = "汽油" if "汽油" in s else ("柴油" if "柴油" in s else None)
    if not m or not kind:
        return None
    return OIL_GRADES.get((str(int(m.group(1))), kind))


def oil_class(oil):
    """规范油品名 → 大类（汽油/柴油）；未知返回 None。"""
    if not oil:
        return None
    if oil.endswith("汽油"):
        return "汽油"
    if oil.endswith("柴油"):
        return "柴油"
    return None


def _load_density_map():
    """读 _diag_lv/oil_density_map.json（180 罐的 sheetId/油品/最后一次有效密度）。缺失不致命。

    路径解析顺序（本机 scripts/ 与云端 dashboard/_extract/ 两处副本共用）：
      1) 环境变量 FORM_SYNC_DENSITY_MAP 指定的文件
      2) <脚本父目录>/_diag_lv/oil_density_map.json      ← 本机 scripts/ 版命中
      3) <脚本父目录的父目录>/_diag_lv/oil_density_map.json ← dashboard/_extract/ 版命中
      4) <脚本目录>/oil_density_map.json（云端可把它一起放进抽取目录）
    都找不到 ⇒ 返回空表，E 列全部改走 B3 油品兜底（不会写错，只是少了历史实测密度）。
    """
    cands = [os.environ.get("FORM_SYNC_DENSITY_MAP") or "",
             os.path.join(ROOT, "_diag_lv", "oil_density_map.json"),
             os.path.join(os.path.dirname(ROOT), "_diag_lv", "oil_density_map.json"),
             os.path.join(HERE, "oil_density_map.json")]
    path = next((p for p in cands if p and os.path.isfile(p)), None)
    if not path:
        warn("density_map", "未找到密度映射文件（试过 %d 个路径），E 列将全部走 B3 兜底"
             % len([c for c in cands if c]))
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d.get("tanks"), dict):
            warn("density_map", "密度映射文件结构异常，E 列将全部走 B3 兜底：%s" % path)
            return {}
        if VERBOSE:
            out("[density_map] 使用 %s（%d 站）" % (path, len(d["tanks"])))
        return d["tanks"]
    except Exception as e:
        warn("density_map", "读密度映射失败（E 列将全部走 B3 兜底）：%s → %r" % (path, e))
        return {}


def map_density(tanks, station, tank_name):
    """查该罐历史最后一次有效密度；无 → None。tank_name 形如 '🛢️ 南站1#罐' 或 '南站1#罐'。"""
    if not tanks:
        return None
    per = tanks.get(station) or {}
    bare = re.sub(r"^[^东-龥]*", "", tank_name).strip()
    for key in (tank_name, "🛢️ " + bare, "🛢️" + bare, bare):
        e = per.get(key)
        if e:
            v = e.get("last_density")
            try:
                v = float(v) if v is not None else None
            except (TypeError, ValueError):
                v = None
            if v is not None and v > 0:
                return v
            return None
    return None


def list_records():
    """读 A 表**全部**记录，返回 [ {station,raw_station,dateStr,tank,C,D,F,id} ]。

    ★ 2026-10-20 P0 修复：`dbsheet list-records` 默认 page_size=100 ⇒ 旧实现只拿到第 1 页 100 条
      （全表 712 条），page 1 之外的提交（韩城全部、宁陕/洋县/汉中/照金/天汉水城 等）从未落到罐表。
      现改为 page_size=1000 一次取全（实测可回 712 条且不返回 offset 游标），
      并保留 offset 游标翻页作为兜底（游标是 "B5" 这类非数字字符串，首屏传空串）。
      跨页按记录 id 去重，防重复计数。
    """
    recs, seen, offset, page, page_size = [], set(), "", 0, PAGE_SIZE
    while True:
        page += 1
        j, err = cli_retry(["dbsheet", "list-records", json.dumps({
            "file_id": A_FILE, "sheet_id": A_SHEET,
            "page_size": page_size, "offset": offset})])
        if not j:
            raise RuntimeError("list-records 失败(page %d): %s" % (page, err))
        # ★★ 业务错误只置 code!=0、**不抛异常**，若不判就会静默返回 0 条，
        #    看上去"今天没人填报"（实测 code=429002 限流 / 429001 日配额 均如此）。
        code = int(j.get("code") or 0)
        if code != 0:
            msg = j.get("message") or j.get("msg") or ""
            detail = j.get("data") or {}
            # P2-2：配额判定**只认 code 白名单**；其余业务错误一律视为「参数/请求被拒」⇒ 回退分页一次。
            quota = code in QUOTA_CODES
            if page == 1 and page_size != PAGE_SIZE_SAFE and not quota:
                warn("paging", "page_size=%d 被拒（code=%s msg=%s）⇒ 回退 page_size=%d 重新翻页"
                     % (page_size, code, msg, PAGE_SIZE_SAFE))
                page_size, page, offset = PAGE_SIZE_SAFE, 0, ""
                continue
            raise RuntimeError("list-records 业务错误 page=%d code=%s msg=%s detail=%s"
                               % (page, code, msg, json.dumps(detail, ensure_ascii=False)[:200]))
        det = ((j.get("data") or {}).get("detail") or (j.get("data") or {}).get("data") or {})
        chunk = det.get("records") or []
        for r in chunk:
            rid = str(r.get("id") or "")
            if rid:
                if rid in seen:
                    continue
                seen.add(rid)
            recs.append(r)
        nxt = str(det.get("offset") or det.get("next_offset") or "")
        if not chunk:
            break
        # ★ 终止条件**主判据是「本页条数 < page_size」**（product-architect 2026-10-20 建议）：
        #   实测 page_size=1000 取完 712 条时 detail.offset 为 null；若只用「有没有游标」判断，
        #   在「恰好整页 + 服务端没给游标」的边界上会误判成结束（历史上就这么丢过第 2 页）。
        #   条数不足一页 ⇒ 确定取完，直接停。
        if len(chunk) < page_size:
            break
        if not nxt or nxt == offset:
            # 整页却没有游标 / 游标原地不动：异常，留痕后停（不静默截断）。
            if nxt == offset and offset:
                warn("paging", "第 %d 页游标没有前进（offset=%r），已取 %d 条，可能漏读，请核对记录总数"
                     % (page, nxt, len(recs)))
            else:
                warn("paging", "第 %d 页返回 %d 条（=page_size %d）且无游标，可能被服务端截断，请核对记录总数"
                     % (page, len(chunk), page_size))
            break
        offset = nxt
        if page >= MAX_PAGES:
            warn("paging", "翻页达上限 %d 页强制停止，共 %d 条（可能不完整）" % (MAX_PAGES, len(recs)))
            break
    # ★ 必改 1：这行**常开**打印（不再只在 FORM_SYNC_VERBOSE 下出现）。
    #   运维靠它一眼确认「翻页到底有没有取全」：条数掉到 100 就是分页又坏了。
    out("[list-records] 翻页 %d 页 / page_size=%d，共取到 %d 条" % (page, page_size, len(recs)))

    rows = []
    for r in recs:
        f = r.get("fields", {}) or {}
        raw_st = str(f.get("站点", "") or "").strip()
        dt = str(f.get("日期", "") or "").strip()
        tk = str(f.get("罐号", "") or "").strip()
        if not raw_st or not dt or not tk:
            warn("dirty", "缺站点/日期/罐号，跳过：id=%s 站点=%r 日期=%r 罐号=%r"
                 % (r.get("id"), raw_st, dt, tk))
            continue
        st = norm_station(raw_st)
        if st != raw_st:
            warn("station_alias", "站点名归一：%s → %s（id=%s 日期=%s 罐=%s）" % (raw_st, st, r.get("id"), dt, tk))
        rows.append({
            "station": st, "raw_station": raw_st, "dateStr": dt, "tank": tk,
            "C": num(f.get("试枪回罐(L)")),
            "D": num(f.get("实际库存(L)")),
            "F": num(f.get("交班温度(℃)")),
            "id": str(r.get("id") or ""),
        })
    _LAST_FETCH["fetched"] = len(recs)
    _LAST_FETCH["parsed"] = len(rows)
    _LAST_FETCH["dropped"] = len(recs) - len(rows)
    return rows


def list_sheets(fid):
    """站 file_id → [(sheetId, sheetName)]（只留 sheetId>=3 且非汇总表）。"""
    j, err = cli_retry(["sheet", "get-sheets-info", json.dumps({"file_id": fid})])
    if not j:
        raise RuntimeError("get-sheets-info 失败 %s: %s" % (fid, err))
    if int(j.get("code") or 0) != 0:
        raise RuntimeError("get-sheets-info 业务错误 code=%s msg=%s"
                           % (j.get("code"), j.get("message") or j.get("msg")))
    res = []
    for s in ((j.get("data") or {}).get("detail") or {}).get("sheetsInfo") or []:
        name = s.get("sheetName") or ""
        sid = s.get("sheetId") or 0
        if sid >= 3 and not any(h in name for h in SKIP_HINTS):
            res.append((sid, name))
    return res


def sheet_prefixes(names):
    """罐表名 → 前缀集合（东站/西站/南站/北站）。"""
    p = set()
    for n in names:
        m = re.search(r"(东站|西站|南站|北站)", n)
        if m:
            p.add(m.group(1))
    return p


def resolve_tank(sheets, tank):
    """罐号 → (sheetId, sheetName, how)；命中多个或命中不到返回 (None, None, reason)。

    how: direct / fallback-east-to-south / ambiguous / ambiguous-fallback / no-match-would-fallback / no-match
    规则（定稿）：
      1) 直配优先：罐表名 endswith(tank) 且唯一 ⇒ 用它；
      2) 直配失败且本站罐表前缀集合恰为 {南站,北站}（本站根本没有东站罐表）时，
         才把「东站x#罐」→「南站x#罐」；命中多个则放弃；
      3) 西站x#罐 在无西站罐表的站一律不猜；
      4) 其它站真实存在的东站罐表走直配，绝不被兜底规则误伤。
    """
    names = [n for _, n in sheets]
    hits = [(sid, n) for sid, n in sheets if n.endswith(tank)]
    if len(hits) == 1:
        return hits[0][0], hits[0][1], "direct"
    if len(hits) > 1:
        return None, None, "ambiguous"

    m = re.match(r"^东站(\d+#罐)$", tank)
    if m:
        if sheet_prefixes(names) == {"南站", "北站"}:
            cand = [(sid, n) for sid, n in sheets if n.endswith("南站" + m.group(1))]
            if len(cand) == 1:
                return cand[0][0], cand[0][1], "fallback-east-to-south"
            if len(cand) > 1:
                return None, None, "ambiguous-fallback"
            return None, None, "no-match"
        return None, None, "no-match"
    return None, None, "no-match"


def read_range(fid, ws, r0, r1, c0, c1):
    """读一个矩形区，返回 {(row,col): text}（空格不占位）。"""
    j, err = cli_retry(["sheet", "get-range-data", json.dumps({
        "file_id": fid, "worksheet_id": ws,
        "range": {"rowFrom": r0, "rowTo": r1, "colFrom": c0, "colTo": c1},
    })])
    if not j or int(j.get("code") or 0) != 0:
        raise RuntimeError("get-range-data 失败 %s ws=%s: %s"
                           % (fid, ws, err or (j or {}).get("message")))
    m = {}
    for c in ((j.get("data") or {}).get("detail") or {}).get("rangeData") or []:
        r, col_ = c.get("originRow"), c.get("originCol")
        if r is not None and col_ is not None:
            m[(r, col_)] = cell_val(c)
    return m


def decide_rc(write_fail, unverified):
    """退出码判定（纯函数，便于单测）。

    0 全部验住
    1 只有写分片被拒   ⇒ 重跑本批通常有效
    3 只有回读没验住   ⇒ 重跑通常无效，必须翻 --report.unverified_rows 逐行核对罐表真实值
    4 两者同时存在     ⇒ 先核对、再重跑补写失败部分（两者的运维动作不同，所以不能只报一个）
    """
    if unverified and write_fail:
        return EXIT_BOTH
    if unverified:
        return EXIT_UNVERIFIED
    if write_fail:
        return EXIT_WRITE_FAIL
    return EXIT_OK


def readback_mismatch(ops, after):
    """回读比对（纯函数）。

    ★ 必改 3：写入值与回读值**都按 fmt() 的同一种文本口径**比，绝不"一边 fmt 一边用原值"。
      否则 5 位以上小数的填报值（落表被四舍五入）会算出 1e-5 差异 ⇒ 假红灯 rc=3。
    """
    bad = []
    for ci, want in ops:
        got = after.get(ci)
        if got is None or fmt(got) != fmt(want):
            bad.append((ci, fmt(want), got))
    return bad


def e_occupied(raw, val):
    """E 列是否「已有值」——P2-1：看**单元格原始文本非空**，不看数值能否解析。

    文本型合法现值（"830 kg/m³" / "830" 文本 / "—" / "待标定"）一律算已有值 ⇒ 保留不覆盖；
    只有真空格与数值 0（密度 0 不可能，属脏值）才允许填。
    """
    return (raw not in (None, "")) and (val != 0)


ROW_READ_SPAN = 250   # 单次 get-range-data 的行跨度上限（≤250 行 × 4 列 = 1000 格，低于 1217 静默截断阈值）
# 截断判据用「返回的 max(originRow) 是否达到请求尾部」，**不用格数**（rangeData 只返回非空格，
# 用格数判会天天假警报）。但「尾部本来为空」也是常态（我们的目标行多数本就是空格），
# 所以再加一个近阈值前提 len(cells) >= 900 才判为疑似截断，避免把「正常空尾」当截断而跳过整表。
TRUNCATE_SUSPECT = 900


def tank_density(oil_raw, mdens):
    """E 列密度判定（**纯函数**：无 I/O、无全局状态、无模块缓存）。

    入参只有两个「已拿到的数据」：
      oil_raw : 罐表 B3 的**原始文本**（如 "0#柴油" / "-10#柴油" / ""）
      mdens   : 该罐**历史最后有效密度**（来自 oil_density_map.json，无则 None）

    返参三元组 (dens, dsrc, note)：
      dens  : 应写入 E 列的值（None = 不写）
      dsrc  : 来源标签，直接进日志「E来源:xxx」（"map" / "oil:0#柴油" / "map(油品未知)" / None）
      note  : 需要调用方**发告警**的情形，元组（空元组 = 无需告警；可同时出现多条）
              "density_conflict" 历史密度与当前 B3 油品跨类 ⇒ 已弃用历史值，改走兜底
              "oil_unknown"      B3 油品识别不出，但沿用了历史密度
              "no_density"       两者都没有 ⇒ E 列不写

    ★ 设计约束（product-architect 2026-10-20 要求）：本函数要作为 AirScript/JS 版实现的
      **行为基准**做 1:1 对照，所以不准在内部读 `oil_cache` / `tanks_map` / 任何模块级变量。
      B3 的读取与历史密度的查表都在调用方完成后传入。
    """
    oil = norm_oil(oil_raw)
    lo, hi = DENSITY_RANGE.get(oil_class(oil), (E_MIN, E_MAX))
    if mdens is not None and lo <= mdens <= hi:
        return mdens, "map", ()
    notes = []
    if mdens is not None:                     # 历史密度存在但与 B3 油品跨类 ⇒ 弃用
        notes.append("density_conflict")
    fb = FALLBACK_DENSITY.get(oil) if oil else None
    if fb is not None:
        return fb, "oil:" + oil, tuple(notes)
    if mdens is not None and E_MIN <= mdens <= E_MAX:
        # 防御分支：油品未知时上面的宽区间已覆盖 600~1200，实际很难走到这里。
        # 保留是为了与历史版本行为一致（JS 版照此实现即可，但不必为该分支写用例）。
        return mdens, "map(油品未知)", tuple(notes + ["oil_unknown"])
    return None, None, tuple(notes + ["no_density"])


def read_rowvals_batch(fid, ws, rows_sorted):
    """按表**批量**读目标行现值：{row: {"num": {col: float|None}, "raw": {col: text}}}。

    ★ 2026-10-20 晚压缩调用：原实现逐行 `read_range(row,row,2,5)`，全量 618 次/轮。
      改为按行跨度分段读（默认 250 行/段 ≤1000 格），本批数据每表只有 7 行 ⇒ 每表 1 次。
    rows_sorted 必须升序（调用方保证）。返回结构与 `rowval_cache` 完全一致，下游逻辑不用改。
    """
    res = {}
    i = 0
    while i < len(rows_sorted):
        r0 = rows_sorted[i]
        j = i
        while j + 1 < len(rows_sorted) and rows_sorted[j + 1] - r0 + 1 <= ROW_READ_SPAN:
            j += 1
        seg = rows_sorted[i:j + 1]
        r1 = seg[-1]
        g = read_range(fid, ws, r0, r1, 2, 5)           # 异常向上抛，由调用方决定跳过整表
        # ★ 截断校验：尾部没读到 且 返回格数接近截断阈值 ⇒ 对半重读一次；仍不齐 ⇒ 返回 None（跳过该表）
        max_r = max((rr for (rr, cc) in g), default=-1)
        if max_r < r1 and len(g) >= TRUNCATE_SUSPECT:
            mid = (r0 + r1) // 2
            g1 = read_range(fid, ws, r0, mid, 2, 5)
            g2 = read_range(fid, ws, mid + 1, r1, 2, 5)
            g = dict(g1)
            g.update(g2)
            max_r = max((rr for (rr, cc) in g), default=-1)
            if max_r < r1:
                warn("range_truncated", "表 ws=%s 请求 %d..%d 只回到 %d（疑似静默截断）⇒ 本次跳过该表不写"
                     % (ws, r0, r1, max_r))
                return None
        for rr in seg:
            res[rr] = {"num": {}, "raw": {}}
        for (rr, cc), v in g.items():
            if rr in res:
                res[rr]["num"][cc] = num(v)
                res[rr]["raw"][cc] = v
        i = j + 1
    return res


def _sanity(ci, want, cur):
    """物理合理性闸门：返回拦截原因（None=放行）。只拦一眼不可能的值，不拦正常补录/修正。"""
    if ci == 2:                                  # C 试枪回罐
        if want < 0 or want > C_MAX:
            return "超出合理范围 0~%d L" % C_MAX
        return None
    if ci == 3:                                  # D 实际库存
        if want > D_MAX:
            return "超出单罐库存上限 %d L（疑似多打一位）" % D_MAX
        if cur not in (None, 0) and cur != 0:
            r = want / float(cur)
            if r < RATIO_LO or r > RATIO_HI:
                return "与目标现值 %s 相差 %.2f 倍（目标 %s / 表单 %s），疑似录入错误" % (
                    fmt(cur), r, fmt(cur), fmt(want))
        return None
    if ci == 5:                                  # F 交班温度
        if want < F_MIN or want > F_MAX:
            return "超出合理温度范围 %d~%d ℃" % (F_MIN, F_MAX)
        return None
    return None


def _conflict(group, key):
    """同键多条提交里某列是否存在「多个有效值且差异>1%」。"""
    vals = [g[key] for g in group if g[key] is not None]
    if key == "D":
        vals = [v for v in vals if v > 0]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    if hi <= 0:
        return None
    if (hi - lo) / abs(hi) > 0.01:
        return vals
    return None


def fuse(group):
    """同(站|罐表|日期) 多条提交 → 逐列取「最后一个有效值」（D<=0 视为缺测，不能覆盖真值）。"""
    res = {"C": None, "D": None, "F": None}
    for g in group:                      # group 已按 API 返回顺序（=创建时间升序）
        if g["C"] is not None:
            res["C"] = g["C"]
        if g["D"] is not None and g["D"] > 0:
            res["D"] = g["D"]
        if g["F"] is not None:
            res["F"] = g["F"]
    return res


def sync(args):
    global VERBOSE
    VERBOSE = VERBOSE or args.verbose
    since = args.since
    only = set(args.stations) if args.stations else None
    tanks_map = _load_density_map()

    rows = list_records()
    n_raw = len(rows)

    # 日期过滤（--since N：只处理最近 N 天的提交）；排除启用日前的测试记录
    if since:
        cutoff = datetime.date.today() - datetime.timedelta(days=since)
        cut_ser = (cutoff - datetime.date(1899, 12, 30)).days
        rows = [r for r in rows if (to_serial(r["dateStr"]) or 0) >= cut_ser]
    drop_test = [r for r in rows if (to_serial(r["dateStr"]) or 0) < MIN_SER]
    for r in drop_test:
        warn("min_ser", "早于启用日(MIN_SER)的测试记录，跳过：%s %s %s id=%s"
             % (r["raw_station"], r["tank"], r["dateStr"], r["id"]))
    rows = [r for r in rows if (to_serial(r["dateStr"]) or 0) >= MIN_SER]

    out("=" * 100)
    out("液位回写（A 表 → 罐表 C/D/E/F）  %s   记录 %d 条"
        % ("DRY-RUN（只读）" if args.dry else "实写", len(rows)))
    # ★ 必改 1：四段漏斗常开打印。本次 P0 的教训就是「读到的条数要能一眼核对」：
    #   712（A 表返回）→ 711（剔除空记录）→ 709（MIN_SER 过滤）→ 实际处理 len(rows)。
    out("A 表返回 %d 条 → 剔除空记录 %d 条 → 解析 %d 条 → MIN_SER 过滤后 %d 条%s"
        % (_LAST_FETCH["fetched"], _LAST_FETCH["dropped"], _LAST_FETCH["parsed"], len(rows),
           "" if args.since is None else "（--since %d）" % args.since))
    out("=" * 100)

    # ── 1) 站点合法性与站过滤
    by_station = {}
    for r in rows:
        st = r["station"]
        if st not in OPT:
            continue
        if only and st not in only:
            continue
        by_station.setdefault(st, []).append(r)
    for st in sorted({r["station"] for r in rows}):
        if st not in OPT:
            n = len([r for r in rows if r["station"] == st])
            warn("unknown_station", "未登记站点，整站跳过：%r（%d 条）" % (st, n))
    for st in sorted(only or []):
        if st not in OPT:
            warn("bad_arg", "--stations 里有未登记站点名：%r（合法值见 OPT 常量）" % st)

    stats = {"rows": len(rows), "groups": 0, "matched": 0,
             "cell_C": 0, "cell_D": 0, "cell_E": 0, "cell_F": 0,
             "fill": 0, "overwrite": 0, "idem": 0, "keep_existing": 0,
             "danger": 0, "d_implausible": 0, "sanity": 0,
             "no_sheet": 0, "no_row": 0, "nosrc": 0,
             "e_from_map": 0, "e_from_oil": 0, "e_from_guess": 0,
             "e_map_rejected": 0, "e_none": 0, "e_keep": 0,
             "written": 0, "write_fail": 0, "unverified": 0, "e_keep_text": 0}
    detail = []
    per_station = {}
    unverified_rows = []      # P1-1：回读未验住的行明细（写进 --report）
    no_density_list = []      # E 列未写的罐（站/罐/B3 原文），结尾单独一行醒目标出
    snapshot = []             # --snapshot-out：写入前快照（含原始文本）
    a_window = {}             # 站 -> [(罐表名, A 列扫描窗内最后非空行号)]

    a_cache = {}       # (fid, ws) -> {serial: row}
    rowval_cache = {}  # (fid, ws, row) -> {"num": {...}, "raw": {...}}
    oil_cache = {}     # (fid, ws) -> oil text (B3)
    sheets_cache = {}

    # ★ 2026-10-20 晚「压缩调用量」改造：**按表聚合**。
    #   原实现：逐行读现值(618) + 逐行写(530) + 逐行回读(530) ⇒ 全量 1931 次调用；
    #   现实现：每表 1 次批量读现值 + 每表合并写(≤100格/片) + 每表 1 次批量回读 ⇒ 约 610 次。
    #   业务判定（幂等/fill-only 保留/危险闸门/物理校验/E 密度/后提交者胜）一行未改，只把 I/O 聚起来。
    sheet_groups = {}      # (fid, ws) -> [(gst, sname, ser, g)]，按 ser 升序
    for st in sorted(by_station):
        fid = OPT[st]
        # ── 2) 罐号 → 罐表
        if st not in sheets_cache:
            try:
                sheets_cache[st] = list_sheets(fid)
            except Exception as e:
                warn("sheets", "取 %s 工作表清单失败，整站跳过：%s" % (st, e))
                continue
        resolved = {}
        for r in by_station[st]:
            tk = r["tank"]
            if tk not in resolved:
                resolved[tk] = resolve_tank(sheets_cache[st], tk)
            sid, sname, how = resolved[tk]
            if sid is None:
                r["_drop"] = how
            else:
                r["_ws"], r["_sname"] = sid, sname
                if how == "fallback-east-to-south":
                    warn("tank_fallback",
                         "罐号兜底映射（站点误选）：%s %s → %s" % (st, tk, re.sub(r"^[^东-龥]*", "", sname)))
        for tk, (sid, sname, how) in resolved.items():
            if sid is None:
                n = len([r for r in by_station[st] if r["tank"] == tk])
                reason = {"ambiguous": "命中多张罐表，放弃", "ambiguous-fallback": "兜底命中多张，放弃",
                          "no-match": "该站无对应罐表（罐号误选/西站罐无表）"}.get(how, how)
                warn("tank_unmatched", "%s %s ×%d 条 → %s" % (st, tk, n, reason))
                stats["no_sheet"] += n
        kept = [r for r in by_station[st] if not r.get("_drop")]

        # ── 3) 去重（映射后口径：(站|罐表|日期)，后提交者胜，逐列取最后有效值）
        groups = {}
        for r in kept:
            ser = to_serial(r["dateStr"])
            if ser is None:
                stats["nosrc"] += 1
                warn("bad_date", "日期无法解析，跳过：%s %s %r（id=%s）" % (st, r["tank"], r["dateStr"], r["id"]))
                continue
            groups.setdefault((st, r["_ws"], ser), []).append(r)
        for (gst, ws, ser), g in sorted(groups.items()):
            sname = re.sub(r"^[^东-龥]*", "", g[0]["_sname"])
            sheet_groups.setdefault((fid, ws), []).append((gst, sname, ser, g))

    # ── 4) 逐表处理：批量读现值 → 逐组判定 → 合并写 → 批量回读
    for (fid, ws) in sheet_groups:
        gs = sheet_groups[(fid, ws)]
        gst0, sname0 = gs[0][0], gs[0][1]

        # A 列日期（每表 1 次）
        if (fid, ws) not in a_cache:
            m, last_nonempty = {}, 0
            try:
                g0 = read_range(fid, ws, A_ROW_FROM, A_ROW_TO, COL_DATE, COL_DATE)
                for (rr, cc), v in g0.items():
                    if v not in (None, ""):
                        last_nonempty = max(last_nonempty, rr)
                    s2 = to_serial(v)
                    if s2 is not None and s2 not in m:
                        m[s2] = rr
            except Exception as e:
                warn("read_date_col", "%s %s 读 A 列失败：%s" % (gst0, sname0, e))
            a_cache[(fid, ws)] = m
            a_window.setdefault(gst0, []).append((sname0, last_nonempty))
        amap = a_cache[(fid, ws)]

        # 先定行（含 multi_value 告警与融合），再统一批量读现值
        todo = []          # [(gst, sname, ser, g, row, val)]
        for gst, sname, ser, g in gs:
            stats["groups"] += 1
            if len(g) > 1:
                for key, tag in (("C", "试枪回罐"), ("D", "实际库存"), ("F", "交班温度")):
                    cf = _conflict(g, key)
                    if cf:
                        warn("multi_value", "%s %s %s 列%s(%s) 同键多条互不相等的重复提交 %s（取最后有效值 %s）"
                             % (gst, sname, g[0]["dateStr"], COL_TAG[2 if key == "C" else (3 if key == "D" else 5)],
                                tag, [fmt(v) for v in cf], fmt(fuse(g)[key])))
            val = fuse(g)
            row = amap.get(ser)
            if row is None:
                stats["no_row"] += 1
                warn("no_row", "%s %s 罐表无此日期行：%s" % (gst, sname, g[0]["dateStr"]))
                continue
            stats["matched"] += 1
            todo.append((gst, sname, ser, g, row, val))
        if not todo:
            continue

        # ★ 批量读目标行现值（每表 1 次）。失败则**整表跳过**：宁可不写，也不拿空值当"空格子"盲写。
        need_rows = sorted({t[4] for t in todo})
        try:
            got = read_rowvals_batch(fid, ws, need_rows)
        except Exception as e:
            warn("read_row", "%s %s 批量读现值失败（%d 行，整表跳过不写）：%s"
                 % (gst0, sname0, len(need_rows), e))
            continue
        for row, d in got.items():
            rowval_cache[(fid, ws, row)] = d

        sheet_ops = {}     # row -> (ops, gst, sname, dateStr, dsrc)
        for gst, sname, ser, g, row, val in todo:
            cur = rowval_cache[(fid, ws, row)]["num"]
            cur_raw = rowval_cache[(fid, ws, row)]["raw"]


            # ── E 密度（①历史最后有效值 ②B3 油品兜底 ③都没有则不写）
            # ★ 必须做「油品类别一致性」校验：油价密度跨类是不可能值（汽油 ~740 / 柴油 ~830）。
            #   实测有 8 张罐的历史密度与当前 B3 油品跨类（如 95#汽油罐的历史值 830、0#柴油罐的历史值 745），
            #   原因是 B3 油品在 17:05 被改过一批（见 scripts/_b3_backup_20261020_170521.json），
            #   历史密度随之失效 ⇒ 一律改用按 B3 推导的兜底常量，并把冲突写进告警清单。
            # ── E 密度：判定全在纯函数 tank_density()（无 I/O、无全局状态，供 JS 版 1:1 对照）
            #   关于 B3 与油品类别一致性校验的来龙去脉见 tank_density() 文档字符串：
            #   实测有 8 张罐的历史密度与当前 B3 油品跨类（95#汽油罐的历史值 830、0#柴油罐的历史值 745），
            #   原因是 B3 油品在 17:05 被改过一批（scripts/_b3_backup_20261020_170521.json），
            #   历史密度随之失效 ⇒ 跨类时弃用历史值、改走按 B3 推导的兜底常量，并把冲突写进告警清单。
            mdens = map_density(tanks_map, gst, g[0]["_sname"])
            if (fid, ws) not in oil_cache:
                try:
                    g2 = read_range(fid, ws, ROW_OIL, ROW_OIL, COL_OIL, COL_OIL)
                    oil_cache[(fid, ws)] = g2.get((ROW_OIL, COL_OIL)) or ""
                except Exception as e:
                    warn("read_oil", "%s %s 读 B3 油品失败：%s" % (gst, sname, e))
                    oil_cache[(fid, ws)] = ""
            oil_raw = oil_cache[(fid, ws)]
            dens, dsrc, dnotes = tank_density(oil_raw, mdens)
            if dsrc == "map" or dsrc == "map(油品未知)":
                stats["e_from_map"] += 1
            elif dsrc:
                stats["e_from_oil"] += 1
                stats["e_from_guess"] += 1        # ★ 写的是"参考值/兜底常量"，不是历史实测
            for _n in dnotes:
                if _n == "density_conflict":
                    stats["e_map_rejected"] += 1
                    warn("density_conflict", "%s %s 历史密度 %s 与当前 B3 油品 %r 跨类不匹配，改用油品兜底常量"
                         % (gst, sname, fmt(mdens), oil_raw))
                elif _n == "oil_unknown":
                    warn("no_density", "%s %s B3 油品识别不出（%r），沿用历史密度 %s"
                         % (gst, sname, oil_raw, fmt(mdens)))
                elif _n == "no_density":
                    stats["e_none"] += 1
                    no_density_list.append((gst, sname, oil_raw))
                    warn("no_density", "%s %s 无历史密度且 B3 油品识别不出（B3=%r），E 列不写"
                         % (gst, sname, oil_raw))

            # ── 组装写 op（只写 C..F；绝不动 M 列）
            ops = []
            for ci, want in ((2, val["C"]), (3, val["D"]), (5, val["F"])):
                if want is None:
                    continue
                c0 = cur.get(ci)
                if c0 is not None and abs(want - c0) < 1e-9:
                    stats["idem"] += 1
                    continue
                if getattr(args, "fill_only", False) and c0 not in (None, 0.0):
                    stats["keep_existing"] += 1
                    continue
                if ci in (3, 5) and want == 0 and c0 is not None and c0 != 0:
                    stats["danger"] += 1
                    warn("danger", "%s %s %s 列%s 源0/目标%s 危险，跳过"
                         % (gst, sname, g[0]["dateStr"], COL_TAG[ci], fmt(c0)))
                    continue
                if ci == 3 and want <= 0:
                    stats["d_implausible"] += 1
                    warn("d_implausible", "%s %s %s D=%s 物理不可信（<=0）视为缺测，不写"
                         % (gst, sname, g[0]["dateStr"], fmt(want)))
                    continue
                bad = _sanity(ci, want, c0)
                if bad:
                    stats["sanity"] += 1
                    warn("sanity", "%s %s %s 列%s %s → 跳过不写，待人工核对"
                         % (gst, sname, g[0]["dateStr"], COL_TAG[ci], bad))
                    continue
                # ★ 决策 1(B)：C/D/F 的权威是**表单实测** ⇒ 目标格是文本（"—"/"830 kg/m³"）时
                #   **放行覆盖**并留痕；只有 E 列（表单不提供密度、属人工标定）才"已有值绝不覆盖"。
                _r0 = cur_raw.get(ci)
                if _r0 not in (None, "") and cur.get(ci) is None:
                    warn("text_cell_overwritten",
                         "%s %s %s 列%s 现值是文本 %r（不可解析）⇒ 按表单实测覆盖为 %s"
                         % (gst, sname, g[0]["dateStr"], COL_TAG[ci], _r0, fmt(want)))
                # ★ 精度对齐：落表值就是 fmt() 的 4 位小数，比较也必须用同一个值，
                #   否则 5 位以上小数的填报值会"落表被四舍五入 ⇒ 回读差 1e-5 ⇒ 假红灯 rc=3"。
                want = round(want, 4)
                ops.append((ci, want))
                if c0 in (None, 0.0):
                    stats["fill"] += 1
                else:
                    stats["overwrite"] += 1
                    if abs(want - c0) / abs(c0) > 0.10:
                        # 覆盖幅度>10%：可能是真修正，也可能是录入错误 ⇒ 只留痕不拦，交人工复核
                        warn("big_overwrite", "%s %s %s 列%s 覆盖 %s → %s（变动 %+.1f%%）"
                             % (gst, sname, g[0]["dateStr"], COL_TAG[ci], fmt(c0), fmt(want),
                                (want - c0) / abs(c0) * 100))

            if dens is not None:
                c0 = cur.get(4)
                raw4 = cur_raw.get(4)
                # ★ P2-1：判「已有值」看**原始文本非空**，不看数值能否解析。
                #   文本型合法现值（"830 kg/m³" / "830" 文本 / "—"）一律视为已有值 ⇒ 保留不覆盖；
                #   只有真空格、或数值 0（密度 0 不可能，属脏值）才允许填。
                occupied = e_occupied(raw4, c0)
                if occupied:
                    stats["e_keep"] += 1           # E 已有值 ⇒ 绝不覆盖
                    if c0 is None:
                        stats["e_keep_text"] += 1  # 其中「文本型现值」单独计数
                elif c0 is not None and abs(dens - c0) < 1e-9:
                    stats["idem"] += 1
                else:
                    ops.append((4, round(dens, 4)))
                    stats["fill"] += 1

            ps = per_station.setdefault(gst, {"groups": 0, "C": 0, "D": 0, "E": 0, "F": 0})
            ps["groups"] += 1
            for ci, _v in ops:
                stats["cell_%s" % COL_TAG[ci]] += 1
                ps[COL_TAG[ci]] += 1
            if args.snapshot_out:
                # 写入前快照：行号 + C/D/E/F 现值（数值 + 原始文本）+ 本次拟写。
                # raw_* 必须留，否则重建不了「E 列已有值」判定（"—" 与空格的区别只在原始文本里）。
                snapshot.append({
                    "fid": fid, "ws": ws, "station": gst, "tank": sname,
                    "row": row, "date": g[0]["dateStr"],
                    "c": cur.get(2), "d": cur.get(3), "e": cur.get(4), "f": cur.get(5),
                    "raw_c": cur_raw.get(2), "raw_d": cur_raw.get(3),
                    "raw_e": cur_raw.get(4), "raw_f": cur_raw.get(5),
                    "ops": [{"col": COL_TAG[ci], "val": fmt(v)} for ci, v in ops]})
            if not ops:
                continue
            detail.append((gst, sname, g[0]["dateStr"], row, [(ci, fmt(v)) for ci, v in ops],
                           dsrc if dens is not None else "-"))
            if args.dry:
                continue
            sheet_ops[row] = (ops, gst, sname, g[0]["dateStr"], dsrc)

        if args.dry or not sheet_ops:
            continue

        # ── 合并写：同表所有行一个 rangeData，按 ≤100 格分片
        rows_sorted = sorted(sheet_ops)
        payload_ops = []
        for row in rows_sorted:
            for ci, want in sheet_ops[row][0]:
                payload_ops.append({"opType": "formula", "rowFrom": row, "rowTo": row,
                                    "colFrom": ci, "colTo": ci, "formula": fmt(want)})
        assert all(o["colFrom"] in (2, 3, 4, 5) for o in payload_ops), "越界写入保护：只允许 C..F"
        n = 0
        for k in range(0, len(payload_ops), MAX_CELLS_PER_WRITE):
            shard = payload_ops[k:k + MAX_CELLS_PER_WRITE]
            payload = json.dumps({"file_id": fid, "worksheet_id": ws, "rangeData": shard},
                                 ensure_ascii=False)
            j, err = cli_retry(["sheet", "update-range-data", payload])
            if not j or int(j.get("code") or 0) != 0:
                stats["write_fail"] += 1
                warn("write_fail", "%s %s r%d~r%d（%d 格）写失败：%s"
                     % (gst0, sname0, shard[0]["rowFrom"] + 1, shard[-1]["rowTo"] + 1, len(shard),
                        (j or {}).get("message") or (j or {}).get("msg") or err or "unknown"))
            else:
                n += len(shard)
            time.sleep(0.3)
        stats["written"] += n

        # ── 批量回读（每表 1 次；逐行比对，unverified_rows 结构不变）
        #   code==0 ≠ 落表成功；回读异常同样计入 unverified 并让退出码非 0（P1-1）。
        try:
            after_all = read_rowvals_batch(fid, ws, rows_sorted)
        except Exception as e:
            for row in rows_sorted:
                ops_u, gst, sname, ds, _dsrc = sheet_ops[row]
                stats["unverified"] += 1
                unverified_rows.append({
                    "station": gst, "sheet": sname, "date": ds, "row": row + 1,
                    "reason": "回读异常（通道故障/返回不可解析）", "error": repr(e),
                    "detail": [{"col": COL_TAG[ci], "expect": fmt(want), "actual": None}
                               for ci, want in ops_u]})
                warn("unverified", "%s %s r%d 回读异常（计入未验住）：%s" % (gst, sname, row + 1, e))
            continue
        for row in rows_sorted:
            ops_u, gst, sname, ds, _dsrc = sheet_ops[row]
            after = after_all[row]["num"]
            bad = [(COL_TAG[ci], w_txt, a_val) for ci, w_txt, a_val in readback_mismatch(ops_u, after)]
            if bad:
                stats["unverified"] += 1
                unverified_rows.append({
                    "station": gst, "sheet": sname, "date": ds, "row": row + 1,
                    "reason": "回读值不一致",
                    "detail": [{"col": t, "expect": e, "actual": (None if a is None else fmt(a))}
                               for t, e, a in bad]})
                warn("unverified", "%s %s %s r%d 回读不一致：%s"
                     % (gst, sname, ds, row + 1,
                        [(t, "期望" + e, "实际" + fmt(a)) for t, e, a in bad]))
            rowval_cache[(fid, ws, row)] = after_all[row]


    # ── 输出明细
    for gst, sname, ds, row, cols, dsrc in detail:
        tag = "[dry]" if args.dry else "[写]"
        cs = " ".join("%s=%s" % (COL_TAG[ci], txt) for ci, txt in cols)
        out("  %s %-5s %-9s %s r%-4d %s  (E来源:%s)" % (tag, gst, sname, ds, row + 1, cs, dsrc))

    out("-" * 100)
    out("合计: 记录%d 组%d 匹配%d | 将写 C=%d D=%d E=%d F=%d 格（填充%d/覆盖%d 幂等%d 保留已有%d）"
        % (stats["rows"], stats["groups"], stats["matched"],
           stats["cell_C"], stats["cell_D"], stats["cell_E"], stats["cell_F"],
           stats["fill"], stats["overwrite"], stats["idem"], stats["keep_existing"]))
    out("      跳过: 无罐表%d 无日期行%d 日期不可解析%d 危险闸门%d D不可信%d 物理不合常理%d"
        % (stats["no_sheet"], stats["no_row"], stats["nosrc"], stats["danger"],
           stats["d_implausible"], stats["sanity"]))
    out("      E 来源: 历史密度%d 油品兜底%d（其中历史密度因跨类被弃用%d；兜底全为参考值%d） 无可用值%d 目标已有值保留%d"
        % (stats["e_from_map"], stats["e_from_oil"], stats["e_map_rejected"],
           stats["e_from_guess"], stats["e_none"], stats["e_keep"]))
    if not args.dry:
        out("      实写: 提交%d 格 失败分片%d 未验住行%d" % (stats["written"], stats["write_fail"], stats["unverified"]))
    if stats["e_keep_text"]:
        out("      （E 已有值保留中有 %d 格是「文本型现值」，按 P2-1 一律保留不覆盖）" % stats["e_keep_text"])

    # ── E 列未写的罐：必须能照着这一行去改哪张表的 B3（只报"有 N 条告警"等于没报）
    if no_density_list:
        items = " ｜ ".join("%s/%s B3=%r" % (st, sh, b3) for st, sh, b3 in no_density_list[:20])
        out("!! E 列未写（no_density）%d 罐：%s%s"
            % (len(no_density_list), items,
               "" if len(no_density_list) <= 20 else " …（共 %d 罐，余下见告警清单）" % len(no_density_list)))

    # ── 分站明细（现场/QA 按站核对用）
    out("-" * 100)
    out("%-10s %6s %6s %6s %6s %6s %6s" % ("站", "罐-日组", "C", "D", "E", "F", "合计格"))
    tc = dict(groups=0, C=0, D=0, E=0, F=0)
    for st in [k for k in OPT if k in per_station]:
        ps = per_station[st]
        n = ps["C"] + ps["D"] + ps["E"] + ps["F"]
        out("%-10s %6d %6d %6d %6d %6d %6d"
            % (st, ps["groups"], ps["C"], ps["D"], ps["E"], ps["F"], n))
        for k in tc:
            tc[k] += ps[k]
    out("%-10s %6d %6d %6d %6d %6d %6d"
        % ("合计", tc["groups"], tc["C"], tc["D"], tc["E"], tc["F"],
           tc["C"] + tc["D"] + tc["E"] + tc["F"]))

    # ── A 列扫描窗观测（P3-5）：一眼确认没顶到边界
    max_last_row = None
    if a_window:
        flat = [(st, nm, ln) for st, arr in a_window.items() for nm, ln in arr]
        max_last_row = max(ln for _, _, ln in flat)
        out("-" * 100)
        out("A 列扫描窗: %d..%d（0-based），窗内最后一个非空行号 max=%d，距窗尾留白 %d 行"
            % (A_ROW_FROM, A_ROW_TO, max_last_row, A_ROW_TO - max_last_row))
        near = sorted([x for x in flat if x[2] >= A_ROW_TO - 5], key=lambda x: -x[2])
        if near:
            warn("a_window", "A 列扫描窗即将顶到边界（rowTo=%d）：%d 张表最后非空行 ≥ %d，最高的是 %s"
                 % (A_ROW_TO, len(near), A_ROW_TO - 5,
                    "、".join("%s %s=%d" % (a, b, c) for a, b, c in near[:3])))

    # ── 未验住行：必须醒目 + 退出码非 0（P1-1）
    if stats["unverified"]:
        out("=" * 100)
        rc_now = decide_rc(stats["write_fail"], stats["unverified"])
        out("!! 有 %d 行未能验证落表（回读不一致或回读异常）—— 退出码 %d，不是 0 !!"
            % (stats["unverified"], rc_now))
        if rc_now == EXIT_BOTH:
            out("!! 本次同时存在写失败(%d 分片)与未验住(%d 行)："
                "先按 unverified_rows 逐行核对罐表实际值，再重跑补写失败部分 !!"
                % (stats["write_fail"], stats["unverified"]))
        for u in unverified_rows[:50]:
            out("   - %s %s %s r%d：%s"
                % (u.get("station"), u.get("sheet"), u.get("date"), u.get("row"), u.get("reason")))
        if len(unverified_rows) > 50:
            out("   ... 其余 %d 行见 --report JSON" % (len(unverified_rows) - 50))
        out("   ⇒ 请按 --report 里的 unverified_rows 逐行人工核对罐表实际值，不要当作已同步。")

    # ── 退出码（CI 靠它判成败）
    #    0 = 全部验住；1 = 有写分片被拒（明确失败，重跑通常有效）；2 = 读表/配额中止；
    #    3 = 写了但回读没验住（重跑通常无效，必须人工核对罐表实际值）
    #    ★ 优先级：未验住(3) 高于 写被拒(1)。理由（QA 第二轮）：两者同时出现时若报 1，
    #      运维的自然反应是"重跑"；而被拒的写重跑一次确实有效、未验住却不会自己消失 ⇒
    #      会陷入"重跑→仍是 1→再重跑"的循环，直到有人翻 --report.unverified_rows。
    #      报 3 能第一时间把人逼到人工核对那条路上。
    if args.snapshot_out:
        try:
            with open(args.snapshot_out, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
            out("快照已写出：%s（%d 行，含 raw_c..raw_f）" % (args.snapshot_out, len(snapshot)))
        except Exception as e:
            warn("snapshot", "快照写出失败：%r" % (e,))

    rc = decide_rc(stats["write_fail"], stats["unverified"])

    # ── 告警清单
    out("=" * 100)
    out("告警清单（%d 条）" % len(_WARN))
    kinds = {}
    for w in _WARN:
        kinds.setdefault(w["kind"], []).append(w["msg"])
    for kind in sorted(kinds, key=lambda k: -len(kinds[k])):
        out("  [%s] ×%d" % (kind, len(kinds[kind])))
        for m in kinds[kind][:200]:
            out("     - " + m)
        if len(kinds[kind]) > 200:
            out("     ... 其余 %d 条见 --report 输出" % (len(kinds[kind]) - 200))

    if args.report:
        rep = {"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "mode": "dry" if args.dry else "write",
               "exit_code": rc,
               "stats": stats, "warnings": _WARN,
               "unverified_rows": unverified_rows,
               "no_density": [{"station": a, "tank": b, "b3": c} for a, b, c in no_density_list],
               "a_window": {"rowFrom": A_ROW_FROM, "rowTo": A_ROW_TO,
                            "max_last_row": max_last_row,
                            "sheets": [{"station": st, "sheet": nm, "last_nonempty_row": ln}
                                       for st, arr in a_window.items() for nm, ln in arr]},
               "detail": [{"station": a, "sheet": b, "date": c, "row": d,
                           "ops": [{"col": COL_TAG[ci], "val": v} for ci, v in e],
                           "e_source": f} for a, b, c, d, e, f in detail]}
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        out("报告已写出：%s" % args.report)
    return rc


# ══════════════════════════════════════════════════════════════════════════════════════
# AirScript 通道（--channel airscript）—— 罐表侧的活全给云端槽位 B 脚本干
#
# 为什么要这条通道（2026-10-20 定案）：
#   CLI 通道一轮全量 = 610 次 kdocs-cli 调用，打爆日配额会连带影响别的业务（当天 429001 已发生）。
#   AirScript 通道下，Python 只做**表单侧**（读 A 表 / 站点归一 / 罐号→罐表 / 去重融合），
#   罐表侧（找日期行 / 读现值 / 闸门 / 写入 / 回读）由槽位 B 脚本在 WPS 云端完成，**不消耗
#   kdocs-cli 配额**。一轮全量 = 1 次 list-records + 15 次 get-sheets-info + 2 次 sync_task。
#
# ★★ CLI 通道行为零变化：sync() 一行未改，本段全是新增函数，main() 只按 --channel 分派。
#    等价性由 scripts/_diag_as_equiv.py 在离线回放上逐三元组对比（不依赖代码共用，避免动锁版）。
# ══════════════════════════════════════════════════════════════════════════════════════

AS_VER = "2026-10-21"          # ★ 必须与 airscript_liquid_level_writeback.js 的 WB_VER 一字不差
AS_HOST_FILE = os.environ.get("AIRSCRIPT_HOST_FILE_ID") or "554602530994"
AS_SCRIPT_ID = os.environ.get("AIRSCRIPT_SCRIPT_ID") or "V2-5hb5sacXygfJPbko3AytmR"
AS_URL = (os.environ.get("AIRSCRIPT_URL")
          or "https://www.kdocs.cn/api/v3/ide/file/%s/script/%s/sync_task" % (AS_HOST_FILE, AS_SCRIPT_ID))
# 文档共享脚本令牌（非用户凭证；与 scripts/_trigger_writeback.js 同一个）。
# 优先取环境变量 AIRSCRIPT_TOKEN，未设时才用内置值 —— 轮换令牌只改环境变量，不动代码。
AS_TOKEN_DEFAULT = "7CaQBbiFopVoL4vkVsXKhI"
AS_TOKEN = (os.environ.get("AIRSCRIPT_TOKEN") or "").strip() or AS_TOKEN_DEFAULT
AS_TIMEOUT = 300               # 单批最长等 5 分钟；脚本侧含 openFile×7 + 逐罐读取，实测 15 站 ~100s
AS_ROWS_MAX = 2500             # argv.rows 上限（618 条全量 payload ≈ 40KB，留足余量）

# 罐表名归一（必须与 JS 侧 /🛢️\s*(南站|北站|东站|西站)\s*(\d+#罐)/ **同构**）
AS_TANK_RE = re.compile(r"🛢️\s*(南站|北站|东站|西站)\s*(\d+#罐)")

# 分批（team-lead 2026-10-20 定）：批 1 七站、批 2 其余全部。
# ★ 批 2 由 OPT - 批 1 反推，绝不手写第二份站点名单 —— 手写就会漏站，漏站就是整站静默不写。
AS_BATCH1 = ("富县", "汉中", "洋县", "旬阳", "韩城", "富平", "南沙")
AS_BATCH2 = tuple(s for s in OPT if s not in AS_BATCH1)
if (set(AS_BATCH1) & set(AS_BATCH2)) or (set(AS_BATCH1) | set(AS_BATCH2)) != set(OPT):
    raise RuntimeError("AS_BATCH1/2 划分有误：必须两两覆盖 OPT 的 %d 个站（重叠 %s，漏 %s）"
                       % (len(OPT), sorted(set(AS_BATCH1) & set(AS_BATCH2)),
                          sorted(set(OPT) - set(AS_BATCH1) - set(AS_BATCH2))))

# 退出码（AirScript 通道专用，与 CLI 通道的 0/1/2/3/4 并存，不覆盖）
EXIT_AS_ASSERT = 5        # 回执自相矛盾（格数不守恒 / 三元组对不上）⇒ **直接失败**，必须人工核对
EXIT_AS_TRIGGER = 6       # 触发链路失败（网络/超时/非 2xx）⇒ 回落 CLI 重跑
EXIT_AS_SCRIPT = 7        # 脚本侧错误（E_VER/E_ARG/E_PROTO/逐站 ok=false）⇒ 回落 CLI 重跑
EXIT_AS_UNVERIFIED = 8    # 写了但回读没验住（written != verified）⇒ 回落 CLI 重写并自己验
# 多批同时出问题时取哪一个报给 CI —— 按「**罐表里可能已经有错值的概率**」由高到低排，不是按数字大小：
#   5（回执自相矛盾）= 连回执都信不过，不确定写了什么，且明确要求人工看、不许回落 ⇒ 最高
#   8（写了没验住）  = 罐表里**可能已经有错值**，证据已落盘 ⇒ 次高
#   7（脚本侧错误）  = 多半什么都没写（E_VER 连文件都没打开）
#   6（触发链路失败）= 请求都没到，什么都没写
#   1 / 4 / 2        = CLI 通道的历史码，AirScript 通道产生不了；真出现就压过一切（说明有更糟的事）
# ★ 这张表被 _diag_lv/as_channel_check.py【四】直接验，改这里必须同步改那里的期望值。
AS_RC_PRIORITY = {0: 0, 6: 1, 7: 2, 8: 3, 5: 4, 1: 5, 4: 9, 2: 9}


def _scrub(t):
    """抹掉令牌再进日志/回执文件 —— 回包与异常里都可能带上 header 回显。"""
    t = "" if t is None else str(t)
    for sec in (AS_TOKEN, AS_TOKEN_DEFAULT):
        if sec:
            t = t.replace(sec, "***")
    return t


def as_sheet_key(sname):
    """罐表名 → 归一 key（如 "南站1#罐"），与 JS 侧 `mm[1] + mm[2]` 完全同构。

    不同构的后果比报错更糟：Python 认为这张表存在、云端脚本匹配不到 ⇒ 该罐整批**静默不写**。
    所以调用方把「归一失败」当硬失败处理（退出码 7 回落 CLI），不允许静默丢行。
    """
    m = AS_TANK_RE.search(sname or "")
    return (m.group(1) + m.group(2)) if m else None


def as_batches(args):
    """→ [(批标签, 该批站点元组)]。--batch 1 / 2 / all（默认 all）。"""
    b = str(getattr(args, "batch", None) or "all").strip().lower()
    if b in ("all", "", "both", "1,2", "2,1"):
        return [("1", AS_BATCH1), ("2", AS_BATCH2)]
    if b == "1":
        return [("1", AS_BATCH1)]
    if b == "2":
        return [("2", AS_BATCH2)]
    raise RuntimeError("--batch 只能是 1 / 2 / all（收到 %r）" % b)


def build_argv(batch_tag, triples, dry, fill_only, ver=None, write_density=True):
    """三元组 → sync_task 请求体。字段名与 JS 侧读取的键**逐字对应**（station/tank/ser/c/d/f）。

    c/d/f 给浮点或 None（None = 该列表单没填；D<=0 已在 fuse() 阶段丢弃）⇒ 云端跳过该列。
    ★ 这里**不做 round、不做 fmt**：JS 的幂等闸门要拿未 round 的原值比 1e-9（G2 用例），
      round(4) 由 JS 在写前自己做；提前 round 会让两侧在 5 位小数上分叉。
    """
    rows = [{"station": st, "tank": tk, "ser": int(ser),
             "c": c, "d": d, "f": f} for (st, tk, ser, c, d, f) in triples]
    return {"Context": {"argv": {
        "ver": ver or AS_VER,
        "batch": str(batch_tag),
        "dryRun": bool(dry),
        "writeDensity": bool(write_density),
        "fillOnly": bool(fill_only),      # ★ 必传，且不给默认值：false 也要显式下发
        "rows": rows,
    }}}


def post_sync_task(payload, timeout=AS_TIMEOUT):
    """POST sync_task → (http_status, body_text)。

    只有**链路层**失败才抛 RuntimeError（调用方据此给 6 回落 CLI）：
      · 连不上 / DNS / 超时
      · 非 2xx：kdocs 的 4xx/5xx 通常连日志都没有，重试无意义 ⇒ 直接回落
    HTTP 200 但回包里没有 WB_RESULTS 行，不算链路失败（那是脚本侧问题，退出码 7）。
    """
    import urllib.error
    import urllib.request
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(AS_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "AirScript-Token": AS_TOKEN,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = ""
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError("sync_task HTTP %s（%s）" % (e.code, _scrub(txt)[:300] or "无响应体"))
    except Exception as e:
        raise RuntimeError("sync_task 触发失败：%s" % _scrub(repr(e)))


def parse_wb_results(body):
    """回包 → (wb|None, log_lines[], note)。

    kdocs sync_task 回包结构：{"code":0,"data":{"logs":[{"args":[...]},...]}}
    脚本自己的 `WB_RESULTS:{json}` 就混在 args 里；args 可能被运行时分段，故先 join 再找前缀。
    """
    wb, logs, note = None, [], ""
    try:
        j = json.loads(body)
    except Exception:
        return None, [], "回包不是 JSON：%s" % _scrub(body)[:300]
    if j.get("error"):
        note = "回包 error 字段：%s" % _scrub(j.get("error"))[:300]
    data = j.get("data") or {}
    for l in (data.get("logs") or []):
        line = " ".join(str(x) for x in (l.get("args") or []))
        logs.append(line)
        if line.startswith("WB_RESULTS:"):
            try:
                wb = json.loads(line[len("WB_RESULTS:"):])
            except Exception as e:
                note = "WB_RESULTS 行解析失败：%r" % (e,)
    if wb is None and not note:
        tail = " | ".join(logs[-3:]) if logs else "无日志"
        hit = [c for c in ("E_VER", "E_ARG", "E_PROTO") if any(c in x for x in logs)]
        note = "回包里没有 WB_RESULTS 行（脚本没跑完%s）：%s" % (
            "，命中 " + "/".join(hit) if hit else "", _scrub(tail)[:300])
    return wb, logs, note


AS_ASSERT_NAMES = (
    "A1 版本/批次回传一致",
    "A2 请求站全有回执",
    "A3 逐站 ok 且 errors 为空",
    "A4 格数守恒 written+samevalue+gated==candidates",
    "A5 写=验 written==verified",
    "A6 窗口零意外改动 unexpected==0",
    "A7 三元组总数==发送行数",
    "A8 逐站三元组数==该站发送行数",
    "A9 dry 不落盘 / 实跑不空转",
)


def assert_airscript(wb, req_stations, send_by_station, dry, batch_tag, expect_rows, expect_ver=None):
    """9 条断言 → [{"no","name","ok","detail"}]。任何一条不过都必须让退出码非 0。

    设计原则：**宁可误报，也不静默放过**。回执是云端脚本自报的，Python 无法去罐表复核，
    所以只能验「自洽性」—— 但它能挡住绝大多数静默失败：脚本没跑 / 跑一半 / 整站被跳过 /
    dry 却真写了 / 写了没验住 / 写窗口顺手改了别人的格子。
    """
    expect_ver = expect_ver or AS_VER
    res = []

    def add(i, ok, detail):
        res.append({"no": i, "name": AS_ASSERT_NAMES[i - 1], "ok": bool(ok), "detail": detail})

    if not wb:
        for i in range(1, 10):
            add(i, False, "没有 WB_RESULTS 回执，无法验证")
        return res

    sts = {}
    for s in (wb.get("stations") or []):
        sts[str(s.get("station"))] = s
    tot = wb.get("totals") or {}
    n = lambda d, k: int((d or {}).get(k) or 0)          # noqa: E731  回执里全是数字或缺失

    # A1 握手值/批次号必须原样回传（挡住「粘的是旧脚本」）
    add(1, str(wb.get("ver") or "") == expect_ver and str(wb.get("batch") or "") == str(batch_tag),
        "回执 ver=%r batch=%r（期望 %r / %r）"
        % (wb.get("ver"), wb.get("batch"), expect_ver, str(batch_tag)))

    # A2 发过数据的站必须都有回执（挡住「整站被跳过」）
    miss = sorted(set(req_stations) - set(sts))
    add(2, not miss, "缺回执的站：%s" % (miss if miss else "无"))

    # A3 逐站 ok + errors 明细（挡住「站级异常吞掉」）
    bad = [(k, v.get("errors")) for k, v in sts.items() if not v.get("ok")]
    add(3, not bad, "ok=false 的站 %d 个%s"
        % (len(bad), "" if not bad else "：" + "；".join("%s→%s" % (k, str(e)[:240]) for k, e in bad[:5])))

    # A4 格数守恒（dry 时「写」记在 wouldWrite 而不是 written —— JS 侧如此，别按 written 算）
    viol = []
    key_w = "wouldWrite" if dry else "written"
    for k, v in sts.items():
        if n(v, key_w) + n(v, "samevalue") + n(v, "gated") != n(v, "candidates"):
            viol.append("%s: %d+%d+%d!=%d" % (k, n(v, key_w), n(v, "samevalue"), n(v, "gated"), n(v, "candidates")))
    if n(tot, key_w) + n(tot, "samevalue") + n(tot, "gated") != n(tot, "candidates"):
        viol.append("合计: %d+%d+%d!=%d" % (n(tot, key_w), n(tot, "samevalue"), n(tot, "gated"), n(tot, "candidates")))
    add(4, not viol, ("不守恒：%s" % "；".join(viol[:5])) if viol else
        "合计 %d 格守恒（%s %d + samevalue %d + gated %d）"
        % (n(tot, "candidates"), key_w, n(tot, key_w), n(tot, "samevalue"), n(tot, "gated")))

    # A5 写了必须全验住（dry 不适用，由 A9 兜）
    if dry:
        add(5, True, "dry-run 不适用（written 由 A9 强判为 0）")
    else:
        add(5, n(tot, "written") == n(tot, "verified"),
            "written=%d verified=%d" % (n(tot, "written"), n(tot, "verified")))

    # A6 写窗口内「未计划修改的格」必须零变化（R2 的核心防线：整块写最容易误清历史）
    add(6, n(tot, "unexpected") == 0, "unexpected=%d" % n(tot, "unexpected"))

    # A7 三元组总数守恒
    add(7, n(tot, "triples") == expect_rows, "回执 triples=%d，发送 %d 行" % (n(tot, "triples"), expect_rows))

    # A8 逐站三元组数守恒（比总数更细：能抓出「A 站多 3 行、B 站少 3 行」这种抵消）
    viol8 = ["%s: 回执 %d / 发送 %d" % (k, n(v, "triples"), int(send_by_station.get(k) or 0))
             for k, v in sts.items() if n(v, "triples") != int(send_by_station.get(k) or 0)]
    for k in sorted(set(send_by_station) - set(sts)):
        viol8.append("%s: 发送 %d / 无回执" % (k, int(send_by_station[k])))
    add(8, not viol8, ("不一致：%s" % "；".join(viol8[:5])) if viol8 else "%d 站逐站相等" % len(sts))

    # A9 dryRun 必须真的不落盘；实跑不许空转（wouldWrite 残留说明 dryRun 没被识别）
    if dry:
        add(9, n(tot, "written") == 0,
            "dry-run 必须 0 落盘：written=%d wouldWrite=%d" % (n(tot, "written"), n(tot, "wouldWrite")))
    else:
        add(9, n(tot, "wouldWrite") == 0, "实跑必须 wouldWrite==0：wouldWrite=%d" % n(tot, "wouldWrite"))
    return res


def decide_rc_airscript(asserts, wb, dry):
    """回执 + 断言 → 退出码。判定顺序即优先级（先拦最需要人管的）。"""
    if not wb:
        return EXIT_AS_SCRIPT                       # 没回执：脚本侧问题，回落 CLI
    if str(wb.get("code") or "") in ("E_VER", "E_ARG", "E_PROTO"):
        return EXIT_AS_SCRIPT
    tot = wb.get("totals") or {}
    if not dry and int(tot.get("written") or 0) != int(tot.get("verified") or 0):
        return EXIT_AS_UNVERIFIED                   # 写了没验住：CLI 重写并自己验最稳
    a3 = next((a for a in asserts if a["no"] == 3), None)
    if a3 is not None and not a3["ok"]:
        return EXIT_AS_SCRIPT                       # 站级异常：回落 CLI
    if any(not a["ok"] for a in asserts):
        return EXIT_AS_ASSERT                       # 回执自相矛盾：别回落，先人工看
    return EXIT_OK


def plan_airscript(args):
    """表单侧计划 → (triples, meta)。triples = [(station, tankKey, ser, C, D, F)]，已升序。

    与 sync() 的**唯一**差别：一个罐表单元格都不读（A 列 / B3 / 现值 / 回读全在云端），
    所以这里没有 no_row、没有 E 密度来源、没有闸门计数 —— 那些数字一律以云端回执为准。

    复用（不复制）的逻辑函数：list_records / list_sheets / resolve_tank / _conflict / fuse /
    norm_station / to_serial / MIN_SER。复制的是**循环骨架**，故意的：
    sync() 是锁版（md5 9d8210cf 起就冻结），改它一行都要重新全量回归，不划算。
    """
    only = set(args.stations) if args.stations else None
    rows = list_records()

    # 日期过滤 + 启用日前测试记录剔除（与 sync() 完全同口径，顺序也一样）
    if getattr(args, "since", None):
        cutoff = datetime.date.today() - datetime.timedelta(days=args.since)
        cut_ser = (cutoff - datetime.date(1899, 12, 30)).days
        rows = [r for r in rows if (to_serial(r["dateStr"]) or 0) >= cut_ser]
    for r in [r for r in rows if (to_serial(r["dateStr"]) or 0) < MIN_SER]:
        warn("min_ser", "早于启用日(MIN_SER)的测试记录，跳过：%s %s %s id=%s"
             % (r["raw_station"], r["tank"], r["dateStr"], r["id"]))
    rows = [r for r in rows if (to_serial(r["dateStr"]) or 0) >= MIN_SER]

    out("=" * 100)
    out("A 表返回 %d 条 → 剔除空记录 %d 条 → 解析 %d 条 → MIN_SER 过滤后 %d 条%s"
        % (_LAST_FETCH["fetched"], _LAST_FETCH["dropped"], _LAST_FETCH["parsed"], len(rows),
           "" if getattr(args, "since", None) is None else "（--since %d）" % args.since))

    by_station = {}
    for r in rows:
        st = r["station"]
        if st not in OPT or (only and st not in only):
            continue
        by_station.setdefault(st, []).append(r)
    for st in sorted({r["station"] for r in rows}):
        if st not in OPT:
            warn("unknown_station", "未登记站点，整站跳过：%r（%d 条）"
                 % (st, len([r for r in rows if r["station"] == st])))
    for st in sorted(only or []):
        if st not in OPT:
            warn("bad_arg", "--stations 里有未登记站点名：%r（合法值见 OPT 常量）" % st)

    triples, bad_key, no_sheet, nosrc = [], [], 0, 0
    for st in sorted(by_station):
        try:
            sheets = list_sheets(OPT[st])
        except Exception as e:
            warn("sheets", "取 %s 工作表清单失败，整站跳过：%s" % (st, e))
            continue
        resolved = {}
        for r in by_station[st]:
            tk = r["tank"]
            if tk not in resolved:
                resolved[tk] = resolve_tank(sheets, tk)
            sid, sname, how = resolved[tk]
            if sid is None:
                r["_drop"] = how
                continue
            key = as_sheet_key(sname)
            if key is None:
                r["_drop"] = "no-key"
                bad_key.append("%s/%s(%r)" % (st, tk, sname))
                continue
            r["_key"] = key
            if how == "fallback-east-to-south":
                warn("tank_fallback", "罐号兜底映射（站点误选）：%s %s → %s" % (st, tk, sname))
        for tk, (sid, sname, how) in resolved.items():
            if sid is None:
                cnt = len([r for r in by_station[st] if r["tank"] == tk])
                reason = {"ambiguous": "命中多张罐表，放弃", "ambiguous-fallback": "兜底命中多张，放弃",
                          "no-match": "该站无对应罐表（罐号误选/西站罐无表）"}.get(how, how)
                warn("tank_unmatched", "%s %s ×%d 条 → %s" % (st, tk, cnt, reason))
                no_sheet += cnt

        # ── 去重（映射后口径：(站|罐表key|日期)，后提交者胜，逐列取最后有效值）
        groups = {}
        for r in by_station[st]:
            if r.get("_drop"):
                continue
            ser = to_serial(r["dateStr"])
            if ser is None:
                nosrc += 1
                warn("bad_date", "日期无法解析，跳过：%s %s %r（id=%s）" % (st, r["tank"], r["dateStr"], r["id"]))
                continue
            groups.setdefault((st, r["_key"], ser), []).append(r)
        for (gst, key, ser), g in sorted(groups.items()):
            if len(g) > 1:
                for k2, tag in (("C", "试枪回罐"), ("D", "实际库存"), ("F", "交班温度")):
                    cf = _conflict(g, k2)
                    if cf:
                        warn("multi_value", "%s %s %s 列%s(%s) 同键多条互不相等的重复提交 %s（取最后有效值 %s）"
                             % (gst, key, g[0]["dateStr"],
                                COL_TAG[2 if k2 == "C" else (3 if k2 == "D" else 5)],
                                tag, [fmt(v) for v in cf], fmt(fuse(g)[k2])))
            v = fuse(g)
            triples.append((gst, key, ser, v["C"], v["D"], v["F"]))

    meta = {"rows": len(rows), "triples": len(triples), "no_sheet": no_sheet,
            "nosrc": nosrc, "bad_key": bad_key,
            "by_station": {st: len([t for t in triples if t[0] == st]) for st in sorted(by_station)}}
    out("-" * 100)
    out("计划: %d 个「站|罐表|日期」三元组（← %d 条记录；无罐表 %d 条，日期不可解析 %d 条）"
        % (len(triples), meta["rows"], no_sheet, nosrc))
    bean = [(C, D, F) for (_st, _tk, _ser, C, D, F) in triples]
    out("      非空列计数: C=%d D=%d F=%d（空列由云端按「表单没填就不写」跳过）"
        % (sum(1 for c, _, _ in bean if c is not None),
           sum(1 for _, d, _ in bean if d is not None),
           sum(1 for _, _, f in bean if f is not None)))
    if bad_key:
        out("!! %d 条记录命中的罐表名不含「🛢️ X站n#罐」，云端脚本匹配不到 ⇒ 本通道处理不了（退出码 %d）：%s"
            % (len(bad_key), EXIT_AS_SCRIPT, "；".join(bad_key[:10])))
    return triples, meta


def as_handshake(args, ver):
    """零写入握手：只校验版本，不读 A 表、不打开任何文件、不写任何格（R4）。"""
    payload = build_argv("handshake", [], True, getattr(args, "fill_only", False), ver=ver)
    out("=" * 100)
    out("AirScript 零写入握手：ver=%r batch=handshake rows=[]（脚本只校验版本，不打开任何文件）" % ver)
    if getattr(args, "as_request_out", None):
        with open(args.as_request_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        out("已写出 payload（未发送）：%s" % args.as_request_out)
        return EXIT_OK
    try:
        status, body = post_sync_task(payload)
    except RuntimeError as e:
        out("✘ 握手失败（触发链路）：%s" % e)
        out("  ⇒ 退出码 %d" % EXIT_AS_TRIGGER)
        return EXIT_AS_TRIGGER
    out("HTTP %s，回包 %d 字节" % (status, len(body.encode("utf-8"))))
    wb, logs, note = parse_wb_results(body)
    for line in logs:
        if not line.startswith("WB_RESULTS:"):
            out("  [log] " + _scrub(line)[:400])
    if note:
        out("  ! " + _scrub(note))
    asserts = assert_airscript(wb, set(), {}, True, "handshake", 0, expect_ver=ver)
    for a in asserts:
        out("  %s %s：%s" % ("✔" if a["ok"] else "✘", a["name"], _scrub(a["detail"])))
    if wb and str(wb.get("ver") or "") == ver:
        out("✔ 握手通过：云端 WB_VER=%r 与 Python ver=%r 一致（未打开任何文件、未写任何格）" % (wb.get("ver"), ver))
        return EXIT_OK
    out("✘ 握手不通过：脚本侧可能仍是旧版（或 --ver 与 WB_VER 不一致）⇒ 退出码 %d，按【操作】文档重贴" % EXIT_AS_SCRIPT)
    return EXIT_AS_SCRIPT


def _ser_to_datestr(ser):
    """Excel 序列号 → YYYY/MM/DD（只用于证据文件的可读性，算不出来就留空）。"""
    try:
        return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(ser))).strftime("%Y/%m/%d")
    except Exception:
        return ""


def as_write_evidence(tag, wb, logs, sel, ver, dry):
    """rc=8（written != verified）时把现场落盘 —— 罐表里可能有一格是错的，这是**唯一现场证据**。

    ★ 诚实边界：JS 回执只带 `written` / `verified` 两个**计数**，不带「哪一格没验住」的格坐标。
      所以这里按「该批全部计划写入格」全量列出（站/罐/日期/期望值），请人工逐格核对罐表实际值。
      要做到「精确到格」，必须在 JS 里加 `unverifiedCells[]` 并重新粘贴脚本 —— 与 `batch_id`
      一起等 Host 决策，现在不做（硬编一个"猜出来的坐标"比没有更糟）。

    落盘位置：<ROOT>/_diag_lv/wb_unverified_<时间戳>_batch<批>.json
    """
    ev_dir = os.path.join(ROOT, "_diag_lv")
    try:
        if not os.path.isdir(ev_dir):
            os.makedirs(ev_dir)
    except Exception:
        ev_dir = HERE

    by_st = {}
    for (st, tk, ser, c, d, f) in sel:
        by_st.setdefault(st, []).append({
            "tank": tk, "ser": int(ser), "date": _ser_to_datestr(ser),
            "expect": {"C": c, "D": d, "F": f, "E": "（云端按 B3 油品 / E 列实时末值决定，Python 侧不知道）"},
            "planned_cols": [k for k, v in (("C", c), ("D", d), ("F", f)) if v is not None] + ["E"],
        })

    sts = []
    for s in (wb.get("stations") or []):
        name = str(s.get("station"))
        w_, v_ = int(s.get("written") or 0), int(s.get("verified") or 0)
        sts.append({
            "station": name, "ok": s.get("ok"),
            "written": w_, "verified": v_, "delta": w_ - v_,
            "candidates": s.get("candidates"), "samevalue": s.get("samevalue"),
            "gated": s.get("gated"), "unexpected": s.get("unexpected"),
            "errors": s.get("errors") or [],
            "planned_cells": by_st.get(name, []),
        })

    doc = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "channel": "airscript", "ver": ver, "batch": str(tag), "dry": bool(dry),
        "endpoint": AS_URL, "reason": "written != verified（写了但回读没验住）",
        "how_to_read": ("回执只有 written/verified 计数，没有格坐标 ⇒ planned_cells 列出该站**全部**"
                        "计划写入格，请按 date/tank 定位罐表行、逐列核对实际值。"
                        "delta 是该站「写了但没验住」的格数，不是错误格数（可能是写入被覆盖/回读通道问题）。"),
        "totals": wb.get("totals") or {},
        "stations": sts,
        "wb_results_raw": wb,                      # 完整回执原文，别信摘要
        "log_lines": [_scrub(x) for x in (logs or [])],
    }
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(ev_dir, "wb_unverified_%s_batch%s.json" % (ts, tag))
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        return path
    except Exception as e:
        warn("evidence", "证据落盘失败（%r）—— 回执原文已在下面的日志里，请手工保存" % (e,))
        return None


def sync_airscript(args):
    """AirScript 通道主流程：计划 → 分批 build_argv → POST → 解析回执 → 9 条断言 → 退出码。"""
    global VERBOSE
    VERBOSE = VERBOSE or getattr(args, "verbose", False)
    ver = (getattr(args, "ver", None) or AS_VER).strip()
    if ver != AS_VER:
        # 只在明确知道云端脚本还是旧版时才这样用；否则会让「脚本没更新」这一最危险的情况静默通过
        warn("as_ver", "--ver=%r 与代码内置 AS_VER=%r 不一致：握手会按你给的版本号校验，"
                       "脚本是否为该版本需你自己确认" % (ver, AS_VER))
        out("!! --ver=%r 与内置 AS_VER=%r 不一致，仅在你明确知道云端脚本版本时使用" % (ver, AS_VER))

    if getattr(args, "handshake", False):
        return as_handshake(args, ver)

    triples, meta = plan_airscript(args)
    rcs, receipts, all_asserts = [], [], []
    rcs_evidence, pending_fallback = [], []          # 裁定 ⑤：证据文件路径 / 待回落的批
    if meta["bad_key"]:
        rcs.append(EXIT_AS_SCRIPT)                 # 归一名匹配不上 ⇒ 回落 CLI 才写得进去
    if not triples:
        out("没有可写的数据（A 表无 ≥MIN_SER 的提交，或 --stations 过滤后为空）⇒ 不发请求")
    if len(triples) > AS_ROWS_MAX:
        out("✘ argv.rows=%d 超过上限 %d，请用 --batch 1 / --batch 2 分批" % (len(triples), AS_ROWS_MAX))
        return EXIT_ABORT

    total_sent, agg = 0, {}
    for tag, stations in as_batches(args):
        sset = set(stations)
        sel = [t for t in triples if t[0] in sset]
        if not sel:
            out("-" * 100)
            out("批 %s（%d 站）：本批无待写三元组 ⇒ 不发请求" % (tag, len(stations)))
            continue
        send_by_station = {}
        for t in sel:
            send_by_station[t[0]] = send_by_station.get(t[0], 0) + 1
        req_stations = set(send_by_station)
        payload = build_argv(tag, sel, args.dry, args.fill_only, ver=ver)
        nbytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        out("=" * 100)
        out("批 %s：%d 站 %d 三元组；argv.rows=%d payload=%d 字节 dryRun=%s fillOnly=%s ver=%s"
            % (tag, len(req_stations), len(sel), len(payload["Context"]["argv"]["rows"]),
               nbytes, args.dry, args.fill_only, ver))

        # 离线①：只落 payload 不发送（给运维人工核对字段名/取值）
        if getattr(args, "as_request_out", None):
            path = args.as_request_out
            if len(as_batches(args)) > 1:
                base, ext = os.path.splitext(path)
                path = "%s.batch%s%s" % (base, tag, ext or ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            out("已写出 payload（未发送）：%s（%d 字节）" % (path, nbytes))
            total_sent += len(sel)
            continue

        # 离线②：用现成回包跑解析 + 断言（把真实回执存盘后随时重放）
        if getattr(args, "as_response_in", None):
            with open(args.as_response_in, encoding="utf-8") as f:
                body = f.read()
            out("使用现成回包（未发送）：%s（%d 字节）" % (args.as_response_in, len(body)))
        else:
            try:
                status, body = post_sync_task(payload)
            except RuntimeError as e:
                out("✘ 批 %s 触发失败：%s" % (tag, e))
                out("  ⇒ 退出码 %d：触发链路不通，改用 --channel cli 重跑同一批（--stations %s）"
                    % (EXIT_AS_TRIGGER, ",".join(stations)))
                rcs.append(EXIT_AS_TRIGGER)
                continue
            out("HTTP %s，回包 %d 字节" % (status, len(body.encode("utf-8"))))

        wb, logs, note = parse_wb_results(body)
        for line in logs:
            if not line.startswith("WB_RESULTS:"):
                out("  [log] " + _scrub(line)[:400])
        if note:
            out("  ! " + _scrub(note))
        asserts = assert_airscript(wb, req_stations, send_by_station, args.dry, tag, len(sel), expect_ver=ver)
        for a in asserts:
            out("  %s %s：%s" % ("✔" if a["ok"] else "✘", a["name"], _scrub(a["detail"])))
        rc = decide_rc_airscript(asserts, wb, args.dry)

        # ── 退出码 8（写了没验住）：**先把证据落盘**，再做别的（product-architect 2026-10-20 裁定 ⑤）
        #    理由：rc=8 意味着罐表里可能有一格是错的，而这一批的回执是唯一现场证据；
        #    任何后续动作（回落 CLI、人工排查）都会覆盖"当时看到的是什么"。
        if rc == EXIT_AS_UNVERIFIED and wb:
            ev = as_write_evidence(tag, wb, logs, sel, ver, args.dry)
            if ev:
                out("  证据已落盘：%s" % ev)
                out("  （回执只带 written/verified 计数，不携带不一致格的格坐标 ⇒ 已按「该批全部计划写入格」"
                    "全量列出，请人工逐格核对罐表实际值）")
                rcs_evidence.append(ev)

        rcs.append(rc)
        if rc == EXIT_AS_UNVERIFIED and not args.dry:
            pending_fallback.append((tag, tuple(stations)))
        all_asserts += [{"batch": tag, "no": a["no"], "name": a["name"], "ok": a["ok"],
                         "detail": _scrub(a["detail"])} for a in asserts]
        total_sent += len(sel)
        if wb:
            receipts.append({"batch": tag, "wb": wb})
            t = wb.get("totals") or {}
            for k in ("stations", "triples", "candidates", "written", "samevalue", "gated",
                      "nofield", "verified", "unexpected", "wouldWrite", "e_from_guess", "errors"):
                agg[k] = agg.get(k, 0) + int(t.get(k) or 0)

    # ── 裁定 ⑤：rc=8 时才考虑回落 CLI。**默认不自动跑**（team-lead 2026-10-20 定）
    #    为什么默认关：CLI 全量一轮 610 次调用，明早的配额是攒了一夜的；
    #    自动回落 = 一次失败就把当天额度烧光，把收油统计/日报这些业务一起拖死 ——
    #    而"省配额"正是这条通道存在的理由。所以默认只给命令，`--fallback-cli` 显式打开才真跑。
    #    无论回落成不成功，退出码**保持 8**：让 CI 知道"兜底成功但主通道写歪过"。
    if pending_fallback:
        out("=" * 100)
        out("!! %d 个批写了没验住（rc=%d）。证据已落盘：%s"
            % (len(pending_fallback), EXIT_AS_UNVERIFIED,
               "、".join(rcs_evidence) if rcs_evidence else "（落盘失败，见告警）"))
        for tag, sts in pending_fallback:
            out("   批 %s 回落命令：python scripts/_sync_form_to_tanks.py --channel cli --stations %s"
                % (tag, ",".join(sts)))
        if getattr(args, "fallback_cli", False):
            out("--fallback-cli 已打开 ⇒ 逐批用 CLI 通道补写（会消耗 kdocs-cli 配额，退出码仍为 %d）"
                % EXIT_AS_UNVERIFIED)
            for tag, sts in pending_fallback:
                out("-" * 100)
                out("【回落 CLI】批 %s：%s" % (tag, ",".join(sts)))
                fa = argparse.Namespace(**vars(args))
                fa.channel = "cli"
                fa.stations = list(sts)
                fa.dry = False
                fa.handshake = False
                fa.as_request_out = None
                fa.as_response_in = None
                try:
                    frc = sync(fa)
                    out("【回落 CLI】批 %s 完成，CLI 退出码 %d（本进程最终仍返回 %d）"
                        % (tag, frc, EXIT_AS_UNVERIFIED))
                except Exception as e:                      # 回落失败也不能把 8 变成 0
                    out("【回落 CLI】批 %s 失败：%s" % (tag, e))
                    warn("fallback_fail", "批 %s 回落 CLI 失败：%s（数据仍不正确，需人工补写）" % (tag, e))
        else:
            out("   默认不自动回落（--fallback-cli 可一键补写）。数据仍不正确，请按上面的命令处理。")

    out("=" * 100)
    out("AirScript 通道合计：发送 %d 三元组；回执 triples=%s candidates=%s %s=%s verified=%s "
        "samevalue=%s gated=%s unexpected=%s e_from_guess=%s errors=%s"
        % (total_sent, agg.get("triples"), agg.get("candidates"),
           "wouldWrite" if args.dry else "written",
           agg.get("wouldWrite" if args.dry else "written"), agg.get("verified"),
           agg.get("samevalue"), agg.get("gated"), agg.get("unexpected"),
           agg.get("e_from_guess"), agg.get("errors")))
    if args.dry:
        out("  （dry-run：云端只读演练，罐表零改动；真跑请去掉 --dry）")

    # 告警清单（与 CLI 通道同格式，便于 QA 逐类对比条数）
    if _WARN:
        out("-" * 100)
        out("告警清单（%d 条）" % len(_WARN))
        kinds = {}
        for w in _WARN:
            kinds.setdefault(w["kind"], []).append(w["msg"])
        for kind in sorted(kinds, key=lambda k: -len(kinds[k])):
            out("  [%s] ×%d" % (kind, len(kinds[kind])))
            for m in kinds[kind][:200]:
                out("     - " + m)

    rc = max(rcs, key=lambda r: AS_RC_PRIORITY.get(r, 0)) if rcs else EXIT_OK
    if rc != EXIT_OK:
        out("=" * 100)
        out("✘ AirScript 通道退出码 %d（%s）"
            % (rc, {EXIT_AS_ASSERT: "回执自相矛盾：必须人工核对，**不要**回落 CLI（回落只会掩盖根因）",
                    EXIT_AS_TRIGGER: "触发链路失败 ⇒ 回落 CLI",
                    EXIT_AS_SCRIPT: "脚本侧错误（版本不符/参数被拒/站级异常）⇒ 回落 CLI",
                    EXIT_AS_UNVERIFIED: "写了没验住 ⇒ 证据已落盘；默认**不自动**回落，按上面逐批命令处理"
                                        "（--fallback-cli 可一键补写，跑完仍退 8）",
                    EXIT_ABORT: "参数/读表中止"}.get(rc, "见退出码约定")))
        if rc != EXIT_AS_UNVERIFIED:
            out("  回落命令：python scripts/_sync_form_to_tanks.py %s--channel cli"
                % ("--dry " if args.dry else ""))

    if getattr(args, "report", None):
        try:
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump({"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "channel": "airscript", "mode": "dry" if args.dry else "write",
                           "exit_code": rc, "ver": ver, "endpoint": AS_URL,
                           "plan": meta, "asserts": all_asserts, "rc_per_batch": rcs,
                           "totals": agg, "warnings": _WARN, "receipts": receipts},
                          f, ensure_ascii=False, indent=1)
            out("报告已写出：%s" % args.report)
        except Exception as e:
            warn("report", "报告写出失败：%r" % (e,))
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--stations")
    ap.add_argument("--since", type=int, default=None, help="只处理最近 N 天的提交")
    ap.add_argument("--fill-only", action="store_true",
                    help="只填空值，不覆盖目标已有非零值（首轮保守模式，建议 QA 通过后先这样跑一轮）")
    ap.add_argument("--snapshot-out", default=None,
                    help="把「写入前行号 + C/D/E/F 现值（含原始文本）」落成 JSON，用于零调用复盘/审计")
    ap.add_argument("--report", default=None, help="把统计与告警写成 JSON")
    ap.add_argument("--verbose", action="store_true")
    # ── AirScript 通道（默认 cli，加了下面这些参数才走云端；不加时 CLI 通道行为零变化）──
    ap.add_argument("--channel", choices=("cli", "airscript"), default="cli",
                    help="cli=本脚本自己调 kdocs-cli 读写（默认）；"
                         "airscript=罐表侧的活交给云端槽位 B 脚本，一轮只花 2 次 HTTP")
    ap.add_argument("--batch", default="all", help="airscript 通道分批：1 / 2 / all（默认 all，两批一次跑完）")
    ap.add_argument("--ver", default=None,
                    help="airscript 通道版本握手值（默认 AS_VER=%s，必须与脚本 WB_VER 一致）" % AS_VER)
    ap.add_argument("--handshake", action="store_true",
                    help="airscript 通道零写入握手：只校验版本，不读 A 表、不打开任何文件")
    ap.add_argument("--as-request-out", default=None,
                    help="airscript 通道：只把 sync_task 请求体落盘，不发送（离线核对字段用）")
    ap.add_argument("--as-response-in", default=None,
                    help="airscript 通道：用现成回包文件代替真实 POST（离线验 9 条断言用）")
    ap.add_argument("--fallback-cli", action="store_true",
                    help="airscript 通道 rc=8（写了没验住）时，自动用 CLI 通道补写同一批。"
                         "默认**关**：CLI 全量一轮 610 次调用，会自动烧掉当天配额；"
                         "无论回落成败，最终退出码仍是 8（让 CI 知道主通道写歪过）")
    a = ap.parse_args()
    a.stations = [s.strip() for s in a.stations.split(",")] if a.stations else None
    if a.channel == "airscript":
        return sync_airscript(a)
    if a.handshake or a.as_request_out or a.as_response_in or a.ver or a.batch != "all" or a.fallback_cli:
        out("!! --handshake / --fallback-cli / --as-request-out / --as-response-in / --ver / --batch "
            "只对 --channel airscript 有效；本次按 CLI 通道执行（忽略上述参数）")
    return sync(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        # 可读错误，不给运维看堆栈；也不静默当"今天没人填报"
        out("")
        out("✘ 同步中止：%s" % e)
        out("  · 已中止，未写入任何单元格。排查后重跑。")
        out("  · 429001=当日调用额度用完（次日 08:00 恢复）；429002=瞬时限流（脚本已自动退避重试 3 次）。")
        out("  · 退出码 %d = 读表/配额中止（未写入）；1=写被拒；3=回读没验住。" % EXIT_ABORT)
        sys.exit(EXIT_ABORT)
    except KeyboardInterrupt:
        out("")
        out("✘ 已被用户中断。已写入的单元格保持已写状态（幂等，重跑不会重复写）。")
        sys.exit(130)
