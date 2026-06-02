import requests, time, re, csv
from pathlib import Path
from bs4 import BeautifulSoup
import pytz
import datetime
from datetime import timedelta
import json
import os
import sys
import itertools

TOKEN       = os.environ["DECODO_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
BATCH_SIZE  = 100


API_URL     = "https://scraper-api.decodo.com/v2/scrape"
TARGET_URL  = "https://publicrecordsearch.elpasoco.com/RealEstate/SearchEntry.aspx"
RESULTS_URL = "https://publicrecordsearch.elpasoco.com/RealEstate/SearchResults.aspx"

api_headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "authorization": f"Basic {TOKEN}",
}

MAX_RETRIES    = 5
SEARCH_RETRIES = 8

# ── Cascade kontrolü (Yöntem 2: cooldown + içeride yeniden başla) ────
# Decodo bazen bozuk session lease veriyor — arama OK ama detaylar anında
# fail. Bu durumda refresh etmek de işe yaramıyor (cascade). Aşağıdaki
# limitler cascade'i tespit edip 120sn bekleyip taze başlatır.
MAX_REFRESHES_PER_ROUND = 3   # 1 round'da kaç refresh denenecek
MAX_ROUNDS              = 3   # toplam kaç round (cooldown'lu deneme)
COOLDOWN_SEC            = 120 # round'lar arası bekleme süresi

# ── Session yönetimi ─────────────────────────────────────
# FIX #1: Session "failed" olunca aynı id'yle tekrar denemek işe yaramıyordu.
# Her refresh'te YENİ session_id üretiyoruz. itertools.count, aynı saniye içinde
# çağrılsa bile benzersizlik garantisi veriyor.
_session_counter = itertools.count(1)

def new_session_id():
    return f"elpaso_{int(time.time())}_{next(_session_counter)}"

SESSION_ID = new_session_id()

# Ardışık fail sayacı — SADECE success'te veya refresh sonrası sıfırlanır,
# sayfa sınırında DEĞİL (eski bug buydu).
consecutive_fails = 0

# Cascade kontrolü sayaçları (Yöntem 2)
# refresh_count: bu round'da kaç refresh yapıldı (başarılı record'ta sıfırlanır)
# round_number: kaçıncı round'dayız (cooldown sonrası artar)
refresh_count = 0
round_number  = 1


class DecodoExhausted(Exception):
    """Tüm round'lar tükendi, Decodo session düzelmiyor.
    Yakalandığında eldeki kayıtlar yine de kaydedilip webhook'a gönderilir."""
    pass

# ==========================================
# TARİH HESAPLAMA
# ==========================================
colorado_tz = pytz.timezone('America/Denver')
today_co    = datetime.datetime.now(colorado_tz)
date_to     = today_co.strftime("%m/%d/%Y")
date_from   = (today_co - timedelta(days=6)).strftime("%m/%d/%Y")

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
    # FIX: Eski xpath //* tüm DOM'u tarıyordu (yavaş). TotalRows span'i sadece
    # arama başarılı olunca render oluyor — daha temiz signal.
    {"type": "wait_for_element", "selector": {"type": "css", "value": "#cphNoMargin_cphNoMargin_SearchCriteriaTop_TotalRows"}, "timeout_s": 30},
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


def detail_has_content(html):
    """FIX #2: Geçerli bir detay sayfası mı?
    Session ölünce gelen sayfa parse'ta tüm alanları boş çıkarıyor ve eski kod
    bunu 'başarılı' sayıp çöp kayıt gönderiyordu (kayıt 95-100 vakası).
    instrument_number her gerçek kayıtta dolu olur — bunu kontrol ediyoruz."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find('span', id=re.compile('txtInstrumentNo$'))
    return bool(el and el.get_text(strip=True))


# ==========================================
# YARDIMCI FONKSİYONLAR
# ==========================================
def do_search(session_id):
    for attempt in range(1, SEARCH_RETRIES + 1):
        print(f"  Deneme {attempt}/{SEARCH_RETRIES}...")
        try:
            r = requests.post(API_URL, json={
                "url": TARGET_URL,
                "headless": "html",
                "session_id": session_id,
                "browser_actions": SEARCH_ACTIONS,
            }, headers=api_headers, timeout=180)
        except Exception as e:
            print(f"  İstek hatası: {e}, tekrar deneniyor...")
            time.sleep(30)
            continue
        if r.ok:
            html = r.json().get("results", [{}])[0].get("content")
            if html and "records found" in html:
                return html
            print(f"  Sonuç yok, tekrar deneniyor...")
        else:
            print(f"  HTTP {r.status_code}, tekrar deneniyor...")
        time.sleep(30)
    return None


def refresh_session(page_num=None):
    """FIX #1: Yeni session_id üret, arama yap, gerekirse sayfaya dön.
    Başarılıysa True döner. Global SESSION_ID'yi günceller.

    YÖNTEM 2 — Cascade kontrolü:
    Bu round'da MAX_REFRESHES_PER_ROUND kez refresh denenmiş ve hâlâ başarı
    yoksa (cascade tespit edildi), COOLDOWN_SEC bekleyip yeni round'a geç.
    MAX_ROUNDS'u da aşarsak DecodoExhausted raise et (run sonlanır)."""
    global SESSION_ID, consecutive_fails, refresh_count, round_number

    # Bu round'da limit aşıldı mı?
    if refresh_count >= MAX_REFRESHES_PER_ROUND:
        if round_number >= MAX_ROUNDS:
            # Tüm round'lar tükendi — pes
            print(f"\n  ⛔ {MAX_ROUNDS} round x {MAX_REFRESHES_PER_ROUND} refresh denendi, "
                  f"Decodo session düzelmiyor. Eldeki kayıtlar kaydedilip çıkılacak.")
            raise DecodoExhausted()

        # Cooldown — Decodo'ya nefes aldır, sonra yeni round
        print(f"\n  ⏸  Round {round_number} bitti ({MAX_REFRESHES_PER_ROUND} refresh boşa).")
        print(f"  💾 Şimdiye kadarki kayıtlar kaydediliyor...")
        checkpoint_save()
        print(f"  ⏳ {COOLDOWN_SEC}sn bekleniyor (Decodo nefes alsın diye)...")
        time.sleep(COOLDOWN_SEC)
        round_number += 1
        refresh_count = 0
        print(f"  🔄 Round {round_number}/{MAX_ROUNDS} başlıyor...")

    # Normal refresh
    refresh_count += 1
    SESSION_ID = new_session_id()
    print(f"  → Yeni session açılıyor: {SESSION_ID} "
          f"(round {round_number}, refresh {refresh_count}/{MAX_REFRESHES_PER_ROUND})")
    html = do_search(SESSION_ID)
    if not html:
        print("  → Session yenileme BAŞARISIZ")
        return False
    # Session'ı doğru sayfaya getir (best-effort; global_id detay çekimi
    # büyük ihtimalle sayfadan bağımsız ama garanti olsun diye).
    if page_num and page_num > 1:
        fetch_page(SESSION_ID, page_num)
    consecutive_fails = 0
    print(f"  → Session yenilendi.")
    return True


def fetch_page(session_id, page_num):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(API_URL, json={
                "url": f"{RESULTS_URL}?pg={page_num}",
                "headless": "html",
                "session_id": session_id,
            }, headers=api_headers, timeout=180)
        except Exception:
            time.sleep(5)
            continue
        if r.ok:
            html = r.json().get("results", [{}])[0].get("content")
            if html and "records found" in html:
                return html
        time.sleep(5)
    return None


def get_page_html(page_num):
    """FIX #4: Sayfa alınamazsa session ölmüş olabilir → yenile ve tekrar dene.
    Eski kod sayfayı komple atlıyordu (sayfa 5 kaybı vakası)."""
    html = fetch_page(SESSION_ID, page_num)
    if html:
        return html
    print(f"  Sayfa {page_num} alınamadı, session yenileniyor...")
    if refresh_session(page_num):
        html = fetch_page(SESSION_ID, page_num)
    return html


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
        try:
            r = requests.post(API_URL, json={
                "url": link,
                "headless": "html",
                "session_id": session_id,
                "browser_actions": [
                    {"type": "wait_for_element", "selector": {"type": "xpath",
                     "value": "//*[contains(text(), 'Details') or contains(text(), 'Grantor')]"},
                     "timeout_s": 20}
                ]
            }, headers=api_headers, timeout=180)
        except Exception as e:
            print(f"    Deneme {attempt}: istek hatası {e}")
            time.sleep(5)
            continue

        if not r.ok:
            print(f"    Deneme {attempt}: HTTP {r.status_code}")
            time.sleep(5)
            continue

        html = r.json().get("results", [{}])[0].get("content")
        if not html:
            print(f"    Deneme {attempt}: içerik boş")
            time.sleep(5)
            continue

        # FIX #2 + erken çıkış: Login sayfası = session ölü. Aynı session'la
        # retry anlamsız, hemen None dön ki çağıran refresh tetiklesin.
        if "acknowledge" in html and "records found" not in html:
            print(f"    Login sayfası (session ölü)")
            return None
        if "records found" in html:
            print(f"    Deneme {attempt}: sonuç sayfası döndü")
            time.sleep(5)
            continue

        # Gerçek detay içeriği var mı? (çöp kayıt engelleme)
        if detail_has_content(html):
            return html
        print(f"    Deneme {attempt}: boş detay (içerik doğrulanamadı)")
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

soup_p1     = BeautifulSoup(html_p1, "html.parser")

# FIX #3: total_records artık doğrudan TotalRows span'inden okunuyor.
# Eski regex r'(\d+)\s*records found' çalışmıyordu çünkü sayı ile "records found"
# ayrı span'larda, aralarında HTML var.
total_records = 0
tr_el = soup_p1.find('span', id=re.compile('TotalRows'))
if tr_el:
    digits = re.sub(r'\D', '', tr_el.get_text())
    total_records = int(digits) if digits else 0

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


def checkpoint_save():
    """Ara kayıt — her 50 kayıtta CSV'ye yaz."""
    with open(CSV_FILE, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(records)
    print(f"  💾 {len(records)} kayıt kaydedildi")


def process_id(g_id, page_num):
    """Bir g_id'yi işle. Başarılıysa records'a ekler ve True, değilse False döner.
    Session ölümünü tespit edince refresh edip bir kez daha dener."""
    global consecutive_fails, refresh_count

    detail_html = fetch_detail(SESSION_ID, g_id)

    if detail_html is None:
        consecutive_fails += 1
        # 2 ardışık fail = session öldü
        if consecutive_fails >= 2:
            print(f"\n  ⚠️  Session öldü ({consecutive_fails} ardışık fail), yenileniyor...")
            if refresh_session(page_num):  # consecutive_fails'i 0'lar
                detail_html = fetch_detail(SESSION_ID, g_id)

    if detail_html is None:
        return False

    # Başarı: hem consecutive_fails'i hem refresh_count'u sıfırla.
    # refresh_count'un sıfırlanması cascade tespitini doğru tutuyor —
    # "ilerleme kaydedilmeden yapılan ardışık refresh" sayılıyor.
    consecutive_fails = 0
    refresh_count = 0
    records.append(parse_detail(detail_html))
    return True


# YÖNTEM 2: Ana döngü + retry pass'i try/except DecodoExhausted ile sarıyoruz.
# Cascade tespit edildiğinde 3 round x 3 refresh tükenirse exception fırlar;
# eldeki kayıtlar yine de kaydedilip webhook'a gönderilir, sonra exit(1).
exhausted = False
try:
    for page_num in range(1, total_pages + 1):
        print(f"\n{'='*50}")
        print(f"[SAYFA {page_num}/{total_pages}]")

        if page_num == 1:
            page_html = html_p1
        else:
            print(f"  ?pg={page_num} ile gidiliyor...")
            page_html = get_page_html(page_num)
            if not page_html:
                print(f"  UYARI: Sayfa {page_num} session yenilemeye rağmen alınamadı, atlanıyor!")
                continue

        page_ids = extract_ids(page_html)
        print(f"  {len(page_ids)} kayıt bulundu.")

        for g_id in page_ids:
            record_num += 1
            print(f"  [{record_num}/{total_records}] {g_id}...", end=" ", flush=True)

            if process_id(g_id, page_num):
                row = records[-1]
                print(f"✓  {row['document_type']} | {row['grantee'][:40]}")
                if len(records) % 50 == 0:
                    checkpoint_save()
            else:
                failed_ids.append(g_id)
                print(f"BAŞARISIZ")

            time.sleep(2)


    # ==========================================
    # AŞAMA 4: Başarısız ID'ler için retry pass
    # ==========================================
    # FIX #5: Eski kod failed_ids'i sadece print ediyordu. Artık taze session'la
    # bir tur daha deniyoruz. global_id detay çekimi sayfadan bağımsız olduğu için
    # hangi sayfada olduklarını bilmemize gerek yok.
    if failed_ids:
        print(f"\n{'='*50}")
        print(f"[RETRY] {len(failed_ids)} başarısız ID tekrar deneniyor...")
        retry_targets = failed_ids[:]
        failed_ids = []

        if refresh_session():
            for g_id in retry_targets:
                print(f"  RETRY {g_id}...", end=" ", flush=True)
                detail_html = fetch_detail(SESSION_ID, g_id)
                if detail_html is None:
                    # Bir kez daha taze session dene
                    if refresh_session():
                        detail_html = fetch_detail(SESSION_ID, g_id)
                if detail_html is not None:
                    records.append(parse_detail(detail_html))
                    print(f"✓  {records[-1]['document_type']}")
                else:
                    failed_ids.append(g_id)
                    print("BAŞARISIZ")
                time.sleep(2)
        else:
            print("  Retry için session açılamadı, atlanıyor.")
            failed_ids = retry_targets

except DecodoExhausted:
    exhausted = True
    print(f"\n{'='*50}")
    print("⛔ Decodo cascade — eldeki kayıtlar kaydediliyor ve webhook'a gönderiliyor.")
    print("   Bir sonraki cron run veya manuel re-run muhtemelen düzelir.")


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
        try:
            resp = requests.post(WEBHOOK_URL, json=batch, timeout=120)
            ok = 200 <= resp.status_code < 300
            flag = "✓" if ok else "⚠️"
            print(f"{flag} Webhook batch {i//BATCH_SIZE + 1}: {resp.status_code} ({len(batch)} kayıt)")
            if not ok:
                # 2xx değilse açıkça uyar. 524 = Cloudflare timeout, n8n'e
                # ulaşmış ama cevap dönmemiş olabilir — n8n log'undan teyit et.
                print(f"    UYARI: 2xx değil. Veri n8n'e düştü mü belirsiz, n8n execution log'unu kontrol et.")
        except Exception as e:
            print(f"⚠️ Webhook batch {i//BATCH_SIZE + 1} hatası: {e}")
        time.sleep(1)

    # JSON kaydet
    with open("elpaso_records.json", "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print("elpaso_records.json kaydedildi.")
else:
    print("Webhook/JSON: Gönderilecek veri yok")


# YÖNTEM 2: Decodo cascade ile çıktıysak exit(1) ile son ver. Böylece
# GitHub Actions job'ı "failed" görür, bir sonraki cron temiz başlar.
if exhausted:
    print("\n⛔ Run incomplete (Decodo cascade). exit(1)")
    sys.exit(1)