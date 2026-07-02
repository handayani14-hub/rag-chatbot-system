# src/telegram_bot.py
"""
Telegram Bot Handlers — Menangani seluruh interaksi chat dengan sales:
menerima pesan, cek access control, deteksi intent, arahkan ke pipeline
RAG yang sesuai, lalu kirim jawaban.
"""

import logging
import re
import time
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from access_control import AccessControl
from config import Config
from rag_pipeline import RAGPipeline, _get_sheet_label
from tidb_client import TiDBClient
from utils import (
    detect_period_reference, detect_query_intent, detect_sheet_from_query,
    detect_sheets_from_query, extract_customer_name_from_query,
    is_partial_snd, is_snd_format,
)

logger = logging.getLogger(__name__)

# =========================================================================
# KONSTANTA TOMBOL MENU UTAMA
# Dipakai di banyak tempat, jadi dijadikan konstanta supaya tidak typo
# =========================================================================

BTN_CARI      = "🔍 Cari Pelanggan"
BTN_RINGKASAN = "📊 Ringkasan Saya"
BTN_ACCOUNT   = "⚙️ Account"
BTN_HELP      = "❓ Help"

# Set untuk cek apakah pesan adalah tombol menu (bukan query biasa)
MENU_BUTTONS = {BTN_CARI, BTN_RINGKASAN, BTN_ACCOUNT, BTN_HELP}

# Keyboard menu utama yang muncul di bawah kolom input Telegram
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_CARI), KeyboardButton(BTN_RINGKASAN)],
     [KeyboardButton(BTN_ACCOUNT), KeyboardButton(BTN_HELP)]],
    resize_keyboard=True,
    one_time_keyboard=False,
    input_field_placeholder="Pilih menu atau ketik pertanyaan..."
)


class TelegramBotHandlers:
    """
    Kelas yang berisi semua handler untuk bot Telegram Billie.

    Setiap method di sini bertanggung jawab untuk satu jenis interaksi:
    - Command handlers  : /start, /register
    - Menu handlers     : klik tombol Cari, Ringkasan, Account, Help
    - Message handler   : teks bebas dari user (query pertanyaan)
    - Callback handler  : klik inline button (sheet selector, logout)
    """

    def __init__(self):
        # Inisialisasi komponen utama yang dipakai di semua handler
        self.db  = TiDBClient()
        self.ac  = AccessControl(self.db)
        self.rag = RAGPipeline()

    # =========================================================================
    # HELPER INTERNAL — Dipakai oleh banyak handler di bawah
    # =========================================================================

    def _get_user(self, chat_id: str) -> Optional[dict]:
        """Ambil data user dari DB berdasarkan chat_id Telegram."""
        return self.db.get_user(chat_id)

    def _require_registered(self, chat_id: str) -> Optional[dict]:
        """
        Cek apakah user sudah terdaftar.
        Return data user kalau sudah, None kalau belum.
        Dipanggil di awal setiap handler yang butuh autentikasi.
        """
        return self._get_user(chat_id)

    def _sheet_filter_keyboard(self) -> InlineKeyboardMarkup:
        """
        Buat inline keyboard untuk pilih kategori tagihan.

        Tampilan di Telegram (satu baris horizontal):
        [Billper] [Billdu] [Billtri] [Keseluruhan]

        callback_data adalah kode yang dikirim balik ke bot
        saat user klik salah satu tombol.
        """
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("Billper",      callback_data="sheet:billper"),
            InlineKeyboardButton("Billdu",       callback_data="sheet:billdu"),
            InlineKeyboardButton("Billtri",      callback_data="sheet:billtri"),
            InlineKeyboardButton("Keseluruhan",  callback_data="sheet:all"),
        ]])

    def _not_registered_msg(self, chat_id: str = "") -> str:
        """Pesan standar untuk user yang belum login (format E1).
        Membedakan: sudah logout vs belum pernah daftar."""
        if chat_id:
            try:
                registered = self.db.get_registered_user(chat_id)
                if registered:
                    return (
                        "🔒 <b>Sesi tidak aktif.</b>\n\n"
                        f"Akun <b>{registered.get('sales_name', 'Anda')}</b> "
                        f"(<code>{registered.get('sales_code', '')}</code>) "
                        "sedang dalam kondisi logout.\n\n"
                        "Gunakan <code>/start</code> untuk masuk kembali."
                    )
            except Exception:
                pass
        return (
            "❌ <b>Belum terdaftar.</b>\n\n"
            "Gunakan perintah berikut untuk mendaftar:\n"
            "<code>/register [kode_sales]</code>\n\n"
            "Contoh: <code>/register AA12345</code>"
        )

    def _not_found_msg(self, identifier: str = "") -> str:
        """
        Pesan standar saat data pelanggan tidak ditemukan (format D2).
        Memberikan dua kemungkinan penyebab supaya user tidak bingung.
        """
        label = f" <b>{identifier}</b>" if identifier else ""
        return (
            f"Data pelanggan{label} tidak ditemukan dalam cakupan tanggung jawab Anda.\n\n"
            "<i>Kemungkinan:\n"
            "• Pelanggan ini ditangani oleh sales lain\n"
            "• Periksa kembali ejaan nama atau nomor yang Anda masukkan</i>"
        )

    # =========================================================================
    # COMMAND HANDLERS — /start, /register
    # =========================================================================

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle perintah /start — pintu masuk pertama ke bot.

        Dua kemungkinan:
        1. User belum terdaftar → sambut dan minta /register
        2. User sudah terdaftar → sambut kembali dengan nama + tampilkan menu
        """
        if not update.message or not update.effective_chat:
            return

        chat_id  = str(update.effective_chat.id)
        user_info = self._get_user(chat_id)

        if user_info:
            # User sudah login aktif — sambut kembali
            nama = user_info.get('sales_name', 'Kak')
            pesan = (
                f"Selamat datang kembali, <b>{nama}</b>! 👋\n\n"
                "Saya siap membantu Anda mengakses informasi tagihan pelanggan.\n"
                "Silakan pilih menu di bawah atau langsung ketik pertanyaan Anda."
            )
            await update.message.reply_text(
                pesan,
                reply_markup=MAIN_KEYBOARD,
                parse_mode=ParseMode.HTML
            )
        else:
            # Cek apakah user terdaftar tapi sedang logout (re-login otomatis)
            returning_user = self.db.login_user(chat_id)
            if returning_user:
                nama = returning_user.get('sales_name', 'Kak')
                pesan = (
                    f"Selamat datang kembali, <b>{nama}</b>! 👋\n\n"
                    "Sesi Anda telah dipulihkan.\n"
                    "Silakan pilih menu di bawah atau langsung ketik pertanyaan Anda."
                )
                self.ac.log_access_attempt(chat_id, returning_user.get('sales_code'), "granted", "Re-login via /start")
                await update.message.reply_text(
                    pesan,
                    reply_markup=MAIN_KEYBOARD,
                    parse_mode=ParseMode.HTML
                )
            else:
                # Benar-benar belum terdaftar — arahkan ke /register
                pesan = (
                    "Halo! Saya <b>Billie</b>, asisten untuk mengakses "
                    "informasi tagihan pelanggan Anda.\n\n"
                    "Untuk mulai, silakan daftar terlebih dahulu dengan mengirimkan:\n"
                    "<code>/register [kode_sales]</code>\n\n"
                    "Contoh: <code>/register AA12345</code>"
                )
                await update.message.reply_text(pesan, parse_mode=ParseMode.HTML)

        logger.info(f"[BOT] /start dari chat_id {chat_id}")

    async def register(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle perintah /register [kode_sales].

        Alur registrasi:
        1. Validasi format perintah (harus ada kode)
        2. Cek apakah kode ada di database embedding
        3. Ambil nama & agency dari database (auto-fill)
        4. Simpan ke sales_registry
        5. Kirim konfirmasi dengan nama sales

        Re-registrasi (kode sama, device berbeda) juga ditangani di sini —
        chat_id lama akan diganti dengan yang baru secara otomatis.
        """
        if not update.message or not update.effective_chat:
            return

        chat_id = str(update.effective_chat.id)

        # Cek apakah kode sales disertakan
        if not context.args:
            await update.message.reply_text(
                "📝 <b>Format Registrasi</b>\n\n"
                "<code>/register [kode_sales]</code>\n\n"
                "Contoh: <code>/register AA12345</code>",
                parse_mode=ParseMode.HTML
            )
            return

        sales_code = context.args[0].strip().upper()

        # Cek apakah kode valid (ada di database embedding)
        if not self.db.sales_code_exists(sales_code):
            self.ac.log_access_attempt(chat_id, sales_code, "denied", "Kode sales tidak valid")
            await update.message.reply_text(
                f"❌ <b>Kode sales <code>{sales_code}</code> tidak ditemukan.</b>\n\n"
                "Pastikan kode yang Anda masukkan sudah benar.\n"
                "Hubungi atasan jika masih bermasalah.",
                parse_mode=ParseMode.HTML
            )
            return

        # Ambil info nama & agency dari data embedding (auto-fill)
        sales_info = self.db.get_sales_info_from_embeddings(sales_code)
        sales_name = "Sales"
        ps_agency  = "N/A"
        datel      = "N/A"

        if sales_info:
            sales_name = sales_info.get('sales_name', 'Sales') or 'Sales'
            ps_agency  = sales_info.get('ps_agency', 'N/A')   or 'N/A'
            datel      = sales_info.get('datel', 'N/A')        or 'N/A'

        # Simpan ke database (register_user sudah handle re-registrasi)
        try:
            self.db.register_user(
                chat_id=chat_id,
                sales_code=sales_code,
                sales_name=sales_name,
                ps_agency=ps_agency,
                datel=datel
            )
            self.ac.log_access_attempt(chat_id, sales_code, "granted", "Registrasi berhasil")

            # Pesan konfirmasi (format E2)
            pesan = (
                f"✅ <b>Registrasi Berhasil!</b>\n\n"
                f"Halo <b>{sales_name}</b>, selamat datang!\n"
                f"Akun Anda telah terhubung dengan kode <code>{sales_code}</code>.\n\n"
                f"Sekarang Anda dapat mengakses informasi tagihan pelanggan Anda.\n\n"
                f"Contoh pertanyaan:\n"
                f"• 'Siapa saja pelanggan saya yang belum lunas?'\n"
                f"• 'Berapa saldo CV Maju Jaya?'"
            )
            await update.message.reply_text(
                pesan,
                reply_markup=MAIN_KEYBOARD,
                parse_mode=ParseMode.HTML
            )

        except ValueError as e:
            # Identity switch atau sales_code sudah dipakai akun lain
            self.ac.log_access_attempt(chat_id, sales_code, "denied", str(e))
            await update.message.reply_text(
                f"❌ <b>Registrasi Ditolak</b>\n\n{str(e)}",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"[BOT] Error registrasi {chat_id}: {e}")
            await update.message.reply_text(
                "❌ Terjadi kesalahan saat registrasi. Silakan coba lagi.",
                parse_mode=ParseMode.HTML
            )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle /help atau klik tombol ❓ Help — panduan penggunaan bot.

        Format F: panduan lengkap dengan semua perintah dan contoh pertanyaan.
        """
        if not update.message:
            return

        pesan = (
            "📖 <b>PANDUAN PENGGUNAAN BILLIE</b>\n\n"
            "Billie membantu Anda mengakses informasi tagihan pelanggan "
            "dengan mudah melalui percakapan.\n\n"

            "<b>Perintah yang tersedia:</b>\n"
            "/start — Tampilkan menu utama\n"
            "/register [kode] — Registrasi untuk akun baru\n\n"

            "<b>Cara Menggunakan Bot:</b>\n\n"
            "1️⃣ <b>Cari Pelanggan Belum Lunas:</b>\n"
            "   Klik 🔍 Cari Pelanggan → pilih kategori tagihan\n\n"

            "2️⃣ <b>Cek Status Satu Pelanggan:</b>\n"
            "   Ketik nama: <code>PT Maju Jaya</code>\n"
            "   Ketik SND: <code>3315000012345</code>\n\n"

            "3️⃣ <b>Lihat Ringkasan Data:</b>\n"
            "   Klik 📊 Ringkasan Saya, atau ketik:\n"
            "   <code>Berapa pelanggan saya yang belum lunas?</code>\n\n"

            "4️⃣ <b>Pertanyaan Bebas:</b>\n"
            "   Ketik pertanyaan dalam bahasa Indonesia yang natural\n"
            "   Contoh: <code>Berapa total saldo tunggakan saya?</code>\n\n"

            "<b>Tips:</b>\n"
            "✓ SND biasanya 10–12 digit angka\n"
            "✓ Bot hanya menampilkan data pelanggan Anda sendiri\n"
            "✓ Untuk pertanyaan non-tagihan, hubungi atasan Anda"
        )
        await update.message.reply_text(pesan, parse_mode=ParseMode.HTML)

    # =========================================================================
    # MENU BUTTON HANDLERS — Klik tombol keyboard di bawah
    # =========================================================================

    async def button_cari_pelanggan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle klik tombol '🔍 Cari Pelanggan'.

        Tampilkan inline keyboard pilihan kategori tagihan:
        [Billper] [Billdu] [Billtri] [Keseluruhan]

        User lalu klik salah satu → ditangani oleh _handle_sheet_callback()
        """
        if not update.message or not update.effective_chat:
            return

        chat_id   = str(update.effective_chat.id)
        user_info = self._require_registered(chat_id)

        if not user_info:
            self.ac.log_access_attempt(chat_id, None, "denied", "User belum terdaftar")
            await update.message.reply_text(self._not_registered_msg(chat_id), parse_mode=ParseMode.HTML)
            return

        await update.message.reply_text(
            "📂 <b>Cari Pelanggan Belum Lunas</b>\n\n"
            "Pilih kategori tagihan yang ingin ditampilkan:",
            reply_markup=self._sheet_filter_keyboard(),
            parse_mode=ParseMode.HTML
        )

    async def button_ringkasan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle klik tombol '📊 Ringkasan Saya'.

        Langsung ambil dan tampilkan ringkasan umum data tagihan
        (total pelanggan, lunas, belum lunas, saldo, breakdown per sheet).
        """
        if not update.message or not update.effective_chat:
            return

        chat_id   = str(update.effective_chat.id)
        user_info = self._require_registered(chat_id)

        if not user_info:
            self.ac.log_access_attempt(chat_id, None, "denied", "User belum terdaftar")
            await update.message.reply_text(self._not_registered_msg(chat_id), parse_mode=ParseMode.HTML)
            return

        sales_code = user_info.get('sales_code', '')

        # Tampilkan loading dulu supaya user tahu bot sedang bekerja
        loading = await update.message.reply_text("⏳ Menyiapkan ringkasan...")

        try:
            start_time = time.time()
            response   = self.rag.pipeline_ringkasan_umum(sales_code)
            elapsed_ms = int((time.time() - start_time) * 1000)

            # Simpan log (response time hanya di DB, tidak ditampilkan ke user)
            self.db.log_conversation(
                chat_id=chat_id,
                sales_code=sales_code,
                user_query="[MENU] Ringkasan Saya",
                bot_response=response,
                response_time_ms=elapsed_ms,
                query_type="ringkasan"
            )

            await loading.delete()
            await update.message.reply_text(response, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"[BOT] Error ringkasan {chat_id}: {e}")
            await loading.delete()
            await update.message.reply_text(
                "❌ Gagal memuat ringkasan. Silakan coba lagi.",
                parse_mode=ParseMode.HTML
            )

    async def button_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle klik tombol '⚙️ Account' — tampilkan info akun user (format E3).

        Menampilkan:
        - Kode Sales
        - Nama Sales
        - PS Agency (Agency tempat sales bekerja)
        - Status: ✅ Aktif
        - Tombol [🚪 Logout]
        """
        if not update.message or not update.effective_chat:
            return

        chat_id   = str(update.effective_chat.id)
        user_info = self._require_registered(chat_id)

        if user_info:
            # Tampilkan info akun yang sudah login
            pesan = (
                "⚙️ <b>ACCOUNT SETTINGS</b>\n\n"
                f"Kode Sales : <code>{user_info.get('sales_code', 'N/A')}</code>\n"
                f"Nama       : <b>{user_info.get('sales_name', 'N/A')}</b>\n"
                f"Agency     : {user_info.get('ps_agency', 'N/A')}\n"
                f"Status     : ✅ Aktif\n\n"
                "Pilih opsi di bawah:"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🚪 Logout", callback_data="logout")
            ]])
        else:
            # Bedakan: sudah pernah terdaftar tapi logout vs belum pernah daftar
            registered = self.db.get_registered_user(chat_id)
            if registered:
                pesan = (
                    "⚙️ <b>ACCOUNT SETTINGS</b>\n\n"
                    f"Kode Sales : <code>{registered.get('sales_code', 'N/A')}</code>\n"
                    f"Nama       : <b>{registered.get('sales_name', 'N/A')}</b>\n"
                    f"Status     : 🔒 Logout\n\n"
                    "Anda sudah terdaftar tetapi sedang tidak aktif.\n"
                    "Gunakan <code>/start</code> untuk masuk kembali."
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔑 Masuk Kembali", callback_data="relogin")
                ]])
            else:
                pesan = (
                    "⚙️ <b>ACCOUNT SETTINGS</b>\n\n"
                    "Anda belum terdaftar.\n\n"
                    "Gunakan <code>/register [kode_sales]</code> untuk mulai."
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📝 Daftar Sekarang", callback_data="register_now")
                ]])

        await update.message.reply_text(
            pesan,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )

    def _is_pronoun_reference(self, text: str) -> bool:
        """
        Deteksi apakah query mengandung referensi pronomina ke konteks percakapan sebelumnya.

        Bot bersifat stateless — tidak bisa resolve "itu", "tersebut", atau sufiks "-nya"
        yang merujuk ke entitas dari pesan sebelumnya (S19 multi-turn limitation).

        Pengecualian: jika query mengandung SND atau prefix nama usaha,
        kemungkinan besar ada entitas spesifik yang bisa diproses.
        """
        words = text.lower().strip().rstrip('?!.').split()

        # Kata ganti penunjuk yang merujuk ke entitas sebelumnya
        if any(w in ("itu", "tersebut") for w in words):
            # Pastikan bukan query yang juga punya entitas spesifik
            has_snd = bool(re.search(r'\b\d{9,13}\b', text))
            has_company = any(
                text.lower().find(p) >= 0
                for p in ['ud ', 'pt ', 'cv ', 'tb ', 'pd ', 'toko ', 'apotek ',
                          'klinik ', 'rs ', 'smk ', 'sma ', 'yayasan ']
            )
            if not has_snd and not has_company:
                return True

        # Sufiks "-nya" pada kata benda/kerja yang merujuk ke entitas sebelumnya
        # Contoh: "tagihannya", "statusnya", "saldo-nya" → referensi ke pelanggan sebelumnya
        # Dikecualikan: "saya" (ends ya bukan nya), kata umum lain
        false_positives = {"saya", "hanya", "biasanya", "unya", "kenya", "anya",
                          "semuanya", "seluruhnya"}
        for word in words:
            if (word.endswith("nya") and len(word) > 4
                    and word not in false_positives):
                has_snd = bool(re.search(r'\b\d{9,13}\b', text))
                has_company = any(
                    text.lower().find(p) >= 0
                    for p in ['ud ', 'pt ', 'cv ', 'tb ', 'pd ', 'toko ', 'apotek ',
                              'klinik ', 'rs ', 'smk ', 'sma ', 'yayasan ']
                )
                if not has_snd and not has_company:
                    return True

        return False

    # =========================================================================
    # MAIN MESSAGE HANDLER — Tangani semua teks bebas dari user
    # =========================================================================

    async def query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handler utama untuk semua pesan teks dari user (bukan command/tombol menu).

        Ini yang paling sering dipanggil — setiap kali user ketik pertanyaan.

        Alur:
        1. Skip kalau pesan adalah command atau tombol menu
        2. Cek registrasi (access control lapis pertama)
        3. Deteksi intent dari pertanyaan
        4. Routing ke pipeline yang tepat:
           - 9-13 digit angka → pipeline_status_by_snd
           - 1-8 digit       → error SND tidak lengkap
           - daftar intent   → tampilkan sheet selector
           - status intent   → pipeline_status_by_name
           - ringkasan intent → pipeline ringkasan spesifik
           - general         → pipeline_general (semantic search)
        5. Log percakapan ke DB
        6. Kirim jawaban ke user
        """
        if not update.message or not update.effective_chat:
            return

        chat_id  = str(update.effective_chat.id)
        teks     = (update.message.text or "").strip()

        # Abaikan kalau pesan kosong, command, atau tombol menu
        if not teks or teks.startswith('/') or teks in MENU_BUTTONS:
            return

        logger.info(f"[BOT] Query dari {chat_id}: '{teks[:60]}...'")

        # === STEP 1: Cek registrasi ===
        user_info = self._require_registered(chat_id)
        if not user_info:
            self.ac.log_access_attempt(chat_id, None, "denied", "User belum terdaftar")
            await update.message.reply_text(self._not_registered_msg(chat_id), parse_mode=ParseMode.HTML)
            return

        sales_code = user_info.get('sales_code', '')

        # === STEP 2: Cek apakah input adalah SND (9-13 digit) ===
        if is_snd_format(teks):
            await self._handle_search_by_snd(update, chat_id, sales_code, teks)
            return

        # Cek SND: angka 9-13 digit dalam kalimat
        # Multi-SND langsung dihandle tanpa perlu kata "SND"
        # Single SND perlu kata kunci atau konteks agar tidak salah tangkap angka biasa
        snd_matches = re.findall(r'\b(\d{9,13})\b', teks)
        if len(snd_matches) > 1:
            await self._handle_search_by_multiple_snd(
                update, chat_id, sales_code, snd_matches
            )
            return
        if snd_matches and re.search(r'\bsnd\b', teks, re.IGNORECASE):
            await self._handle_search_by_multiple_snd(
                update, chat_id, sales_code, snd_matches
            )
            return

        # Kalau angka tapi kurang dari 9 digit → kasih tahu format yang benar
        if is_partial_snd(teks):
            await update.message.reply_text(
                f"⚠️ Nomor SND yang Anda masukkan terlalu pendek.\n\n"
                f"Anda memasukkan: <code>{teks}</code> ({len(teks.strip())} digit)\n\n"
                f"SND biasanya 10–12 digit angka. Contoh: <code>1234567890</code>",
                parse_mode=ParseMode.HTML
            )
            return

        # === STEP 3: Deteksi intent dari teks pertanyaan ===
        intent = detect_query_intent(teks)
        logger.debug(f"[BOT] Intent terdeteksi: {intent}")

        # Tampilkan loading indicator
        loading = await update.message.reply_text("⏳ Sedang memproses...")

        try:
            start_time = time.time()
            response   = ""
            query_type = intent

            # === STEP 3.5: Cek referensi pronominal S19 (multi-turn stateless) ===
            # Bot tidak menyimpan konteks percakapan — query seperti "tagihannya",
            # "pelanggan itu" tidak bisa diselesaikan tanpa nama/SND eksplisit
            # Skip jika intent sudah terdeteksi spesifik (bukan general) —
            # artinya "-nya" merujuk ke entitas dalam kalimat yang sama
            if intent == "general" and self._is_pronoun_reference(teks):
                await loading.delete()
                await update.message.reply_text(
                    "⚠️ <b>Konteks percakapan tidak tersimpan</b>\n\n"
                    "Saya tidak dapat mengingat pesan sebelumnya. "
                    "Silakan sebutkan nama pelanggan atau nomor SND secara langsung.\n\n"
                    "💡 <i>Contoh:\n"
                    "• 'Status tagihan PT. ABC'\n"
                    "• '1234567890'</i>",
                    parse_mode=ParseMode.HTML
                )
                self.db.log_conversation(
                    chat_id=chat_id, sales_code=sales_code,
                    user_query=teks, bot_response="[S19: Pronoun reference detected]",
                    response_time_ms=int((time.time() - start_time) * 1000),
                    query_type="s19_pronoun"
                )
                return

            # === STEP 4: Routing ke pipeline yang sesuai ===

            if intent == "sapaan":
                # B1: Sapaan / Greeting — balas ramah dengan info nama sales
                sales_info = self.db.get_user(chat_id)
                nama = sales_info.get("sales_name", "") if sales_info else ""
                nama_sapaan = f", <b>{nama}</b>" if nama else ""
                response = (
                    f"Halo{nama_sapaan}! 👋\n\n"
                    "Saya <b>Billie</b>, asisten Anda untuk informasi tagihan pelanggan.\n\n"
                    "Yang bisa saya bantu:\n"
                    "• <i>Status tagihan PT. ABC</i>\n"
                    "• <i>Siapa pelanggan saya yang belum lunas?</i>\n"
                    "• <i>Berapa total saldo saya?</i>\n"
                    "• <i>1234567890</i> (cek via nomor SND)\n\n"
                    "Silakan ketik pertanyaan atau gunakan menu di bawah. 😊"
                )

            elif intent == "query_waktu":
                # S8: Query berbasis durasi/waktu — data historis tidak tersedia
                response = (
                    "⚠️ <b>Data historis tidak tersedia</b>\n\n"
                    "Saya hanya memiliki data tagihan yang <b>saat ini aktif</b>, "
                    "tanpa informasi kapan pelanggan mulai menunggak.\n\n"
                    "Yang bisa saya bantu:\n"
                    "• Daftar pelanggan <b>belum lunas saat ini</b>\n"
                    "• Pelanggan dengan <b>saldo terbesar</b>\n"
                    "• Ringkasan tagihan per kategori\n\n"
                    "💡 <i>Gunakan menu Cari Pelanggan atau tanya "
                    "'siapa pelanggan belum lunas terbesar?'</i>"
                )

            elif intent == "daftar_pelanggan":
                # --- S3: Cek referensi periode/bulan dalam query ---
                current_period = Config.CURRENT_DATA_PERIOD
                period_ref = detect_period_reference(teks)

                if period_ref and period_ref.lower() != current_period.lower():
                    # Periode berbeda dari data aktif → TOLAK sepenuhnya (jangan tampilkan
                    # selector karena data yang dikembalikan tetap periode aktif = menyesatkan)
                    resp_s3 = (
                        f"⚠️ <b>Data periode {period_ref} tidak tersedia</b>\n\n"
                        f"Sistem saat ini hanya menyimpan data tagihan periode "
                        f"<b>{current_period}</b>. Permintaan data bulan "
                        f"<b>{period_ref}</b> tidak dapat dipenuhi karena sistem "
                        f"hanya menyimpan satu periode tagihan aktif.\n\n"
                        f"💡 <i>Untuk melihat tagihan periode {current_period}, ketik:\n"
                        f"'siapa saja pelanggan saya yang belum lunas?'\n"
                        f"atau gunakan menu 🔍 <b>Cari Pelanggan</b> di bawah.</i>"
                    )
                    self.db.log_conversation(
                        chat_id=chat_id, sales_code=sales_code,
                        user_query=teks, bot_response=resp_s3,
                        response_time_ms=int((time.time() - start_time) * 1000),
                        query_type="s3_period_unavailable"
                    )
                    await loading.delete()
                    await update.message.reply_text(resp_s3, parse_mode=ParseMode.HTML)
                    return

                # --- S17: Deteksi permintaan data pelanggan yang sudah lunas ---
                lunas_request_phrases = [
                    "maupun yang lunas", "termasuk yang lunas", "dan yang lunas",
                    "yang sudah lunas", "baik yang lunas", "juga yang lunas",
                ]
                has_lunas_request = any(k in teks.lower() for k in lunas_request_phrases)

                # --- S3 (periode sama): auto-route ke BILLPER (tagihan berjalan = bulan tsb) ---
                # Contoh: "tagihan bulan april" → user minta billper (tagihan berjalan April)
                # Tidak perlu tampilkan selector karena sheet sudah jelas dari konteks bisnis
                if period_ref and period_ref.lower() == current_period.lower():
                    response, _, _ = self.rag.pipeline_daftar(
                        sales_code=sales_code, sheet_name="billper"
                    )
                    response = (
                        f"📅 <b>TAGIHAN BERJALAN PERIODE {current_period.upper()} (BILLPER)</b>\n\n"
                        + response
                    )

                    # S17: Jika user juga minta data lunas, tampilkan nama-namanya
                    if has_lunas_request:
                        try:
                            lunas_customers = self.db.get_paid_customers(
                                sales_code, sheet_name="billper"
                            )
                            if lunas_customers:
                                lunas_lines = [
                                    f"{i}. {c.get('customer_name','?')} - <code>{c.get('snd','-')}</code>"
                                    for i, c in enumerate(lunas_customers, 1)
                                ]
                                response += (
                                    f"\n\n─────────────────────────\n"
                                    f"✅ <b>PELANGGAN SUDAH LUNAS ({len(lunas_customers)} pelanggan):</b>\n\n"
                                    + "\n".join(lunas_lines)
                                )
                            else:
                                response += "\n\n✅ <i>Tidak ada pelanggan lunas untuk BILLPER.</i>"
                        except Exception as _e:
                            logger.warning(f"[BOT] Gagal ambil data lunas: {_e}")

                else:
                    # Tidak ada referensi periode → alur normal
                    detected_sheets = detect_sheets_from_query(teks)

                    if len(detected_sheets) > 1:
                        # Multi-sheet → gabungkan
                        label_gabung = " + ".join(s.upper() for s in detected_sheets)
                        response, _, _ = self.rag.pipeline_daftar_multi_sheet(
                            sales_code=sales_code,
                            sheet_names=detected_sheets
                        )
                        header_gabung = f"<b>📋 GABUNGAN {label_gabung}</b>\n\n"
                        response = header_gabung + response

                    elif len(detected_sheets) == 1:
                        # Satu sheet terdeteksi → langsung tampilkan
                        response, _, _ = self.rag.pipeline_daftar(
                            sales_code=sales_code,
                            sheet_name=detected_sheets[0]
                        )

                        # S17: Tampilkan nama pelanggan lunas jika diminta
                        if has_lunas_request:
                            try:
                                lunas_customers = self.db.get_paid_customers(
                                    sales_code, sheet_name=detected_sheets[0]
                                )
                                if lunas_customers:
                                    lunas_lines = [
                                        f"{i}. {c.get('customer_name','?')} - <code>{c.get('snd','-')}</code>"
                                        for i, c in enumerate(lunas_customers, 1)
                                    ]
                                    response += (
                                        f"\n\n─────────────────────────\n"
                                        f"✅ <b>PELANGGAN SUDAH LUNAS ({len(lunas_customers)} pelanggan):</b>\n\n"
                                        + "\n".join(lunas_lines)
                                    )
                                else:
                                    response += (
                                        "\n\n✅ <i>Semua pelanggan lainnya sudah berstatus lunas "
                                        "atau tidak ada data lunas untuk kategori ini.</i>"
                                    )
                            except Exception as _e:
                                logger.warning(f"[BOT] Gagal ambil data lunas: {_e}")
                                response += (
                                    "\n\n⚠️ <i>Catatan: Data pelanggan lunas tidak dapat "
                                    "dimuat saat ini. Ketik nama atau SND untuk cek individual.</i>"
                                )

                    else:
                        # Sheet tidak diketahui → tampilkan selector
                        await loading.delete()
                        await update.message.reply_text(
                            "📂 <b>Cari Pelanggan Belum Lunas</b>\n\n"
                            "Pilih kategori tagihan yang ingin ditampilkan:",
                            reply_markup=self._sheet_filter_keyboard(),
                            parse_mode=ParseMode.HTML
                        )
                        self.db.log_conversation(
                            chat_id=chat_id, sales_code=sales_code,
                            user_query=teks, bot_response="[Sheet selector ditampilkan]",
                            response_time_ms=int((time.time() - start_time) * 1000),
                            query_type=query_type
                        )
                        return

            elif intent == "status_pelanggan":
                # Cek apakah ada SND yang tertanam dalam kalimat (misal "dari 131520412319")
                snd_list_in_query = re.findall(r'\b(\d{9,13})\b', teks)
                if len(snd_list_in_query) > 1:
                    # B3: Multi-SND dalam satu pesan — gabungkan respon
                    await loading.delete()
                    await self._handle_search_by_multiple_snd(
                        update, chat_id, sales_code, snd_list_in_query
                    )
                    self.db.log_conversation(
                        chat_id=chat_id, sales_code=sales_code,
                        user_query=teks,
                        bot_response=f"[Multi-SND: {len(snd_list_in_query)} SND]",
                        response_time_ms=int((time.time() - start_time) * 1000),
                        query_type="status_pelanggan_multi_snd"
                    )
                    return
                elif snd_list_in_query:
                    # Single SND dalam kalimat → route ke pipeline SND (exact match)
                    snd = snd_list_in_query[0]
                    logger.debug(f"[BOT] SND terdeteksi dalam kalimat: {snd}")
                    response, _ = self.rag.pipeline_status_by_snd(
                        user_query=teks,
                        sales_code=sales_code,
                        snd=snd
                    )
                    query_type = "status_pelanggan_snd"
                else:
                    # Tidak ada SND → ekstrak nama dari kalimat, lalu cari
                    customer_name = extract_customer_name_from_query(teks)
                    response, _, _ = self.rag.pipeline_status_by_name(
                        user_query=teks,
                        sales_code=sales_code,
                        customer_name=customer_name
                    )

            elif intent == "ringkasan_belum_lunas":
                response = self.rag.pipeline_ringkasan_belum_lunas(sales_code)

            elif intent == "ringkasan_saldo":
                sheet_ctx = detect_sheet_from_query(teks)
                response = self.rag.pipeline_ringkasan_saldo(
                    sales_code, sheet_name=sheet_ctx
                )

            elif intent == "ringkasan":
                response = self.rag.pipeline_ringkasan_umum(sales_code)

            else:
                # Cek apakah query terlalu pendek / ambigu sebelum semantic search
                # Query 1-2 kata tanpa angka dan tanpa prefix nama usaha → tampilkan panduan
                _words = teks.strip().split()
                # B2: Cek prefix toleran titik & spasi ("PT. ABC", "PT ABC", "CV.XYZ")
                _company_prefix_re = re.compile(
                    r'^(ud|pt|cv|tb|pd|bpr|toko|apotek|klinik|rs|smk|sma|sd|smp|'
                    r'puskesmas|yayasan|koperasi|bumdes)\.?\s+',
                    re.IGNORECASE
                )
                _has_company_prefix = bool(_company_prefix_re.match(teks.strip()))
                _is_ambiguous = (
                    len(_words) <= 2
                    and not re.search(r'\d', teks)
                    and not _has_company_prefix
                )
                if _is_ambiguous:
                    response = (
                        "❓ <b>Pertanyaan kurang spesifik</b>\n\n"
                        "Silakan ketik lebih lengkap, misalnya:\n"
                        "• <i>Siapa pelanggan saya yang belum lunas?</i>\n"
                        "• <i>Status tagihan PT. ABC</i>\n"
                        "• <i>Berapa total saldo saya?</i>\n"
                        "• <i>1234567890</i> (nomor SND)\n\n"
                        "Atau gunakan tombol menu di bawah untuk memulai."
                    )
                else:
                    # Fallback: general semantic search
                    response, _ = self.rag.pipeline_general(
                        user_query=teks,
                        sales_code=sales_code
                    )

            elapsed_ms = int((time.time() - start_time) * 1000)

            # === STEP 5: Log percakapan ke DB ===
            # Response time disimpan di sini — TIDAK ditampilkan ke user
            self.db.log_conversation(
                chat_id=chat_id,
                sales_code=sales_code,
                user_query=teks,
                bot_response=response,
                response_time_ms=elapsed_ms,
                query_type=query_type
            )
            self.ac.log_access_attempt(chat_id, sales_code, "granted", f"Query intent: {intent}")

            # === STEP 6: Kirim jawaban ===
            await loading.delete()
            await update.message.reply_text(response, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"[BOT] Error memproses query dari {chat_id}: {e}")
            await loading.delete()
            await update.message.reply_text(
                "❌ Terjadi kesalahan saat memproses pertanyaan Anda.\n"
                "Silakan coba lagi atau klik tombol ❓ Help untuk bantuan.",
                parse_mode=ParseMode.HTML
            )

    # =========================================================================
    # SEARCH HANDLERS — Dipanggil dari query() untuk pencarian spesifik
    # =========================================================================

    async def _handle_search_by_snd(
        self,
        update: Update,
        chat_id: str,
        sales_code: str,
        snd: str
    ):
        """
        Handle pencarian berdasarkan nomor SND (9-13 digit).
        Dipanggil dari query() setelah validasi is_snd_format().
        """
        if not update.message:
            return

        loading = await update.message.reply_text("⏳ Mencari data pelanggan...")

        try:
            start_time = time.time()
            response, record = self.rag.pipeline_status_by_snd(
                user_query=f"Status tagihan {snd}",
                sales_code=sales_code,
                snd=snd
            )
            elapsed_ms = int((time.time() - start_time) * 1000)

            self.db.log_conversation(
                chat_id=chat_id, sales_code=sales_code,
                user_query=f"SND: {snd}", bot_response=response,
                response_time_ms=elapsed_ms, query_type="status_pelanggan",
                retrieved_documents_count=1 if record else 0
            )
            self.ac.log_access_attempt(
                chat_id, sales_code,
                "granted" if record else "denied",
                f"SND search: {snd}"
            )

            await loading.delete()
            await update.message.reply_text(response, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"[BOT] Error SND search {snd}: {e}")
            await loading.delete()
            await update.message.reply_text(
                "❌ Gagal mencari data. Silakan coba lagi.",
                parse_mode=ParseMode.HTML
            )

    async def _handle_search_by_multiple_snd(
        self,
        update: Update,
        chat_id: str,
        sales_code: str,
        snd_list: list
    ):
        """
        Handle pencarian multi-SND dalam satu pesan (B3).
        Loop tiap SND, panggil pipeline_status_by_snd, gabungkan respon dengan separator.

        Maksimum 5 SND per pesan untuk hindari respon terlalu panjang.
        """
        if not update.message:
            return

        # Deduplikasi sambil pertahankan urutan
        unique_snd = list(dict.fromkeys(snd_list))
        MAX_SND = 5
        truncated = len(unique_snd) > MAX_SND
        unique_snd = unique_snd[:MAX_SND]

        loading = await update.message.reply_text(
            f"⏳ Mencari data {len(unique_snd)} pelanggan..."
        )

        responses = []
        start_time = time.time()
        granted_count = 0

        for snd in unique_snd:
            try:
                resp, record = self.rag.pipeline_status_by_snd(
                    user_query=f"Status tagihan {snd}",
                    sales_code=sales_code,
                    snd=snd
                )
                responses.append(resp)
                if record:
                    granted_count += 1
            except Exception as e:
                logger.error(f"[BOT] Error multi-SND search {snd}: {e}")
                responses.append(
                    f"❌ Gagal memproses SND <code>{snd}</code>."
                )

        separator = "\n\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        combined = separator.join(responses)

        if truncated:
            combined += (
                f"\n\n⚠️ <i>Hanya {MAX_SND} SND pertama yang ditampilkan. "
                f"Silakan kirim sisanya di pesan berikutnya.</i>"
            )

        elapsed_ms = int((time.time() - start_time) * 1000)

        self.db.log_conversation(
            chat_id=chat_id, sales_code=sales_code,
            user_query=f"Multi-SND: {', '.join(unique_snd)}",
            bot_response=combined,
            response_time_ms=elapsed_ms,
            query_type="status_pelanggan_multi_snd",
            retrieved_documents_count=granted_count
        )

        await loading.delete()
        await update.message.reply_text(combined, parse_mode=ParseMode.HTML)

    # =========================================================================
    # CALLBACK HANDLER — Tangani klik inline button
    # =========================================================================

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle semua klik inline button.

        Routing berdasarkan callback_data:
        - 'sheet:billper'  → tampilkan data BILLPER
        - 'sheet:billdu'   → tampilkan data BILLDU
        - 'sheet:billtri'  → tampilkan data BILLTRI
        - 'sheet:all'      → tampilkan semua sheet
        - 'logout'         → proses logout user
        - 'register_now'   → tampilkan instruksi register
        """
        cb = update.callback_query
        if not cb or not cb.from_user:
            return

        try:
            await cb.answer()
        except Exception:
            pass  # Callback expired (>10s) karena koneksi putus — abaikan saja

        chat_id   = str(cb.from_user.id)
        data      = cb.data or ""

        # === Routing sheet selector ===
        if data.startswith("sheet:"):
            sheet_name = data.split(":", 1)[1]  # Ambil bagian setelah 'sheet:'
            await self._handle_sheet_callback(cb, chat_id, sheet_name)

        elif data == "logout":
            await self._handle_logout_callback(cb, chat_id)

        elif data == "register_now":
            await cb.edit_message_text(
                "Gunakan perintah:\n"
                "<code>/register [kode_sales]</code>\n\n"
                "Contoh: <code>/register MB20100</code>",
                parse_mode=ParseMode.HTML
            )

        elif data == "relogin":
            user = self.db.login_user(chat_id)
            if user:
                self.ac.log_access_attempt(
                    chat_id, user.get('sales_code'), "granted", "Re-login via /start"
                )
                await cb.edit_message_text(
                    f"✅ <b>Login Berhasil</b>\n\n"
                    f"Selamat datang kembali, <b>{user.get('sales_name', 'User')}</b>!\n"
                    f"Silakan pilih menu di bawah atau langsung ketik pertanyaan Anda.",
                    parse_mode=ParseMode.HTML
                )
            else:
                await cb.edit_message_text(
                    "❌ Gagal masuk. Data registrasi tidak ditemukan.\n"
                    "Gunakan <code>/register [kode_sales]</code> untuk mendaftar.",
                    parse_mode=ParseMode.HTML
                )

    async def _handle_sheet_callback(self, cb, chat_id: str, sheet_name: str):
        """
        Handle setelah user klik salah satu pilihan sheet.

        Ambil data pelanggan belum lunas dari sheet yang dipilih,
        lalu format dan kirim ke user.

        sheet_name bisa: 'billper', 'billdu', 'billtri', atau 'all'
        """
        # Cek registrasi dulu (keamanan: jangan sampai callback lama masih bisa dipakai)
        user_info = self._require_registered(chat_id)
        if not user_info:
            await cb.edit_message_text(
                self._not_registered_msg(chat_id),
                parse_mode=ParseMode.HTML
            )
            return

        sales_code = user_info.get('sales_code', '')
        label      = _get_sheet_label(sheet_name)

        # Update pesan lama dengan status loading
        await cb.edit_message_text(
            f"⏳ Mengambil data {label}...",
            parse_mode=ParseMode.HTML
        )

        try:
            start_time = time.time()

            # sheet_name 'all' → None (berarti semua sheet)
            filter_sheet = None if sheet_name == 'all' else sheet_name

            response, documents, total_count = self.rag.pipeline_daftar(
                sales_code=sales_code,
                sheet_name=filter_sheet
            )
            elapsed_ms = int((time.time() - start_time) * 1000)

            # Judul response
            header = f"<b>{label}</b>\n\n"

            # Log ke DB (response time hanya di sini, tidak ditampilkan ke user)
            self.db.log_conversation(
                chat_id=chat_id,
                sales_code=sales_code,
                user_query=f"[SHEET] {sheet_name}",
                bot_response=response,
                response_time_ms=elapsed_ms,
                query_type="daftar_pelanggan",
                retrieved_documents_count=len(documents)
            )

            # Edit pesan loading dengan jawaban sebenarnya
            await cb.edit_message_text(
                header + response,
                parse_mode=ParseMode.HTML
            )

        except Exception as e:
            logger.error(f"[BOT] Error sheet callback {sheet_name}: {e}")
            await cb.edit_message_text(
                f"❌ Gagal memuat data {label}. Silakan coba lagi.",
                parse_mode=ParseMode.HTML
            )

    async def _handle_logout_callback(self, cb, chat_id: str):
        """
        Handle klik tombol Logout.

        Hapus user dari sales_registry — setelah ini user harus /register ulang
        untuk bisa mengakses bot lagi.
        """
        try:
            berhasil = self.db.logout_user(chat_id)

            if berhasil:
                self.ac.log_access_attempt(chat_id, None, "logout", "User logout")
                await cb.edit_message_text(
                    "🚪 <b>Logout Berhasil</b>\n\n"
                    "Sesi Anda telah diakhiri.\n"
                    "Data registrasi tetap tersimpan.\n\n"
                    "Gunakan <code>/start</code> untuk masuk kembali.",
                    parse_mode=ParseMode.HTML
                )
            else:
                await cb.edit_message_text(
                    "⚠️ Sesi tidak ditemukan. Mungkin sudah logout sebelumnya.",
                    parse_mode=ParseMode.HTML
                )

        except Exception as e:
            logger.error(f"[BOT] Error logout {chat_id}: {e}")
            await cb.edit_message_text(
                "❌ Gagal logout. Silakan coba lagi.",
                parse_mode=ParseMode.HTML
            )

    # =========================================================================
    # CANCEL — Untuk membatalkan operasi yang sedang berjalan
    # =========================================================================

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Batalkan operasi yang sedang berjalan dan kembali ke menu utama."""
        if not update.message:
            return
        await update.message.reply_text(
            "❌ Dibatalkan. Ketik /start untuk kembali ke menu utama.",
            reply_markup=MAIN_KEYBOARD
        )