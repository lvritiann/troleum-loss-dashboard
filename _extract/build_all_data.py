import json, glob, os, re, datetime, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "all_data.json")

# 站点顺序（与业务一致）
STATION_ORDER = ["秦岭","宁陕","洋县","汉中","富平","韩城","富县","南沙","华山","白河",
                 "旬阳","略阳","玉华宫","照金","天汉水城"]

def load_station(f):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict) or "tanks" not in d:
        return None
    return d

# 1) 载入所有站点
stations_raw = {}
for f in sorted(glob.glob(os.path.join(HERE, "*.json"))):
    name = os.path.basename(f)
    if name in ("process_station.py", "build_fuxian_ws2.py"):
        continue
    d = load_station(f)
    if d is None:
        continue
    nm = d.get("name") or name.replace(".json", "")
    stations_raw[nm] = d

print("loaded stations:", len(stations_raw), sorted(stations_raw))

PRODUCTS = ["92#汽油", "95#汽油", "0#柴油"]
PRODUCT_LABEL = {"92#汽油": "92汽油", "95#汽油": "95汽油", "0#柴油": "柴油"}

def norm_product(p):
    if p is None:
        return None
    s = str(p).strip()
    if not s:
        return None
    if "92#汽油" in s: return "92#汽油"
    if "95#汽油" in s: return "95#汽油"
    if "-20#柴油" in s or "0#柴油" in s or "-10#柴油" in s or "柴油" in s: return "0#柴油"
    return s

stations_out = []
all_unload = []
totalL = 0.0
totalT = 0.0

def med(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else 0.0

def win_stats(items, days):
    """按固定自然日窗口聚合（以该罐最新日期为锚点往前 days-1 天）。
    与前端「各罐自身最近 N 条」的相对位次不同：这里是真正的同一天区间，
    用于「近 7 日 / 近 30 日」切换；数据不足时返回 None 由前端降级。"""
    if not items:
        return None
    dates = [x.get("date") for x in items if x.get("date")]
    if not dates:
        return None
    end = max(dates)
    try:
        y, m, d = (int(v) for v in end.split("-"))
        start = (datetime.date(y, m, d) - datetime.timedelta(days=days - 1)).isoformat()
    except Exception:
        return None
    sel = [x for x in items if start <= x.get("date", "") <= end]
    if not sel:
        return None
    loss = [x.get("lossL") or 0 for x in sel]
    rate = [x.get("lossRate5") or 0 for x in sel]
    unl = [x.get("unloadL") or 0 for x in sel]
    return {
        "days": days, "from": start, "to": end, "n": len(sel),
        "lossSumL": round(sum(loss), 2),
        "lossAvgL": round(sum(loss) / len(loss), 2),
        "lossRateAvg": round(med(rate), 5),
        "unloadSumL": round(sum(unl), 1),
        "unloadDays": sum(1 for v in unl if v > 0),
    }

def agg_loss(ts, stockL_total):
    """按油品聚合损溢：均值=各罐中位数日均损溢之平均；合计=各罐中位数之和；
    率=合计/该油品总库存；anomalyCount=异常罐数。"""
    meds = [t.get("lossAvg5L") for t in ts if isinstance(t.get("lossAvg5L"), (int, float))]
    if not meds:
        return {"lossAvg5L": 0.0, "lossSum5L": 0.0, "lossRateAvg5": 0.0, "anomalyCount": 0}
    avg = round(sum(meds) / len(meds), 2)
    s = round(sum(meds), 2)
    rate = round(s / stockL_total, 5) if stockL_total else 0.0
    return {"lossAvg5L": avg, "lossSum5L": s,
            "lossRateAvg5": rate, "anomalyCount": sum(1 for t in ts if t.get("anomaly"))}

for nm in STATION_ORDER:
    if nm not in stations_raw:
        print("  !! missing station:", nm)
        continue
    d = stations_raw[nm]
    tanks = d.get("tanks", [])
    # 建 罐->油品 映射（用于补全 unload 的油品）
    tank_prod = {}
    kept = []
    CUT = 0.15  # |原始损溢量| > 15% 当前库存 → 期初建账/周期重置伪值，剔除（单日损溢不可能超库存15%）
    for t in tanks:
        prod = norm_product(t.get("product"))
        t["product"] = prod
        tank_prod[t.get("tank")] = prod
        # 丢弃空罐占位（无油品且库存为0）
        if prod is None and (t.get("stockL") or 0) == 0:
            continue
        last5 = t.get("last5", [])
        t["last5Raw"] = last5
        # 剔除重置伪值：单日损溢量 > 15% 当前库存 视为建账行，非真实日损溢
        clean = [x for x in last5
                 if abs(x.get("lossL") or 0) <= CUT * (t.get("stockL") or 0)]
        if not clean:
            clean = last5  # 极端兜底：全为伪值时保留原样，避免趋势空白
        t["last5"] = clean
        # 30 日序列（老数据没有 series，回退到 last5）+ 同时剔除同一套伪值
        ser = t.get("series") or t.get("last5Raw") or []
        ser_clean = [x for x in ser if abs(x.get("lossL") or 0) <= CUT * (t.get("stockL") or 0)] or ser
        t["series"] = ser_clean
        t["win7"] = win_stats(ser_clean, 7)
        t["win30"] = win_stats(ser_clean, 30)
        # 单罐损溢聚合用中位数（抗残余极端日）
        t["lossAvg5L"] = round(med([x.get("lossL") for x in clean]), 2)
        t["lossRateAvg5"] = round(med([x.get("lossRate5") for x in clean]), 5)
        # 异常罐标记：|日均损溢| > max(200L, 1%库存) 视为需关注
        t["anomaly"] = bool(abs(t["lossAvg5L"]) > max(200.0, 0.01 * (t.get("stockL") or 0)))
        kept.append(t)
        if prod in PRODUCTS:
            totalL += t.get("stockL", 0.0) or 0.0
            totalT += t.get("stockT", 0.0) or 0.0

    # 站点级按油品聚合
    by_prod = {}
    for p in PRODUCTS:
        ts = [t for t in kept if t.get("product") == p]
        if not ts:
            continue
        sl = sum((t.get("stockL") or 0) for t in ts)
        by_prod[p] = {
            "stockL": round(sl, 1),
            "stockT": round(sum((t.get("stockT") or 0) for t in ts), 1),
            **agg_loss(ts, sl),
        }

    # unload：补全油品并丢弃无法识别的
    unload = []
    for r in d.get("unload", []):
        prod = norm_product(r.get("product"))
        if prod is None:
            prod = tank_prod.get(r.get("tank"))
        if prod is None:
            continue
        r["product"] = prod
        unload.append(r)
        all_unload.append(r)

    stations_out.append({
        "name": nm,
        "byProduct": by_prod,
        "tanks": kept,
        "unload": unload,
    })

# 公司级
company = {"byProduct": {}, "totalStockL": round(totalL, 1), "totalStockT": round(totalT, 1)}
for p in PRODUCTS:
    ts = [t for s in stations_out for t in s["tanks"] if t.get("product") == p]
    if not ts:
        continue
    sl = sum((t.get("stockL", 0.0) or 0.0) for t in ts)
    company["byProduct"][p] = {
        "stockL": round(sl, 1),
        "stockT": round(sum((t.get("stockT", 0.0) or 0.0) for t in ts), 1),
        **agg_loss(ts, sl),
    }

# 公司级固定窗口（近 7 日 / 近 30 日）：所有罐按各自窗口汇总后相加
_all_tanks = [t for s in stations_out for t in s["tanks"]]
_stock_all = sum((t.get("stockL") or 0) for t in _all_tanks)
company["windows"] = {}
for _key, _days in (("win7", 7), ("win30", 30)):
    _sumL = sum((t.get(_key) or {}).get("lossSumL", 0) for t in _all_tanks)
    _unl = sum((t.get(_key) or {}).get("unloadSumL", 0) for t in _all_tanks)
    _cov = sum(1 for t in _all_tanks if t.get(_key))
    company["windows"][_key] = {
        "days": _days,
        "lossSumL": round(_sumL, 2),
        "lossRate": round(_sumL / _stock_all * 1000, 2) if _stock_all else 0,
        "unloadSumL": round(_unl, 1),
        "tanksCovered": _cov,
        "tanksTotal": len(_all_tanks),
    }

out = {
    "generatedAt": datetime.date.today().isoformat() + " (真实数据·15站自动抽取)",
    "isMock": False,
    "products": PRODUCTS,
    "productLabel": PRODUCT_LABEL,
    "company": company,
    "stations": stations_out,
    "unload": all_unload,
}

json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# 汇总
print("\n=== 汇总 ===")
print("站点数:", len(stations_out))
print("公司总库存 L=%.0f  t=%.1f" % (company["totalStockL"], company["totalStockT"]))
for p in PRODUCTS:
    b = company["byProduct"].get(p)
    if b:
        print("  %s: 库存 %.0f L / %.1f t | 近5损溢量均值 %.1f L | 率 %.4f%%"
              % (p, b["stockL"], b["stockT"], b["lossAvg5L"], b["lossRateAvg5"]*100))
for _k in ("win7","win30"):
    _w = company["windows"][_k]
    print("  %s: 合计 %.1f L | 率 %.2f‰ | 卸油 %.0f L | 覆盖 %d/%d 罐"
          % (_k, _w["lossSumL"], _w["lossRate"], _w["unloadSumL"], _w["tanksCovered"], _w["tanksTotal"]))
print("接卸记录总数:", len(all_unload))
print("各站罐数:", {s["name"]: len(s["tanks"]) for s in stations_out})
print("各站接卸数:", {s["name"]: len(s["unload"]) for s in stations_out})
print("written ->", os.path.normpath(OUT))
