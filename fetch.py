"""每天抓最新季報與成交量，產生網頁用的 data/result.js。

  python fetch.py          財報季才更新（GitHub 每天自動跑這個）
  python fetch.py --force  不管日期，立刻更新一次

資料來源：公開資訊觀測站（綜合損益表）、證交所、櫃買中心（每日行情）。
注意：觀測站的損益表是「今年累計」，單季要用本季累計減上季累計。
"""
import io
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
VOLUME = DATA / "volume"
FINANCE = DATA / "finance"
TAIPEI = timezone(timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
MOPS_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t163sb04"
MOPS_PROFILE_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t05st03"
NEWS_URL = "https://news.google.com/rss/search"
NEWS_WORDS = re.compile(r"法說|財報|毛利|EPS|獲利|季|營收|每股|賺")
NEWS_BLOGS = re.compile(r"網誌|玩股網|同學會|部落格|理財估測站|PTT|Mobile01")
NEWS_REFRESH_DAYS = 3
PROFILE_LIMIT = 150  # 每次最多查幾家，避免跑太久；沒查到的下次再補
VOLUME_DAYS = 20  # 「月平均」＝最近 20 個交易日
MIN_VOLUME_DAYS = 15  # 少於這個天數就視為抓取失敗，不更新網頁

# 財報季的抓取期間 (起月, 起日, 迄月, 迄日)；法定期限是 3/31、5/15、8/14、11/14
SEASONS = [(3, 1, 4, 5), (4, 15, 5, 20), (7, 15, 8, 20), (10, 15, 11, 20)]


def in_season(d):
    return any((m1, d1) <= (d.month, d.day) <= (m2, d2) for m1, d1, m2, d2 in SEASONS)


def current_quarter(d):
    """今天應該看哪一季的財報，回傳 (西元年, 季)。"""
    md = (d.month, d.day)
    if md < (3, 1):
        return d.year - 1, 3
    if md < (4, 15):
        return d.year - 1, 4
    if md < (7, 15):
        return d.year, 1
    if md < (10, 15):
        return d.year, 2
    return d.year, 3


def previous_quarter(year, quarter):
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def number(value):
    value = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(value) else float(value)


def fetch_mops(year, quarter, market):
    """抓一季一個市場的累計損益表，回傳 {代號: {name, rev, gp, op}}。金融業沒有毛利，不在這張表。"""
    form = dict(encodeURIComponent=1, step=1, firstin=1, off=1, isQuery="Y",
                TYPEK=market, year=year - 1911, season=f"{quarter:02d}")
    for attempt in range(6):  # 觀測站偶爾完全不回應，等久沒用，早點放棄重來
        time.sleep(5 + attempt * 10)
        try:
            response = requests.post(MOPS_URL, data=form, headers=HEADERS, timeout=45)
            response.encoding = "utf-8"
            html = response.text
        except requests.RequestException as error:
            print(f"  觀測站連線失敗，重試：{error}")
            continue
        if "公司名稱" in html:
            break
        if "查無" in html:
            return {}
        print(f"  觀測站沒有回傳報表（可能查太快），重試：{html[:80]!r}")
    else:
        raise RuntimeError(f"觀測站抓不到 {year}Q{quarter} {market}")

    companies = {}
    for table in pd.read_html(io.StringIO(html)):
        table.columns = [re.sub(r"\s", "", str(c)) for c in table.columns]
        if "公司代號" not in table.columns or "營業毛利（毛損）淨額" not in table.columns:
            continue
        for row in table.to_dict("records"):
            code = str(row["公司代號"]).strip()
            if re.fullmatch(r"\d{4}[0-9A-Z]?", code):
                companies[code] = {
                    "name": str(row["公司名稱"]).strip(),
                    "rev": number(row["營業收入"]),
                    "gp": number(row["營業毛利（毛損）淨額"]),
                    "op": number(row["營業利益（損失）"]),
                    "ex": number(row["營業費用"]),
                }
    return companies


def fetch_quarter(year, quarter):
    """上市＋上櫃合併，公司轉上市時才接得起來。"""
    merged = {}
    for market, label in (("sii", "上市"), ("otc", "上櫃")):
        rows = fetch_mops(year, quarter, market)
        print(f"  {year}Q{quarter} {label} {len(rows)} 家")
        for code, row in rows.items():
            merged[code] = {**row, "market": label}
    return merged


def load_quarter(year, quarter, is_current):
    """每次都重抓（才接得到晚交的公司）並存檔；舊季度抓不到時改用存檔，本季抓不到就算失敗。"""
    FINANCE.mkdir(parents=True, exist_ok=True)
    path = FINANCE / f"{year}Q{quarter}.json"
    try:
        merged = fetch_quarter(year, quarter)
    except RuntimeError:
        if is_current or not path.exists():
            raise
        print(f"  警告：{year}Q{quarter} 抓不到，改用上次存檔")
        return json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(merged, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return merged


def single_quarter(cumulative, year, quarter, code):
    """把累計數字換算成單季。缺資料時回傳 None。"""
    now = cumulative[(year, quarter)].get(code)
    if now is None:
        return None
    if quarter == 1:
        return {k: now.get(k) for k in ("rev", "gp", "op", "ex")}
    before = cumulative[(year, quarter - 1)].get(code)
    if before is None:
        return None
    return {k: None if now.get(k) is None or before.get(k) is None else now[k] - before[k]
            for k in ("rev", "gp", "op", "ex")}


def margin(part, revenue):
    if part is None or not revenue or revenue <= 0:
        return None
    return round(part / revenue * 100, 2)


def billions(value):
    return f"{value / 1e5:,.2f} 億"


def highlight(now, before):
    """用財報數字說明獲利為什麼變好。只講數字看得出來的事，實際原因要看新聞。"""
    gm, gm_prev = margin(now["gp"], now["rev"]), margin(before["gp"], before["rev"])
    om, om_prev = margin(now["op"], now["rev"]), margin(before["op"], before["rev"])
    ex, ex_prev = margin(now["ex"], now["rev"]), margin(before["ex"], before["rev"])
    if None in (gm, gm_prev, om, om_prev) or om <= om_prev:
        return None
    if before["op"] < 0 <= now["op"]:
        profit = f"本業由虧轉盈（{billions(before['op'])} → {billions(now['op'])}）"
    elif now["op"] < 0:
        profit = f"本業仍虧損但縮小（{billions(before['op'])} → {billions(now['op'])}）"
    elif before["op"] == 0:
        profit = f"營業利益 {billions(now['op'])}"
    else:
        profit = f"營業利益 {billions(before['op'])} → {billions(now['op'])}（{(now['op'] / before['op'] - 1) * 100:+.0f}%）"
    text = f"{profit}，毛利率 {gm - gm_prev:+.2f} 個百分點"
    if ex is None or ex_prev is None:
        return text + "。"
    gross_gain, expense_gain = gm - gm_prev, ex_prev - ex
    text += f"，費用率 {ex_prev:.1f}% → {ex:.1f}%"
    if gross_gain > 0 and (expense_gain <= 0 or gross_gain >= expense_gain * 2):
        return text + "。改善主要來自毛利率提升。"
    if expense_gain > 0 and (gross_gain <= 0 or expense_gain >= gross_gain * 2):
        return text + "。改善主要來自費用被營收攤薄。"
    return text + "。毛利率提升和費用攤薄都有貢獻。"


def get_json(url, tries):
    for attempt in range(tries):
        try:
            return requests.get(url, headers=HEADERS, timeout=30).json()
        except (requests.RequestException, ValueError) as error:
            print(f"  {url[:50]}… 失敗：{type(error).__name__}")
            time.sleep(5 + attempt * 10)
    return None


def lots(text):
    return round(int(str(text).replace(",", "")) / 1000)


def fetch_otc_day(d):
    """上櫃行情。休市日沒資料，回傳 None。要先查這個，因為證交所遇到休市日會卡住不回應。"""
    url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={d:%Y/%m/%d}&id=&response=json"
    payload = get_json(url, 4)
    if payload is None:
        print(f"  警告：{d} 櫃買中心抓不到，略過這天")
        return None
    table = (payload.get("tables") or [{}])[0]
    if not table.get("data") or table.get("date") != f"{d.year - 1911}/{d:%m/%d}":
        return None
    column = [f.strip() for f in table["fields"]].index("成交股數")
    # 這張表含零股（和證交所的算法一致），但也含上萬檔權證，只留股票代號
    return {row[0]: lots(row[column]) for row in table["data"] if re.fullmatch(r"\d{4}[0-9A-Z]?", row[0])}


def fetch_listed_day(d):
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={d:%Y%m%d}&type=ALLBUT0999"
    payload = get_json(url, 4)
    for table in (payload or {}).get("tables", []):
        if "每日收盤行情" in (table.get("title") or "") and table.get("data"):
            column = table["fields"].index("成交股數")
            return {row[0]: lots(row[column]) for row in table["data"]}
    return None


def load_volumes(today):
    """回傳最近 20 個交易日的 {日期: {代號: 張數}}，已抓過的日子直接讀檔。"""
    VOLUME.mkdir(parents=True, exist_ok=True)
    days = {}
    d = today
    while len(days) < VOLUME_DAYS and (today - d).days < 45:
        if d.weekday() < 5:
            path = VOLUME / f"{d:%Y%m%d}.json"
            if path.exists():
                days[d] = json.loads(path.read_text(encoding="utf-8"))
            else:
                otc = fetch_otc_day(d)
                if otc is not None:
                    time.sleep(3)
                    listed = fetch_listed_day(d)
                    if listed:
                        days[d] = {**listed, **otc}
                        path.write_text(json.dumps(days[d], separators=(",", ":")), encoding="utf-8")
                        print(f"  成交量 {d} {len(days[d])} 檔")
                    else:
                        print(f"  警告：{d} 證交所抓不到，略過這天")
                time.sleep(3)
        d -= timedelta(days=1)
    if len(days) < MIN_VOLUME_DAYS:
        raise RuntimeError(f"成交量只抓到 {len(days)} 天，不足 {MIN_VOLUME_DAYS} 天")
    keep = {f"{d:%Y%m%d}.json" for d in days}
    for path in VOLUME.glob("*.json"):
        if path.name not in keep:
            path.unlink()
    return days


def average_volumes(days):
    totals, counts = {}, {}
    for stocks in days.values():
        for code, volume in stocks.items():
            totals[code] = totals.get(code, 0) + volume
            counts[code] = counts.get(code, 0) + 1
    return {code: round(totals[code] / counts[code]) for code in totals}


def fetch_business(code):
    """觀測站公司基本資料裡的「主要經營業務」，抓不到回傳 None。"""
    form = dict(encodeURIComponent=1, step=1, firstin=1, off=1, keyword4="", code1="", TYPEK2="", checkbtn="",
                queryName="co_id", inpuType="co_id", TYPEK="all", co_id=code)
    try:
        response = requests.post(MOPS_PROFILE_URL, data=form, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    response.encoding = "utf-8"
    match = re.search(r"主要經營業務.*?<td[^>]*>(.*?)</td>", response.text, re.S)
    if not match:
        return None
    lines = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>|&nbsp;?", " ", line)).strip()
             for line in re.split(r"<br\s*/?>|\n", match.group(1))]
    return "；".join(line for line in lines if line) or None


def fetch_news(code, name):
    """Google 新聞裡最近 45 天和這家公司財報有關的標題，回傳最新 3 則。"""
    short = re.sub(r"[*＊]|-創$", "", name)
    query = f"{short} {code} (財報 OR 法說 OR 毛利率 OR 第三季 OR 獲利) when:45d"
    params = dict(q=query, hl="zh-TW", gl="TW", ceid="TW:zh-Hant")
    try:
        response = requests.get(NEWS_URL, params=params, headers=HEADERS, timeout=30)
        items = ET.fromstring(response.content).findall(".//item")
    except (requests.RequestException, ET.ParseError):
        return None
    news = {}
    for item in items:
        title, _, source = item.findtext("title", "").rpartition(" - ")
        title = title.split(" - ")[0] if " - " in title and short not in title.split(" - ")[-1] else title
        if short in title and NEWS_WORDS.search(title):
            published = parsedate_to_datetime(item.findtext("pubDate")).astimezone(TAIPEI)
            news.setdefault(title[:18], {"title": title, "source": source, "url": item.findtext("link"), "date": f"{published:%Y-%m-%d}"})
    # 媒體報導優先，部落格和討論區排後面
    newest_first = sorted(news.values(), key=lambda n: n["date"], reverse=True)
    return sorted(newest_first, key=lambda n: bool(NEWS_BLOGS.search(n["source"] + n["title"])))[:3]


def update_profiles(rows, today):
    """幫財報三項都變好的公司補上業務介紹和新聞，成交量大的優先。"""
    path = DATA / "profiles.json"
    profiles = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    stale = (today - timedelta(days=NEWS_REFRESH_DAYS)).isoformat()
    good = [r for r in rows if r["gm"] is not None and r["gmPrev"] is not None and r["om"] is not None
            and r["omPrev"] is not None and r["gm"] > r["gmPrev"] and r["om"] > r["omPrev"] and r["rev"] > r["revPrev"]]
    good.sort(key=lambda r: r["vol"] or 0, reverse=True)
    checked = 0
    for row in good:
        profile = profiles.setdefault(row["code"], {})
        if profile.get("biz") and profile.get("newsDate", "") > stale:
            continue
        if checked >= PROFILE_LIMIT:
            break
        checked += 1
        if not profile.get("biz"):
            time.sleep(3)
            profile["biz"] = fetch_business(row["code"])
        if profile.get("newsDate", "") <= stale:
            time.sleep(1)
            news = fetch_news(row["code"], row["name"])
            if news is not None:
                profile.update(news=news, newsDate=today.isoformat())
    path.write_text(json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    missing = sum(not profiles.get(r["code"], {}).get("biz") for r in good)
    print(f"  公司介紹：查了 {checked} 家，缺業務說明 {missing} 家")
    return profiles


def update_first_seen(key, codes, today):
    """記下每家公司第一次出現的日期，網頁才知道誰是今天新公布。第一次執行的那批不算新。"""
    path = DATA / "first_seen.json"
    seen = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    baseline = key not in seen
    quarter_seen = seen.setdefault(key, {})
    for code in codes:
        quarter_seen.setdefault(code, None if baseline else today.isoformat())
    path.write_text(json.dumps(seen, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return quarter_seen


def build(today):
    year, quarter = current_quarter(today)
    prev_year, prev_quarter = previous_quarter(year, quarter)
    print(f"本季 {year}Q{quarter}，比較 {prev_year}Q{prev_quarter}")

    needed = {(year, quarter), (prev_year, prev_quarter)}
    if quarter > 1:
        needed.add((year, quarter - 1))
    if prev_quarter > 1:
        needed.add((prev_year, prev_quarter - 1))
    cumulative = {yq: load_quarter(*yq, yq == (year, quarter)) for yq in sorted(needed)}
    if not cumulative[(prev_year, prev_quarter)]:
        raise RuntimeError("上一季財報是空的，不更新網頁")

    days = load_volumes(today)
    volumes = average_volumes(days)

    filed = cumulative[(year, quarter)]
    seen = update_first_seen(f"{year}Q{quarter}", filed, today)
    rows = []
    for code, company in filed.items():
        now = single_quarter(cumulative, year, quarter, code)
        before = single_quarter(cumulative, prev_year, prev_quarter, code)
        if now is None or before is None:
            continue
        rows.append({
            "code": code, "name": company["name"], "market": company["market"],
            "rev": now["rev"], "revPrev": before["rev"],
            "gm": margin(now["gp"], now["rev"]), "gmPrev": margin(before["gp"], before["rev"]),
            "om": margin(now["op"], now["rev"]), "omPrev": margin(before["op"], before["rev"]),
            "vol": volumes.get(code), "filed": seen.get(code), "why": highlight(now, before),
        })

    profiles = update_profiles(rows, today)
    for row in rows:
        profile = profiles.get(row["code"], {})
        row["biz"], row["news"] = profile.get("biz"), profile.get("news")

    result = {
        "updated": datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "quarter": f"{year} 年第 {quarter} 季",
        "prevQuarter": f"{prev_year} 年第 {prev_quarter} 季",
        "filed": len(filed),
        "total": len(cumulative[(prev_year, prev_quarter)]),
        "volumeFrom": min(days).isoformat(), "volumeTo": max(days).isoformat(), "volumeDays": len(days),
        "rows": rows,
    }
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    (DATA / "result.js").write_text(f"window.DATA = {text};\n", encoding="utf-8")
    print(f"完成：已公布 {len(filed)} / {result['total']} 家，可比較 {len(rows)} 家")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    today = datetime.now(TAIPEI).date()
    if in_season(today) or "--force" in sys.argv:
        build(today)
    else:
        # GitHub 會停掉 60 天沒動靜的排程，非財報季每月寫一次檔案讓它保持運作
        (DATA / "heartbeat.txt").write_text(f"{today:%Y-%m}\n", encoding="utf-8")
        print("不在財報季，略過")


if __name__ == "__main__":
    main()
