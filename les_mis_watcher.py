#!/usr/bin/env python3
"""
《Les Misérables》Théâtre du Châtelet 票務監控
================================================

監控模式（設定 "mode" 或環境變數 LESMIS_MODE）：

  youth（預設）    只通知「Jeune - de 30 ans」青年票
                   （官方售票 + 官方轉售 Bourse 中的青年票）
  youth_or_under30 青年票，或任何 < €30 的票（€30 整不算，含轉售）

演出期間 2026/11/11 – 2027/01/10，不挑日期與時段。
不符合資格的票種（Enfant -15 ans、Carte Châtelet 會員價、團體…）會被排除。

只用 Python 標準庫；可在 macOS 內建 /usr/bin/python3 或 GitHub Actions 上執行。
Email 設定可放在 config.json，或用環境變數 SMTP_USER / SMTP_APP_PASSWORD / NOTIFY_TO。

用法：
  python3 les_mis_watcher.py              # 檢查一次，有「新釋出」的票就寄信
  python3 les_mis_watcher.py --dry-run    # 只印出結果，不寄信、不更新狀態
  python3 les_mis_watcher.py --test-email # 寄一封測試信
"""

import argparse
import concurrent.futures
import datetime as dt
import gzip
import html
import http.cookiejar
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

PRODUCT_ID = "10229516301326"  # Les Misérables 在 Châtelet 售票系統的 productId
MAIN = "https://billetterie.chatelet.com"
BOURSE = "https://billetterie-bourse.chatelet.com"

FIRST_DAY = dt.date(2026, 11, 11)
LAST_DAY = dt.date(2027, 1, 10)

PRICE_LIMIT_CENTS = 30000  # 價格以 1/1000 歐元表示：30000 = €30.00；必須「嚴格小於」

# 青年票關鍵字
YOUTH_KEYWORDS = ("jeune", "- de 30", "-30", "moins de 30", "under 30")

# 不符合資格（22 歲、無 Carte Châtelet 會員）的票種關鍵字
INELIGIBLE_KEYWORDS = (
    "enfant", "- de 15", "-15", "carte châtelet", "carte chatelet", "adh2",
    "adhérent", "adherent", "groupe", "scolaire", "senior", "+ de 65",
    "pmr", "accompagnateur", "invitation", "professionnel", "abonn",
    "demandeur d'emploi", "minima sociaux",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("LESMIS_CONFIG", os.path.join(BASE_DIR, "config.json"))
STATE_FILE = os.environ.get("LESMIS_STATE", os.path.join(BASE_DIR, "state.json"))

# 同一張票「消失後又出現」時，至少間隔多久才再次通知（避免購物車暫留造成洗信）
RENOTIFY_COOLDOWN_MIN = 60

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
REQUEST_PAUSE_SEC = 0.4  # 每個請求之間稍作停頓，對售票網站友善
PARALLEL_REQUESTS = 4    # 同時查詢的場次數（太大會對售票網站造成負擔）


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Http:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, url, headers=None, retries=3):
        hdrs = {
            "User-Agent": UA,
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip",
        }
        hdrs.update(headers or {})
        last_err = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with self.opener.open(req, timeout=30) as r:
                    data = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    final_url = r.geturl()
                time.sleep(REQUEST_PAUSE_SEC)
                if "queue-it" in final_url or "queue." in urllib.parse.urlparse(final_url).netloc:
                    raise RuntimeError("售票網站目前啟用排隊系統（queue），稍後再試")
                return data.decode("utf-8", errors="replace")
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"GET 失敗 {url}: {last_err}")


def clean(s):
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def eur(cents):
    if cents is None:
        return "?"
    return f"€{cents / 1000:.2f}".replace(".00", "")


# --------------------------------------------------------------------------
# 解析：場次列表
# --------------------------------------------------------------------------

def list_performances(http, base):
    """回傳 [{perf_id, date, time, status, html}]；一次請求即可取得整檔所有場次。"""
    seen = {}
    for year, month in ((2026, 11), (2026, 12), (2027, 1)):
        url = (f"{base}/ajax/event/date/performances?year={year}&month={month}"
               f"&productId={PRODUCT_ID}&advantageId=&ppid=&crossSellId=&reservationIdx=&lang=fr")
        page = http.get(url)
        for li in re.findall(r"<li data-venue-id.*?</li>", page, re.S):
            m = re.search(r'id="(\d{8,})"', li)
            if not m:
                continue
            pid = m.group(1)
            if pid in seen:
                continue
            d = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', li)
            t = re.search(r'class="time">\s*([^<]+?)\s*<', li)
            cls = re.search(r'class="([^"]*performance_EVENT[^"]*)"', li, re.S)
            classes = cls.group(1).split() if cls else []
            status = next((c for c in classes if c in (
                "available", "limited", "sold_out", "soldout", "unavailable", "good")), "?")
            seen[pid] = {
                "perf_id": pid,
                "date": d.group(1) if d else "?",
                "time": t.group(1) if t else "?",
                "status": status,
                "html": li,
            }
        # 第一個請求通常已含全部場次；若已涵蓋到最後一天就不用再查
        if any(p["date"] >= LAST_DAY.isoformat() for p in seen.values()):
            break
    perfs = [p for p in seen.values()
             if p["date"] == "?" or FIRST_DAY.isoformat() <= p["date"] <= LAST_DAY.isoformat()]
    return sorted(perfs, key=lambda p: (p["date"], p["time"]))


# --------------------------------------------------------------------------
# 解析：官方正常售票價格表
# --------------------------------------------------------------------------

def parse_price_table(page):
    """解析 /selection/event/seat?table=1 頁面，回傳每一列 (類別, 票種, 價格, 是否可買)。"""
    rows = []
    current_cat = None
    for chunk in re.split(r'(?=<tr class="v2-seatcat_)', page)[1:]:
        tr = chunk.split("</tr>")[0]
        cat_m = re.search(r'<th class="category[^"]*"[^>]*>(.*?)</th>', tr, re.S)
        if cat_m:
            name = clean(cat_m.group(1))
            name = re.split(r"\s+Préférence|\s+Automatique|\s+Epuisé", name)[0].strip()
            current_cat = name or current_cat
        tar_m = re.search(r'class="audience-subcat-name">(.*?)</span>\s*(?:<div|</th>)', tr, re.S)
        tariff = clean(tar_m.group(1)) if tar_m else "?"
        price_m = re.search(r'class="unit_price.*?data-amount="(\d+)"', tr, re.S)
        price = int(price_m.group(1)) if price_m else None
        qty_m = re.search(r'<td class="quantity[^"]*">(.*?)</td>', tr, re.S)
        qty = qty_m.group(1) if qty_m else ""
        bookable = ("<select" in qty) and ("buy_unavailable" not in qty)
        max_q = 0
        if bookable:
            opts = [int(x) for x in re.findall(r'<option value="(\d+)"', qty)]
            max_q = max(opts) if opts else 0
            bookable = max_q > 0
        rows.append({
            "category": current_cat or "?",
            "tariff": tariff,
            "price": price,
            "bookable": bookable,
            "max_qty": max_q,
        })
    # 整個類別售罄時（如「Catégorie 4 - Epuisé」）頁面不會出現 <tr>，自然不會列入
    return rows


def is_youth(tariff):
    t = tariff.lower()
    return any(k in t for k in YOUTH_KEYWORDS)


def classify(tariff, price, youth_only):
    """依購票標準判斷。回傳 (是否符合, 原因, 是否需要確認資格)。"""
    t = tariff.lower()
    if is_youth(tariff):
        return True, "青年票 Jeune -30", False
    if youth_only:
        return False, "", False
    if any(k in t for k in INELIGIBLE_KEYWORDS):
        return False, "不符資格票種", False
    if price is not None and price < PRICE_LIMIT_CENTS:
        known = t in ("plein tarif", "tarif plein", "?")
        return True, "低於 €30", not known
    return False, "", False


def fetch_all(http, urls, log):
    """平行抓取多個網址，回傳 {url: html 或 None}。"""
    out = {}
    with concurrent.futures.ThreadPoolExecutor(PARALLEL_REQUESTS) as ex:
        futs = {ex.submit(http.get, u): u for u in urls}
        for f in concurrent.futures.as_completed(futs):
            u = futs[f]
            try:
                out[u] = f.result()
            except Exception as e:  # noqa: BLE001
                log(f"  ! 讀取失敗 {u}：{e}")
                out[u] = None
    return out


def check_main(http, perfs, log, youth_only):
    matches = []
    perfs = [p for p in perfs if p["status"] not in ("sold_out", "soldout", "unavailable")]
    url_of = {p["perf_id"]: f"{MAIN}/selection/event/seat?perfId={p['perf_id']}"
                            f"&table=1&productId={PRODUCT_ID}&lang=fr" for p in perfs}
    pages = fetch_all(http, url_of.values(), log)
    for p in perfs:
        url = url_of[p["perf_id"]]
        page = pages.get(url)
        if page is None:
            continue
        rows = parse_price_table(page)
        if not rows:
            log(f"  ? {p['date']} {p['time']} 找不到價格表（可能售罄或頁面改版）")
            continue
        for r in rows:
            if not r["bookable"]:
                continue
            ok, reason, verify = classify(r["tariff"], r["price"], youth_only)
            if not ok:
                continue
            matches.append({
                "key": f"main|{p['perf_id']}|{r['category']}|{r['tariff']}|{r['price']}",
                "channel": "官方售票",
                "date": p["date"], "time": p["time"],
                "category": r["category"], "tariff": r["tariff"],
                "price": r["price"], "qty": r["max_qty"],
                "reason": reason, "verify": verify,
                "url": url,
            })
    return matches


# --------------------------------------------------------------------------
# 解析：官方轉售 Bourse aux billets
# --------------------------------------------------------------------------

def check_bourse_youth(http, log):
    """轉售票會保留原票種名稱；找出標示為「Jeune - de 30 ans」的轉售票。"""
    matches = []
    perfs = [p for p in list_performances(http, BOURSE)
             if re.search(r'data-available="[1-9]', p["html"])]
    url_of = {p["perf_id"]: f"{BOURSE}/selection/resale/item?performanceId={p['perf_id']}"
                            f"&productId={PRODUCT_ID}&lang=fr" for p in perfs}
    pages = fetch_all(http, url_of.values(), log)
    for p in perfs:
        url = url_of[p["perf_id"]]
        page = pages.get(url)
        if not page:
            continue
        area = "?"
        for part in re.split(r'(?=<span class="seat_info_block_code)|(?=<span class="seat_info_block_available_tariffs)', page):
            if part.startswith('<span class="seat_info_block_code'):
                m = re.search(r'</span>([^<]+)</span>', part)
                area = clean(m.group(1)) if m else area
            elif part.startswith('<span class="seat_info_block_available_tariffs'):
                tariff = clean(part.split("</span>")[0])
                if not is_youth(tariff):
                    continue
                price = re.search(r'data-amount="(\d+)"', part)
                matches.append({
                    "key": f"bourse|{p['perf_id']}|{area}|{tariff}",
                    "channel": "官方轉售 Bourse",
                    "date": p["date"], "time": p["time"],
                    "category": area, "tariff": tariff,
                    "price": int(price.group(1)) if price else None, "qty": 0,
                    "reason": "轉售青年票", "verify": False,
                    "url": url,
                })
    return matches


def check_bourse(http, log):
    matches = []
    perfs = list_performances(http, BOURSE)
    for p in perfs:
        li = p["html"]
        avail = re.search(r'data-available="(\d+)"', li)
        if not avail or int(avail.group(1)) == 0:
            continue
        minp = re.search(r'data-min-price="(\d+)"', li)
        if not minp or int(minp.group(1)) >= PRICE_LIMIT_CENTS:
            continue
        url = f"{BOURSE}/selection/resale/item?performanceId={p['perf_id']}&productId={PRODUCT_ID}&lang=fr"
        found = []
        try:
            page = http.get(url)
            for blk in page.split('<div class="seat-info-category-legend">')[1:]:
                amt = re.search(r'data-amount="(\d+)"', blk)
                name = re.search(r'<span class="name">(.*?)</span>', blk, re.S)
                if not amt or int(amt.group(1)) >= PRICE_LIMIT_CENTS:
                    continue
                item = (clean(name.group(1)) if name else "?", int(amt.group(1)))
                if item not in found:  # 圖例在頁面中可能出現兩次
                    found.append(item)
        except Exception as e:  # noqa: BLE001
            log(f"  ! 轉售 {p['date']} {p['time']} 細節讀取失敗：{e}")
        if not found:  # 細節頁讀不到時，至少用列表上的最低價通知
            cats = clean(" ".join(re.findall(r'<span class="name">(.*?)</span>', li)))
            found = [(cats or "?", int(minp.group(1)))]
        for name, price in found:
            matches.append({
                "key": f"bourse|{p['perf_id']}|{name}|{price}",
                "channel": "官方轉售 Bourse",
                "date": p["date"], "time": p["time"],
                "category": name, "tariff": "轉售（Plein tarif）",
                "price": price, "qty": 0,
                "reason": "轉售低於 €30", "verify": False,
                "url": url,
            })
    return matches


# --------------------------------------------------------------------------
# 狀態與 Email
# --------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def fmt_date(d):
    try:
        x = dt.date.fromisoformat(d)
        return f"{x.year}/{x.month:02d}/{x.day:02d}（{WEEKDAYS[x.weekday()]}）"
    except ValueError:
        return d


def row_html(m, new_keys):
    notes = []
    if "sans visibilit" in m["category"].lower() or "no visibility" in m["category"].lower():
        notes.append("⚠️ 無舞台視野")
    elif "visibilité réduite" in m["category"].lower() or "restricted" in m["category"].lower():
        notes.append("視野受限")
    if m["verify"]:
        notes.append("⚠️ 請確認票種資格")
    badge = '<b style="color:#d6005c">NEW</b> ' if m["key"] in new_keys else ""
    return (
        "<tr>"
        f"<td>{badge}{fmt_date(m['date'])} {html.escape(m['time'])}</td>"
        f"<td>{html.escape(m['channel'])}</td>"
        f"<td>{html.escape(m['category'])}</td>"
        f"<td>{html.escape(m['tariff'])}</td>"
        f"<td><b>{eur(m['price'])}</b></td>"
        f"<td>{m['qty'] if m['qty'] else ''}</td>"
        f"<td>{' / '.join(notes)}</td>"
        f"<td><a href=\"{html.escape(m['url'])}\">立即購買</a></td>"
        "</tr>"
    )


def build_email(new, all_matches, youth_only=False):
    new_keys = {m["key"] for m in new}
    youth = sum(1 for m in new if "Jeune" in m["reason"] or "青年" in m["reason"])
    cheapest = min((m["price"] for m in new if m["price"] is not None), default=None)
    if youth_only:
        subject = f"🎭 悲慘世界青年票釋出！{len(new)} 筆（{eur(cheapest)}）快去買"
    else:
        subject = (f"🎭 悲慘世界有票！{len(new)} 筆新釋出"
                   f"{f'（含 {youth} 筆青年票）' if youth else ''}，最低 {eur(cheapest)}")
    criteria = ("<b>Jeune - de 30 ans 青年票</b>（官方售票與官方轉售）" if youth_only else
                "<b>Jeune - de 30 ans 青年票</b> 或 <b>任何低於 €30 的票</b>（含官方轉售）")
    style = "border-collapse:collapse;font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:14px"
    th = "".join(f"<th style='text-align:left;border-bottom:2px solid #333;padding:6px'>{h}</th>"
                 for h in ("場次", "渠道", "座位類別", "票種", "價格", "可選張數", "備註", "連結"))
    rows_new = "".join(row_html(m, new_keys) for m in new)
    others = [m for m in all_matches if m["key"] not in new_keys]
    rows_old = "".join(row_html(m, new_keys) for m in others)
    body = f"""
<div style="font-family:-apple-system,Helvetica,Arial,sans-serif">
<h2>《Les Misérables》Théâtre du Châtelet — 符合你條件的票</h2>
<p>條件：{criteria}。青年票入場時需出示年齡證件。手腳要快，青年票通常很快被搶完。</p>
<h3>🆕 新釋出（{len(new)} 筆）</h3>
<table style="{style}" cellpadding="6"><tr>{th}</tr>{rows_new}</table>
{"<h3>目前仍可購買的其他符合條件票（" + str(len(others)) + " 筆）</h3><table style='" + style + "' cellpadding='6'><tr>" + th + "</tr>" + rows_old + "</table>" if others else ""}
<p style="color:#666;font-size:12px">
官方售票：<a href="{MAIN}/selection/event/date?productId={PRODUCT_ID}">billetterie.chatelet.com</a> ·
官方轉售：<a href="{BOURSE}/selection/event/date?productId={PRODUCT_ID}">billetterie-bourse.chatelet.com</a><br>
提醒：開演前約 15 分鐘的現場 last-minute 青年票無法線上監控，需親自到售票窗口詢問。<br>
檢查時間：{paris_now().strftime('%Y-%m-%d %H:%M:%S')}（巴黎時間）（由 les_mis_watcher 自動發送）
</p></div>"""
    return subject, body


def send_email(cfg, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["notify_to"]
    msg.attach(MIMEText(clean(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"),
                          int(cfg.get("smtp_port", 465)), context=ctx, timeout=30) as s:
        s.login(cfg["smtp_user"], cfg["smtp_app_password"].replace(" ", ""))
        s.sendmail(cfg["smtp_user"], [a.strip() for a in cfg["notify_to"].split(",")],
                   msg.as_string())


# --------------------------------------------------------------------------
# 主程式
# --------------------------------------------------------------------------

def paris_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Paris")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return dt.datetime.now()


def load_config():
    """config.json 為主，環境變數（GitHub Actions Secrets）可覆蓋。"""
    cfg = load_json(CONFIG_FILE, {})
    for key, env in (("smtp_user", "SMTP_USER"), ("smtp_app_password", "SMTP_APP_PASSWORD"),
                     ("notify_to", "NOTIFY_TO"), ("mode", "LESMIS_MODE")):
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()
    if not cfg.get("notify_to") and cfg.get("smtp_user"):
        cfg["notify_to"] = cfg["smtp_user"]
    cfg.setdefault("mode", "youth")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Les Misérables @ Châtelet 票務監控")
    ap.add_argument("--dry-run", action="store_true", help="只印結果，不寄信、不更新狀態")
    ap.add_argument("--test-email", action="store_true", help="寄一封測試信後結束")
    args = ap.parse_args()

    def log(msg):
        print(f"[{paris_now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

    cfg = load_config()
    youth_only = cfg["mode"] != "youth_or_under30"
    if not args.dry_run:
        missing = [k for k in ("smtp_user", "smtp_app_password", "notify_to") if not cfg.get(k)]
        if missing:
            log(f"缺少設定：{', '.join(missing)}（config.json 或環境變數 SMTP_USER / SMTP_APP_PASSWORD）")
            return 2

    if args.test_email:
        send_email(cfg, "✅ 悲慘世界票務監控：測試信",
                   "<p>這是一封測試信。之後只要有《Les Misérables》"
                   + ("青年票（Jeune - de 30 ans）" if youth_only else "青年票或低於 €30 的票")
                   + "釋出，就會寄到這個信箱。</p>")
        log(f"測試信已寄出到 {cfg['notify_to']}")
        return 0

    if paris_now().date() > LAST_DAY:
        log("演出已全部結束（2027/01/10），不再檢查。可以停用監控了。")
        return 0

    http = Http()
    log(f"模式：{'只要青年票' if youth_only else '青年票或 < €30'}。開始檢查官方售票…")
    perfs = list_performances(http, MAIN)
    log(f"共 {len(perfs)} 場演出")
    matches = check_main(http, perfs, log, youth_only)
    log(f"官方售票符合條件：{len(matches)} 筆")

    log("開始檢查官方轉售 Bourse…")
    try:
        b = check_bourse_youth(http, log) if youth_only else check_bourse(http, log)
        log(f"轉售符合條件：{len(b)} 筆")
        matches += b
    except Exception as e:  # noqa: BLE001
        log(f"轉售檢查失敗：{e}")

    if cfg.get("exclude_no_visibility"):
        matches = [m for m in matches if not re.search(
            r"sans visibilit|no visibility", m["category"], re.I)]
    matches.sort(key=lambda m: (m["date"], m["time"], m["price"] or 0))

    state = load_json(STATE_FILE, {"available": {}, "notified": {}})
    now = time.time()
    prev_avail = state.get("available", {})
    notified = state.get("notified", {})
    new = []
    for m in matches:
        k = m["key"]
        if k in prev_avail:
            continue  # 上次就已經在了，不重複通知
        last = notified.get(k, 0)
        if now - last < RENOTIFY_COOLDOWN_MIN * 60:
            continue
        new.append(m)

    for m in matches:
        flag = "NEW " if m in new else "    "
        log(f"{flag}{m['date']} {m['time']} | {m['channel']} | {m['category']} | "
            f"{m['tariff']} | {eur(m['price'])}")

    if args.dry_run:
        log(f"[dry-run] 若正式執行，將通知 {len(new)} 筆新票")
        return 0

    if new:
        subject, body = build_email(new, matches, youth_only)
        try:
            send_email(cfg, subject, body)
            log(f"已寄出通知：{subject}")
            for m in new:
                notified[m["key"]] = int(now)
        except Exception as e:  # noqa: BLE001
            log(f"寄信失敗：{e}（下次會再嘗試）")
            # 寄信失敗時不更新 available，讓下次仍視為新票
            matches = [m for m in matches if m not in new]
    else:
        log("沒有新釋出的票")

    # 清掉一週前的通知紀錄
    notified = {k: v for k, v in notified.items() if now - v < 7 * 86400}
    # 狀態檔只在票況改變時才變動（加上每週一次心跳），方便雲端只在必要時 commit
    year, week, _ = paris_now().isocalendar()
    save_json(STATE_FILE, {
        "available": {m["key"]: prev_avail.get(m["key"], int(now)) for m in matches},
        "notified": notified,
        "heartbeat": f"{year}-W{week:02d}",
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
