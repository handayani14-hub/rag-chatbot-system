# src/config.py

"""
Configuration Loader - Load environment variables dari .env dan sediakan
konfigurasi Google Sheets, TiDB, OpenAI, dan Telegram Bot secara terpusat.
"""

import os
import sys
import io
from dotenv import load_dotenv
import json
import logging
from pathlib import Path

# Load environment variables dari .env file
load_dotenv()

# ========== UNICODE FIX FOR WINDOWS ==========
# Force UTF-8 encoding untuk Windows terminal (fix UnicodeEncodeError)
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ========== LOGGING CONFIGURATION ==========
# Create logs directory if not exists
log_dir = 'logs'
os.makedirs(log_dir, exist_ok=True)

# Setup logging format dengan UTF-8 encoding
log_format = '%(asctime)s - [%(levelname)s] - %(message)s'
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.FileHandler(f'{log_dir}/bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

# Suppress verbose HTTP logging dari libraries
logging.getLogger('openai').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('google.auth').setLevel(logging.WARNING)
logging.getLogger('gcloud').setLevel(logging.WARNING)

# CRITICAL: Suppress httpx logs (yang masih muncul di Windows)
logging.getLogger('httpx').setLevel(logging.ERROR)
logging.getLogger('httpx._client').setLevel(logging.ERROR)
logging.getLogger('h11').setLevel(logging.ERROR)

class Config:
    """
    Kelas untuk manage semua configuration yang digunakan di project.
    Reads dari .env file dan environment variables.
    """
    
    # ========== GOOGLE SHEETS CONFIG ==========
    GOOGLE_SHEETS_API_KEY = os.getenv('GOOGLE_SHEETS_API_KEY', 'path/to/chatbot-rag-service.json')
    GOOGLE_SPREADSHEET_ID = os.getenv('GOOGLE_SPREADSHEET_ID', '')
    
    # Sheet names yang akan di-load (sesuaikan dengan project Hani)
    SHEET_NAMES = {
        'billper': 'BILLPER-APRIL',
        'billdu': 'BILLDU-APRIL',
        'billtri': 'BILLTRI-APRIL'
    }

    # Periode data aktif — diekstrak dari nama sheet (mis: 'BILLPER-APRIL' → 'April')
    # Update otomatis saat SHEET_NAMES diubah untuk periode baru
    _sheet_raw = list(SHEET_NAMES.values())[0]
    CURRENT_DATA_PERIOD = _sheet_raw.split('-', 1)[1].capitalize() if '-' in _sheet_raw else 'saat ini'
    
    # ========== TIDB CONFIG ==========
    TIDB_HOST = os.getenv('TIDB_HOST', 'gateway01.ap-southeast-1.prod.aws.tidbcloud.com')
    TIDB_PORT = int(os.getenv('TIDB_PORT', 4000))
    TIDB_USER = os.getenv('TIDB_USER', '')
    TIDB_PASSWORD = os.getenv('TIDB_PASSWORD', '')
    TIDB_DATABASE = os.getenv('TIDB_DATABASE', 'RAG')
    TIDB_SSL_CA = os.getenv("TIDB_SSL_CA", "")
    
    # ========== OPENAI CONFIG ==========
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', None)  # Support untuk Maia Router atau custom endpoint
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
    OPENAI_EMBEDDING_MODEL = os.getenv('OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')
    OPENAI_EMBEDDING_DIM = 1536  # Dimensi embedding vector
    
    # ========== TELEGRAM BOT CONFIG ==========
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    
    # ========== LOGGING CONFIG ==========
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_DIR = 'logs'
    
    # ========== SCHEDULER CONFIG (untuk auto-update data) ==========
    SCHEDULER_ENABLED = os.getenv('SCHEDULER_ENABLED', 'True').lower() == 'true'
    SCHEDULER_HOUR = int(os.getenv('SCHEDULER_HOUR', 10))
    SCHEDULER_MINUTE = int(os.getenv('SCHEDULER_MINUTE', 0))
    SCHEDULER_DAY_OF_WEEK = os.getenv('SCHEDULER_DAY_OF_WEEK', '0-4')  # Weekday only
    
    # ========== DEBUG CONFIG ==========
    DEBUG_MODE = os.getenv('DEBUG_MODE', 'False').lower() == 'true'
    
    @staticmethod
    def validate():
        """
        Validate bahwa semua required config sudah ada.
        Throw error jika ada yang missing.
        """
        required_keys = [
            'GOOGLE_SPREADSHEET_ID',
            'TIDB_USER',
            'TIDB_PASSWORD',
            'OPENAI_API_KEY',
            'TELEGRAM_BOT_TOKEN'
        ]
        
        missing = []
        for key in required_keys:
            if not getattr(Config, key):
                missing.append(key)
        
        if missing:
            raise ValueError(f"Missing config: {', '.join(missing)}. Check .env file!")
        
        # Validate service account JSON path
        service_account_path = Path(Config.GOOGLE_SHEETS_API_KEY)
        if not service_account_path.exists():
            raise FileNotFoundError(
                f"Service account JSON not found: {Config.GOOGLE_SHEETS_API_KEY}"
            )
        
        print("[CONFIG] ✓ All configurations validated successfully")
    
    @staticmethod
    def display():
        """
        Display semua config (tanpa credentials) untuk debugging.
        """
        print("[CONFIG] Configuration Summary:")
        print(f"  - Google Spreadsheet: {Config.GOOGLE_SPREADSHEET_ID[:20]}...")
        print(f"  - TiDB Database: {Config.TIDB_DATABASE}@{Config.TIDB_HOST}")
        print(f"  - OpenAI Model: {Config.OPENAI_MODEL}")
        print(f"  - Scheduler: {'ENABLED' if Config.SCHEDULER_ENABLED else 'DISABLED'}")
        print(f"  - Debug Mode: {Config.DEBUG_MODE}")


if __name__ == '__main__':
    # Test: Load dan validate config
    Config.validate()
    Config.display()
    print("[CONFIG] ✓ Config test passed!")