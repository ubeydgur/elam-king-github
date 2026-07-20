import requests, time, re, csv, io
from pathlib import Path
from bs4 import BeautifulSoup
import pytz, datetime
import pdfplumber
import os

API_URL     = "https://scraper-api.decodo.com/v2/scrape"
TOKEN       = os.environ["DECODO_TOKEN"]
CAPTCHA_KEY = os.environ["CAPTCHA_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
BATCH_SIZE  = 100

ALLOWED_COUNTIES = {"chester", "delaware"}   # müşteri: sadece bu ikisi (site fazladan county döndürüyor)

api_headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "authorization": f"Basic {TOKEN}",
}

MAX_RETRIES            = 5
SEARCH_RETRIES         = 8
CONSECUTIVE_FAIL_LIMIT = 2

SESSION_ID  = f"pnpa_{int(time.time())}"
last_cookies = []   # en son Decodo GET'inin cookie'leri (POST'ta session taşımak için)
colorado_tz = pytz.timezone('America/New_York')
today_co    = datetime.datetime.now(colorado_tz)
yesterday    = today_co - datetime.timedelta(days=1)
date_from_str = f"{yesterday.month}/{yesterday.day}/{yesterday.year}"
date_to_str   = f"{today_co.month}/{today_co.day}/{today_co.year}"
date_str      = date_from_str  # is_valid_search için


SEARCH_ACTIONS = [
    {"type": "wait_for_element", "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_divCounty"}, "timeout_s": 20},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_divCounty"}},
    {"type": "wait", "wait_time_s": 2},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_lstCounty > li:nth-child(23) > label"}},   # Delaware
    {"type": "wait", "wait_time_s": 3},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_lstCounty > li:nth-child(15) > label"}},   # Chester
    {"type": "wait", "wait_time_s": 3},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_divDateRange"}},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_txtDateFrom"}},
    {"type": "input",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_txtDateFrom"}, "value": date_from_str},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_txtDateTo"}},
    {"type": "input",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_txtDateTo"}, "value": date_to_str},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_rbRange"}},
    {"type": "click",  "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_as1_btnGo"}},
    {"type": "wait_for_element", "selector": {"type": "css", "value": ".wsResultsGrid"}, "timeout_s": 20},
]


# ── YARDIMCI: Decodo isteği — retry'lı ────────────────────
def decodo_get(url, browser_actions=None, retries=MAX_RETRIES):
    global last_cookies
    payload = {"url": url, "headless": "html", "session_id": SESSION_ID}
    if browser_actions:
        payload["browser_actions"] = browser_actions
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(API_URL, json=payload, headers=api_headers)
            data = r.json()
            if "results" in data:
                last_cookies = data["results"][0].get("cookies", [])
                return data["results"][0]["content"]
            print(f"  Decodo hatası (deneme {attempt}/{retries}): {data}")
        except Exception as e:
            print(f"  İstek hatası (deneme {attempt}/{retries}): {e}")
        time.sleep(5)
    return None


# ── YARDIMCI: Arama sonucunu doğrula ─────────────────────
def is_valid_search(html):
    """PA: 'El Paso' hard-check kaldırıldı, sadece tarih kontrolü.
    Kriter metnini basıyoruz ki ilk run'da Delaware/Chester seçilmiş mi + tarih
    formatı doğru mu görelim → sonra validation'ı kesinleştiririz."""
    criteria = re.search(r'class="criteria">(.*?)</span>', html, re.DOTALL)
    if not criteria:
        return False
    text = criteria.group(1)
    print(f"  [DEBUG] Kriter: {text[:200]}")
    if date_str not in text:
        print(f"  Kontrol: Tarih ({date_str}) bulunamadı")
        return False
    return True


# ── YARDIMCI: Arama yap — retry'lı ───────────────────────
def do_search():
    for attempt in range(1, SEARCH_RETRIES + 1):
        print(f"  Arama deneme {attempt}/{SEARCH_RETRIES}...")
        html = decodo_get(
            "https://www.publicnoticepa.com/default.aspx",
            browser_actions=SEARCH_ACTIONS,
            retries=1,
        )
        if html and "wsResultsGrid" in html:
            if is_valid_search(html):
                return html
            print(f"  Geçersiz arama sonucu, tekrar deneniyor...")
        else:
            print(f"  wsResultsGrid yok, tekrar deneniyor...")
        time.sleep(30)
    return None


# ── YARDIMCI: 2captcha ile CAPTCHA çöz (Turnstile + reCAPTCHA) ─────────
def solve_captcha(pageurl, sitekey, captcha_type="turnstile", retries=5):
    # 2captcha method + parametreleri sağlayıcıya göre seç
    if captcha_type == "turnstile":
        in_params = {"key": CAPTCHA_KEY, "method": "turnstile",
                     "sitekey": sitekey, "pageurl": pageurl, "json": 1}
    else:  # recaptcha
        in_params = {"key": CAPTCHA_KEY, "method": "userrecaptcha",
                     "googlekey": sitekey, "pageurl": pageurl, "json": 1}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post("http://2captcha.com/in.php", data=in_params)
            captcha_id = resp.json().get("request")
        except Exception as e:
            print(f"  2captcha gönderim hatası (deneme {attempt}/{retries}): {e}")
            time.sleep(10)
            continue

        if not captcha_id:
            print(f"  2captcha boş yanıt (deneme {attempt}/{retries}): {resp.text}")
            time.sleep(10)
            continue

        for _ in range(30):
            time.sleep(5)
            try:
                res = requests.get(f"http://2captcha.com/res.php?key={CAPTCHA_KEY}&action=get&id={captcha_id}&json=1")
                if res.json().get("status") == 1:
                    return res.json()["request"]
            except Exception as e:
                print(f"  2captcha polling hatası: {e}")

        print(f"  Token alınamadı (deneme {attempt}/{retries})")
        time.sleep(10)

    return None


# ── YARDIMCI: Hangi CAPTCHA sağlayıcısı? (site değişirse GÜNCELLENECEK TEK YER) ──
def detect_captcha(html):
    """Interstitial'daki CAPTCHA tipini + POST alan adını döndür.
    Bilinmeyen sağlayıcı gelirse (None, None) → çağıran yüksek sesle uyarır."""
    if "cf-turnstile" in html or "challenges.cloudflare.com/turnstile" in html:
        return "turnstile", "cf-turnstile-response"
    if "g-recaptcha" in html or "www.google.com/recaptcha" in html or "recaptcha/api.js" in html:
        return "recaptcha", "g-recaptcha-response"
    return None, None


# ── YARDIMCI: Detay sayfasını al — retry'lı ──────────────
def fetch_detail(detail_url, notice_id):
    for attempt in range(1, MAX_RETRIES + 1):
        html = decodo_get(detail_url, retries=1)
        if not html:
            print(f"  Detay boş (deneme {attempt}/{MAX_RETRIES})")
            time.sleep(5)
            continue

        # ── Interstitial ("I Agree" + CAPTCHA) sayfası mı? ────────────────
        # Tespiti CAPTCHA tipinden DEĞİL, sitenin kendi butonundan yapıyoruz
        # → sağlayıcı değişse bile burası kırılmaz.
        if "btnViewNotice" in html:
            captcha_type, response_field = detect_captcha(html)

            if captcha_type is None:
                # Bilinen CAPTCHA yok → site değişmiş olabilir. Sessiz dönme, BAĞIR.
                keys = re.findall(r'data-sitekey=["\']([^"\']+)["\']', html)
                print("  ⚠️⚠️ CAPTCHA TANINAMADI — site interstitial'ı değişmiş olabilir!")
                print(f"       'I Agree' sayfasındayız ama turnstile/recaptcha yok. sitekey'ler: {keys}")
                print("       → detect_captcha() güncellenmeli. Bu notice atlanıyor.")
                return None

            sk_match = re.search(r'data-sitekey=["\']([^"\']+)["\']', html)
            if not sk_match:
                print("  Sitekey bulunamadı!")
                time.sleep(5)
                continue
            sitekey = sk_match.group(1)

            print(f"  CAPTCHA ({captcha_type}) çözülüyor... (deneme {attempt}/{MAX_RETRIES})")
            token = solve_captcha(detail_url, sitekey, captcha_type=captcha_type)
            if not token:
                time.sleep(5)
                continue

            soup_c   = BeautifulSoup(html, "html.parser")
            vs       = soup_c.find("input", {"name": "__VIEWSTATE"})["value"]
            vsg      = soup_c.find("input", {"name": "__VIEWSTATEGENERATOR"})["value"]
            ev       = soup_c.find("input", {"name": "__EVENTVALIDATION"})["value"]
            form_sid = re.search(r'SID=([a-z0-9]+)', soup_c.find("form").get("action", "")).group(1)
            post_url = f"https://www.publicnoticepa.com/Details.aspx?SID={form_sid}&ID={notice_id}"

            cookie_jar = {c["key"]: c["value"] for c in last_cookies}
            html = requests.post(post_url, data={
                "ctl00_ToolkitScriptManager1_HiddenField": "",
                "__EVENTTARGET": "", "__EVENTARGUMENT": "",
                "__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vsg, "__EVENTVALIDATION": ev,
                response_field: token,
                "ctl00$ContentPlaceHolder1$PublicNoticeDetailsBody1$btnViewNotice": "View Notice",
            }, headers={"User-Agent": "Mozilla/5.0", "Referer": post_url}, cookies=cookie_jar).text

            if "btnViewNotice" in html:
                print(f"  CAPTCHA geçilemedi (deneme {attempt}/{MAX_RETRIES})")
                time.sleep(5)
                continue

        # İçerik kontrolü — marker DEĞİL, gerçekten dolu mu? (session bozulunca boş
        # şablon geliyor; onu 'başarılı' saymayalım ki refresh tetiklensin.)
        if "lblContentText" in html or "Notice Content" in html:
            _chk = BeautifulSoup(html, "html.parser")
            _c   = _chk.find("span", id=re.compile("lblContentText"))
            _h   = _c.find("span", style=re.compile(r"display\s*:\s*none")) if _c else None
            if _h: _h.extract()
            if (_c and _c.get_text(strip=True)) or _chk.find("a", href=re.compile("PDFDocument", re.I)):
                return html

        print(f"  Boş/geçersiz içerik (deneme {attempt}/{MAX_RETRIES})")
        time.sleep(5)

    return None


# ══════════════════════════════════════════
# ADIM 1: Arama yap
# ══════════════════════════════════════════
print("Arama yapılıyor...")
html = do_search()
if not html:
    print("KRİTİK HATA: Arama başarısız, çıkılıyor.")
    exit(1)

soup  = BeautifulSoup(html, "html.parser")
sid_m = re.search(r'SID=([a-z0-9]+)', html)
if not sid_m:
    print("KRİTİK HATA: SID bulunamadı, çıkılıyor.")
    exit(1)

sid = sid_m.group(1)
ids = [m for m in list(dict.fromkeys(re.findall(r'ID=(\d+)', html))) if len(m) >= 4]

total_pages_el = soup.find('span', id=re.compile('lblTotalPages'))
if not total_pages_el:
    total_pages = 1
    print(f"Tek sayfa — {len(ids)} notice")
else:
    total_pages = int(re.search(r'\d+', total_pages_el.get_text()).group())
    print(f"Sayfa 1/{total_pages} — {len(ids)} notice bu sayfada")

all_ids = ids.copy()


# ══════════════════════════════════════════
# ADIM 2: Sonraki sayfalar
# ══════════════════════════════════════════
for page in range(2, total_pages + 1):
    print(f"Sayfa {page}/{total_pages} alınıyor...")
    html_next = decodo_get(
        f"https://www.publicnoticepa.com/Search.aspx",
        browser_actions=[
            {"type": "wait_for_element", "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_WSExtendedGridNP1_GridView1_ctl01_btnNext"}, "timeout_s": 15},
            {"type": "click",            "selector": {"type": "css", "value": "#ctl00_ContentPlaceHolder1_WSExtendedGridNP1_GridView1_ctl01_btnNext"}},
            {"type": "wait", "wait_time_s": 3},
            {"type": "wait_for_element", "selector": {"type": "css", "value": ".wsResultsGrid"}, "timeout_s": 20},
        ]
    )
    if not html_next:
        print(f"  Sayfa {page} alınamadı, atlanıyor.")
        continue
    ids_next = [m for m in list(dict.fromkeys(re.findall(r'ID=(\d+)', html_next))) if len(m) >= 4]
    all_ids.extend(ids_next)
    print(f"  {len(ids_next)} notice alındı")

all_ids = list(dict.fromkeys(all_ids))
print(f"\nToplam: {len(all_ids)} notice")


# ══════════════════════════════════════════
# ADIM 3: Her notice için detay sayfası
# ══════════════════════════════════════════
results           = []
failed_ids        = []
consecutive_fails = 0

keys = [
    "noticeId", "publicationName", "publicationCity", "publicationState",
    "publicationCounty", "publicationDate", "authenticationNo",
    "publicationUrl", "pdfText", "scraped_at",
]

for i, notice_id in enumerate(all_ids, 1):
    print(f"\n[{i}/{len(all_ids)}] Notice {notice_id} işleniyor...")
    detail_url = f"https://www.publicnoticepa.com/Details.aspx?SID={sid}&ID={notice_id}"

    html_detail = fetch_detail(detail_url, notice_id)

    if html_detail is None:
        consecutive_fails += 1
        print(f"  BAŞARISIZ ({consecutive_fails} ardışık hata)")

        if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
            print(f"\n  Session yenileniyor...")
            SESSION_ID = f"pnpa_{int(time.time())}_{i}"   # taze id — 'failed' session aynı id'yle düzelmiyor
            new_html = do_search()
            if new_html:
                new_sid = re.search(r'SID=([a-z0-9]+)', new_html)
                if new_sid:
                    sid = new_sid.group(1)
                    print(f"  Session yenilendi, yeni SID: {sid}")
                    detail_url = f"https://www.publicnoticepa.com/Details.aspx?SID={sid}&ID={notice_id}"
                    html_detail = fetch_detail(detail_url, notice_id)
                    if html_detail:
                        consecutive_fails = 0

        if html_detail is None:
            failed_ids.append(notice_id)
            continue

    consecutive_fails = 0

    # Parse et
    soup_d = BeautifulSoup(html_detail, "html.parser")

    def get_span(id_pattern):
        el = soup_d.find("span", id=re.compile(id_pattern))
        return el.get_text(strip=True) if el else ""

    pub_url_tag = soup_d.find("a", id=re.compile("lnkPubURL"))
    pdf_tag     = soup_d.find("a", href=re.compile(r"PDFDocument", re.I))
    pdf_href    = pdf_tag.get("href", "") if pdf_tag else ""
    pdf_text    = ""

    # 1) PDF varsa metnini çek (PA: href göreli → domain ekle, (S(...)) yok)
    if pdf_href:
        try:
            pdf_url  = "https://www.publicnoticepa.com/" + pdf_href.lstrip("/")
            pdf_resp = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
            with pdfplumber.open(io.BytesIO(pdf_resp.content)) as pdf:
                pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            print(f"  PDF hatası: {e}")

    # 2) PDF yoksa/boşsa → ekrandaki notice metnine düş (lblContentText).
    #    Baştaki gizli 'display:none' watermark span'ini (UUID + "Commercial use...")
    #    silip öyle alıyoruz, yoksa çöp metin gelir.
    if not pdf_text.strip():
        content_span = soup_d.find("span", id=re.compile("lblContentText"))
        if content_span:
            hidden = content_span.find("span", style=re.compile(r"display\s*:\s*none"))
            if hidden:
                hidden.decompose()
            pdf_text = content_span.get_text("\n", strip=True)

    record = {
        "noticeId":          notice_id,
        "publicationName":   get_span("lblPubName1"),
        "publicationCity":   get_span("lblCity"),
        "publicationState":  get_span("lblState"),
        "publicationCounty": get_span("lblCounty"),
        "publicationDate":   get_span("lblPublicationDAte"),
        "authenticationNo":  get_span("lblNoticeAuthenticationNo"),
        "publicationUrl":    pub_url_tag.get("href", "") if pub_url_tag else "",
        "pdfText":           pdf_text,
        "scraped_at":        datetime.datetime.now(colorado_tz).strftime("%Y-%m-%d %H:%M"),
    }
    if record["publicationCounty"].strip().lower() not in ALLOWED_COUNTIES:
        print(f"  ⏭  Atlandı — {record['publicationCounty']} (Chester/Delaware dışı)")
        continue
    results.append(record)

    print(f"  ✓ {record['publicationCity']} | pdf={'✓' if pdf_text else '✗'}")
    
    # Her 50 kayıtta CSV'ye yaz
    if len(results) % 50 == 0:
        with open("pa_notices.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)
        print(f"  💾 {len(results)} kayıt kaydedildi")


# ══════════════════════════════════════════
# ADIM 4: CSV'ye yaz
# ══════════════════════════════════════════


with open("pa_notices.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=keys)
    writer.writeheader()
    writer.writerows(results)

print(f"\n{'='*50}")
print(f"✓ {len(results)} notice işlendi")
print(f"✗ {len(failed_ids)} notice başarısız: {failed_ids}")
print(f"pa_notices.csv kaydedildi.")


webhook_data = [
    {
        # Müşterinin istediği alanlar
        "noticeContent":   r["pdfText"],
        "publicationCity": r["publicationCity"],
        "publicationCounty": r["publicationCounty"],
        "publicationState": r["publicationState"],
        "publicationUrl":  r["publicationUrl"],
        # Bizim eklediğimiz ekstra alanlar
        "noticeId":        r["noticeId"],
        "publicationName": r["publicationName"],
        "publicationDate": r["publicationDate"],
        "authenticationNo": r["authenticationNo"],
        "scraped_at":      r["scraped_at"],
    }
    for r in results
]

if webhook_data:
    for i in range(0, len(webhook_data), BATCH_SIZE):
        batch = webhook_data[i:i + BATCH_SIZE]
        resp  = requests.post(WEBHOOK_URL, json=batch)
        print(f"Webhook batch {i//BATCH_SIZE + 1}: {resp.status_code} ({len(batch)} kayıt)")
        time.sleep(1)
else:
    print("Webhook: Gönderilecek veri yok")



import json

with open("pa_notices.json", "w", encoding="utf-8") as f:
    json.dump(webhook_data, f, ensure_ascii=False, indent=2)

print("pa_notices.json kaydedildi.")