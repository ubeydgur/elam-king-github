import requests, time, os
import datetime, pytz

# ── Kimlik bilgileri (lokal test için; GitHub'a alırken env/secret yapacaksın) ──

CAPTCHA_KEY = os.environ["CAPTCHA_KEY"]
PROXY_USER  = os.environ["DECODO_PROXY_USER"]
PROXY_PASS  = os.environ["DECODO_PROXY_PASS"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

PROXY = f"http://{PROXY_USER}:{PROXY_PASS}@us.decodo.com:10001"   

BASE      = "https://chestercountypa-web.tylerhost.net"
SEARCH_ID = "DOCSEARCH2050S2"
DISCLAIMER_URL = f"{BASE}/web/user/disclaimer"
CHECKHUMAN_URL = f"{BASE}/web/checkHuman"
SEARCHPOST_URL = f"{BASE}/web/searchPost/{SEARCH_ID}"
RESULTS_URL    = f"{BASE}/web/searchResults/{SEARCH_ID}"
SITEKEY   = "6LemVGAUAAAAAB_iW1wbaE4_s0Z5SoSakm6GI8St"

# Client'ın 19 hedef doküman tipi (kod, isim)
DOC_TYPES = [
    ("ASM","Assignment Of Mortgage"), ("BLK","Blanket Document"),
    ("CONDEM","Condemnation (MSC)"), ("COU","Court Order (MSC)"),
    ("DSC","Declaration (MSC)"), ("DST","Declaration - Taxable (MST)"),
    ("MSC","Miscellaneous"), ("MSA","Miscellaneous W/ Aopc"),
    ("MST","Miscellaneous With Taxes"), ("MTG","Mortgage"),
    ("NOT","Notice (MSC)"), ("POA","Power Of Attorney"),
    ("QCD","Quit Claim Deed"), ("WRA","Writ Of Assistance"),
    ("SHD","Sheriff's Deed"), ("STD","State Decree (Fees Waived)"),
    ("TXC","Tax Claim"), ("UCC","UCC"), ("UFN","Unused File Number"),
]

# Tarih — PA = Eastern, ~1 gün lookback (dün→bugün)
tz = pytz.timezone("America/New_York")
today = datetime.datetime.now(tz)
start_date = (today - datetime.timedelta(days=1)).strftime("%-m/%-d/%Y")
end_date   = today.strftime("%-m/%-d/%Y")

s = requests.Session()
s.proxies = {"http": PROXY, "https": PROXY}
s.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "ajaxRequest": "true",
    "Origin": BASE,
    "Referer": DISCLAIMER_URL,
})


def solve_captcha(pageurl, sitekey, retries=5):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post("http://2captcha.com/in.php", data={
                "key": CAPTCHA_KEY, "method": "userrecaptcha",
                "googlekey": sitekey, "pageurl": pageurl, "json": 1})
            cid = resp.json().get("request")
        except Exception as e:
            print(f"  2captcha gönderim hatası: {e}"); time.sleep(10); continue
        if not cid:
            print(f"  2captcha boş yanıt: {resp.text}"); time.sleep(10); continue
        for _ in range(30):
            time.sleep(5)
            try:
                r = requests.get(f"http://2captcha.com/res.php?key={CAPTCHA_KEY}&action=get&id={cid}&json=1")
                if r.json().get("status") == 1:
                    return r.json()["request"]
            except Exception as e:
                print(f"  2captcha polling hatası: {e}")
        print(f"  Token alınamadı (deneme {attempt}/{retries})"); time.sleep(10)
    return None


# 0) Proxy sağlaması — çıkış IP'si US mi?
try:
    print(f"Proxy IP: {s.get('https://ip.decodo.com/json', timeout=60).json()}")
except Exception as e:
    print(f"Proxy IP kontrol hatası: {e}")

# 1) Disclaimer → JSESSIONID
print("Disclaimer alınıyor...")
s.get(DISCLAIMER_URL, timeout=60)

# 2) reCAPTCHA çöz
print("reCAPTCHA çözülüyor...")
token = solve_captcha(DISCLAIMER_URL, SITEKEY)
if not token:
    raise SystemExit("KRİTİK: captcha çözülemedi")

# 3) checkHuman
r = s.post(CHECKHUMAN_URL, data={"g-recaptcha-response": token}, timeout=60)
print(f"checkHuman: {r.status_code} → {r.text[:80]}")

# 4) accept → disclaimerAccepted cookie
r = s.post(DISCLAIMER_URL, timeout=60)
print(f"accept: {r.status_code} | cookies: {s.cookies.get_dict()}")

# 4.5) Arama formu sayfası — server-side context kurulsun + referer düzelsin
SEARCH_PAGE_URL = f"{BASE}/web/search/{SEARCH_ID}"
r = s.get(SEARCH_PAGE_URL, timeout=60)
print(f"search page: {r.status_code} ({len(r.text)} byte)")
s.headers["Referer"] = SEARCH_PAGE_URL

# 5) searchPost — tam form (boş alanlar + tarih + 19 doküman tipi)
payload = [
    ("field_BothNamesID-containsInput","Contains Any"), ("field_BothNamesID",""),
    ("field_GrantorID-containsInput","Contains Any"), ("field_GrantorID",""),
    ("field_GranteeID-containsInput","Contains Any"), ("field_GranteeID",""),
    ("field_DocumentNumberID",""),
    ("field_BookPageID_DOT_Book",""), ("field_BookPageID_DOT_Page",""),
    ("field_RecordingDateID_DOT_StartDate", start_date),
    ("field_RecordingDateID_DOT_EndDate", end_date),
    ("field_UPIID",""),
    ("field_PlattedLegalID_DOT_Subdivision-containsInput","Contains Any"),
    ("field_PlattedLegalID_DOT_Subdivision",""),
    ("field_PlattedLegalID_DOT_Lot",""), ("field_LegalRemarksID",""),
]
for code, name in DOC_TYPES:
    payload.append(("field_selfservice_documentTypes-holderInput", code))
    payload.append(("field_selfservice_documentTypes-holderValue", name))
payload += [
    ("field_selfservice_documentTypes-containsInput","Contains Any"),
    ("field_selfservice_documentTypes",""),
    ("field_UseAdvancedSearch",""),
]

print(f"Arama: {start_date} - {end_date}, {len(DOC_TYPES)} tip")
r = s.post(SEARCHPOST_URL, data=payload, timeout=120,
           headers={"Accept": "application/json, text/javascript, */*; q=0.01"})

print(f"searchPost: {r.status_code} ({len(r.text)} byte)")

# 6) Sonuç sayfası — tarayıcı navigasyonu gibi (AJAX header'ları OLMADAN)
r = s.get(RESULTS_URL, timeout=120, headers={
    "X-Requested-With": None,
    "ajaxRequest": None,
    "Referer": SEARCHPOST_URL,
})
print(f"searchResults: {r.status_code}")


# ── Sonuçları CSV export'tan çek ──────────────────────────────
r = s.get(f"{BASE}/web/viewSearchResultsReport/{SEARCH_ID}/CSV", timeout=120, headers={
    "X-Requested-With": None, "ajaxRequest": None, "Referer": RESULTS_URL,
})
print(f"CSV export: {r.status_code} ({len(r.text)} byte)")

import csv, io, json
all_rows = list(csv.reader(io.StringIO(r.text)))
# satır 0: filtre özeti, satır 1: kolon adları, 2+: veri
header = all_rows[1] if len(all_rows) > 1 else []
idx = {name: i for i, name in enumerate(header)}
def col(row, name):
    i = idx.get(name)
    return row[i].strip() if (i is not None and i < len(row)) else ""

records = []
for row in all_rows[2:]:
    if not any(c.strip() for c in row):
        continue
    records.append({
        "instrument_number": col(row, "Document Number"),
        "date_recorded":     col(row, "Recording Date"),
        "document_type":     col(row, "Description"),
        "grantor":           col(row, "Grantor"),
        "grantee":           col(row, "Grantee"),
        "property_address":  "",
        "legal_description": col(row, "Legal"),
        "book_page":         col(row, "Book Page"),
        "county":            "Chester, PA",
        "scraped_at":        today.strftime("%m/%d/%Y %I:%M %p"),
    })
print(f"Parse edilen kayıt: {len(records)}")

# ── Çıktı: CSV + JSON ─────────────────────────────────────────
CSV_COLUMNS = ["instrument_number","date_recorded","document_type","grantor","grantee",
               "property_address","legal_description","book_page","county","scraped_at"]
with open("chester_records.csv","w",newline="",encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=CSV_COLUMNS); w.writeheader(); w.writerows(records)
with open("chester_records.json","w",encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=2)
print(f"chester_records.csv + .json kaydedildi ({len(records)} kayıt)")

# ── Webhook (100'lük batch) — WEBHOOK_URL boşsa gönderilmez ────
if WEBHOOK_URL and records:
    for i in range(0, len(records), 100):
        batch = records[i:i+100]
        try:
            resp = requests.post(WEBHOOK_URL, json=batch, timeout=120)
            print(f"Webhook batch {i//100+1}: {resp.status_code} ({len(batch)} kayıt)")
        except Exception as e:
            print(f"Webhook batch {i//100+1} hatası: {e}")
        time.sleep(1)
else:
    print("Webhook URL boş — sadece dosyaya yazıldı, gönderilmedi.")


