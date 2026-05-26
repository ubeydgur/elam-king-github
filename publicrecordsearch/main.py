import requests, time, re, csv
from pathlib import Path
from bs4 import BeautifulSoup
import pytz
import datetime
from datetime import timedelta
import json
import os

TOKEN       = os.environ["DECODO_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
BATCH_SIZE  = 100


API_URL     = "https://scraper-api.decodo.com/v2/scrape"
TARGET_URL  = "https://publicrecordsearch.elpasoco.com/RealEstate/SearchEntry.aspx"
RESULTS_URL = "https://publicrecordsearch.elpasoco.com/RealEstate/SearchResults.aspx"

SESSION_ID = f"elpaso_{int(time.time())}"

api_headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "authorization": f"Basic {TOKEN}",
}

MAX_RETRIES    = 5
SEARCH_RETRIES = 8

# ==========================================
# TARİH HESAPLAMA
# ==========================================
colorado_tz = pytz.timezone('America/Denver')
today_co    = datetime.datetime.now(colorado_tz)
date_to     = today_co.strftime("%m/%d/%Y")
date_from   = (today_co - timedelta(days=5)).strftime("%m/%d/%Y")

print(f"Aratılacak Tarihler: {date_from} - {date_to}")
print(f"Session: {SESSION_ID}")

# ==========================================
# SEARCH ACTIONS
# ==========================================
SEARCH_ACTIONS = [
    {"type": "wait_for_element", "selector": {"type": "xpath", "value": "//a[contains(text(), 'acknowledge')]"}, "timeout_s": 15},
    {"type": "click",            "selector": {"type": "xpath", "value": "//a[contains(text(), 'acknowledge')]"}},
    {"type": "wait_for_element", "selector": {"type": "css",   "value": "#cphNoMargin_f_ddcDateFiledFrom"}, "timeout_s": 20},
    {"type": "click",            "selector": {"type": "css",   "value": "#cphNoMargin_f_ddcDateFiledFrom input"}},
    {"type": "input",            "selector": {"type": "css",   "value": "#cphNoMargin_f_ddcDateFiledFrom input"}, "value": date_from},
    {"type": "wait", "wait_time_s": 1},
    {"type": "click",            "selector": {"type": "css",   "value": "#cphNoMargin_f_ddcDateFiledTo input"}},
    {"type": "input",            "selector": {"type": "css",   "value": "#cphNoMargin_f_ddcDateFiledTo input"}, "value": date_to},
    {"type": "wait", "wait_time_s": 1},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_72"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_48"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_98"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_57"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_88"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_41"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_46"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_0"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_35"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_2"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_45"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_3"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_67"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_18"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_74"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_97"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_73"}},
    {"type": "click", "selector": {"type": "css", "value": "#cphNoMargin_f_dclDocType_69"}},
    {"type": "wait", "wait_time_s": 2},
    {"type": "click",            "selector": {"type": "css",   "value": "#cphNoMargin_SearchButtons1_btnSearch"}},
    {"type": "wait_for_element", "selector": {"type": "xpath", "value": "//*[contains(text(), 'records found') or contains(text(), 'Criteria')]"}, "timeout_s": 30},
]


# ==========================================
# DETAY SAYFASI PARSE FONKSİYONU
# ==========================================
def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")

    def get_span(id_suffix):
        el = soup.find('span', id=re.compile(re.escape(id_suffix) + '$'))
        return el.get_text(strip=True) if el else ""

    instrument_no = get_span('txtInstrumentNo')
    date_recorded = get_span('DataLabel3')
    doc_type = get_span('Datalabel2') or get_span('DataLabel2') or get_span('datalabel2')

    grantors = []
    for i in range(20):
        last  = get_span(f'DataList11_ctl{i:02d}_lblGrantorLastName')
        if not last:
            break
        first = get_span(f'DataList11_ctl{i:02d}_lblGrantorFirstName')
        grantors.append(f"{last} {first}".strip())

    grantees = []
    for i in range(20):
        last  = get_span(f'Datalist1_ctl{i:02d}_lblGranteeLastName')
        if not last:
            break
        first = get_span(f'Datalist1_ctl{i:02d}_lblGranteeFirstName')
        grantees.append(f"{last} {first}".strip())


    # ── Legal Description & Property Address ──────────────────
    # 1. Tüm TD'lerden entry'leri topla, duplicate'leri ayıkla
    entries = []
    seen_keys = set()

    for td in soup.find_all('td'):
        if not td.find_all('span', id=lambda x: x and 'Hdr' in (x or '')):
            continue
        all_spans = td.find_all('span')
        entry = {}
        for i, sp in enumerate(all_spans):
            if 'Hdr' in sp.get('id', '') and i + 1 < len(all_spans):
                label = sp.get_text(strip=True).rstrip(':')
                value = all_spans[i + 1].get_text(strip=True)
                entry[label] = value

        # Sadece dolu alanlar üzerinden duplicate tespiti
        key = tuple(sorted((k, v) for k, v in entry.items() if v))
        if key and key not in seen_keys:
            seen_keys.add(key)
            entries.append(entry)

    # 2. property_address: sadece FREEFORM tipindeki gerçek adres
    addr_parts = []
    for entry in entries:
        if entry.get('Type', '').upper() == 'FREEFORM':
            val = entry.get('Freeform Legal', '')
            if val:
                addr_parts.append(val)

    property_address = ' -- '.join(addr_parts)

    # 3. legal_description: tüm detaylar (Plat name, Lot, Section vb.)
    legal_parts = []
    for entry in entries:
        legal_type = entry.get('Type', '').upper()
        fields = []
        skip = {'Type', 'Gov. Unit'}
        if legal_type == 'FREEFORM':
            val = entry.get('Freeform Legal', '')
            if val:
                fields.append(f"FREEFORM: {val}")
        else:
            fields.append(f"Type: {entry.get('Type', '')}")
            for label, value in entry.items():
                if label not in skip and value:
                    fields.append(f"{label}: {value}")
        if fields:
            legal_parts.append(' '.join(fields))

    legal_desc = ' -- '.join(legal_parts)

    return {
        'instrument_number': instrument_no,
        'date_recorded':     date_recorded,
        'document_type':     doc_type,
        'grantor':           ' | '.join(grantors),
        'grantee':           ' | '.join(grantees),
        'property_address': property_address,
        'legal_description': legal_desc,
        'county':            'El Paso, CO',
        'scraped_at':        today_co.strftime("%m/%d/%Y %I:%M %p"),
    }


# ==========================================
# YARDIMCI FONKSİYONLAR
# ==========================================
def do_search(session_id):
    for attempt in range(1, SEARCH_RETRIES + 1):
        print(f"  Deneme {attempt}/{SEARCH_RETRIES}...")
        r = requests.post(API_URL, json={
            "url": TARGET_URL,
            "headless": "html",
            "session_id": session_id,
            "browser_actions": SEARCH_ACTIONS,
        }, headers=api_headers)
        if r.ok:
            html = r.json().get("results", [{}])[0].get("content")
            if html and "records found" in html:
                return html
            print(f"  Sonuç yok, tekrar deneniyor...")
        else:
            print(f"  HTTP {r.status_code}, tekrar deneniyor...")
        time.sleep(30)
    return None


def fetch_page(session_id, page_num):
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.post(API_URL, json={
            "url": f"{RESULTS_URL}?pg={page_num}",
            "headless": "html",
            "session_id": session_id,
        }, headers=api_headers)
        if r.ok:
            html = r.json().get("results", [{}])[0].get("content")
            if html and "records found" in html:
                return html
        time.sleep(5)
    return None


def extract_ids(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    return [
        cell.text.strip()
        for cell in soup.find_all("td", class_="igede12b9d")
        if cell.text.strip().startswith("OPR")
    ]


def fetch_detail(session_id, g_id):
    link = f"{RESULTS_URL}?global_id={g_id}&type=dtl"
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.post(API_URL, json={
            "url": link,
            "headless": "html",
            "session_id": session_id,
            "browser_actions": [
                {"type": "wait_for_element", "selector": {"type": "xpath",
                 "value": "//*[contains(text(), 'Details') or contains(text(), 'Grantor')]"},
                 "timeout_s": 20}
            ]
        }, headers=api_headers)
        if r.ok:
            html = r.json().get("results", [{}])[0].get("content")
            if not html:
                print(f"    Deneme {attempt}: İçerik boş")
            elif "acknowledge" in html and "records found" not in html:
                print(f"    Deneme {attempt}: Login sayfası")
            elif "records found" in html:
                print(f"    Deneme {attempt}: Arama sonuç sayfası döndü")
            else:
                return html
        else:
            print(f"    Deneme {attempt}: HTTP {r.status_code}")
        time.sleep(5)
    return None


# ==========================================
# AŞAMA 1: Arama
# ==========================================
print(f"\n[AŞAMA 1] Arama yapılıyor...")
html_p1 = do_search(SESSION_ID)
if not html_p1:
    print("KRİTİK HATA: Arama başarısız.")
    exit(1)

match       = re.search(r'(\d+)\s*records found', html_p1)
total_records = int(match.group(1)) if match else 0
soup_p1     = BeautifulSoup(html_p1, "html.parser")
sel_el      = soup_p1.find('select', {'name': lambda x: x and 'ItemList' in x if x else False})
total_pages = len(sel_el.find_all('option')) if sel_el else 1
print(f"  Toplam kayıt: {total_records} | Toplam sayfa: {total_pages}")


# ==========================================
# AŞAMA 2-3: Her sayfa → ID çıkar → detay çek → parse
# ==========================================
CSV_FILE    = "elpaso_records.csv"
CSV_COLUMNS = [
    'instrument_number', 'date_recorded', 'document_type',
    'grantor', 'grantee', 'property_address', 'legal_description', 'county', 'scraped_at'
]

records    = []
record_num = 0
failed_ids = []

# Session takip değişkenleri
consecutive_login_fails = 0
last_failed_id          = None

for page_num in range(1, total_pages + 1):
    print(f"\n{'='*50}")
    print(f"[SAYFA {page_num}/{total_pages}]")

    if page_num == 1:
        page_html = html_p1
    else:
        print(f"  ?pg={page_num} ile gidiliyor...")
        page_html = fetch_page(SESSION_ID, page_num)
        if not page_html:
            print(f"  UYARI: Sayfa {page_num} alınamadı, atlanıyor!")
            continue

    page_ids = extract_ids(page_html)
    print(f"  {len(page_ids)} kayıt bulundu.")

    

    # Bu değişkeni döngü başında tanımla:
    consecutive_login_fails = 0

    # Detail fetch bloğunu şu şekilde değiştir:
    for g_id in page_ids:
        record_num += 1
        print(f"  [{record_num}/{total_records}] {g_id}...", end=" ", flush=True)

        detail_html = fetch_detail(SESSION_ID, g_id)

        if detail_html is None:
            consecutive_login_fails += 1
            # 2 ardışık başarısızlık = session sona erdi
            if consecutive_login_fails >= 2:
                print(f"\n  ⚠️  Session sona erdi! Yeniden oturum açılıyor...")
                new_html = do_search(SESSION_ID)
                if new_html:
                    # Bulunduğumuz sayfaya geri dön
                    if page_num > 1:
                        fetch_page(SESSION_ID, page_num)
                    consecutive_login_fails = 0
                    print(f"  Session yenilendi, sayfa {page_num}'e dönüldü. Tekrar deneniyor...")
                    detail_html = fetch_detail(SESSION_ID, g_id)

            if detail_html is None:
                failed_ids.append(g_id)
                print(f"BAŞARISIZ")
            else:
                consecutive_login_fails = 0
                row = parse_detail(detail_html)
                records.append(row)
                print(f"✓  {row['document_type']} | {row['grantee'][:40]}")
                if len(records) % 50 == 0:
                    with open(CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
                        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                        writer.writeheader()
                        writer.writerows(records)
                    print(f"  💾 {len(records)} kayıt kaydedildi")
        else:
            consecutive_login_fails = 0
            row = parse_detail(detail_html)
            records.append(row)
            print(f"✓  {row['document_type']} | {row['grantee'][:40]}")
            if len(records) % 50 == 0:
                with open(CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                    writer.writeheader()
                    writer.writerows(records)
                print(f"  💾 {len(records)} kayıt kaydedildi")

        time.sleep(2)


# ==========================================
# CSV KAYDET
# ==========================================
print(f"\n{'='*50}")
if records:
    with open(CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(records)
    print(f"CSV kaydedildi: {CSV_FILE}  ({len(records)} kayıt)")
else:
    print("Hiç kayıt toplanamadı.")

if failed_ids:
    print(f"Başarısız olan {len(failed_ids)} ID: {failed_ids}")

print(f"\nToplam: {len(records)} başarılı / {len(failed_ids)} başarısız / {total_records} beklenen")


if records:
    # Webhook — 100'er 100'er gönder
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        resp  = requests.post(WEBHOOK_URL, json=batch)
        print(f"Webhook batch {i//BATCH_SIZE + 1}: {resp.status_code} ({len(batch)} kayıt)")
        time.sleep(1)

    # JSON kaydet
    with open("elpaso_records.json", "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print("elpaso_records.json kaydedildi.")
else:
    print("Webhook/JSON: Gönderilecek veri yok")
