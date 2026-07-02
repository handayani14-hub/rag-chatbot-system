"""
scripts/reset_test_data.py
─────────────────────────────────────────────────────────────────────────────
Script untuk mereset data pengujian tanpa menghapus embedding.

Yang DIHAPUS:
  - sales_registry     → agar bisa uji registrasi dari awal
  - conversation_log   → bersihkan riwayat percakapan
  - access_control_log → bersihkan log akses (jika koneksi punya izin)

Yang DIPERTAHANKAN:
  - embeddings         → data vektor (mahal untuk dibuat ulang)

CARA PAKAI:
  cd D:\Skripsi\Chatbot_RAG
  python scripts/reset_test_data.py

  Untuk hapus satu user saja (tanpa truncate semua):
  python scripts/reset_test_data.py --chat-id 123456789
─────────────────────────────────────────────────────────────────────────────
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from tidb_client import TiDBClient


def reset_all(db: TiDBClient) -> None:
    """Hapus semua data test (kecuali embeddings)."""
    conn   = db.get_connection()
    cursor = conn.cursor()

    results = {}

    # 1. sales_registry — wajib dihapus agar bisa uji S10, S11, S12
    cursor.execute("SELECT COUNT(*) as cnt FROM sales_registry")
    before = cursor.fetchone()['cnt']
    cursor.execute("DELETE FROM sales_registry")
    conn.commit()
    results['sales_registry'] = before

    # 2. conversation_log — riwayat percakapan
    cursor.execute("SELECT COUNT(*) as cnt FROM conversation_log")
    before = cursor.fetchone()['cnt']
    cursor.execute("DELETE FROM conversation_log")
    conn.commit()
    results['conversation_log'] = before

    # 3. access_control_log — append-only, mungkin tidak bisa dihapus app user
    try:
        cursor.execute("SELECT COUNT(*) as cnt FROM access_control_log")
        before = cursor.fetchone()['cnt']
        cursor.execute("DELETE FROM access_control_log")
        conn.commit()
        results['access_control_log'] = before
    except Exception as e:
        results['access_control_log'] = f"SKIP (tidak ada izin hapus: {e})"

    cursor.close()

    # Tampilkan hasil
    print("\n" + "=" * 55)
    print("  RESET DATA PENGUJIAN SELESAI")
    print("=" * 55)
    print(f"  {'Tabel':<25} {'Baris Dihapus'}")
    print("-" * 55)
    for tabel, jumlah in results.items():
        print(f"  {tabel:<25} {jumlah}")
    print("-" * 55)

    # Verifikasi embedding masih aman
    hasil = db.execute_query("SELECT COUNT(*) as cnt FROM embeddings")
    print(f"  {'embeddings (aman)':<25} {hasil[0]['cnt']} baris (tidak disentuh)")
    print("=" * 55)
    print("\n  ✅ Bot siap digunakan untuk pengujian ulang dari awal.")
    print("  Mulai dari skenario S10 (tanpa registrasi) di Telegram.\n")


def reset_one_user(db: TiDBClient, chat_id: str) -> None:
    """Hapus data satu user berdasarkan chat_id (lebih surgical)."""
    conn   = db.get_connection()
    cursor = conn.cursor()

    # Cek apakah user ada
    cursor.execute("SELECT sales_code, sales_name FROM sales_registry WHERE chat_id = %s", (chat_id,))
    user = cursor.fetchone()

    if not user:
        print(f"\n⚠️  Chat ID '{chat_id}' tidak ditemukan di sales_registry.")
        cursor.close()
        return

    sales_code = user['sales_code']
    sales_name = user['sales_name']

    # Hapus dari sales_registry
    cursor.execute("DELETE FROM sales_registry WHERE chat_id = %s", (chat_id,))

    # Hapus conversation_log user ini
    cursor.execute("DELETE FROM conversation_log WHERE chat_id = %s", (chat_id,))

    # access_control_log: coba hapus, skip kalau tidak bisa
    try:
        cursor.execute("DELETE FROM access_control_log WHERE chat_id = %s", (chat_id,))
    except Exception:
        pass

    conn.commit()
    cursor.close()

    print("\n" + "=" * 55)
    print("  USER BERHASIL DIRESET")
    print("=" * 55)
    print(f"  Chat ID    : {chat_id}")
    print(f"  Sales Code : {sales_code}")
    print(f"  Nama       : {sales_name}")
    print("=" * 55)
    print(f"\n  ✅ User '{sales_name}' ({sales_code}) sudah dihapus.")
    print("  Kirim /start di Telegram untuk memulai dari awal.\n")


def main():
    parser = argparse.ArgumentParser(
        description='Reset data pengujian chatbot Billie (tanpa hapus embeddings)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Contoh:
  Reset semua data test:
    python scripts/reset_test_data.py

  Reset satu user saja:
    python scripts/reset_test_data.py --chat-id 123456789
        """
    )
    parser.add_argument(
        '--chat-id',
        type=str,
        help='Hapus data satu user berdasarkan Telegram chat_id (opsional)'
    )
    args = parser.parse_args()

    db = TiDBClient()

    try:
        if args.chat_id:
            reset_one_user(db, args.chat_id)
        else:
            # Konfirmasi sebelum hapus semua
            print("\n⚠️  Ini akan menghapus SEMUA data di sales_registry dan conversation_log.")
            print("   Embeddings TIDAK akan disentuh.")
            konfirmasi = input("   Ketik 'ya' untuk melanjutkan: ").strip().lower()
            if konfirmasi == 'ya':
                reset_all(db)
            else:
                print("   Dibatalkan.\n")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        raise
    finally:
        db.disconnect()


if __name__ == '__main__':
    main()
