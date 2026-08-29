# -*- coding: utf-8 -*-
"""
کروزنیوز بات - ارسال خودکار اخبار ورزشی خوزستان از خبرگزاری ایسنا به کانال تلگرام
"""
import argparse
import io
import json
import os
import re
import sys
import time

import requests
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ----------------------------------------------------------------------
# تنظیمات
# مقادیر پیش‌فرض با متغیر محیطی قابل بازنویسی هستند (برای GitHub Actions)
# ----------------------------------------------------------------------
_raw_token = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_TOKEN = _raw_token if _raw_token else "8952332443:AAEWO--5PxOgiHrx95G4tRVriDpE611jB6Q"
CHANNEL_ID = int(os.environ.get("CHANNEL_ID") or "-1003716968370")  # آیدی کانال @khoozsport
TAG_NAME = "اخبار ورزشی - خوزستان"
TAG_URL = "https://www.isna.ir/tag/" + requests.utils.quote(TAG_NAME) + "/rss"
CHROME_PATH = os.environ.get("CHROME_PATH") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "sent_news.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL") or 300)   # ثانیه - فاصله بین هر چک در حالت عادی
OFFLINE_INTERVAL = 30                            # ثانیه - فاصله چک مجدد وقتی اینترنت قطع است
MAX_RETRIES = 5                                  # تعداد تلاش مجدد برای هر درخواست شبکه
RETRY_BASE_DELAY = 5                             # ثانیه - شروع فاصله بین تلاش‌ها (ضربدر ۲ می‌شود)
# پروکسی تلگرام: روی سرور لوکال لازم است؛ روی GitHub با TELEGRAM_PROXY="" غیرفعال می‌شود
_raw_proxy = os.environ.get("TELEGRAM_PROXY")
if _raw_proxy == "":
    TELEGRAM_PROXY = None
elif _raw_proxy is None:
    TELEGRAM_PROXY = "http://127.0.0.1:12334"
else:
    TELEGRAM_PROXY = _raw_proxy
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9"}
TG_PROXIES = {"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY} if TELEGRAM_PROXY else None
TG_BASE = "https://api.telegram.org/bot" + TELEGRAM_TOKEN
MAX_CAPTION_LINES = 10


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def request_with_retry(method, url, max_retries=MAX_RETRIES, **kwargs):
    """درخواست HTTP با تلاش مجدد (برای تحمل قطعی اینترنت)"""
    delay = RETRY_BASE_DELAY
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries:
                log("خطای شبکه (تلاش %d/%d): %s - تلاش مجدد در %d ثانیه"
                    % (attempt, max_retries, e.__class__.__name__, delay))
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                log("اینترنت قطع است: %s" % e.__class__.__name__)
                raise
    raise last_exc


def clean_html(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&#039;", "'").replace("&lt;", "<")
          .replace("&gt;", ">"))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def remove_isna(text):
    text = re.sub(r"\(\s*ایسنا\s*\)", "", text)
    text = re.sub(r"به\s+گزارش\s+ایسنا\s*[،,]?\s*", "", text)
    text = re.sub(r"خبرنگار\s+ایسنا\s*[،,]?\s*", "", text)
    text = text.replace("ایسنا", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text.strip()


def news_id_of(url):
    m = re.search(r"/news/(\d+)", url)
    return m.group(1) if m else None


def fetch_tag_page():
    r = request_with_retry("GET", TAG_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_items(html):
    items = []
    pattern = re.compile(r'<div class="desc">(.*?)(?=<div class="desc">|<figure>|</body>|\Z)', re.S)
    for m in pattern.finditer(html):
        block = m.group(1)
        hm = re.search(r'<h[34]><a[^>]*href="([^"]+)"[^>]*>.*?<span class="category">.*?</span>(.*?)</a>', block, re.S)
        if not hm:
            hm = re.search(r'<h[34]><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not hm:
            continue
        url = hm.group(1)
        if "/news/" not in url:
            continue
        title = clean_html(hm.group(2))
        pm = re.search(r"<p>(.*?)</p>", block, re.S)
        desc = clean_html(pm.group(1)) if pm else ""
        start = m.start()
        prev = html[max(0, start - 5000):start]
        imgs = re.findall(r'src="(https://cdn\.isna\.ir/[^"]+)"', prev)
        img = imgs[-1] if imgs else ""
        items.append({
            "id": news_id_of(url),
            "title": title,
            "url": "https://www.isna.ir" + url,
            "desc": desc,
            "img": img,
        })
    return items


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with io.open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("sent", [])
        except Exception:
            return []
    return []


def save_state(sent):
    with io.open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"sent": sent}, f, ensure_ascii=False)


def _chrome_launch_args():
    if os.path.exists(CHROME_PATH):
        return {"executable_path": CHROME_PATH}
    return {}


def fetch_article(url, fallback_title="", fallback_desc=""):
    """گرفتن متن کامل خبر. در صورت بروز خطا، خلاصه صفحه تگ به عنوان جایگزین برمی‌گردد."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, **_chrome_launch_args())
            page = browser.new_page(user_agent=USER_AGENT, locale="fa-IR")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(7000)
            try:
                page.wait_for_selector("article", timeout=15000)
            except Exception:
                pass
            data = page.evaluate(
                "() => {"
                "  let title = document.title.replace(/\u0020-\u0020\u0627\u06cc\u0633\u0646\u0627\s*$/, '').trim();"
                "  if (!title) { const h1 = document.querySelector('h1'); title = h1 ? h1.innerText.trim() : ''; }"
                "  const art = document.querySelector('article') || document.body;"
                "  const paras = [];"
                "  art.querySelectorAll('p').forEach(p => { const t = p.innerText.trim(); if (t) paras.push(t); });"
                "  let img = '';"
                "  const og = document.querySelector('meta[property=\"og:image\"]');"
                "  if (og) img = og.content || '';"
                "  if (!img) { const im = document.querySelector('.item-image img') || document.querySelector('article img'); if (im) img = im.currentSrc || im.src || ''; }"
                "  return {title: title, paras: paras, img: img};"
                "}"
            )
            browser.close()
        if not data.get("paras"):
            raise RuntimeError("محتوای خبر یافت نشد")
        return data
    except Exception as e:
        log("خطا در گرفتن متن کامل خبر (استفاده از خلاصه صفحه): " + str(e))
        paras = [fallback_desc] if fallback_desc else []
        return {"title": fallback_title, "paras": paras, "img": ""}


MAX_PARA_LEN = 480


def truncate_text(text, limit=MAX_PARA_LEN):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "؛" in cut:
        cut = cut.rsplit("؛", 1)[0] + "؛"
    elif "،" in cut:
        cut = cut.rsplit("،", 1)[0] + "،"
    elif " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def make_catchy_title(title):
    t = (title or "").strip()
    if not t:
        return ""
    if t.endswith("؟") or t.endswith("?"):
        return t
    return t


def build_report(item, article):
    title = make_catchy_title(article.get("title") or item["title"])
    paras = [p for p in (article.get("paras") or []) if p]
    body_parts = []
    for p in paras:
        if len(body_parts) >= 4:
            break
        t = truncate_text(remove_isna(p))
        if t and t not in body_parts:
            body_parts.append(t)
    if not body_parts:
        body_parts = [remove_isna(item.get("desc") or "")]
    body = "\n\n".join(body_parts)
    report = (title + "\n\n" + body) if title else body
    report = remove_isna(report)
    # محدود کردن به ۱۰ خط
    lines = report.splitlines()
    while len(lines) > MAX_CAPTION_LINES and body_parts:
        body_parts.pop()
        body = "\n\n".join(body_parts)
        report = (title + "\n\n" + body) if title else body
        report = remove_isna(report)
        lines = report.splitlines()
    return report


def download_image(url, dst):
    r = request_with_retry("GET", url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    with open(dst, "wb") as f:
        f.write(r.content)
    return dst


def send_photo(caption, img_path):
    with open(img_path, "rb") as f:
        r = request_with_retry(
            "POST",
            TG_BASE + "/sendPhoto",
            data={"chat_id": CHANNEL_ID, "caption": caption},
            files={"photo": ("news.jpg", f, "image/jpeg")},
            proxies=TG_PROXIES,
            timeout=60,
        )
    if r.status_code != 200:
        raise RuntimeError("Telegram send failed: " + r.text)
    return r.json()


def process_new_items(items, sent, send=True):
    new_items = [it for it in items if it["id"] not in sent]
    if not new_items:
        return sent
    # مرتب‌سازی صعودی بر اساس شناسه خبر = قدیمی‌تر اول (به‌ترتیب انتشار)
    new_items.sort(key=lambda it: int(it["id"]))
    for it in new_items:
        try:
            log("خبر جدید: " + it["title"])
            article = fetch_article(it["url"], it["title"], it.get("desc") or "")
            report = build_report(it, article)
            img = it["img"] or article.get("img") or ""
            if send and img:
                tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_news.jpg")
                download_image(img, tmp)
                send_photo(report, tmp)
                log("ارسال شد: " + it["id"])
            else:
                log("DRY-RUN (بدون ارسال):\n" + report + "\nIMG: " + img)
            sent.append(it["id"])
            save_state(sent)
        except Exception as e:
            log("خطا در پردازش خبر " + str(it.get("id")) + ": " + str(e))
    return sent


def main():
    parser = argparse.ArgumentParser(description="کروزنیوز بات")
    parser.add_argument("--once", action="store_true", help="فقط یک بار اجرا کن")
    parser.add_argument("--dry-run", action="store_true", help="ارسال نکن، فقط چاپ کن")
    parser.add_argument("--send-existing", action="store_true",
                        help="در اولین اجرا، اخبار موجود را هم بفرست (پیش‌فرض: فقط ذخیره و ادامه)")
    args = parser.parse_args()

    while True:
        current_interval = POLL_INTERVAL
        try:
            html = fetch_tag_page()
            items = parse_items(html)
            log("تعداد اخبار صفحه: " + str(len(items)))
            sent = load_state()
            if not sent:
                log("اجرای اولیه - بدون ارسال، اخبار فعلی به عنوان پایه ثبت شد")
                sent = [it["id"] for it in items]
                save_state(sent)
                if args.send_existing:
                    sent = process_new_items(items, [], send=not args.dry_run)
            else:
                sent = process_new_items(items, sent, send=not args.dry_run)
        except (requests.ConnectionError, requests.Timeout) as e:
            log("اینترنت قطع است - تلاش مجدد در %d ثانیه" % OFFLINE_INTERVAL)
            current_interval = OFFLINE_INTERVAL
        except Exception as e:
            log("خطا: " + str(e))
        if args.once:
            break
        time.sleep(current_interval)


if __name__ == "__main__":
    main()
