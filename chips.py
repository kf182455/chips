#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台指期籌碼快訊 v3
重點：小台/微台散戶多空比、三大法人(含外資)買賣超與部位變化

資料來源：臺灣期貨交易所(TAIFEX)、臺灣證券交易所(TWSE) 公開資料

用法:
    python chips.py --html
    python chips.py -d 2026/09/04 --html
    python chips.py --watch --html     # 等 15:00 自動輪詢
安裝:
    pip install requests pandas lxml beautifulsoup4
"""

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile

import pandas as pd
import requests

BASE = "https://www.taifex.com.tw"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Referer": BASE + "/cht/3/futContractsDate",
                        "Origin": BASE})
OUTDIR = os.path.dirname(os.path.abspath(__file__))

# 三大法人表的商品名稱 -> 代碼
PRODUCTS = {"TX": "臺股期貨", "MTX": "小型臺指期貨", "TMF": "微型臺指期貨",
            "TE": "電子期貨", "TF": "金融期貨"}


# ------------------------------------------------------------------ 工具
def _post_html(path, data, retry=3):
    url = BASE + path
    last = None
    for i in range(retry):
        try:
            r = SESSION.post(url, data=data, timeout=25)
            r.encoding = "utf-8"
            if r.status_code == 200 and len(r.text) > 500:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = repr(e)
        time.sleep(2 + i * 2)
    raise RuntimeError(f"抓取失敗 {url}: {last}")


def _tables(html, min_rows=1):
    out = []
    for df in pd.read_html(io.StringIO(html)):
        if len(df) < min_rows:
            continue
        df = df.copy()
        df.columns = range(df.shape[1])
        for c in (0, 1, 2):
            if c in df.columns:
                df[c] = df[c].ffill()
        out.append(df)
    return out


def _num(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).replace(",", "").replace("　", "").strip()
    if s in ("-", "", "nan"):
        return None
    s = s.replace("(", "-").replace(")", "")
    m = re.search(r"-?\d+\.?\d*", s)
    return int(float(m.group())) if m else None


def _find_row(df, *kw):
    for _, row in df.iterrows():
        j = " ".join(str(v) for v in row.tolist())
        if all(k in j for k in kw):
            return row
    return None


def _dump(df, name, date_str):
    if df is None:
        return
    try:
        df.to_csv(os.path.join(OUTDIR, f"raw_{name}_{date_str.replace('/', '')}.csv"),
                  index=False, encoding="utf-8-sig")
    except Exception:
        pass


def last_trading_day(today=None):
    d = today or dt.date.today()
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def prev_trading_day(s):
    d = dt.datetime.strptime(s, "%Y/%m/%d").date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y/%m/%d")


# ------------------------------------------------- 三大法人 期貨
def fetch_inst_futures(date_str):
    html = _post_html("/cht/3/futContractsDate", {
        "queryType": "2", "goDay": "", "doQuery": "1",
        "dateaddcnt": "", "queryDate": date_str, "commodityId": "",
    })
    tbs = _tables(html, min_rows=5)
    if not tbs:
        raise RuntimeError("三大法人期貨：查無資料（尚未公布或非交易日）")
    df = max(tbs, key=len)

    def grab(prod, who):
        row = _find_row(df, prod, who)
        if row is None:
            return None
        return {"買賣超口數": _num(row.get(7)),
                "多方未平倉": _num(row.get(9)),
                "空方未平倉": _num(row.get(11)),
                "淨未平倉": _num(row.get(13))}

    return ({k: {w: grab(v, w) for w in ("自營商", "投信", "外資")}
             for k, v in PRODUCTS.items()}, df)


# ------------------------------------------------- 三大法人 選擇權
def fetch_inst_options(date_str):
    html = _post_html("/cht/3/callsAndPutsDate", {
        "queryType": "2", "goDay": "", "doQuery": "1",
        "dateaddcnt": "", "queryDate": date_str, "commodityId": "TXO",
    })
    tbs = _tables(html, min_rows=5)
    if not tbs:
        raise RuntimeError("三大法人選擇權：查無資料")
    df = max(tbs, key=len)

    def grab(cp, who):
        row = _find_row(df, cp, who)
        if row is None:
            return None
        return {"買賣超口數": _num(row.get(8)), "淨未平倉": _num(row.get(14))}

    return ({"CALL": {w: grab("買權", w) for w in ("自營商", "投信", "外資")},
             "PUT": {w: grab("賣權", w) for w in ("自營商", "投信", "外資")}}, df)


# ------------------------------------------------- 全市場未平倉量
def fetch_total_oi(date_str, commodity):
    """
    行情表欄位: 0契約 1到期月份 2開 3高 4低 5收 6漲跌 7漲跌% 8成交量
                9結算價 10未沖銷契約量 ...
    必須指定「一般交易時段」(marketCode=0)，盤後時段該欄全為 '-'。
    """
    # 策略 A：每日下載 ZIP（最穩定，無表單參數問題）
    y, m, d = date_str.split("/")
    try:
        r = SESSION.get(f"{BASE}/file/taifex/Dailydownload/DailydownloadCSV/"
                        f"Daily_{y}_{m}_{d}.zip", timeout=45)
        if r.status_code == 200 and r.content[:2] == b"PK":
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            raw = zf.read(zf.namelist()[0])
            for enc in ("big5", "cp950", "utf-8"):
                try:
                    df = pd.read_csv(io.BytesIO(raw), encoding=enc,
                                     on_bad_lines="skip")
                    break
                except Exception:
                    df = None
            if df is not None:
                df.columns = [str(c).strip() for c in df.columns]
                cc = next((c for c in df.columns if c.startswith("契約")), None)
                cm = next((c for c in df.columns if "到期月份" in c), None)
                co = next((c for c in df.columns if "未沖銷" in c), None)
                if cc and cm and co:
                    s = df[df[cc].astype(str).str.strip() == commodity]
                    s = s[~s[cm].astype(str).str.contains("/")]
                    for c in df.columns:
                        if "交易時段" in c:
                            s = s[s[c].astype(str).str.contains("一般")]
                            break
                    tot = pd.to_numeric(s[co], errors="coerce").sum()
                    if tot > 0:
                        return int(tot), "zip"
    except Exception:
        pass

    # 策略 B：每日交易行情查詢頁（一般交易時段）
    variants = [
        {"queryType": "2", "marketCode": "0", "MarketCode": "0", "dateaddcnt": "0",
         "commodity_id": commodity, "commodity_id2": "", "commodity_idt": commodity,
         "queryDate": date_str, "doQuery": "1"},
        {"queryType": "2", "marketCode": "0", "commodity_id": commodity,
         "queryDate": date_str},
    ]
    for v in variants:
        try:
            html = _post_html("/cht/3/futDailyMarketReport", v, retry=2)
            if date_str not in html:
                continue
            for df in _tables(html, min_rows=1):
                if str(df.iloc[0, 0]).strip() != commodity:
                    continue
                tot = 0
                for _, row in df.iterrows():
                    if "/" in str(row.get(1)):        # 排除價差
                        continue
                    n = _num(row.get(10))
                    if n:
                        tot += n
                if tot > 0:
                    return tot, "report"
        except Exception:
            continue
    return None, None


def retail(inst_fut, prod, total_oi):
    """散戶多單=全市場OI-三大法人多單；散戶空單=全市場OI-三大法人空單"""
    if not total_oi:
        return None
    legs = [inst_fut[prod][w] for w in ("自營商", "投信", "外資")]
    if any(x is None for x in legs):
        return None
    il = sum(x["多方未平倉"] or 0 for x in legs)
    isr = sum(x["空方未平倉"] or 0 for x in legs)
    rl, rs = total_oi - il, total_oi - isr
    return {"全市場OI": total_oi, "散戶多單": rl, "散戶空單": rs,
            "散戶淨": rl - rs, "多空比": round((rl - rs) / total_oi * 100, 2)}


# ------------------------------------------------- 現貨三大法人
def fetch_twse_inst(date_str):
    d = date_str.replace("/", "")
    try:
        js = SESSION.get(f"https://www.twse.com.tw/rwd/zh/fund/BFI82U"
                         f"?dayDate={d}&type=day&response=json", timeout=20).json()
        if js.get("stat") != "OK":
            return None
        out = {}
        for r in js["data"]:
            name = r[0].strip()
            v = _num(r[3])
            if v is None:
                continue
            if "外" in name:
                out["外資"] = out.get("外資", 0) + v
            elif "投信" in name:
                out["投信"] = v
            elif "自營" in name:
                out["自營商"] = out.get("自營商", 0) + v
            elif "合計" in name:
                out["合計"] = v
        return out
    except Exception:
        return None


# ------------------------------------------------- P/C Ratio
def fetch_pc_ratio(date_str):
    try:
        html = _post_html("/cht/3/pcRatio",
                          {"queryStartDate": date_str, "queryEndDate": date_str})
        tbs = _tables(html, min_rows=1)
        if not tbs:
            return None
        row = max(tbs, key=len).iloc[-1]
        return {"未平倉PC比": str(row.get(6)), "成交量PC比": str(row.get(3))}
    except Exception:
        return None


# ------------------------------------------------- 組報表
def build_report(date_str):
    rep = {"日期": date_str}

    fut, fut_df = fetch_inst_futures(date_str)
    rep["期貨"] = fut
    _dump(fut_df, "fut", date_str)

    try:
        opt, opt_df = fetch_inst_options(date_str)
        rep["選擇權"] = opt
        _dump(opt_df, "opt", date_str)
    except Exception as e:
        rep["選擇權"] = {"error": str(e)}

    # 前一交易日（算部位增減）
    rep["前日"] = None
    try:
        p = prev_trading_day(date_str)
        pf, _ = fetch_inst_futures(p)
        po = None
        try:
            po, _ = fetch_inst_options(p)
        except Exception:
            pass
        rep["前日"] = {"日期": p, "期貨": pf, "選擇權": po,
                       "現貨": fetch_twse_inst(p)}
    except Exception:
        pass

    # 散戶多空比（小台、微台）
    rep["散戶"] = {}
    for prod in ("MTX", "TMF"):
        oi, src = fetch_total_oi(date_str, prod)
        r = retail(fut, prod, oi)
        if r:
            r["來源"] = src
        rep["散戶"][prod] = r

    rep["現貨"] = fetch_twse_inst(date_str)
    rep["PCRatio"] = fetch_pc_ratio(date_str)
    return rep


def chg(rep, kind, prod, who, field):
    """今日 vs 前日 差額"""
    try:
        a = rep[kind][prod][who][field]
        b = rep["前日"][kind][prod][who][field]
        return a - b
    except Exception:
        return None


# ------------------------------------------------- 終端機輸出
def print_report(rep):
    f = lambda v: "—" if v is None else f"{v:+,}"
    print("\n" + "═" * 54)
    print(f"  台指期籌碼快訊　{rep['日期']}")
    print("═" * 54)

    print("\n★ 散戶多空比")
    for prod, lbl in (("MTX", "小台"), ("TMF", "微台")):
        r = rep["散戶"].get(prod)
        if r:
            print(f"  {lbl}  {r['多空比']:+.2f}%   "
                  f"多單 {r['散戶多單']:,} / 空單 {r['散戶空單']:,}"
                  f"   (全市場OI {r['全市場OI']:,})")
        else:
            print(f"  {lbl}  — 全市場未平倉量取得失敗")

    print("\n★ 三大法人籌碼變化　(括號為前日增減)")
    print(f"  {'':<6}{'現貨(億)':>12}{'台指(口)':>16}{'Call(口)':>16}{'Put(口)':>16}")
    for who in ("外資", "投信", "自營商"):
        sp = (rep.get("現貨") or {}).get(who)
        spp = ((rep.get("前日") or {}).get("現貨") or {}).get(who)
        sp_s = f"{sp/1e8:+,.2f}" if sp is not None else "—"
        tx = rep["期貨"]["TX"].get(who) or {}
        cells = [f"{sp_s:>12}"]
        for kind, prod, key in (("期貨", "TX", None), ("選擇權", "CALL", None),
                                ("選擇權", "PUT", None)):
            src = rep.get(kind)
            v = None
            if isinstance(src, dict) and "error" not in src:
                v = (src.get(prod) or {}).get(who, {}).get("淨未平倉")
            d = chg(rep, kind, prod, who, "淨未平倉")
            cells.append(f"{('—' if v is None else format(v, ',')):>9}"
                         f"({'—' if d is None else format(d, '+,')})".rjust(16))
        print(f"  {who:<6}" + "".join(cells))

    print("\n★ 外資買賣超（當日交易口數淨額）")
    for prod, lbl in (("TX", "台指期"), ("MTX", "小台"), ("TMF", "微台")):
        v = (rep["期貨"][prod].get("外資") or {}).get("買賣超口數")
        print(f"  {lbl:<6} {f(v):>10} 口")
    sp = (rep.get("現貨") or {}).get("外資")
    if sp is not None:
        print(f"  {'現貨':<6} {sp/1e8:>+10,.2f} 億元")

    if rep.get("PCRatio"):
        print(f"\n  P/C Ratio 未平倉比 {rep['PCRatio']['未平倉PC比']}")
    print()


# ------------------------------------------------- HTML 輸出
def write_html(rep, path):
    def sp(v):
        if v is None:
            return "<span class=na>—</span>"
        c = "up" if v > 0 else ("dn" if v < 0 else "")
        return f"<span class={c}>{v:+,}</span>"

    def small(v):
        if v is None:
            return ""
        c = "up" if v > 0 else ("dn" if v < 0 else "")
        return f"<span class='s {c}'>({v:+,})</span>"

    H = []

    # 散戶多空比（頭條）
    H.append("<h2>散戶多空比</h2><div class=cards>")
    for prod, lbl in (("MTX", "小台"), ("TMF", "微台")):
        r = rep["散戶"].get(prod)
        if r:
            c = "up" if r["多空比"] > 0 else "dn"
            H.append(f"<div class=card><div class=lbl>{lbl}</div>"
                     f"<div class='big {c}'>{r['多空比']:+.2f}%</div>"
                     f"<div class=sub>多單 {r['散戶多單']:,}　空單 {r['散戶空單']:,}<br>"
                     f"全市場OI {r['全市場OI']:,}</div></div>")
        else:
            H.append(f"<div class=card><div class=lbl>{lbl}</div>"
                     f"<div class='big na'>—</div>"
                     f"<div class=sub>全市場未平倉量取得失敗</div></div>")
    H.append("</div>")

    # 三大法人籌碼變化
    H.append("<h2>三大法人籌碼變化　<span class=s>口 / 億元，括號為前日增減</span></h2>")
    H.append("<table class=grid><tr><th></th><th>現貨(億)</th><th>台指(口)</th>"
             "<th>Call(口)</th><th>Put(口)</th></tr>")
    for who in ("外資", "投信", "自營商"):
        tds = [f"<th class=who>{who}</th>"]
        v = (rep.get("現貨") or {}).get(who)
        pv = ((rep.get("前日") or {}).get("現貨") or {}).get(who)
        if v is None:
            tds.append("<td class=na>—</td>")
        else:
            c = "up" if v > 0 else "dn"
            d = "" if pv is None else (f"<span class='s {'up' if v-pv>0 else 'dn'}'>"
                                       f"({(v-pv)/1e8:+,.2f})</span>")
            tds.append(f"<td><span class={c}>{v/1e8:+,.2f}</span><br>{d}</td>")
        for kind, prod in (("期貨", "TX"), ("選擇權", "CALL"), ("選擇權", "PUT")):
            src = rep.get(kind)
            val = None
            if isinstance(src, dict) and "error" not in src:
                val = (src.get(prod) or {}).get(who, {}).get("淨未平倉")
            d = chg(rep, kind, prod, who, "淨未平倉")
            tds.append(f"<td>{sp(val)}<br>{small(d)}</td>")
        H.append("<tr>" + "".join(tds) + "</tr>")
    H.append("</table>")

    # 外資買賣超
    H.append("<h2>外資買賣超（當日交易淨額）</h2><table>")
    for prod, lbl in (("TX", "台指期"), ("MTX", "小台"), ("TMF", "微台"),
                      ("TE", "電子期"), ("TF", "金融期")):
        v = (rep["期貨"][prod].get("外資") or {}).get("買賣超口數")
        H.append(f"<tr><td>{lbl}</td><td class=r>{sp(v)} 口</td></tr>")
    v = (rep.get("現貨") or {}).get("外資")
    if v is not None:
        c = "up" if v > 0 else "dn"
        H.append(f"<tr><td>現貨</td><td class=r>"
                 f"<span class={c}>{v/1e8:+,.2f}</span> 億</td></tr>")
    H.append("</table>")

    if rep.get("PCRatio"):
        H.append("<h2>Put / Call Ratio</h2><table>"
                 f"<tr><td>未平倉量比</td><td class=r>{rep['PCRatio']['未平倉PC比']}</td></tr>"
                 f"<tr><td>成交量比</td><td class=r>{rep['PCRatio']['成交量PC比']}</td></tr>"
                 "</table>")

    html = f"""<!doctype html><html lang=zh-Hant><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>台指期籌碼快訊 {rep['日期']}</title>
<style>
:root{{--up:#d32f2f;--dn:#2e7d32}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"Noto Sans TC","PingFang TC",sans-serif;margin:0;
padding:16px;max-width:700px;color:#1a1a1a;background:#fff}}
h1{{font-size:19px;margin:0;border-bottom:3px solid #c62828;padding-bottom:8px}}
.date{{color:#777;font-size:12px;margin:8px 0 20px}}
h2{{font-size:14px;margin:24px 0 8px;color:#c62828}}
.s{{font-size:11px;font-weight:400;color:#888}}
.cards{{display:flex;gap:10px}}
.card{{flex:1;border:1px solid #e8e8e8;border-radius:10px;padding:12px;text-align:center}}
.card .lbl{{font-size:12px;color:#777}}
.card .big{{font-size:26px;font-weight:700;margin:4px 0;
font-variant-numeric:tabular-nums}}
.card .sub{{font-size:11px;color:#888;line-height:1.5}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border-bottom:1px solid #eee;padding:9px 5px;text-align:center}}
table.grid th{{background:#f5f5f5;font-size:12px;color:#555}}
th.who{{background:#fafafa;width:56px}}
td:first-child{{text-align:left}}
.r{{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}}
.up{{color:var(--up)}} .dn{{color:var(--dn)}} .na{{color:#bbb}}
footer{{margin-top:26px;color:#999;font-size:11px;line-height:1.6}}
</style>
<h1>台指期籌碼快訊</h1>
<div class=date>資料日期 {rep['日期']}　·　更新 {dt.datetime.now():%m-%d %H:%M}</div>
{''.join(H)}
<footer>散戶多單=全市場OI−三大法人多單；散戶空單=全市場OI−三大法人空單；
多空比=(多單−空單)/全市場OI×100%。<br>
資料來源：臺灣期貨交易所、臺灣證券交易所。僅供參考，不構成投資建議。</footer></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)


# ------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--date")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--html", action="store_true")
    a = ap.parse_args()

    date_str = a.date or last_trading_day().strftime("%Y/%m/%d")

    if a.watch:
        t = dt.datetime.combine(dt.date.today(), dt.time(15, 0))
        if dt.datetime.now() < t:
            w = (t - dt.datetime.now()).total_seconds()
            print(f"等待至 15:00（{w/60:.0f} 分鐘）…")
            time.sleep(w)
        for _ in range(30):
            try:
                rep = build_report(date_str)
                break
            except Exception as e:
                print(f"[{dt.datetime.now():%H:%M:%S}] 尚未公布：{e}")
                time.sleep(60)
        else:
            sys.exit("逾時")
    else:
        rep = build_report(date_str)

    print_report(rep)
    tag = date_str.replace("/", "")
    with open(os.path.join(OUTDIR, f"chips_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2, default=str)
    if a.html:
        write_html(rep, os.path.join(OUTDIR, f"chips_{tag}.html"))
        print("→ HTML 已產生")


if __name__ == "__main__":
    main()
