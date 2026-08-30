# -*- coding: utf-8 -*-
"""
کروزنیوز بات - ارسال خودکار اخبار ورزشی خوزستان از منابع مختلف به کانال تلگرام

منابع از فایل sources.json خوانده می‌شوند. برای افزودن منبع جدید فقط آن را به
sources.json اضافه کن (نام آن باید در پارسرهای پایین ثبت شده باشد).
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
# تنظیمات (با متغیر محیطی قابل بازنویسی)
# ----------------------------------------------------------------------
_raw_token = os.environ.get("TELEGRAM_TOKEN")
if not _raw_token:
    raise SystemExit("خطا: متغیر محیطی TELEGRAM_TOKEN تنظیم نشده است. "
                     "در GitHub آن را به‌عنوان Secret اضافه کن، یا روی سیستم خودت با "
                     "set TELEGRAM_TOKEN=<token> آن را بگذار.")
TELEGRAM_TOKEN = _raw_token
CHANNEL_ID = int(os.environ.get("CHANNEL_ID") or "-1003716968370")  # آیدی کانال @khoozsport
CHROME_PATH = os.environ.get("CHROME_PATH") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.environ.get("SOURCES_FILE") or os.path.join(BASE_DIR, "sources.json")
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(BASE_DIR, "sent_news.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL") or 300)   # ثانیه - فاصله بین هر چک در حالت عادی
OFFLINE_INTERVAL = 30                            # ثانیه - فاصله چک مجدد وقتی اینترنت قطع است
MAX_RETRIES = 3                                  # تعداد تلاش مجدد برای هر درخواست شبکه
RETRY_BASE_DELAY = 3                             # ثانیه - شروع فاصله بین تلاش‌ها (ضربدر ۲ می‌شود)
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

# کلید API Gemini (برای بازنویسی هوشمند تیتر و متن)
GEMINI_KEY = os.environ.get("GEMINI_KEY") or ""

# تنظیمات کانال
CHANNEL_NAME = os.environ.get("CHANNEL_NAME") or "ورزش خوزستان"
# فقط منابع مشخصی پردازش شوند (جدا با کاما) - برای تقسیم کار بین گیت‌هاب و سرور لوکال
ONLY_SOURCES = os.environ.get("ONLY_SOURCES") or ""

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "fa-IR,fa;q=0.9"}
TG_PROXIES = {"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY} if TELEGRAM_PROXY else None
TG_BASE = "https://api.telegram.org/bot" + TELEGRAM_TOKEN
MAX_CAPTION_LINES = 10
MAX_PARA_LEN = 480


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
          .replace("&gt;", ">").replace("&laquo;", "«").replace("&raquo;", "»")
          .replace("&zwnj;", "").replace("&ndash;", "-").replace("&mdash;", "-"))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def remove_isna(text):
    text = re.sub(r"\(\s*ایسنا\s*\)", "", text)
    text = re.sub(r"به\s+گزارش\s+ایسنا\s*[،,]?\s*", "", text)
    text = re.sub(r"خبرنگار\s+ایسنا\s*[،,]?\s*", "", text)
    # حذف عبارت‌های «به گزارش ...» برای خبرگزاری‌های مختلف
    text = re.sub(r"به\s+گزارش\s+(پایگاه خبری|خبرگزاری|ایسنا|فارس|ایرنا|مهر|تسنیم|خوزنیوز|ورزش و جوانان)[^،;]{0,40}[،،]?\s*", "", text)
    text = text.replace("ایسنا", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text.strip()


def make_abs(url, base):
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return base.rstrip("/") + "/" + url.lstrip("/")


# ======================================================================
# پارسرهای فهرست اخبار هر منبع (بر اساس نام منبع)
# ======================================================================
def parse_isna_items(html, cfg):
    base = cfg.get("base_url", "https://www.isna.ir")
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
        items.append({"id": news_id_of(url), "title": title,
                      "url": make_abs(url, base), "desc": desc, "img": img})
    return items


def parse_khouznews_items(html, cfg):
    base = cfg.get("base_url", "https://khouznews.ir")
    items = []
    pattern = re.compile(
        r'<a class="picLink" href="(/fa/news/[^"]+)"[^>]*>\s*<img[^>]*data-src="([^"]+)"',
        re.S)
    for m in pattern.finditer(html):
        href = m.group(1)
        img = m.group(2)
        # عنوان در ادامه همان بلوک
        end = html.find('<div class="item', m.end())
        if end == -1:
            end = m.end() + 3000
        chunk = html[m.end():end]
        tm = re.search(r'<a class="kh_title"[^>]*>(.*?)</a>', chunk, re.S)
        if not tm:
            continue
        title = clean_html(tm.group(1))
        idm = re.search(r"/fa/news/(\d+)", href)
        if not idm:
            continue
        items.append({"id": idm.group(1), "title": title,
                      "url": make_abs(href, base), "desc": title,
                      "img": make_abs(img, base)})
    return items


def parse_rss_items(xml, cfg):
    """پارسر عمومی RSS - برای سایت‌هایی که فید RSS دارند (مثل خوزنیوز)"""
    import xml.etree.ElementTree as ET
    items = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log("خطا در پارس RSS: " + str(e))
        return items
    for it in root.iter("item"):
        title = ""
        link = ""
        desc = ""
        for child in it:
            tag = child.tag.split("}")[-1]
            if tag == "title":
                title = (child.text or "").strip()
            elif tag == "link":
                link = (child.text or "").strip()
            elif tag == "description":
                desc = clean_html(child.text or "")
        idm = re.search(r"/(?:fa/)?news/(\d+)", link)
        if not idm:
            continue
        items.append({"id": idm.group(1), "title": title,
                      "url": link, "desc": desc, "img": ""})
    return items


def parse_msy_items(html, cfg):
    """استخراج اخبار از صفحه وزارت ورزش (بر اساس تب مشخص در config)"""
    tab = cfg.get("tab")
    items = []
    if tab:
        m = re.search(r"id=['\"]" + tab + r"['\"]", html)
        if m:
            start = m.start()
            next_m = re.search(r"id=['\"](tab_\d+)['\"]", html[start + 10:])
            end = start + 10 + next_m.start() if next_m else len(html)
            seg = html[start:end]
        else:
            seg = html
    else:
        seg = html
    seen = {}
    for lm in re.finditer(
            r'<a href="(https://khouzestan\.msy\.gov\.ir/fa/news/(\d+)-[^"]*\.html)"[^>]*title="([^"]*)"[^>]*>.*?</a>',
            seg, re.S):
        url, nid, title = lm.group(1), lm.group(2), lm.group(3).strip()
        if not title or nid in seen:
            continue
        img_m = re.search(r'<img src="(https://khouzestan\.msy\.gov\.ir/fa/\.\./temporary/images/[^"]+)"', lm.group(0))
        img = img_m.group(1) if img_m else ""
        img = re.sub(r"/fa/\.\./", "/", img)
        seen[nid] = {"id": nid, "title": title, "url": url, "img": img, "desc": title}
    return list(seen.values())


def parse_khw_items(html, cfg):
    """پارسر هیأت کشتی خوزستان (khwb.ir)"""
    base = cfg.get("base_url", "http://www.khwb.ir")
    items = []
    seen = {}
    for m in re.finditer(r'<a href="(http://www\.khwb\.ir/content/news/(\d+)/)"', html):
        url, nid = m.group(1), m.group(2)
        chunk = html[m.start():m.start() + 2000]
        tm = re.search(r'<h[234][^>]*>\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        if not tm:
            tm = re.search(r'alt="([^"]+)"', chunk)
        if tm:
            title = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
        else:
            title = ""
        if not title or nid in seen:
            continue
        im = re.search(r'<img src="([^"]+)"', chunk)
        img = im.group(1) if im else ""
        seen[nid] = {"id": nid, "title": title, "url": url, "img": img, "desc": title}
    return list(seen.values())


def parse_volleyball_items(html, cfg):
    """پارسر والیبال ایران (volleyball.ir/tag/خوزستان) - وردپرس"""
    items = []
    seen = {}
    for m in re.finditer(r'<h2><a href="(https://volleyball\.ir/[^"]+)">(.*?)</a></h2>', html, re.S):
        url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not title:
            continue
        nid = re.search(r"volleyball\.ir/(\d+)", url)
        nid = nid.group(1) if nid else url.split("/")[-2][:20]
        # تصویر
        im = re.search(r'<a href="' + re.escape(url) + r'"><img[^>]*src="([^"]+)"', html[m.start() - 2000:m.start()])
        img = im.group(1) if im else ""
        if nid in seen:
            continue
        seen[nid] = {"id": nid, "title": title, "url": url, "img": img, "desc": title}
    return list(seen.values())


def parse_iranbbf_items(html, cfg):
    """پارسر فدراسیون بدنسازی (iranbbf.ir/News/Ostan?id=21)"""
    base = cfg.get("base_url", "https://www.iranbbf.ir")
    items = []
    seen = {}
    for m in re.finditer(
            r'<a href="/News/d\?id=(\d+)[^"]*">\s*<img[^>]*src="/www/photonews/([^"]+)"', html, re.S):
        nid = m.group(1)
        img = "https://www.iranbbf.ir/www/photonews/" + m.group(2)
        # find title after this block
        chunk = html[m.end():m.end() + 2000]
        tm = re.search(r'<h2 class="post-title[^"]*">\s*<a href="/News/d\?id=' + re.escape(nid) + r'[^"]*"[^>]*>(.*?)</a>', chunk, re.S)
        if not tm:
            continue
        title = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
        if not title or nid in seen:
            continue
        seen[nid] = {"id": nid, "title": title, "url": base + "/News/d?id=" + nid, "img": img, "desc": title}
    return list(seen.values())


def parse_footballi_items(html, cfg):
    """پارسر فوتبالی (footballi.net) - هیئت فوتبال خوزستان"""
    base = cfg.get("base_url", "https://footballi.net")
    items = []
    seen = {}
    for m in re.finditer(r'<a[^>]*class="one-news"[^>]*href="(/news/r/(\d+)/[^"]*)"[^>]*>', html):
        url, nid = m.group(1), m.group(2)
        chunk = html[m.start():m.start() + 2000]
        im = re.search(r'<img[^>]*alt="([^"]*)"[^>]*src="(https://cdn\.oddrun\.ir/[^"]+)"', chunk)
        if not im:
            continue
        title = im.group(1).strip()
        img = im.group(2)
        if not title or nid in seen:
            continue
        seen[nid] = {"id": nid, "title": title, "url": base + url, "img": img, "desc": title}
    return list(seen.values())


PARSERS = {
    "isna": parse_isna_items,
    "khouznews": parse_khouznews_items,
    "rss": parse_rss_items,
    "msy": parse_msy_items,
    "khw": parse_khw_items,
    "volleyball": parse_volleyball_items,
    "iranbbf": parse_iranbbf_items,
    "footballi": parse_footballi_items,
}


# ======================================================================
# گرفتن متن کامل خبر
# ======================================================================
def news_id_of(url):
    m = re.search(r"/news/(\d+)", url)
    return m.group(1) if m else None


def _chrome_launch_args():
    if os.path.exists(CHROME_PATH):
        return {"executable_path": CHROME_PATH}
    return {}


def fetch_article_chrome(url, fallback_title="", fallback_desc=""):
    """متن کامل خبر برای سایت‌هایی که پشت چالش JS هستند (مثل ایسنا)"""
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
                "  let title = document.title.replace(/\\u0020-\\u0020\\u0627\\u06cc\\u0633\\u0646\\u0627\\s*$/, '').trim();"
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
        log("خطا در گرفتن متن کامل خبر (استفاده از خلاصه): " + str(e))
        paras = [fallback_desc] if fallback_desc else []
        return {"title": fallback_title, "paras": paras, "img": ""}


def fetch_article_http(url, fallback_title="", fallback_desc=""):
    """متن کامل خبر برای سایت‌های عادی (بدون چالش JS)"""
    try:
        r = request_with_retry("GET", url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        html = r.text
        # عنوان: اول og:title، بعد title (بدون اسم سایت)، بعد h1
        title = ""
        m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if m:
            title = clean_html(m.group(1))
        if not title:
            m = re.search(r"<title>(.*?)</title>", html, re.S)
            if m:
                t = re.sub(r"^[^>]*::", "", m.group(1).strip())
                title = clean_html(t)
        if not title:
            m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
            if m:
                title = clean_html(m.group(1))
        if not title:
            title = fallback_title
        # متن خبر
        block = html
        for cls in ["sis-desc", "item-text", "sis-content-text", "newsbody", "item-body", "body"]:
            im = re.search(r'class="' + cls + r'"(.*?)(?=<div class="|<footer|</article>|</body>)', html, re.S)
            if im:
                block = im.group(1)
                break
        paras = []
        for pm in re.finditer(r"<p[^>]*>(.*?)</p>", block, re.S):
            t = clean_html(pm.group(1))
            if t:
                paras.append(t)
        og = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        img = og.group(1) if og else ""
        if not paras:
            raise RuntimeError("محتوای خبر یافت نشد")
        return {"title": title, "paras": paras, "img": img}
    except Exception as e:
        log("خطا در گرفتن متن کامل خبر (استفاده از خلاصه): " + str(e))
        paras = [fallback_desc] if fallback_desc else []
        return {"title": fallback_title, "paras": paras, "img": ""}


ARTICLE_FETCHERS = {
    "chrome": fetch_article_chrome,
    "http": fetch_article_http,
}


# ======================================================================
# ساخت گزارش و ارسال
# ======================================================================
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


def title_emoji(text):
    """ایموجی عمومی برای تیتر (بدون توجه به نوع ورزش)"""
    return "⚡"


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def make_catchy_title(title):
    """تیتر را به شکل درشت و جنجالی درمی‌آورد (با ایموجی و <b>)"""
    t = (title or "").strip()
    if not t:
        return ""
    t = re.sub(r"</?b>", "", t)
    t = re.sub(r"\s+", " ", t)
    emoji = title_emoji(t)
    return "<b>%s %s</b>" % (emoji, html_escape(t))


def split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[\.!؟?؛])\s+", text) if s.strip()]


def is_quote_sentence(s):
    return any(k in s for k in ("«", "گفت:", "اظهار کرد", "خاطرنشان کرد", "بیان کرد",
                                "عنوان کرد", "تاکید کرد", "یادآور شد", "افزود:"))


def rewrite_body(paras, max_sentences=6):
    """متن اصلی را به گزارش خلاصه و بازنویسی شده تبدیل می‌کند (نه کپی مستقیم)"""
    result = []
    if not paras:
        return ""
    # سطر اول: لید خبر (خلاصه اصلی) - حذف پیشوند خبرگزاری
    lead = remove_isna(paras[0])
    lead = re.sub(r"^\s*خوزستان\s*[-–]\s*", "", lead).strip()
    if lead:
        result.append(lead)
    # بقیه: فقط جمله‌های غیر نقل‌قول (حقایق اصلی)
    for p in paras[1:]:
        p = remove_isna(p)
        for s in split_sentences(p):
            if len(result) >= max_sentences:
                break
            if is_quote_sentence(s):
                continue
            s = re.sub(r"«.*?»", "", s).strip()
            s = re.sub(r"^[،،;\s]+", "", s)
            if s and s not in result:
                result.append(s)
        if len(result) >= max_sentences:
            break
    if not result:
        result = [remove_isna(paras[0])]
    return " ".join(result)


def gemini_rewrite(title, text):
    """بازنویسی تیتر و متن با هوش مصنوعی Gemini (جنجالی + خلاصه)"""
    if not GEMINI_KEY:
        return None
    try:
        body_preview = text[:2000] if text else ""
        prompt = (
            "تو یک خبرنگار ورزشی خوزستانی هستی. خبر زیر را بازنویسی کن:\n\n"
            "--- خبر اصلی ---\n"
            "تیتر: " + (title or "") + "\n"
            "متن: " + body_preview + "\n\n"
            "--- خواسته‌ها ---\n"
            "۱. تیتر جدید جنجالی، انتقادی و گیرا بنویس (کاملاً متفاوت از تیتر اصلی)\n"
            "۲. متن خبر را در ۳-۵ جمله خلاصه کن، انگار خودت گزارش رو نوشتی (نه کپی)\n"
            "۳. هیچ اشاره‌ای به خبرگزاری یا منبع نکن\n"
            "۴. به صورت گزارشی روان و پیوسته بنویس، اطلاعات اصلی (تیم‌ها، نتایج، زمان) حتماً حفظ بشه\n"
            "۵. تیتر و متن را به فارسی روان بنویس\n\n"
            "فرمت خروجی دقیقاً:\n"
            "تیتر: <تیتر جدید>\n"
            "متن: <متن بازنویسی شده>"
        )
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        r = request_with_retry("POST", url, params={"key": GEMINI_KEY}, json=payload,
                                proxies=TG_PROXIES, timeout=60)
        if r.status_code != 200:
            log("Gemini خطا: " + str(r.status_code) + " " + r.text[:200])
            return None
        data = r.json()
        resp = data["candidates"][0]["content"]["parts"][0]["text"]
        log("Gemini پاسخ: " + resp[:150])
        # استخراج تیتر و متن
        new_title = ""
        new_body = ""
        for line in resp.splitlines():
            if line.startswith("تیتر:"):
                new_title = line.replace("تیتر:", "", 1).strip()
            elif line.startswith("متن:"):
                new_body = line.replace("متن:", "", 1).strip()
        if new_title or new_body:
            return {"title": new_title, "body": new_body}
        return None
    except Exception as e:
        log("Gemini خطا: " + str(e))
        return None


def build_report(item, article):
    orig_title = article.get("title") or item["title"]
    paras = [p for p in (article.get("paras") or []) if p]
    body = rewrite_body(paras)
    if not body:
        body = remove_isna(item.get("desc") or "")

    # بازنویسی با هوش مصنوعی (اگر کلید Gemini موجود باشد)
    ai = gemini_rewrite(orig_title, body) if GEMINI_KEY else None
    if ai and (ai.get("title") or ai.get("body")):
        title = make_catchy_title(ai.get("title") or orig_title)
        body = ai.get("body") or body
    else:
        title = make_catchy_title(orig_title)

    # ساختار نهایی: تیتر | فاصله | متن | فاصله انتها (هدر به صورت تصویر)
    parts = []
    if title:
        parts.append(title)
        parts.append("")
    parts.append(body)
    parts.append("")
    report = "\n".join(parts)
    report = remove_isna(report)
    lines = report.splitlines()
    if len(lines) > MAX_CAPTION_LINES:
        while len(report.splitlines()) > MAX_CAPTION_LINES and len(body) > 40:
            body = body[:body.rfind(" ")]
            parts = []
            if title:
                parts.append(title)
                parts.append("")
            parts.append(body)
            parts.append("")
            report = "\n".join(parts)
            report = remove_isna(report)
    return report


def download_image(url, dst):
    r = request_with_retry("GET", url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    with open(dst, "wb") as f:
        f.write(r.content)
    return dst


# ======================================================================
# مدیریت کامندهای تلگرام (برای افزودن خودکار منابع)
# ======================================================================
def process_telegram_commands():
    """بررسی پیام‌های دریافتی ربات و اجرای کامندها"""
    try:
        r = request_with_retry("GET", TG_BASE + "/getUpdates", proxies=TG_PROXIES, timeout=25)
        data = r.json()
        if not data.get("ok"):
            return
        import json as _json
        changed = False
        for upd in data.get("result", []):
            msg = upd.get("message") or {}
            text = (msg.get("text") or "").strip()
            chat_id = msg.get("chat", {}).get("id")
            if not text or not chat_id:
                continue
            if text.startswith("/addsource "):
                parts = text.split()
                if len(parts) < 4:
                    reply = "نادرست. فرمت: /addsource <name> <type> <list_url> [article_method]"
                else:
                    name = parts[1]
                    stype = parts[2]
                    s_url = parts[3]
                    am = parts[4] if len(parts) > 4 else "http"
                    if name in ("isna", "khouznews", "rss"):
                        reply = "این نام رزرو شده است. نام دیگری انتخاب کن."
                    else:
                        sources = load_sources()
                        # تست منبع
                        parser = PARSERS.get(stype)
                        if parser is None:
                            reply = "نوع '%s' ناشناخته است. انواع: %s" % (stype, ", ".join(PARSERS.keys()))
                        else:
                            try:
                                tr = request_with_retry("GET", s_url, headers=HEADERS, timeout=20)
                                test_items = parser(tr.text, {"base_url": s_url.split("/fa/")[0] if "/fa/" in s_url else s_url})
                                if not test_items:
                                    reply = "منبع تست شد ولی خبری یافت نشد. لینک را بررسی کن."
                                else:
                                    sources.append({"name": name, "title": name, "type": stype,
                                                     "list_url": s_url, "base_url": s_url,
                                                     "article_method": am})
                                    with io.open(SOURCES_FILE, "w", encoding="utf-8") as f:
                                        _json.dump({"sources": sources}, f, ensure_ascii=False, indent=2)
                                    reply = "منبع %s با %d خبر اضافه شد." % (name, len(test_items))
                                    changed = True
                            except Exception as e:
                                reply = "خطا در تست منبع: " + str(e)
                    send_plain(chat_id, reply)
            elif text == "/listsources":
                sources = load_sources()
                reply = "منابع فعال:\n" + "\n".join("  %s: %s (%s)" % (s["name"], s.get("list_url", "?"), s.get("type", "?")) for s in sources)
                send_plain(chat_id, reply)
            elif text == "/help":
                reply = "کامندها:\n" + "/addsource <name> <type> <url> [method] - افزودن منبع جدید\n" + "/listsources - لیست منابع\n" + "/help - راهنما"
                send_plain(chat_id, reply)
            elif text.startswith("/"):
                send_plain(chat_id, "کامند ناشناخته. /help را ببین.")
        if changed:
            # آپدیت‌های پردازش‌شده را پاک کن
            request_with_retry("GET", TG_BASE + "/getUpdates", params={"offset": data["result"][-1]["update_id"] + 1}, proxies=TG_PROXIES, timeout=25)
    except Exception as e:
        log("خطا در پردازش کامندهای تلگرام: " + str(e))


def send_photo(caption, img_path):
    with open(img_path, "rb") as f:
        r = request_with_retry(
            "POST",
            TG_BASE + "/sendPhoto",
            data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("news.jpg", f, "image/jpeg")},
            proxies=TG_PROXIES,
            timeout=60,
        )
    if r.status_code != 200:
        raise RuntimeError("Telegram send failed: " + r.text)
    return r.json()


def send_plain(chat_id, text):
    try:
        request_with_retry("GET", TG_BASE + "/sendMessage", params={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, proxies=TG_PROXIES, timeout=25)
    except Exception:
        pass


# ======================================================================
# مدیریت وضعیت (کلید: نام منبع + آیدی خبر)
# ======================================================================
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


def load_sources():
    with io.open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("sources", [])


def process_source(source, sent, send=True):
    name = source["name"]
    parser_key = source.get("type", name)
    parser = PARSERS.get(parser_key)
    if parser is None:
        log("منبع ناشناخته: " + name)
        return sent
    timeout = source.get("timeout", 30)
    max_retries = source.get("max_retries", MAX_RETRIES)
    html = request_with_retry("GET", source["list_url"], headers=HEADERS, timeout=timeout, max_retries=max_retries)
    html.raise_for_status()
    items = parser(html.text, source)
    log("[%s] تعداد اخبار: %d" % (name, len(items)))
    new_items = [it for it in items if (name + ":" + it["id"]) not in sent]
    if not new_items:
        return sent
    new_items.sort(key=lambda it: int(it["id"]))
    fetcher = ARTICLE_FETCHERS.get(source.get("article_method", "http"))
    tmp_news = os.path.join(BASE_DIR, "tmp_news.jpg")
    for it in new_items:
        try:
            log("[%s] خبر جدید: %s" % (name, it["title"]))
            article = fetcher(it["url"], it["title"], it.get("desc") or "")
            report = build_report(it, article)
            if send:
                # دانلود تصویر خبر (اگر داشته باشد)
                dl_img = None
                img = it["img"] or article.get("img") or ""
                if img:
                    try:
                        download_image(img, tmp_news)
                        dl_img = tmp_news
                    except Exception as e:
                        log("[%s] خطا در دانلود تصویر خبر: %s" % (name, str(e)))
                if dl_img:
                    send_photo(report, dl_img)
                else:
                    send_plain(CHANNEL_ID, report)
                log("[%s] ارسال شد: %s" % (name, it["id"]))
            else:
                log("[%s] DRY-RUN (بدون ارسال):\n%s" % (name, report))
            sent.append(name + ":" + it["id"])
            save_state(sent)
        except Exception as e:
            log("[%s] خطا در پردازش خبر %s: %s" % (name, it.get("id"), str(e)))
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
            # پردازش کامندهای تلگرام (افزودن خودکار منبع بدون نیاز به گیت‌هاب)
            process_telegram_commands()
            sources = load_sources()
            if ONLY_SOURCES:
                names = [n.strip() for n in ONLY_SOURCES.split(",") if n.strip()]
                sources = [s for s in sources if s["name"] in names]
            log("تعداد منابع: " + str(len(sources)))
            sent = load_state()
            first_run = not sent
            if first_run:
                # اولین اجرا: پایه ثبت کن (هیچ خبری ارسال نشه)
                for source in sources:
                    try:
                        name = source["name"]
                        parser_key = source.get("type", name)
                        parser = PARSERS.get(parser_key)
                        if parser is None:
                            continue
                        html = request_with_retry("GET", source["list_url"], headers=HEADERS, timeout=30)
                        items = parser(html.text, source)
                        for it in items:
                            sent.append(name + ":" + it["id"])
                        log("[%s] پایه ثبت شد: %d خبر" % (name, len(items)))
                    except Exception as e:
                        log("[%s] خطا در ثبت پایه: %s" % (source.get("name", "?"), str(e)))
                save_state(sent)
                log("اجرای اولیه کامل شد - از این به بعد فقط اخبار جدید ارسال می‌شود")
                if args.send_existing:
                    # کاربر خواسته اخبار فعلی هم ارسال شود
                    for source in sources:
                        try:
                            sent = process_source(source, sent, send=not args.dry_run)
                        except Exception as e:
                            log("[%s] خطا: %s" % (source.get("name", "?"), str(e)))
            else:
                for source in sources:
                    try:
                        sent = process_source(source, sent, send=not args.dry_run)
                    except (requests.ConnectionError, requests.Timeout) as e:
                        log("[%s] اینترنت قطع است: %s" % (source["name"], e.__class__.__name__))
                        current_interval = OFFLINE_INTERVAL
                    except Exception as e:
                        log("[%s] خطا: %s" % (source["name"], str(e)))
        except Exception as e:
            log("خطا: " + str(e))
        if args.once:
            break
        time.sleep(current_interval)


if __name__ == "__main__":
    main()
