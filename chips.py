#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台指期籌碼快訊
期貨 / 選擇權 / P-C Ratio / 十大交易人 / 現貨 / 散戶多空比
資料來源：TAIFEX、TWSE

用法: python chips.py            (最近交易日)
      python chips.py -d 2026/09/04
"""
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


# ---------------------------------------------------------------- 工具
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
    """讀表格，多層表頭攤平成名稱（不依賴欄位位置）"""
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


# ---------------------------------------------------------------- 抓取
def inst_fut(date):
    html = post("/cht/3/futContractsDate",
                {"queryType": "2", "goDay": "", "doQuery": "1",
                 "dateaddcnt": "", "queryDate": date, "commodityId": ""})
    df = max(tables(html), key=len)
    df.iloc[:, 1] = df.iloc[:, 1].ffill()
    out = {}
    for code, name in PROD.items():
        out[code] = {}
        for w in WHO:
            r = rowfind(df, name, w)
            if r is None:
                continue
            out[code][w] = {"買賣超": num(r.iloc[7]), "多單": num(r.iloc[9]),
                            "空單": num(r.iloc[11]), "淨未平倉": num(r.iloc[13])}
    return out


def inst_opt(date):
    html = post("/cht/3/callsAndPutsDate",
                {"queryType": "2", "goDay": "", "doQuery": "1",
                 "dateaddcnt": "", "queryDate": date, "commodityId": "TXO"})
    df = max(tables(html), key=len)
    out = {}
    for cp, kw in (("CALL", "買權"), ("PUT", "賣權")):
        out[cp] = {}
        for w in WHO:
            r = rowfind(df, kw, w)
            if r is not None:
                out[cp][w] = {"買賣超": num(r.iloc[8]), "淨未平倉": num(r.iloc[14])}
    return out


def large_trader(date, code="TX"):
    """回傳 (十大交易人淨, 十大特定法人淨, 全市場OI)"""
    html = post("/cht/3/largeTraderFutQry",
                {"queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "",
                 "queryDate": date, "commodityId": code, "contractId": code})
    for df in tables(html):
        c_oi = col(df, "全市場未沖銷")
        c_b = col(df, "買方", "前十")
        c_s = col(df, "賣方", "前十")
        if not c_oi:
            continue
        res = {"十大交易人": None, "十大特定法人": None, "全市場OI": None}
        for _, r in df.iterrows():
            j = " ".join(str(v) for v in r.tolist())
            if "所有契約" not in j:
                continue
            oi = num(r[c_oi])
            if oi:
                res["全市場OI"] = oi
            if c_b and c_s:
                b, s = num(r[c_b]), num(r[c_s])
                if b is not None and s is not None:
                    key = "十大特定法人" if "特定法人" in j else "十大交易人"
                    res[key] = b - s
        if res["全市場OI"]:
            return res
    return {"十大交易人": None, "十大特定法人": None, "全市場OI": None}


def pc_ratio(date):
    try:
        df = max(tables(post("/cht/3/pcRatio",
                 {"queryStartDate": date, "queryEndDate": date})), key=len)
        r = df.iloc[-1]
        return {"未平倉比": str(r.iloc[6]), "成交量比": str(r.iloc[3])}
    except Exception:
        return None


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


def retail(fut, code, oi):
    legs = [fut[code].get(w) for w in WHO]
    if not oi or any(x is None for x in legs):
        return None
    il = sum(x["多單"] or 0 for x in legs)
    isr = sum(x["空單"] or 0 for x in legs)
    rl, rs = oi - il, oi - isr
    return {"多單": rl, "空單": rs, "淨": rl - rs,
            "多空比": round((rl - rs) / oi * 100, 2), "OI": oi}


# ---------------------------------------------------------------- 組裝
def build(date):
    rep = {"日期": date, "期貨": inst_fut(date)}
    try:
        rep["選擇權"] = inst_opt(date)
    except Exception as e:
        rep["選擇權"] = {"error": str(e)}

    rep["大額"] = {c: large_trader(date, c) for c in ("TX", "MTX", "TMF")}
    rep["散戶"] = {c: retail(rep["期貨"], c, rep["大額"][c]["全市場OI"])
                   for c in ("MTX", "TMF")}
    rep["現貨"] = spot(date)
    rep["PC"] = pc_ratio(date)

    p = prev_day(date)
    try:
        rep["前日"] = {"日期": p, "期貨": inst_fut(p),
                       "選擇權": inst_opt(p), "現貨": spot(p)}
    except Exception:
        rep["前日"] = None
    return rep


def diff(rep, kind, key, who):
    try:
        return rep[kind][key][who]["淨未平倉"] - rep["前日"][kind][key][who]["淨未平倉"]
    except Exception:
        return None


# ---------------------------------------------------------------- 輸出
def render(rep):
    d = rep["日期"]
    f = lambda v: "—" if v is None else f"{v:+,}"

    print(f"\n=== 台指期籌碼快訊 {d} ===\n")
    for c, lbl in (("MTX", "小台"), ("TMF", "微台")):
        r = rep["散戶"].get(c)
        print(f"{lbl}散戶多空比 " + (f"{r['多空比']:+.2f}%  多單 {r['多單']:,} "
              f"空單 {r['空單']:,}  OI {r['OI']:,}" if r
              else f"取得失敗 (OI={rep['大額'][c]['全市場OI']})"))
    print()
    for w in WHO:
        tx = rep["期貨"]["TX"].get(w, {})
        sp = rep["現貨"].get(w)
        print(f"{w:<4} 台指 {f(tx.get('淨未平倉'))}({f(diff(rep,'期貨','TX',w))})"
              f"  現貨 " + (f"{sp/1e8:+,.2f}億" if sp is not None else "—"))
    if rep.get("PC"):
        print(f"\nP/C 未平倉比 {rep['PC']['未平倉比']}  成交量比 {rep['PC']['成交量比']}")
    lt = rep["大額"]["TX"]
    print(f"十大交易人 {f(lt['十大交易人'])}  十大特定法人 {f(lt['十大特定法人'])}")

    # ---- HTML ----
    def sp_(v, u=""):
        if v is None:
            return "<span class=g>—</span>"
        c = "u" if v > 0 else "n"
        return f"<span class={c}>{v:+,}{u}</span>"

    def sm(v):
        return "" if v is None else \
            f"<span class='s {'u' if v>0 else 'n'}'>({v:+,})</span>"

    H = ["<h2>散戶多空比</h2><div class=c>"]
    for c, lbl in (("MTX", "小台"), ("TMF", "微台")):
        r = rep["散戶"].get(c)
        if r:
            H.append(f"<div class=k><div>{lbl}</div>"
                     f"<b class={'u' if r['多空比']>0 else 'n'}>{r['多空比']:+.2f}%</b>"
                     f"<div class=s>多單 {r['多單']:,}　空單 {r['空單']:,}<br>"
                     f"全市場OI {r['OI']:,}</div></div>")
        else:
            H.append(f"<div class=k><div>{lbl}</div><b class=g>—</b></div>")
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
            dd = "" if pv is None else \
                f"<span class='s {'u' if v-pv>0 else 'n'}'>({(v-pv)/1e8:+,.2f})</span>"
            td.append(f"<td><span class={'u' if v>0 else 'n'}>"
                      f"{v/1e8:+,.2f}</span><br>{dd}</td>")
        for kind, key in (("期貨", "TX"), ("選擇權", "CALL"), ("選擇權", "PUT")):
            src = rep.get(kind)
            val = (src.get(key) or {}).get(w, {}).get("淨未平倉") \
                if isinstance(src, dict) and "error" not in src else None
            td.append(f"<td>{sp_(val)}<br>{sm(diff(rep, kind, key, w))}</td>")
        H.append("<tr>" + "".join(td) + "</tr>")
    H.append("</table>")

    H.append("<h2>外資買賣超（當日交易淨額）</h2><table>")
    for c, lbl in (("TX", "台指期"), ("MTX", "小台"), ("TMF", "微台"),
                   ("TE", "電子期"), ("TF", "金融期")):
        H.append(f"<tr><td>{lbl}</td><td class=r>"
                 f"{sp_((rep['期貨'][c].get('外資') or {}).get('買賣超'))} 口</td></tr>")
    v = rep["現貨"].get("外資")
    if v is not None:
        H.append(f"<tr><td>現貨</td><td class=r><span class={'u' if v>0 else 'n'}>"
                 f"{v/1e8:+,.2f}</span> 億</td></tr>")
    H.append("</table>")

    H.append("<h2>大額交易人 TX</h2><table>"
             f"<tr><td>十大交易人淨部位</td><td class=r>{sp_(lt['十大交易人'])}</td></tr>"
             f"<tr><td>十大特定法人淨部位</td><td class=r>"
             f"{sp_(lt['十大特定法人'])}</td></tr></table>")

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
           "font-size:26px;margin:4px 0;font-variant-numeric:tabular-nums}"
           "table{border-collapse:collapse;width:100%;font-size:14px}"
           "td,th{border-bottom:1px solid #eee;padding:9px 5px;text-align:center}"
           "table.grid th{background:#f5f5f5;font-size:12px;color:#555}"
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
