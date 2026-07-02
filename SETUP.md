# SETUP GUIDE - RAG Chatbot untuk Sistem Informasi Tagihan Pelanggan

Panduan ini memandu setup project dari awal sampai bot siap dijalankan.

**Waktu yang dibutuhkan:** ~30-45 menit untuk first-time setup

---

## **Prerequisites**

- [ ] Python 3.9 atau lebih tinggi (`python --version`)
- [ ] Git (untuk clone repository)
- [ ] Akun & credentials sudah siap:
  - [ ] Google Sheets API (Service Account JSON)
  - [ ] OpenAI API Key
  - [ ] Telegram Bot Token (dari @BotFather)
  - [ ] TiDB Cloud cluster + credentials

---

## **TAHAP 1: Clone Repository**

```bash
git clone https://github.com/handayani14-hub/rag-chatbot-system.git
cd rag-chatbot-system
```

Struktur folder yang akan Anda dapatkan:

```
rag-chatbot-system/
├── .env.example              # Template environment variables
├── .gitignore
├── requirements.txt          # Python dependencies
├── README.md
├── SETUP.md
├── src/                      # Kode utama sistem
│   ├── main_bot.py           # Entry point bot
│   ├── config.py             # Configuration loader
│   ├── rag_pipeline.py       # RAG logic (retrieve-augment-generate)
│   ├── telegram_bot.py       # Handler chat Telegram
│   ├── access_control.py     # Registrasi & RBAC
│   ├── tidb_client.py        # Database client
│   ├── embedding_generator.py
│   ├── google_sheets_loader.py
│   ├── initialize_db.py      # Setup tabel & load data awal
│   ├── metrics_collector.py  # Kumpulkan metrik evaluasi (BAB IV)
│   └── utils.py
├── scripts/                  # Script pendukung pengujian & maintenance
│   ├── benchmark_embedding.py
│   ├── export_test_data.py
│   ├── extract_test_results.py
│   └── reset_test_data.py
└── database/
    └── schema.sql             # TiDB schema (source of truth)
```

---

## **TAHAP 2: Setup Python Virtual Environment**

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**Mac/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

Berhasil jika muncul prefix `(venv)` di terminal.

---

## **TAHAP 3: Install Python Packages**

```bash
pip install -r requirements.txt
```

Jika ada error, pastikan venv sudah aktif (`(venv)` terlihat di terminal), lalu coba `pip install --upgrade pip` terlebih dahulu.

---

## **TAHAP 4: Setup Credentials (.env)**

### Langkah 4A: Copy Template

```bash
cp .env.example .env    # Mac/Linux
copy .env.example .env  # Windows
```

### Langkah 4B: Isi `.env`

Buka `.env` dan isi setiap value (jangan biarkan placeholder):

```
GOOGLE_SHEETS_API_KEY=path/to/chatbot-rag-service.json
GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here
TIDB_HOST=gateway01.ap-southeast-1.prod.aws.tidbcloud.com
TIDB_PORT=4000
TIDB_USER=your_tidb_username_here
TIDB_PASSWORD=your_tidb_password_here
TIDB_DATABASE=RAG
OPENAI_API_KEY=sk-proj-your_key_here
TELEGRAM_BOT_TOKEN=your_bot_token_here
```

**Cara mendapatkan setiap credential:**

1. **GOOGLE_SHEETS_API_KEY** — Buat Service Account di Google Cloud Console, download JSON key, simpan di folder project, isi dengan path relatifnya. Jangan lupa share spreadsheet ke email service account tersebut.
2. **GOOGLE_SPREADSHEET_ID** — Ambil dari URL spreadsheet: `https://docs.google.com/spreadsheets/d/[SPREADSHEET_ID]/edit`
3. **TIDB credentials** — Login ke TiDB Cloud → Cluster → Connect → copy host, port, user, password.
4. **OPENAI_API_KEY** — platform.openai.com → API keys → Create new.
5. **TELEGRAM_BOT_TOKEN** — Chat `@BotFather` di Telegram → `/newbot` → ikuti instruksi.

### Langkah 4C: Verifikasi

- [ ] File `.env` sudah dibuat (bukan `.env.example`)
- [ ] Semua value sudah diisi, tidak ada placeholder
- [ ] `.env` **tidak akan ter-commit** ke Git (sudah dicover `.gitignore`)

---

## **TAHAP 5: Setup TiDB Database Schema**

Jalankan `database/schema.sql` di TiDB Cloud melalui salah satu cara berikut:

**Opsi A — MySQL client:**
```bash
mysql -h <TIDB_HOST> -P 4000 -u <TIDB_USER> -p RAG < database/schema.sql
```

**Opsi B — TiDB Cloud SQL Editor:**
1. Buka dashboard TiDB Cloud → Cluster → SQL Editor
2. Copy seluruh isi `database/schema.sql`, paste, lalu jalankan

Ini akan membuat 4 tabel: `embeddings`, `sales_registry`, `conversation_log`, `access_control_log`.

---

## **TAHAP 6: Initialize Data & Embeddings**

```bash
python src/initialize_db.py
```

Script ini akan:
1. Memastikan seluruh tabel dari `database/schema.sql` sudah ada
2. Mengambil data tagihan dari Google Sheets
3. Membuat vector embedding untuk tiap baris data (OpenAI `text-embedding-3-small`)
4. Menyimpan hasilnya ke tabel `embeddings` di TiDB

---

## **TAHAP 7: Jalankan Bot**

```bash
python src/main_bot.py
```

Expected output:
```
[INFO] Bot started. Listening for messages...
```

Buka Telegram, cari bot Anda, lalu kirim `/start` untuk mulai registrasi sales.

Hentikan bot dengan `Ctrl + C`.

---

## **TAHAP 8: Troubleshooting**

### `ModuleNotFoundError: No module named 'src'` atau modul internal lain
- Pastikan menjalankan command dari root folder project (`rag-chatbot-system/`), bukan dari dalam `src/`

### `No module named 'mysql.connector'`
- Pastikan venv aktif, lalu `pip install mysql-connector-python==8.2.0`

### `Invalid credentials for Google Sheets`
- Cek path `GOOGLE_SHEETS_API_KEY` di `.env`
- Pastikan spreadsheet sudah di-share ke email service account (`client_email` di file JSON)

### `TiDB Connection Timeout`
- Cek koneksi internet dan credentials TiDB di `.env`
- Pastikan IP Anda diizinkan di TiDB Cloud (Cluster → Networking → jika ada Allow List)

### `Telegram Bot Token Invalid`
- Pastikan token di-copy lengkap tanpa spasi, format: `123456789:ABC...`

---

## **Script Pendukung Pengujian**

Digunakan untuk keperluan evaluasi sistem (BAB IV):

```bash
# Reset data test (sales_registry, conversation_log, access_control_log) tanpa hapus embeddings
python scripts/reset_test_data.py

# Benchmark latensi embedding
python scripts/benchmark_embedding.py

# Ekstrak hasil pengujian formal ke Markdown/JSON untuk BAB IV
python scripts/extract_test_results.py --after "2026-06-18 10:00:00"

# Export raw data dari conversation_log/access_control_log/sales_registry
python scripts/export_test_data.py
```

---

## **Checklist Sebelum Lanjut**

- [ ] Virtual environment aktif & `requirements.txt` terinstall
- [ ] `.env` sudah diisi lengkap dengan credentials asli
- [ ] Schema TiDB berhasil dijalankan (4 tabel terbentuk)
- [ ] `initialize_db.py` berhasil load data & generate embeddings
- [ ] Bot berjalan dan merespons `/start` di Telegram

---

## **Support**

Ada pertanyaan atau menemukan bug? Hubungi: handayani.id14@gmail.com
