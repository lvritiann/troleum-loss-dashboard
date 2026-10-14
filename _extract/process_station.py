import json, sys, os, re, datetime

# 截止日期：原来写死 2026-8-28，导致 8/28 之后写入的真实库存被 dashboard 排除（表现为"旬阳库存=0"）。
# 改为取运行当天，每次重跑管线都会自动读到最新一行有效库存，避免再次过期。
TODAY = datetime.date.today()
DATE_RE = re.compile(r'^\d{4}-\d{1,2}-\d{1,2}$')
WIDTH = 14  # 罐表数据行固定 14 列 (A:N)

def is_date(v):
    return isinstance(v, str) and bool(DATE_RE.match(v))

def parse_date(v):
    if not isinstance(v, str):
        return None
    try:
        y, m, d = map(int, v.split("-"))
        return datetime.date(y, m, d)
    except Exception:
        return None

def num(cell):
    if cell is None:
        return None
    v = cell.get("value")
    if isinstance(v, (int, float)):
        return float(v)
    return None

def cell_val(r, k):
    if len(r) > k:
        return r[k].get("value")
    return None

def norm_product(p):
    if p is None:
        return None
    s = str(p).strip()
    if not s:
        return None
    # get_typed_value 抓取时 B 列油品是原始值（92/95/0/-20 等数字或 92# 短码），
    # 须先归一为标准油品名，否则按油品聚合/筛选时这些记录会被丢弃。
    m = re.match(r'^(-?\d+)(#|$)', s)
    if m:
        n = m.group(1)
        if n == "92":
            return "92#汽油"
        if n == "95":
            return "95#汽油"
        return "0#柴油"  # 0 / -10 / -20 等柴油标号
    if "92#汽油" in s:
        return "92#汽油"
    if "95#汽油" in s:
        return "95#汽油"
    if "-20#柴油" in s or "0#柴油" in s or "-10#柴油" in s or "柴油" in s:
        return "0#柴油"
    return s

def norm_tank_key(s):
    """罐号归一化，用于 ws2 映射与罐表标题的匹配。"""
    if s is None:
        return ""
    s = str(s).strip()
    # 去掉常见修饰，仅保留 方向+数字，如 '南站4'/'南4号' -> '南4'
    m = re.search(r'([南北东西])[站]?\s*(\d+)', s)
    if m:
        return m.group(1) + m.group(2)
    return s

def load_tv(path):
    d = json.load(open(path, encoding="utf-8"))
    # 兼容两类落盘：直接 {"code":..,"data":..} 或 包装后的结构
    if isinstance(d, dict) and "data" in d and isinstance(d["data"], dict) and "typedValues" in d["data"]:
        if d.get("code", 0) != 0:
            raise RuntimeError("kdocs err: %s" % d.get("message"))
        return d["data"]["typedValues"]
    if isinstance(d, dict) and "typedValues" in d:
        return d["typedValues"]
    raise RuntimeError("unexpected payload in %s" % path)

def find_header(tv):
    for i, c in enumerate(tv):
        if c.get("value") == "📅 日期":
            return i
    return None

def parse_tank_rows(tv):
    """按日期定界切分罐表数据行。

    罐表常见两种坑：
    1) 行内日期串：部分行把“期初库存/账面库存”列填成了日期，该日期值恰等于下一行
       行首日期（如 2-19 行的期初库存=2-20，正是 2-20 行行首）。故：**一个日期若与
       下一个日期单元格的值相同，则是行内日期，应排除**。
    2) 行宽随日期变化（9/10/14 列不等），不再假设固定宽度，直接用定界切分。
    """
    hi = find_header(tv)
    if hi is None:
        return []
    dates = [(i, c.get("value")) for i, c in enumerate(tv)
             if is_date(c.get("value")) and i > hi]
    if not dates:
        return []
    delims = []
    n = len(dates)
    for idx, (i, v) in enumerate(dates):
        # 保留：末位，或与下一个日期值不同（说明不是行内重复日期）
        if idx == n - 1 or v != dates[idx + 1][1]:
            delims.append(i)
    rows = []
    for a, b in zip(delims, delims[1:] + [len(tv)]):
        rows.append(tv[a:b])
    return rows

def parse_title(tv):
    t = tv[0].get("value") if tv else ""
    if not isinstance(t, str):
        return None, None
    parts = t.split(" · ")
    tank_raw = parts[0] if parts else t
    prod_raw = parts[1] if len(parts) > 1 else None
    tank = tank_raw.replace("🛢️ ", "").replace("#罐", "").replace("站", "").strip()
    return tank, prod_raw

def is_real_row(r):
    """排除预填的全零未来行。真实行需：密度>0，或库存/原始损溢量/5日损溢率任一非零。"""
    if isinstance(cell_val(r, 4), (int, float)) and cell_val(r, 4) != 0:
        return True  # 密度非零
    for k in (3, 11, 13):
        v = cell_val(r, k)
        if isinstance(v, (int, float)) and v != 0:
            return True
    return False

DENS_MIN, DENS_MAX = 600.0, 1000.0   # 合理燃料密度范围 kg/m³
STOCK_MAX = 200000.0                  # 单罐库存上限(L)，用于剔除明显错位值

def row_signal(r):
    for k in (3, 4, 11, 13):
        if k >= len(r):
            continue
        v = r[k].get("value")
        if isinstance(v, (int, float)) and v != 0:
            return True
    return False

def safe_num(v):
    return v if isinstance(v, (int, float)) else None

def process_tank(tv, errors, tank_prod_fb, prod_density_fb):
    tank, prod_raw = parse_title(tv)
    rows = parse_tank_rows(tv)
    real = []
    for r in rows:
        if not r:
            continue
        d = parse_date(cell_val(r, 0))
        if d is None:
            continue
        real.append((d, r))
    if not real:
        errors.append("tank %s: no dated rows" % tank)
        return None
    real_today = [x for x in real if x[0] <= TODAY]
    if not real_today:
        errors.append("tank %s: no rows <= TODAY" % tank)
        return None
    real_today.sort(key=lambda x: x[0])
    # 当前行：优先取末条≤TODAY、库存有效(0<col3≤MAX)且密度也有效
    # (DENS_MIN≤col4≤DENS_MAX)的记录。密度无效的行多为源表误填/清空占位行，
    # 不应作为当前库存（如华山 2026-08-20 行密度被误填为 33377.5）。
    # 退而求其次：仅要求库存有效（密度可回退）；再不行用末条含有效信号的行。
    def valid_dens(r):
        v = safe_num(cell_val(r, 4))
        return v is not None and DENS_MIN <= v <= DENS_MAX
    stocked = [x for x in real_today
               if isinstance(cell_val(x[1], 3), (int, float))
               and 0 < cell_val(x[1], 3) <= STOCK_MAX
               and valid_dens(x[1])]
    if stocked:
        cur = stocked[-1]
    else:
        any_stock = [x for x in real_today
                     if isinstance(cell_val(x[1], 3), (int, float))
                     and 0 < cell_val(x[1], 3) <= STOCK_MAX]
        if any_stock:
            cur = any_stock[-1]
        else:
            cur = real_today[-1]
            for x in reversed(real_today):
                if row_signal(x[1]):
                    cur = x
                    break
    cur_d, cur = cur
    raw_stock = safe_num(cell_val(cur, 3)) or 0.0
    raw_dens = safe_num(cell_val(cur, 4)) or 0.0
    stockL = raw_stock if 0 <= raw_stock <= STOCK_MAX else 0.0
    density = raw_dens if DENS_MIN <= raw_dens <= DENS_MAX else 0.0
    # 近5日损溢：取末5条含有效损溢信号的行
    loss_rows = [x for x in real_today if row_signal(x[1])]
    last5_src = (loss_rows if loss_rows else real_today)[-5:]
    last5_out, lossLs, lossRs = [], [], []
    for d, r in last5_src:
        lL = safe_num(cell_val(r, 11)) if len(r) > 11 else None
        lR = safe_num(cell_val(r, 13)) if len(r) > 13 else None
        lL = lL if isinstance(lL, (int, float)) else 0.0
        lR = lR if isinstance(lR, (int, float)) else 0.0
        last5_out.append({"date": d.isoformat(), "lossL": lL, "lossRate5": lR})
        lossLs.append(lL)
        lossRs.append(lR)
    lossAvg5L = sum(lossLs) / len(lossLs) if lossLs else 0.0
    lossRateAvg5 = sum(lossRs) / len(lossRs) if lossRs else 0.0
    stockT = stockL * density / 1e6
    product = norm_product(prod_raw)
    if product is None or product == "未知":
        product = tank_prod_fb.get(norm_tank_key(tank)) or "未知"
    # 罐表密度缺失/越界时，用本站该油品的收油反算密度回退
    if density == 0.0 and product in prod_density_fb:
        density = prod_density_fb[product]
        stockT = stockL * density / 1e6
    return {
        "tank": tank, "product": product,
        "stockL": stockL, "stockT": stockT,
        "last5": last5_out, "lossAvg5L": lossAvg5L, "lossRateAvg5": lossRateAvg5,
        "density": density,
    }

def process_ws2(tv, station_name, errors):
    unload = []
    tank_prod = {}
    prod_density_samples = {}
    for i in range(len(tv)):
        if not is_date(tv[i].get("value")):
            continue
        r = tv[i:i + 12]
        d = parse_date(cell_val(r, 0))
        if d is None:
            continue
        product = norm_product(cell_val(r, 1) if len(r) > 1 else None)
        tank = cell_val(r, 10) if len(r) > 10 else None
        truck = cell_val(r, 3) if len(r) > 3 else None
        volumeL = num(r[7]) if len(r) > 7 else None
        massT = num(r[5]) if len(r) > 5 else None
        gainLossT = num(r[11]) if len(r) > 11 else None
        if tank is not None and product is not None:
            tank_prod[norm_tank_key(tank)] = product
        # 用收油记录反算各油品密度（质量t × 1e6 / 体积L = kg/m³）
        if product is not None and massT and volumeL and volumeL > 0:
            dens = massT * 1e6 / volumeL
            if DENS_MIN <= dens <= DENS_MAX:
                prod_density_samples.setdefault(product, []).append(dens)
        unload.append({
            "date": d.isoformat(),
            "product": product,
            "station": station_name,
            "truck": truck,
            "tank": tank,
            "volumeL": volumeL or 0.0,
            "massT": massT or 0.0,
            "gainLossT": gainLossT or 0.0,
        })
    prod_density = {}
    for p, lst in prod_density_samples.items():
        if lst:
            prod_density[p] = sum(lst) / len(lst)
    return unload, tank_prod, prod_density

def aggregate(by_prod_tanks):
    out = {}
    for prod, tanks in by_prod_tanks.items():
        if not tanks or prod == "未知":
            continue
        stockL = sum(t["stockL"] for t in tanks)
        stockT = sum(t["stockT"] for t in tanks)
        lossAvg5L = sum(t["lossAvg5L"] for t in tanks) / len(tanks)
        lossRateAvg5 = sum(t["lossRateAvg5"] for t in tanks) / len(tanks)
        out[prod] = {
            "stockL": stockL, "stockT": stockT,
            "lossAvg5L": lossAvg5L, "lossRateAvg5": lossRateAvg5,
        }
    return out

def main():
    station = sys.argv[1]
    ws2_path = sys.argv[2]
    tank_paths = sys.argv[3:]
    errors = []
    ws2_tv = load_tv(ws2_path)
    unload, tank_prod, prod_density = process_ws2(ws2_tv, station, errors)
    tanks = []
    by_prod = {}
    for p in tank_paths:
        try:
            tv = load_tv(p)
            t = process_tank(tv, errors, tank_prod, prod_density)
            if t:
                tanks.append(t)
                if t["product"] != "未知":
                    by_prod.setdefault(t["product"], []).append(t)
        except Exception as e:
            errors.append("tank %s: %s" % (p, e))
    byProduct = aggregate(by_prod)
    result = {
        "name": station,
        "byProduct": byProduct,
        "tanks": tanks,
        "unload": unload,
        "errors": errors,
    }
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(out_dir, station + ".json")
    json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("station", station, "tanks", len(tanks), "unload", len(unload), "errors", len(errors))
    for e in errors[:10]:
        print("  ERR", e)

if __name__ == "__main__":
    main()
