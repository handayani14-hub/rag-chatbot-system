# src/main_bot.py
"""
Main Bot - Entry point utama Telegram bot Billie
Jalankan file ini untuk menghidupkan bot: python main_bot.py

Project: RAG Chatbot untuk Akses Informasi Tagihan Pelanggan
Author: Hani Handayani
"""

import logging
import sys
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from apscheduler.schedulers.background import BackgroundScheduler

from config import Config
from telegram_bot import TelegramBotHandlers
from initialize_db import initialize_database


# ─────────────────────────────────────────────
# Setup logging — output ke terminal + file log
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    handlers=[
        logging.StreamHandler(),           # tampil di terminal
        logging.FileHandler('logs/bot.log') # disimpan juga ke file
    ]
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER - Refresh data otomatis setiap hari kerja
# ─────────────────────────────────────────────────────────────────────────────

async def daily_data_refresh():
    """
    Task yang dijalankan oleh scheduler setiap hari kerja.
    Tujuannya supaya data tagihan yang ada di TiDB selalu sinkron
    dengan Google Sheets terbaru (re-embed semua dokumen).
    """
    logger.info("[SCHEDULER] Memulai daily data refresh...")

    try:
        success = initialize_database()

        if success:
            logger.info("[SCHEDULER] ✓ Daily refresh selesai")
        else:
            logger.error("[SCHEDULER] ✗ Daily refresh gagal")

    except Exception as e:
        logger.error(f"[SCHEDULER] Error saat refresh: {e}")


def setup_scheduler():
    """
    Inisialisasi APScheduler dan daftarkan job refresh harian.
    Job hanya diaktifkan kalau Config.SCHEDULER_ENABLED = True.
    Waktu dan hari kerja diambil dari .env supaya mudah dikonfigurasi.
    """
    scheduler = BackgroundScheduler()

    if Config.SCHEDULER_ENABLED:
        # Jadwalkan sesuai jam dan hari yang diatur di .env
        scheduler.add_job(
            daily_data_refresh,
            'cron',
            hour=Config.SCHEDULER_HOUR,
            minute=Config.SCHEDULER_MINUTE,
            day_of_week=Config.SCHEDULER_DAY_OF_WEEK,  # '0-4' = Senin-Jumat
            id='daily_refresh'
        )

        scheduler.start()
        logger.info(
            f"[SCHEDULER] Scheduler aktif. Refresh setiap hari kerja jam "
            f"{Config.SCHEDULER_HOUR}:{Config.SCHEDULER_MINUTE:02d}"
        )
    else:
        logger.info("[SCHEDULER] Scheduler dinonaktifkan via config")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN - Setup handler dan jalankan bot
# ─────────────────────────────────────────────────────────────────────────────

def main():
    """
    Entry point utama.
    Urutan kerjanya:
    1. Validasi config (.env lengkap atau tidak)
    2. Cek database — kalau kosong, jalankan initialize_database()
    3. Daftarkan semua handler ke Telegram Application
    4. Aktifkan scheduler
    5. Polling (bot mulai dengerin pesan)
    """

    logger.info("=" * 70)
    logger.info("  BILLIE - RAG CHATBOT TAGIHAN PELANGGAN")
    logger.info("  Memulai bot...")
    logger.info("=" * 70)

    try:
        # ── 1. Validasi konfigurasi dari .env ──────────────────────────────
        logger.info("[INIT] Memeriksa konfigurasi...")
        Config.validate()
        Config.display()

        # ── 2. Cek apakah database sudah berisi data embedding ─────────────
        logger.info("\n[INIT] Memeriksa database...")
        from tidb_client import TiDBClient
        db = TiDBClient()
        result = db.execute_query("SELECT COUNT(*) as cnt FROM embeddings")
        count = result[0]['cnt']

        if count == 0:
            # Database kosong — perlu inisialisasi pertama kali
            logger.warning("[INIT] Database kosong. Menjalankan inisialisasi...")
            success = initialize_database()
            if not success:
                logger.error("[INIT] Inisialisasi database gagal. Bot tidak bisa dijalankan.")
                return False
        else:
            logger.info(f"[INIT] ✓ Database siap dengan {count} embeddings")

        db.disconnect()

        # ── 3. Setup Telegram Application ─────────────────────────────────
        logger.info("\n[INIT] Menyiapkan Telegram bot...")
        application = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()

        # Buat instance handler — semua method ada di telegram_bot.py
        handlers = TelegramBotHandlers()

        # ── 4. Daftarkan command handlers ──────────────────────────────────
        # Command handlers diproses sebelum message handlers.

        # Sales commands
        application.add_handler(CommandHandler("start",    handlers.start))
        application.add_handler(CommandHandler("register", handlers.register))
        application.add_handler(CommandHandler("cancel",   handlers.cancel))

        # ── 5. Daftarkan handler tombol menu utama (ReplyKeyboard) ─────────
        # Urutan penting: handler yang lebih spesifik harus didaftarkan DULUAN
        # supaya tidak tersedot oleh message handler generik di bawah
        application.add_handler(MessageHandler(
            filters.TEXT & filters.Regex(r'^🔍 Cari Pelanggan$'),
            handlers.button_cari_pelanggan
        ))
        application.add_handler(MessageHandler(
            filters.TEXT & filters.Regex(r'^📊 Ringkasan Saya$'),
            handlers.button_ringkasan
        ))
        application.add_handler(MessageHandler(
            filters.TEXT & filters.Regex(r'^⚙️ Account$'),
            handlers.button_account
        ))
        application.add_handler(MessageHandler(
            filters.TEXT & filters.Regex(r'^❓ Help$'),
            handlers.help_command
        ))

        # ── 6. Handler untuk inline buttons (sheet selector, logout, dll) ──
        # CallbackQueryHandler menangkap semua callback_data dari InlineKeyboard
        application.add_handler(CallbackQueryHandler(handlers.button_callback))

        # ── 7. Handler generik untuk pesan teks bebas (query pengguna) ──────
        # Ini HARUS didaftarkan PALING AKHIR supaya tidak mengambil alih
        # pesan dari tombol menu yang sudah didaftarkan di atas
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.query)
        )

        logger.info("[INIT] ✓ Semua handler berhasil didaftarkan")

        # ── 8. Aktifkan scheduler ─────────────────────────────────────────
        logger.info("\n[INIT] Menyiapkan scheduler...")
        setup_scheduler()

        # ── 9. Jalankan bot ───────────────────────────────────────────────
        logger.info("\n" + "=" * 70)
        logger.info("  ✓ BOT AKTIF - Menunggu pesan masuk...")
        logger.info("  Tekan Ctrl+C untuk menghentikan bot")
        logger.info("=" * 70 + "\n")

        # run_polling akan terus berjalan sampai di-interrupt (Ctrl+C)
        application.run_polling()

    except KeyboardInterrupt:
        logger.info("\n[MAIN] Bot dihentikan oleh user")
        return True

    except Exception as e:
        logger.error(f"[MAIN] Error fatal: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = main()
    # Exit code 0 = sukses, 1 = ada error — berguna kalau dijalankan via script
    sys.exit(0 if success else 1)
