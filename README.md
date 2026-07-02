# RAG Chatbot untuk Sistem Informasi Tagihan Pelanggan

Implementasi chatbot berbasis Retrieval-Augmented Generation (RAG) untuk akses informasi tagihan pelanggan melalui Telegram Bot.

**Thesis Project:** Universitas Widyatama, Program Studi Sistem Informasi  
**Author:** Hani Handayani (41122100013)

## Quick Start

```bash
# 1. Setup environment
python -m venv venv
source venv/bin/activate  # Mac/Linux
# atau venv\Scripts\activate (Windows)

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env dengan credentials kamu

# 4. Initialize database
python src/initialize_db.py

# 5. Run bot
python src/main_bot.py
```

## Features

- RAG-based semantic search for billing data
- Role-based access control (RBAC)
- Telegram Bot interface
- Automatic daily data refresh
- Multi-sheet data integration

## Documentation

- [Setup Guide](./SETUP.md) - Panduan setup lengkap


## Contact

For questions, contact: [handayani.id14@gmail.com]