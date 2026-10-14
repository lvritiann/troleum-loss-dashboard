import json, sys, os, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import process_station as ps

TODAY = ps.TODAY
EXTRACT = os.path.dirname(os.path.abspath(__file__))

def load_raw(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict) and isinstance(d.get("data"), dict) and "typedValues" in d["data"]:
        return d, d["data"]["typedValues"]
    if isinstance(d, dict) and "typedValues" in d:
        return d, d["typedValues"]
    raise RuntimeError("unexpected payload in %s" % path)

def row_has_garbage(r):
    """未来行是否带实体数据垃圾：D~I 列(index 3~8：实际库存/密度/温度/卸油/进货)
    任一单元格非零/非空即判为垃圾。J 及之后是公式派生列(损溢率等)，所有站都有，
    不属于本次要清的 D/E/F 实体库存垃圾。"""
    for k in range(3, 9):
        if k >= len(r):
            continue
        v = r[k].get("value")
        if isinstance(v, (int, float)) and v != 0:
            return True
        if isinstance(v, str) and v.strip():
            return True
    return False

def main():
    dry = "--dry" in sys.argv
    files = sorted(glob.glob(os.path.join(EXTRACT, "raw_*_ws*.txt")))
    total = 0
    per_station = {}
    for f in files:
        d, tv = load_raw(f)
        rows = ps.parse_tank_rows(tv)
        cleared = 0
        for r in rows:
            dt = ps.parse_date(ps.cell_val(r, 0))
            # 仅清：未来日期 且 D~I 实体列确实带垃圾数据
            if dt and dt > TODAY and row_has_garbage(r):
                if not dry:
                    for k in range(1, 9):  # B..I (当日销售~当日进货)
                        if k < len(r):
                            r[k]["value"] = ""
                            r[k]["type"] = "string"
                cleared += 1
        if cleared:
            per_station[os.path.basename(f)] = cleared
            total += cleared
            if not dry:
                json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False)
    for f, n in sorted(per_station.items()):
        print("  %s : %d future rows" % (f, n))
    print("TOTAL cleared: %d  (%s)" % (total, "DRY-RUN" if dry else "WRITTEN"))

if __name__ == "__main__":
    main()
