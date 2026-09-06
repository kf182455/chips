#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台指期籌碼快訊：期貨/選擇權/PC比/十大交易人/現貨/散戶多空比"""
import argparse, datetime as dt, io, json, os, re, time
import pandas as pd, requests

BASE = "https://www.taifex.com.tw"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": BASE + "/cht/3/futContractsDate", "Origin": BASE})
OUT = os.path.dirname(os.path.abspath(__file__))
PROD = {"TX": "臺股期貨", "MTX": "小型臺指期貨", "TMF": "微型臺指期貨",
        "TE": "電子期貨", "TF": "金融期貨"}
WHO = ("外資", "投信", "自營商")
DBG = []


def post(path, data, retry=3):
    last = None
    for i in range(retry):
        try:
            r = S.post(BASE + path, data=data, timeout=30)
            r.encoding = "utf-8"
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = repr(e)
        time.sleep(2 + i * 2)
    raise RuntimeError(f"{path}: {last}")


def tables(html):
    res = []
    for df in pd.read_html(io.StringIO(html)):
        cols = []
        for c in df.columns:
            if isinstance(c, tuple):
                seen = []
                for x in c:
                    x = str(x).strip()
                    if not x.startswith("Unnamed") and x not in seen:
                        seen.append(x)
                cols.append(" ".join(seen))
            else:
                cols.append(str(c).strip())
        d = df.copy()
        d.columns = cols
        res.append(d)
    return res


def col(df, *kw):
    for c in df.columns:
        if all(k in str(c) for k in kw):
            return c
    return None


def num(x):
    s = str(x).replace(",", "").strip()
    if s in ("-", "", "nan", "None"):
        return None
    m = re.search(r"-?\d+\.?\d*", s.replace("(", "-").replace(")", ""))
    return int(float(m.group())) if m else None


def rowfind(df, *kw):
    for _, r in df.iterrows():
        if all(k in " ".join(str(v) for v in r.tolist()) for k in kw):
            return r
    return None


def prev_day(s):
    d = dt.datetime.strptime(s, "%Y/%m/%d").date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y/%m/%d")


# ---------------- 三大法人 ----------------
def inst_fut(date):
    df = max(tables(post("/cht/3/futContractsDate",
        {"queryType": "2", "goDay": "", "doQuery": "1", "dateaddcnt": "",
         "queryDate": date, "commodityId": ""})), key=len)
    df.iloc[:, 1] = df.iloc[:, 1].ffill()
    out = {}
    for code, name in PROD.items():
        out[code] = {}
        for w in WHO:
            r = rowfind(df, name, w)
            if r is not None:
                out[code][w] = {"買賣超": num(r.iloc[7]), "多單": num(r.iloc[9]),
                                "空單": num(r.iloc[11]), "淨未平倉": num(r.iloc[13])}
    return out


def inst_opt(date):
    df = max(tables(post("/cht/3/callsAndPutsDate",
        {"queryType": "2", "goDay": "", "doQuery": "1", "dateaddcnt": "",
         "queryDate": date, "commodityId": "TXO"})), key=len)
    o = {}
    for cp, kw in (("CALL", "買權"), ("PUT", "賣權")):
        o[cp] = {}
        for w in WHO:
            r = rowfind(df, kw, w)
            if r is not None:
                o[cp][w] = {"買賣超": num(r.iloc[8]), "淨未平倉": num(r.iloc[14])}
    return o


# ---------------- 全市場未平倉量 ----------------
def _oi_from_html(html, code):
    """從行情表加總該商品各月份未沖銷契約量（用欄位名稱定位）"""
    for df in tables(html):
        c_oi, c_c = col(df, "未沖銷"), col(df, "契約")
        c_m = col(df, "到期", "月份")
        if not (c_oi and c_c and c_m):
            continue
        tot = 0
        for _, r in df.iterrows():
            if str(r[c_c]).strip() != code or "/" in str(r[c_m]):
                continue
            n = num(r[c_oi])
            if n:
                tot += n
        if tot:
            return tot
    return None


def total_oi(date, code):
    """期貨每日交易行情（一般交易時段 marketCode=0）"""
    variants = [
        {"queryType": "2", "marketCode": "0", "MarketCode": "0", "dateaddcnt": "",
         "commodity_id": code, "commodity_id2": "", "commodity_idt": code,
         "commodity_id2t": "", "commodity_id2t2": "", "queryDate": date},
        {"queryType": "2", "marketCode": "0", "commodity_id": code,
         "commodity_idt": code, "queryDate": date},
        {"queryType": "2", "marketCode": "0", "commodity_id": code,
         "queryDate": date, "doQuery": "1"},
    ]
    for i, v in enumerate(variants):
        try:
            html = post("/cht/3/futDailyMarketReport", v, retry=2)
            n = _oi_from_html(html, code)
            DBG.append(f"OI {code} 參數組{i+1}: {n}  日期符合={date in html}")
            if n and date in html:
                return n
        except Exception as e:
            DBG.append(f"OI {code} 參數組{i+1}: ERR {e}")
    return None


def large_trader(date):
    """台指類合併 OI（TX+MTX/4+TMF/20）與十大交易人淨部位，用於驗算"""
    out = {"十大交易人": None, "十大特定法人": None, "合併OI": None}
    try:
        for df in tables(post("/cht/3/largeTraderFutQry",
            {"queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "",
             "queryDate": date, "contractId": "TX", "commodityId": "TX"})):
            c_oi = col(df, "全市場未沖銷")
            c_b, c_s = col(df, "買方", "前十"), col(df, "賣方", "前十")
            if not c_oi:
                continue
            for _, r in df.iterrows():
                j = " ".join(str(v) for v in r.tolist())
                if "臺股期貨" not in j and "TX+MTX" not in j:
                    continue
                if "所有" not in j:
                    continue
                out["合併OI"] = num(r[c_oi])
                if c_b and c_s:
                    b, s = num(r[c_b]), num(r[c_s])
                    if b is not None and s is not None:
                        out["十大交易人"] = b - s
                        # 括號內為特定法人，再取一次
                        m = re.findall(r"\(([\d,]+)\)", str(r[c_b]) + "|" + str(r[c_s]))
                        if len(m) >= 2:
                            out["十大特定法人"] = num(m[0]) - num(m[1])
                break
            if out["合併OI"]:
                break
    except Exception as e:
        DBG.append(f"大額交易人: ERR {e}")
    return out


def spot(date):
    try:
        js = S.get("https://www.twse.com.tw/rwd/zh/fund/BFI82U"
                   f"?dayDate={date.replace('/','')}&type=day&response=json",
                   timeout=20).json()
        if js.get("stat") != "OK":
            return {}
        o = {}
        for r in js["data"]:
            n, v = r[0].strip(), num(r[3])
            if v is None:
                continue
            k = "外資" if "外" in n else "投信" if "投信" in n else \
                "自營商" if "自營" in n else None
            if k:
                o[k] = o.get(k, 0) + v
        return o
    except Exception:
        return {}


def pc_ratio(date):
    try:
        r = max(tables(post("/cht/3/pcRatio",
            {"queryStartDate": date, "queryEndDate": date})), key=len).iloc[-1]
        return {"未平倉比": str(r.iloc[6]), "成交量比": str(r.iloc[3])}
    except Exception:
        return None


def retail(fut, code, oi):
    legs = [fut[code].get(w) for w in WHO]
    if not oi or any(x is None for x in legs):
        return None
    il = sum(x["多單"] or 0 for x in legs)
    isr = sum(x["空單"] or 0 for x in legs)
    rl, rs = oi - il, oi - isr
    return {"多單": rl, "空單": rs, "淨": rl - rs,
            "多空比": round((rl - rs) / oi * 100, 2), "OI": oi}


# ---------------- 組裝 ----------------
def build(date):
    rep = {"日期": date, "期貨": inst_fut(date)}
    try:
        rep["選擇權"] = inst_opt(date)
    except Exception as e:
        rep["選擇權"] = {"error": str(e)}
    rep["大額"] = large_trader(date)
    rep["OI"] = {c: total_oi(date, c) for c in ("TX", "MTX", "TMF")}
    rep["散戶"] = {c: retail(rep["期貨"], c, rep["OI"][c]) for c in ("MTX", "TMF")}
    rep["現貨"] = spot(date)
    rep["PC"] = pc_ratio(date)

    # 驗算：TX + MTX/4 + TMF/20 應等於大額交易人的合併OI
    o, c = rep["OI"], rep["大額"]["合併OI"]
    if all(o.get(k) for k in ("TX", "MTX", "TMF")) and c:
        calc = o["TX"] + o["MTX"] / 4 + o["TMF"] / 20
        rep["驗算"] = {"計算": round(calc), "官方": c,
                       "誤差": round(calc - c), "通過": abs(calc - c) < c * 0.02}
    p = prev_day(date)
    try:
        rep["前日"] = {"日期": p, "期貨": inst_fut(p), "選擇權": inst_opt(p),
                       "現貨": spot(p)}
    except Exception:
        rep["前日"] = None
    return rep


def diff(rep, kind, key, who):
    try:
        return rep[kind][key][who]["淨未平倉"] - rep["前日"][kind][key][who]["淨未平倉"]
    except Exception:
        return None


# ---------------- 輸出 ----------------
def render(rep):
    d, f = rep["日期"], lambda v: "—" if v is None else f"{v:+,}"
    print(f"\n=== 台指期籌碼快訊 {d} ===")
    for c, lbl in (("MTX", "小台"), ("TMF", "微台")):
        r = rep["散戶"].get(c)
        print(f"{lbl}散戶多空比 " + (f"{r['多空比']:+.2f}%  多單 {r['多單']:,} "
              f"空單 {r['空單']:,}  OI {r['OI']:,}" if r else f"失敗 OI={rep['OI'][c]}"))
    if rep.get("驗算"):
        v = rep["驗算"]
        print(f"驗算 TX+MTX/4+TMF/20 = {v['計算']:,} vs 官方 {v['官方']:,} "
              f"→ {'通過' if v['通過'] else '不符'}")
    for w in WHO:
        tx, sp = rep["期貨"]["TX"].get(w, {}), rep["現貨"].get(w)
        print(f"{w:<4} 台指 {f(tx.get('淨未平倉'))}({f(diff(rep,'期貨','TX',w))})"
              f"  現貨 " + (f"{sp/1e8:+,.2f}億" if sp is not None else "—"))
    if DBG:
        print("\n[診斷]")
        for x in DBG:
            print(" ", x)

    def n_(v):
        return "<span class=g>—</span>" if v is None else \
            f"<span class={'u' if v>0 else 'n'}>{v:+,}</span>"

    def s_(v):
        return "" if v is None else \
            f"<span class='s {'u' if v>0 else 'n'}'>({v:+,})</span>"

    H = ["<h2>散戶多空比</h2><div class=c>"]
    for c, lbl in (("MTX", "小台"), ("TMF", "微台")):
        r = rep["散戶"].get(c)
        H.append(f"<div class=k><div>{lbl}</div>" + (
            f"<b class={'u' if r['多空比']>0 else 'n'}>{r['多空比']:+.2f}%</b>"
            f"<div class=s>多單 {r['多單']:,}　空單 {r['空單']:,}<br>"
            f"全市場OI {r['OI']:,}</div>" if r else "<b class=g>—</b>") + "</div>")
    H.append("</div>")

    H.append("<h2>三大法人籌碼變化 <span class=s>括號為前日增減</span></h2>"
             "<table class=grid><tr><th></th><th>現貨(億)</th><th>台指(口)</th>"
             "<th>Call(口)</th><th>Put(口)</th></tr>")
    for w in WHO:
        td = [f"<th>{w}</th>"]
        v = rep["現貨"].get(w)
        pv = ((rep.get("前日") or {}).get("現貨") or {}).get(w)
        if v is None:
            td.append("<td class=g>—</td>")
        else:
            dd = "" if pv is None else (f"<span class='s {'u' if v-pv>0 else 'n'}'>"
                                        f"({(v-pv)/1e8:+,.2f})</span>")
            td.append(f"<td><span class={'u' if v>0 else 'n'}>{v/1e8:+,.2f}"
                      f"</span><br>{dd}</td>")
        for kind, key in (("期貨", "TX"), ("選擇權", "CALL"), ("選擇權", "PUT")):
            src = rep.get(kind)
            val = (src.get(key) or {}).get(w, {}).get("淨未平倉") \
                if isinstance(src, dict) and "error" not in src else None
            td.append(f"<td>{n_(val)}<br>{s_(diff(rep, kind, key, w))}</td>")
        H.append("<tr>" + "".join(td) + "</tr>")
    H.append("</table><h2>外資買賣超（當日交易淨額）</h2><table>")
    for c, lbl in (("TX", "台指期"), ("MTX", "小台"), ("TMF", "微台"),
                   ("TE", "電子期"), ("TF", "金融期")):
        H.append(f"<tr><td>{lbl}</td><td class=r>"
                 f"{n_((rep['期貨'][c].get('外資') or {}).get('買賣超'))} 口</td></tr>")
    v = rep["現貨"].get("外資")
    if v is not None:
        H.append(f"<tr><td>現貨</td><td class=r><span class={'u' if v>0 else 'n'}>"
                 f"{v/1e8:+,.2f}</span> 億</td></tr>")
    H.append("</table>")
    lt = rep["大額"]
    H.append("<h2>大額交易人 台指類</h2><table>"
             f"<tr><td>十大交易人淨部位</td><td class=r>{n_(lt['十大交易人'])}</td></tr>"
             f"<tr><td>合併未平倉(TX當量)</td><td class=r>"
             f"{'—' if not lt['合併OI'] else format(lt['合併OI'], ',')}</td></tr></table>")
    if rep.get("PC"):
        H.append("<h2>Put / Call Ratio</h2><table>"
                 f"<tr><td>未平倉量比</td><td class=r>{rep['PC']['未平倉比']}</td></tr>"
                 f"<tr><td>成交量比</td><td class=r>{rep['PC']['成交量比']}</td></tr>"
                 "</table>")

    css = ("body{font-family:-apple-system,'Noto Sans TC',sans-serif;padding:16px;"
           "max-width:680px;margin:0}h1{font-size:19px;margin:0;border-bottom:3px "
           "solid #c62828;padding-bottom:8px}.d{color:#888;font-size:12px;"
           "margin:8px 0 18px}h2{font-size:14px;color:#c62828;margin:24px 0 8px}"
           ".s{font-size:11px;font-weight:400;color:#888}.c{display:flex;gap:10px}"
           ".k{flex:1;border:1px solid #e5e5e5;border-radius:10px;padding:12px;"
           "text-align:center;font-size:12px;color:#777}.k b{display:block;"
           "font-size:26px;margin:4px 0}table{border-collapse:collapse;width:100%;"
           "font-size:14px}td,th{border-bottom:1px solid #eee;padding:9px 5px;"
           "text-align:center}table.grid th{background:#f5f5f5;font-size:12px}"
           "td:first-child{text-align:left}.r{text-align:right;font-weight:600}"
           ".u{color:#d32f2f}.n{color:#2e7d32}.g{color:#bbb}"
           "footer{margin-top:26px;color:#999;font-size:11px;line-height:1.6}")

    os.makedirs(os.path.join(OUT, "docs"), exist_ok=True)
    with open(os.path.join(OUT, "docs", "index.html"), "w", encoding="utf-8") as fh:
        fh.write(f"<!doctype html><html lang=zh-Hant><meta charset=utf-8>"
                 f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                 f"<title>籌碼快訊 {d}</title><style>{css}</style>"
                 f"<h1>台指期籌碼快訊</h1><div class=d>資料日期 {d}　·　更新 "
                 f"{dt.datetime.now():%m-%d %H:%M}</div>{''.join(H)}"
                 f"<footer>散戶多單=全市場OI−三大法人多單；空單同理；"
                 f"多空比=(多單−空單)/OI×100%。<br>資料來源：臺灣期貨交易所、"
                 f"臺灣證券交易所。僅供參考，不構成投資建議。</footer></html>")
    rep["診斷"] = DBG
    with open(os.path.join(OUT, f"chips_{d.replace('/','')}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=2, default=str)
    print("\n→ docs/index.html 已產生")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--date")
    a = ap.parse_args()
    d = dt.date.today()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    render(build(a.date or d.strftime("%Y/%m/%d")))
