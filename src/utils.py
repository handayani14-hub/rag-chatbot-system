# src/utils.py
"""
Utility Functions — Kumpulan fungsi bantu lintas modul: format currency
dan timestamp, deteksi intent, validasi SND, logging, timer, dan parsing.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import colorlog


# =========================================================================
# LOGGER — Supaya output terminal lebih mudah dibaca (pakai warna)
# =========================================================================

class Logger:
    """
    Custom logger dengan output berwarna di terminal.

    Warna membantu saat debugging — ERROR merah, WARNING kuning,
    INFO hijau, jadi langsung tahu mana yang perlu diperhatikan.
    """

    _logger = None  # Singleton: buat satu instance, pakai terus

    @staticmethod
    def setup(log_level: str = 'INFO', log_dir: str = 'logs') -> logging.Logger:
        """
        Setup logger dengan dua output:
        1. File log (semua level, untuk arsip penelitian)
        2. Terminal (INFO ke atas, pakai warna supaya enak dibaca)
        """
        if Logger._logger is not None:
            return Logger._logger  # Kalau sudah dibuat, pakai yang lama

        Path(log_dir).mkdir(exist_ok=True)

        logger = logging.getLogger('rag_chatbot')
        logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        logger.handlers.clear()

        # Format untuk file — lengkap dengan timestamp dan nama module
        file_formatter = logging.Formatter(
            '[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # Format untuk terminal — pakai warna biar enak dibaca
        console_formatter = colorlog.ColoredFormatter(
            '[%(cyan)s%(asctime)s%(reset)s] [%(log_color)s%(levelname)s%(reset)s] %(message)s',
            datefmt='%H:%M:%S',
            log_colors={
                'DEBUG':    'blue',
                'INFO':     'green',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'red,bg_white',
            }
        )

        # Simpan log ke file (berguna untuk audit trail penelitian)
        log_file = Path(log_dir) / f"rag_chatbot_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        # Tampilkan di terminal pakai warna
        console_handler = colorlog.StreamHandler()
        console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)

        Logger._logger = logger
        return logger

    @staticmethod
    def get_logger() -> logging.Logger:
        """Ambil logger yang sudah dibuat, atau buat baru kalau belum ada."""
        if Logger._logger is None:
            Logger._logger = Logger.setup()
        return Logger._logger


def get_logger() -> logging.Logger:
    """Shortcut supaya bisa langsung tulis get_logger() di file lain."""
    return Logger.get_logger()


# =========================================================================
# DETEKSI INTENT — Menentukan maksud dari pertanyaan user
# =========================================================================

def detect_query_intent(message: str) -> str:
    """
    Deteksi intent dari pertanyaan user berdasarkan kata kunci.

    Fungsi ini menentukan 'jalur' mana yang akan diambil oleh bot.
    Ada 6 kemungkinan intent:

    1. 'ringkasan_belum_lunas' → user tanya BERAPA/jumlah yang belum lunas
    2. 'ringkasan_saldo'       → user tanya total saldo/nominal rupiah
    3. 'ringkasan'             → user minta ringkasan umum / statistik
    4. 'daftar_pelanggan'      → user minta LIST nama-nama pelanggan
    5. 'status_pelanggan'      → user tanya status satu pelanggan tertentu
    6. 'general'               → fallback semantic search

    PENTING: Ringkasan harus dicek SEBELUM daftar_pelanggan karena beberapa
    kata kunci overlap (misal "belum bayar" ada di daftar, tapi "berapa yang
    belum bayar" harus masuk ringkasan_belum_lunas).

    Args:
        message: teks pertanyaan dari user (sebelum diproses)

    Returns:
        str: nama intent yang terdeteksi
    """
    if not message or not message.strip():
        return "general"

    text = message.lower().strip()

    # --- Intent 0: Sapaan / Greeting (B1) ---
    # Cek dulu sebelum intent lain karena sapaan biasanya pendek dan tidak mengandung kata kunci spesifik.
    # Match hanya jika sapaan adalah pesan utuh (bukan bagian dari kalimat panjang) agar tidak false-positive.
    keywords_sapaan = [
        "halo", "hai", "hi", "hallo", "helo", "hey", "hello",
        "selamat pagi", "selamat siang", "selamat sore", "selamat malam",
        "pagi", "siang", "sore", "malam",
        "assalamualaikum", "assalamu'alaikum", "salam",
        "permisi", "good morning", "good afternoon", "good evening", "good night",
    ]
    # Bersihkan emoji & tanda baca untuk pencocokan
    text_clean = re.sub(r'[^\w\s]', '', text).strip()
    if text_clean in keywords_sapaan or any(
        text_clean == k or text_clean.startswith(k + " ") for k in keywords_sapaan
    ):
        # Hanya match kalau seluruh pesan adalah sapaan (max 4 kata)
        if len(text_clean.split()) <= 4:
            return "sapaan"

    # --- Intent 1: Ringkasan khusus belum lunas ---
    # User tanya BERAPA yang belum lunas (angka/jumlah), bukan minta list nama
    keywords_ringkasan_bl = [
        "berapa pelanggan belum lunas", "berapa yang belum lunas",
        "total belum lunas", "jumlah belum lunas", "berapa tunggakan",
        "berapa customer belum", "berapa yang belum bayar",
        "total tunggakan", "total piutang",
        "ada berapa", "berapa total pelanggan", "berapa pelanggan saya",
        "berapa customer saya", "berapa jumlah pelanggan",
        "berapa pelanggan yang", "berapa customer yang",
        "berapa yang belom bayar", "berapa yang belim bayar",
        "berapa pelanggan belom", "berapa pelanggan belim",
        # English (S20)
        "how many unpaid", "how many customers are unpaid", "count unpaid",
        "number of unpaid", "how many haven't paid", "total unpaid customers",
    ]
    if any(k in text for k in keywords_ringkasan_bl):
        return "ringkasan_belum_lunas"

    # --- Intent 2: Ringkasan saldo / nominal ---
    # User fokus ke angka rupiah, bukan jumlah pelanggan
    keywords_saldo = [
        "total saldo", "berapa total saldo", "total tagihan saya",
        "nominal", "jumlah saldo", "saldo saya", "total uang",
        "total nilai", "berapa saldo saya",
        # Aggregate per sheet — cek SEBELUM daftar agar "billtri"/"billdu"/"billper"
        # tidak langsung ditangkap oleh keywords_daftar
        "jumlah tagihan saldo",
        "berapa saldo billtri", "berapa saldo billdu", "berapa saldo billper",
        "total saldo billtri", "total saldo billdu", "total saldo billper",
        "jumlah saldo billtri", "jumlah saldo billdu", "jumlah saldo billper",
        "berapa total billtri", "berapa total billdu", "berapa total billper",
        "total tagihan billtri", "total tagihan billdu", "total tagihan billper",
        # English (S20)
        "total balance", "total outstanding balance", "total amount owed",
        "total receivable",
    ]
    if any(k in text for k in keywords_saldo):
        return "ringkasan_saldo"

    # --- Intent 3: Ringkasan umum / statistik ---
    # User minta gambaran besar data tagihan mereka
    keywords_ringkasan = [
        "ringkasan", "summary", "statistik", "rekap",
        "rekap tagihan", "laporan", "overview", "gambaran",
        "semua tagihan", "my summary", "give me a summary", "my report",
    ]
    if any(k in text for k in keywords_ringkasan):
        return "ringkasan"

    # --- Intent 4: Query berbasis waktu/durasi (S8) — cek SEBELUM daftar ---
    # Bot tidak punya data historis durasi tunggakan → tolak dengan graceful message
    keywords_waktu = [
        "paling lama", "berapa lama", "sudah berapa lama", "durasi",
        "sejak kapan", "terlama", "lama nunggak", "lama belum bayar",
        "lama tidak bayar", "lama menunggak",
        # English
        "how long", "how long has", "longest overdue", "for how long",
        "how many days", "overdue the longest", "longest unpaid",
    ]
    if any(k in text for k in keywords_waktu):
        return "query_waktu"

    # --- Intent 5: Daftar pelanggan belum lunas ---
    # User minta LIST nama-nama pelanggan
    # CATATAN: "siapa" (tanpa konteks) dihapus — terlalu luas, menangkap "siapa PIC"
    keywords_daftar = [
        "daftar", "siapa saja", "list", "tampilkan semua",
        "sebutkan", "siapa pelanggan", "siapa customer", "mana saja", "customer saya",
        # Kata kunci informal / variasi bahasa
        "nunggak", "belum bayar", "belum lunas", "belom bayar", "belom lunas",
        "belim bayar", "belim lunas",
        "jatuh tempo", "outstanding", "yang nunggak", "yang belum",
        "yg nunggak", "yg belum", "yg belom", "yg belim",
        "pelanggan saya yang", "pelanggan saya yg", "customer yang",
        # Kata kunci sheet langsung (untuk multi-sheet query S14)
        "billper", "billdu", "billtri",
        "gabungkan", "dari billper", "dari billdu", "dari billtri",
        # English (S20)
        "show me", "show all", "list all", "get all",
        "unpaid customers", "unpaid customer", "overdue customers", "overdue bills",
        "customers who", "not paid yet",
    ]
    if any(k in text for k in keywords_daftar):
        return "daftar_pelanggan"

    # --- Intent 6: Status satu pelanggan tertentu (via kata kunci) ---
    # User tanya info detail tentang satu pelanggan spesifik
    keywords_status = [
        "status tagihan", "cek tagihan", "info pelanggan",
        "berapa saldo", "tagihan siapa", "status pembayaran",
        # Tambahan untuk query field spesifik (Bug 1+2)
        "jenis tagihan",
        "tagihan dari",
        "cek pelanggan",
        "cari pelanggan",
        # Tambahan untuk query PIC (Bug 2-L1)
        "siapa pic", "nama pic", "telepon pic", "nomor pic",
        "no telepon pic", "no hp pic", "nomor telepon pic",
        "siapa yang jadi pic", "siapa yang menjadi pic",
        # English (S20)
        "payment status", "billing status", "status of",
        "bill for", "invoice for", "check customer", "check billing",
        "who is the pic", "pic of", "pic for",
    ]
    if any(k in text for k in keywords_status):
        return "status_pelanggan"

    # --- Intent 5b: Nama pelanggan langsung (prefix badan usaha) ---
    # User langsung ketik nama perusahaan tanpa kata kunci status
    # Regex toleran titik dan spasi (B2): "PT. ABC", "PT ABC", "pt.abc"
    company_prefix_pattern = re.compile(
        r'^(ud|pt|cv|tb|pd|bpr|toko|apotek|klinik|rs|smk|sma|sd|smp|'
        r'puskesmas|yayasan|koperasi|bumdes)\.?\s+',
        re.IGNORECASE
    )
    if company_prefix_pattern.match(text):
        return "status_pelanggan"

    # --- Fallback: general ---
    return "general"


def detect_sheet_from_query(message: str) -> Optional[str]:
    """
    Deteksi sheet mana yang dimaksud user dari teks pertanyaan.

    Dipakai bersama detect_query_intent: kalau intent adalah daftar_pelanggan
    dan sheet bisa dideteksi dari query, langsung tampilkan tanpa sheet selector.

    Returns:
        'billper', 'billdu', 'billtri', atau None (tidak diketahui → tampilkan selector)
    """
    if not message:
        return None

    text = message.lower().strip()

    # --- Billdu: tagihan jatuh tempo ---
    keywords_billdu = [
        "billdu", "jatuh tempo", "lewat jatuh tempo", "sudah jatuh tempo",
        "overdue", "terlambat bayar", "telat bayar"
    ]
    if any(k in text for k in keywords_billdu):
        return "billdu"

    # --- Billtri: tunggakan tiga bulan ---
    keywords_billtri = [
        "billtri", "tiga bulan", "3 bulan", "tunggakan tiga", "tunggakan 3",
        "akan diputus", "akan diisolasi", "isolasi"
    ]
    if any(k in text for k in keywords_billtri):
        return "billtri"

    # --- Billper: tagihan periode berjalan ---
    keywords_billper = [
        "billper", "periode berjalan", "bulan ini", "tagihan berjalan",
        "tagihan sekarang", "periode ini"
    ]
    if any(k in text for k in keywords_billper):
        return "billper"

    # Tidak bisa disimpulkan → tampilkan selector
    return None


def detect_period_reference(message: str) -> Optional[str]:
    """
    Deteksi apakah query menyebut bulan/periode tertentu yang berbeda dari data aktif.

    Dipakai untuk memberi disclaimer saat user menanyakan data periode yang tidak
    tersedia — sistem hanya menyimpan satu snapshot periode tagihan aktif (S3).

    Returns:
        Nama bulan yang disebutkan (dikapitalisasi), atau None jika tidak ada.
    """
    if not message:
        return None

    text = message.lower()

    match = re.search(
        r'\b(januari|februari|maret|april|mei|juni|juli|agustus|'
        r'september|oktober|november|desember|'
        r'january|february|march|june|july|august|october|december)\b',
        text
    )
    if match:
        return match.group(1).capitalize()

    relative_refs = ['bulan lalu', 'bulan depan', 'tahun lalu', 'bulan kemarin']
    for ref in relative_refs:
        if ref in text:
            return ref

    return None


def detect_sheets_from_query(message: str) -> List[str]:
    """
    Deteksi SEMUA sheet yang dimaksud user dari satu kalimat pertanyaan.

    Berbeda dengan detect_sheet_from_query() yang hanya mengembalikan sheet
    pertama yang cocok, fungsi ini memeriksa semua sheet dan mengembalikan
    list — berguna untuk query multi-sheet seperti:
    "gabungkan billper dan billdu" → ['billper', 'billdu']

    Returns:
        List berisi kombinasi 'billper', 'billdu', 'billtri'.
        Kosong jika tidak ada sheet yang terdeteksi → tampilkan selector.
        Satu elemen jika satu sheet terdeteksi → langsung tampilkan.
        Dua/tiga elemen jika multi-sheet → pipeline_daftar_multi_sheet.
    """
    if not message:
        return []

    text = message.lower().strip()
    detected: List[str] = []

    keywords_billdu = [
        "billdu", "jatuh tempo", "lewat jatuh tempo", "sudah jatuh tempo",
        "overdue", "terlambat bayar", "telat bayar"
    ]
    if any(k in text for k in keywords_billdu):
        detected.append("billdu")

    keywords_billtri = [
        "billtri", "tiga bulan", "3 bulan", "tunggakan tiga", "tunggakan 3",
        "akan diputus", "akan diisolasi", "isolasi"
    ]
    if any(k in text for k in keywords_billtri):
        detected.append("billtri")

    keywords_billper = [
        "billper", "periode berjalan", "bulan ini", "tagihan berjalan",
        "tagihan sekarang", "periode ini"
    ]
    if any(k in text for k in keywords_billper):
        detected.append("billper")

    return detected


def is_all_sheets_request(message: str) -> bool:
    """
    Deteksi apakah user secara eksplisit minta data dari SEMUA kategori
    tagihan sekaligus (billper + billdu + billtri digabung), setara dengan
    klik tombol 'Keseluruhan' pada sheet selector.

    Tanpa fungsi ini, query seperti "semua periode" atau "seluruh kategori"
    tidak dikenali oleh detect_sheets_from_query() (yang hanya cocok dengan
    nama sheet spesifik), sehingga jatuh ke selector alih-alih langsung
    menampilkan data gabungan.

    Returns:
        True jika query menyebut permintaan untuk semua/seluruh sheet.
    """
    if not message:
        return False

    text = message.lower()

    if "keseluruhan" in text:
        return True

    all_words = ["semua", "seluruh"]
    scope_words = [
        "periode", "kategori", "sheet", "lembar kerja", "tagihan",
        "jenis tagihan", "data pelanggan",
    ]
    return any(a in text for a in all_words) and any(s in text for s in scope_words)


# =========================================================================
# VALIDASI FORMAT SND — Untuk routing query ke pipeline yang tepat
# =========================================================================

def is_snd_format(text: str) -> bool:
    """
    Cek apakah teks yang diketik user adalah SND (nomor layanan) yang valid.

    SND di data aktual memiliki 9-13 digit angka (bervariasi per area),
    contoh: 2232191116 (10 digit), 131123873518 (12 digit).
    Kalau user ketik angka 9-13 digit, bot langsung cari via SND exact match
    — tidak perlu embedding atau semantic search.

    Args:
        text: teks yang dikirim user

    Returns:
        True kalau 9-13 digit angka, False kalau bukan
    """
    cleaned = text.strip().replace(" ", "")  # Hapus spasi di awal/akhir/tengah
    return bool(re.fullmatch(r'\d{9,13}', cleaned))


def is_partial_snd(text: str) -> bool:
    """
    Cek apakah teks adalah angka tapi terlalu pendek untuk jadi SND (1-8 digit).

    Kalau user ketik '33150' (cuma 5 digit), bot perlu kasih tahu
    bahwa SND biasanya 10-12 digit — supaya user tidak bingung kenapa tidak ketemu.

    Args:
        text: teks yang dikirim user

    Returns:
        True kalau angka 1-8 digit (terlalu pendek untuk SND), False kalau bukan
    """
    cleaned = text.strip().replace(" ", "")
    return bool(re.fullmatch(r'\d{1,8}', cleaned))


def extract_customer_name_from_query(message: str) -> str:
    """
    Ekstrak nama pelanggan dari kalimat query user.

    Menghapus prefix umum seperti "apa status tagihan dari",
    "berapa saldo dari", dll. sehingga hanya nama pelanggan yang tersisa.

    Contoh:
        "Apa status tagihan dari DEPOT WAHYU?"  → "DEPOT WAHYU"
        "berapa saldo dari KLINIK MAMAN"        → "KLINIK MAMAN"
        "DEPOT WAHYU"                           → "DEPOT WAHYU" (tidak berubah)
    """
    if not message:
        return message

    text = message.strip().rstrip('?').strip()

    prefixes = [
        # Indonesian
        r'^apa status tagihan dari\s+',
        r'^apa status tagihan\s+',         # tanpa "dari" (Bug B1)
        r'^apa jenis tagihan dari\s+',
        r'^apa jenis tagihan\s+',
        r'^jenis tagihan dari\s+',
        r'^jenis tagihan\s+',
        r'^status tagihan dari\s+',
        r'^status tagihan\s+',
        r'^tagihan dari\s+',
        r'^tagihan\s+',
        r'^berapa saldo dari\s+',
        r'^berapa saldo\s+',
        r'^saldo dari\s+',
        r'^cari pelanggan dengan nama\s+',
        r'^cari pelanggan bernama\s+',
        r'^cari pelanggan atas nama\s+',
        r'^cari pelanggan\s+',
        r'^cek pelanggan dengan nama\s+',
        r'^cek pelanggan\s+',
        r'^info pelanggan\s+',
        r'^informasi dari\s+',
        # PIC queries — "siapa/nama/telepon PIC [dari] X" → ekstrak nama pelanggan (Bug B2-L1)
        r'^siapa pic dari\s+',
        r'^siapa pic\s+',
        r'^nama pic dari\s+',
        r'^nama pic\s+',
        r'^telepon pic dari\s+',
        r'^telepon pic\s+',
        r'^nomor pic dari\s+',
        r'^nomor pic\s+',
        r'^nomor telepon pic dari\s+',
        r'^nomor telepon pic\s+',
        r'^cek\s+',
        r'^cari\s+',
        r'^data\s+',
        # English (S20)
        r'^what is the (billing|payment) status (of|for)\s+',
        r'^(check|get|show) (the )?(billing|payment|invoice) (status )?(of|for)\s+',
        r'^payment status (of|for)\s+',
        r'^billing status (of|for)\s+',
        r'^status of\s+',
        r'^bill for\s+',
        r'^invoice for\s+',
        r'^check\s+',
        r'^show me (the )?(status|billing|invoice) (of|for)\s+',
    ]

    for pattern in prefixes:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE).strip()

    # Bersihkan sisa prefix penghubung yang mungkin tertinggal
    text = re.sub(r'^(dengan nama|atas nama|bernama|yang bernama|nama)\s+',
                  '', text, flags=re.IGNORECASE).strip()

    return text if text else message.strip()


# =========================================================================
# FORMAT TAMPILAN — Supaya output bot lebih rapi dan enak dibaca
# =========================================================================

def format_currency(amount: Any) -> str:
    """
    Ubah angka menjadi format Rupiah yang manusiawi.

    Contoh: 1500000 → 'Rp 1.500.000'

    Kenapa perlu fungsi ini?
    Karena data di database disimpan sebagai angka biasa,
    tapi saat ditampilkan ke user harus pakai format Indonesia
    (titik sebagai pemisah ribuan, bukan koma).
    """
    try:
        # Bersihkan dulu kalau ada karakter non-angka (misal data dari spreadsheet)
        if isinstance(amount, str):
            amount = amount.replace('.', '').replace(',', '.')
        nilai = float(amount)
        return f"Rp {nilai:,.0f}".replace(',', '.')
    except (TypeError, ValueError):
        return "Rp 0"


def format_timestamp(dt: Optional[datetime] = None) -> str:
    """
    Format datetime ke bahasa Indonesia yang ramah — untuk pesan bot.

    Contoh output: 'Senin, 26 Mei 2026 pukul 10:30 WIB'

    Sengaja pakai format panjang supaya pesan bot terasa lebih personal
    dan mudah dimengerti oleh sales di lapangan.
    """
    if dt is None:
        dt = datetime.now()

    hari_indo = {
        0: 'Senin', 1: 'Selasa', 2: 'Rabu',
        3: 'Kamis', 4: 'Jumat', 5: 'Sabtu', 6: 'Minggu'
    }
    bulan_indo = {
        1: 'Januari',   2: 'Februari', 3: 'Maret',
        4: 'April',     5: 'Mei',      6: 'Juni',
        7: 'Juli',      8: 'Agustus',  9: 'September',
        10: 'Oktober', 11: 'November', 12: 'Desember'
    }

    hari  = hari_indo[dt.weekday()]
    bulan = bulan_indo[dt.month]
    return f"{hari}, {dt.day} {bulan} {dt.year} pukul {dt.hour:02d}:{dt.minute:02d} WIB"


def format_currency_short(amount: Any) -> str:
    """
    Format Rupiah versi singkat — untuk tabel atau space yang terbatas.

    Contoh:
    - 1.500.000   → 'Rp 1,5 jt'
    - 500.000.000 → 'Rp 500 jt'
    - 1.200.000.000 → 'Rp 1,2 M'

    Berguna kalau ada banyak pelanggan dalam satu pesan
    dan tidak mau pesan terlalu panjang di Telegram.
    """
    try:
        if isinstance(amount, str):
            amount = amount.replace('.', '').replace(',', '.')
        nilai = float(amount)

        if nilai >= 1_000_000_000:
            return f"Rp {nilai / 1_000_000_000:.1f} M"
        elif nilai >= 1_000_000:
            return f"Rp {nilai / 1_000_000:.1f} jt"
        else:
            return f"Rp {nilai:,.0f}".replace(',', '.')
    except (TypeError, ValueError):
        return "Rp 0"


# =========================================================================
# TEXT PROCESSING — Bersihkan dan normalisasi teks
# =========================================================================

def clean_text(text: str) -> str:
    """
    Bersihkan teks dari spasi berlebih, newline, dan karakter aneh.

    Berguna sebelum teks dari user dikirim ke OpenAI atau disimpan ke DB,
    supaya tidak ada whitespace yang tidak perlu.
    """
    if not text:
        return ""
    # Gabungkan semua whitespace (spasi, tab, newline) jadi satu spasi
    return ' '.join(text.split()).strip()


def extract_sales_code_from_kcontact(kcontact: str) -> Optional[str]:
    """
    Ekstrak kode sales dari kolom KCONTACT di Google Sheets.

    KCONTACT adalah kolom semi-terstruktur yang formatnya tidak seragam.
    Ada 4 format yang perlu dikenali:

    Format 1: |MB20100|/Nama Usaha/0812345/email@example.com
              → ambil bagian ke-1 setelah split '|'  → MB20100

    Format 2: PSB;ISE228893;Nama Sales;0812345;Produk
              → ambil bagian ke-2 setelah split ';'  → ISE228893

    Format 3: AMRBS;205907;Nama;PIC - 085599;PSB INET
              → ambil bagian ke-2 setelah split ';'  → 205907

    Format 4: MD|MYDB-2025|SC18675/MC37653|Nama|0812345
              → ambil bagian ke-3, ambil sebelum '/' → SC18675

    Catatan: fungsi ini juga ada di google_sheets_loader.py,
    tapi disediakan di sini untuk dipakai di tempat lain kalau perlu.
    """
    if not kcontact or kcontact.strip() in ('N/A', '', 'nan'):
        return None

    text = str(kcontact).strip()

    # Format 1: dimulai dengan '|'
    if text.startswith('|'):
        parts = [p.strip() for p in text.split('|') if p.strip()]
        if parts:
            return parts[0]

    # Format 2: dimulai dengan 'PSB;'
    if text.startswith('PSB;'):
        parts = [p.strip() for p in text.split(';') if p.strip()]
        if len(parts) > 1:
            return parts[1]

    # Format 3: dimulai dengan 'AMRBS;'
    if text.startswith('AMRBS;'):
        parts = [p.strip() for p in text.split(';') if p.strip()]
        if len(parts) > 1:
            return parts[1]

    # Format 4: dimulai dengan 'MD|'
    if text.startswith('MD|'):
        parts = [p.strip() for p in text.split('|') if p.strip()]
        if len(parts) > 2:
            return parts[2].split('/')[0]

    # Fallback: pakai regex untuk cari pola MB/MN + 5 angka
    match = re.search(r'(M[BN]\d{5})', text)
    if match:
        return match.group(1)

    return None


def parse_json_safely(json_str: str, default: Any = None) -> Any:
    """
    Parse JSON string dengan aman — tidak crash kalau formatnya rusak.

    Berguna saat baca kolom metadata dari database yang kadang
    bisa berisi JSON tidak valid kalau ada kesalahan saat insert.
    """
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


# =========================================================================
# TIMER — Ukur berapa lama setiap proses berjalan
# =========================================================================

class Timer:
    """
    Stopwatch sederhana untuk ukur execution time.

    Bisa dipakai dua cara:
    1. Manual: timer.start() → ... → timer.stop() → timer.elapsed_ms()
    2. Context manager: with Timer() as t: ... → t.elapsed_ms()

    Kenapa perlu diukur?
    Response time adalah salah satu metrik yang dikumpulkan untuk BAB IV —
    menunjukkan bahwa sistem cukup responsif untuk dipakai sales di lapangan.
    Hasilnya disimpan ke conversation_log, TIDAK ditampilkan ke user.
    """

    def __init__(self, name: str = ""):
        self.name       = name
        self.start_time: Optional[datetime] = None
        self.end_time:   Optional[datetime] = None

    def start(self):
        """Mulai pencatatan waktu."""
        self.start_time = datetime.now()
        return self  # Biar bisa chaining: timer.start().something()

    def stop(self):
        """Hentikan pencatatan waktu."""
        self.end_time = datetime.now()
        return self

    def elapsed_ms(self) -> int:
        """Kembalikan selisih waktu dalam milidetik (integer)."""
        if self.start_time and self.end_time:
            delta = self.end_time - self.start_time
            return int(delta.total_seconds() * 1000)
        return 0

    def elapsed_seconds(self) -> float:
        """Kembalikan selisih waktu dalam detik (float)."""
        return self.elapsed_ms() / 1000.0

    # Context manager — supaya bisa pakai 'with Timer() as t:'
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def __str__(self) -> str:
        label = f"[{self.name}] " if self.name else ""
        return f"{label}{self.elapsed_seconds():.2f}s ({self.elapsed_ms()}ms)"


# =========================================================================
# HELPER LAIN — Fungsi kecil yang berguna di berbagai tempat
# =========================================================================

def truncate_string(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """
    Potong teks kalau terlalu panjang — berguna untuk preview di log.

    Args:
        text       : teks yang mau dipotong
        max_length : batas panjang maksimal
        suffix     : tanda bahwa teks dipotong (default '...')
    """
    if len(text) > max_length:
        return text[:max_length - len(suffix)] + suffix
    return text


def batch_list(items: List[Any], batch_size: int) -> List[List[Any]]:
    """
    Bagi list besar menjadi potongan-potongan kecil (batching).

    Dipakai saat generate embedding — tidak kirim semua 1551 data
    sekaligus ke OpenAI, tapi dibagi per batch supaya tidak timeout.

    Contoh: batch_list([1,2,3,4,5], 2) → [[1,2], [3,4], [5]]
    """
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def normalize_sales_code(sales_code: str) -> str:
    """
    Normalisasi format kode sales — uppercase dan hapus spasi.

    Supaya 'mb20100', 'MB20100', ' MB20100 ' semua dianggap sama.
    """
    return sales_code.strip().upper()


def dict_to_pretty(data: Dict, indent: int = 2) -> str:
    """
    Convert dictionary ke string yang rapi — berguna untuk debugging di log."""
    return json.dumps(data, indent=indent, ensure_ascii=False, default=str)


# =========================================================================
# MAIN — Test semua fungsi di atas
# =========================================================================

if __name__ == '__main__':
    print("=" * 60)
    print("TEST: Utils — Verifikasi Semua Fungsi")
    print("=" * 60)

    # Test format_currency
    print(f"\n[OK] format_currency(1500000) = {format_currency(1500000)}")
    print(f"[OK] format_currency('2500000') = {format_currency('2500000')}")
    print(f"[OK] format_currency_short(1500000) = {format_currency_short(1500000)}")

    # Test format_timestamp
    print(f"\n[OK] format_timestamp() = {format_timestamp()}")

    # Test is_snd_format & is_partial_snd
    test_cases = [
        ("2232191116",    True,  False),  # valid SND 10 digit (data aktual)
        ("131123873518",  True,  False),  # valid SND 12 digit (data aktual)
        ("3315000012345", True,  False),  # valid SND 13 digit
        ("33150",         False, True),   # partial SND (5 digit, terlalu pendek)
        ("PT ABC",        False, False),  # bukan angka
        ("12345678901234",False, False),  # terlalu panjang (14 digit)
    ]
    print("\n[OK] Test is_snd_format & is_partial_snd:")
    for text, exp_snd, exp_partial in test_cases:
        hasil_snd     = is_snd_format(text)
        hasil_partial = is_partial_snd(text)
        status        = "✓" if (hasil_snd == exp_snd and hasil_partial == exp_partial) else "✗"
        print(f"  {status} '{text}' → SND={hasil_snd}, Partial={hasil_partial}")

    # Test detect_query_intent
    test_queries = [
        ("Siapa saja pelanggan saya yang belum lunas?", "daftar_pelanggan"),
        ("Berapa saldo Rini Handayani?",                "status_pelanggan"),
        ("Berapa pelanggan yang belum lunas?",          "ringkasan_belum_lunas"),
        ("Berapa total saldo saya?",                    "ringkasan_saldo"),
        ("Ringkasan tagihan saya bulan ini",            "ringkasan"),
        ("Apa kabar?",                                  "general"),
        ("pelanggna saya yang belom bayar?",            "daftar_pelanggan"),
        ("berapa pelanggna saya yang belim bayar",      "daftar_pelanggan"),
        ("pelanggan saya yang belim lunas",             "daftar_pelanggan"),
    ]
    print("\n[OK] Test detect_query_intent:")
    for query_text, expected in test_queries:
        result = detect_query_intent(query_text)
        status = "✓" if result == expected else "✗"
        print(f"  {status} '{query_text[:40]}...' → {result}")

    # Test Timer
    import time
    print("\n[OK] Test Timer:")
    with Timer("sleep 0.1s") as t:
        time.sleep(0.1)
    print(f"  Hasil: {t}")

    print("\n" + "=" * 60)
    print("[OK] Semua test utils selesai.")
    print("=" * 60)