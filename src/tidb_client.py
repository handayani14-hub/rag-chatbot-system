# src/tidb_client.py
"""
TiDB Database Client — Layer database untuk semua query SQL, vector
search, dan operasi CRUD ke TiDB Cloud.
"""

import json
import logging
from typing import cast, Dict, List, Optional, Tuple

import mysql.connector
from mysql.connector import Error as MySQLError

from config import Config

logger = logging.getLogger(__name__)


class TiDBClient:
    """
    Kelas utama untuk semua operasi database ke TiDB Cloud.

    TiDB dipilih karena mendukung vector search secara native
    (tidak butuh extension tambahan), jadi kita bisa simpan embedding
    dan lakukan pencarian semantik langsung dari sini.

    Fitur yang tersedia:
    - Koneksi SSL/TLS (wajib untuk TiDB Cloud)
    - CRUD untuk tabel embeddings
    - Vector similarity search (cosine distance)
    - Pencarian exact match (nama pelanggan & SND)
    - Statistik dan ringkasan data tagihan
    - Manajemen user (registrasi, login, logout)
    - Logging percakapan dan akses kontrol
    """

    def __init__(self):
        self.connection = None
        self.connect()

    # =========================================================================
    # KONEKSI & MANAJEMEN
    # =========================================================================

    def connect(self) -> bool:
        """
        Sambungkan ke TiDB Cloud dengan SSL/TLS.

        SSL/TLS wajib dipakai karena TiDB Cloud ada di internet (bukan localhost),
        jadi koneksi harus dienkripsi supaya data tidak bisa disadap.
        """
        try:
            ssl_args = {}
            if Config.TIDB_SSL_CA:
                ssl_args = {
                    "ssl_ca": Config.TIDB_SSL_CA,
                    "ssl_verify_cert": True,
                    "ssl_verify_identity": True,
                }
            else:
                ssl_args = {
                    "ssl_disabled": False,
                    "ssl_verify_cert": False,
                    "ssl_verify_identity": False,
                }

            self.connection = mysql.connector.connect(
                host=Config.TIDB_HOST,
                port=Config.TIDB_PORT,
                user=Config.TIDB_USER,
                password=Config.TIDB_PASSWORD,
                database=Config.TIDB_DATABASE,
                **ssl_args,
                autocommit=True,
                connection_timeout=30
            )
            logger.info(f"[DB] ✓ Terhubung ke TiDB — {Config.TIDB_HOST}:{Config.TIDB_PORT}")
            return True
        except MySQLError as e:
            logger.error(f"[DB] ✗ Gagal koneksi ke TiDB: {e}")
            raise

    def disconnect(self):
        """Tutup koneksi database dengan bersih."""
        try:
            if self.connection and self.connection.is_connected():
                self.connection.close()
                logger.info("[DB] Koneksi ke TiDB ditutup.")
        except Exception:
            pass

    def ensure_connection(self):
        """
        Pastikan koneksi masih hidup, kalau putus langsung reconnect.
        Ini penting karena TiDB Cloud bisa disconnect setelah idle beberapa waktu.
        """
        try:
            if self.connection is None or not self.connection.is_connected():
                logger.warning("[DB] Koneksi terputus. Mencoba reconnect...")
                self.connect()
        except Exception:
            logger.warning("[DB] Cek koneksi gagal. Memaksa reconnect...")
            self.connect()

    def get_connection(self):
        """Return koneksi aktif, sambil pastikan masih valid."""
        self.ensure_connection()
        if self.connection is None:
            raise MySQLError("Koneksi TiDB tidak tersedia.")
        return self.connection

    # =========================================================================
    # HELPER UMUM — Query dan Insert
    # =========================================================================

    def execute_query(self, query: str, params: Optional[Tuple] = None) -> list:
        """
        Jalankan SELECT query dan kembalikan hasilnya sebagai list of dict.

        Kenapa pakai dictionary=True? Biar hasil bisa diakses pakai nama kolom
        (misal: row['customer_name']) bukan index angka (row[3]) — lebih readable.
        """
        self.ensure_connection()
        try:
            conn = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, params) if params else cursor.execute(query)
            result = cursor.fetchall()
            cursor.close()
            return result
        except MySQLError as e:
            logger.error(f"[DB] Query gagal: {e}")
            raise

    def execute_insert(self, table: str, data: Dict) -> int:
        """
        Insert satu baris data ke tabel.

        Data berupa dictionary: key = nama kolom, value = nilai yang mau diisi.
        Kalau gagal, otomatis rollback supaya tidak ada data setengah jadi di DB.
        """
        try:
            self.ensure_connection()
            columns   = ', '.join(data.keys())
            placeholders = ', '.join(['%s'] * len(data))
            query     = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"

            conn   = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(query, tuple(data.values()))
            conn.commit()

            last_id = cursor.lastrowid or 0
            cursor.close()
            logger.debug(f"[DB] Insert ke {table} berhasil, ID: {last_id}")
            return int(last_id)

        except MySQLError as e:
            logger.error(f"[DB] Insert gagal: {e}")
            if self.connection and self.connection.is_connected():
                self.connection.rollback()
            raise

    def _format_vector(self, vector) -> str:
        """
        Ubah Python list (embedding) menjadi format string yang bisa
        diterima oleh TiDB VECTOR type.

        Contoh: [0.1, 0.25, -0.3] → '[0.1,0.25,-0.3]'
        TiDB perlu format ini karena VECTOR adalah tipe data khusus,
        berbeda dengan INT atau VARCHAR biasa.
        """
        if isinstance(vector, list):
            return "[" + ",".join(str(float(x)) for x in vector) + "]"
        return str(vector)

    # =========================================================================
    # EMBEDDING — Simpan data pelanggan + vektor ke database
    # =========================================================================

    def insert_embeddings(
        self,
        embeddings_data: List[Dict],
        progress_callback=None
    ) -> int:
        """
        Simpan banyak embedding sekaligus ke tabel 'embeddings' (bulk insert).

        Kenapa bulk insert? Karena kalau insert satu-satu untuk 1551 records
        akan sangat lambat. Dengan executemany(), satu kali request untuk semua.

        Args:
            embeddings_data : list of dict, setiap dict = satu record pelanggan
            progress_callback: fungsi opsional yang dipanggil tiap record diproses
                               (untuk tampilkan progress bar di terminal)

        Returns:
            int: jumlah baris yang berhasil diinsert
        """
        if not embeddings_data:
            logger.warning("[DB] Tidak ada data embedding untuk diinsert.")
            return 0

        try:
            self.ensure_connection()
            conn   = self.get_connection()
            cursor = conn.cursor()

            # Query bulk insert — VEC_FROM_TEXT() untuk konversi string ke vektor TiDB
            query = """
                INSERT INTO embeddings (
                    sheet_name, snd, sales_code, sales_name, ps_agency,
                    customer_name, address, pic_name, phone_number,
                    datel, sto, jenis_tagihan,
                    status_pembayaran, saldo,
                    embedding_vector, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    VEC_FROM_TEXT(%s), %s
                )
            """

            values = []
            for idx, record in enumerate(embeddings_data):
                embedding_text = self._format_vector(record.get('embedding_vector', []))
                values.append((
                    record.get('sheet_name'),
                    record.get('snd'),
                    record.get('sales_code'),
                    record.get('sales_name'),
                    record.get('ps_agency'),
                    record.get('customer_name'),
                    record.get('address'),
                    record.get('pic_name'),
                    record.get('phone_number'),
                    record.get('datel'),
                    record.get('sto'),
                    record.get('jenis_tagihan'),
                    record.get('status_pembayaran'),
                    record.get('saldo'),
                    embedding_text,
                    json.dumps(record.get('metadata', {}))
                ))
                if progress_callback:
                    progress_callback(idx + 1)

            cursor.executemany(query, values)
            conn.commit()

            rows_inserted = cursor.rowcount
            cursor.close()
            logger.info(f"[DB] ✓ {rows_inserted} embedding berhasil disimpan.")
            return rows_inserted

        except MySQLError as e:
            logger.error(f"[DB] Bulk insert gagal: {e}")
            if self.connection and self.connection.is_connected():
                self.connection.rollback()
            raise

    # =========================================================================
    # PENCARIAN — Berbagai cara cari data pelanggan
    # =========================================================================

    def search_by_vector(
        self,
        embedding_vector: List[float],
        sales_code: str,
        limit: int = 5,
        sheet_name: Optional[str] = None
    ) -> List[Dict]:
        """
        Cari pelanggan menggunakan vector similarity (semantic search).

        Cara kerjanya: query user dikonversi ke vektor, lalu dicari vektor
        yang paling "mirip" di database pakai cosine distance.
        Makin kecil distance-nya, makin relevan hasilnya.

        Access control sudah diterapkan di sini: query SELALU difilter
        berdasarkan sales_code, jadi sales A tidak bisa lihat data sales B.

        Args:
            embedding_vector : vektor hasil embedding dari query user
            sales_code       : kode sales yang sedang login (untuk filter akses)
            limit            : maksimal berapa hasil yang dikembalikan
            sheet_name       : opsional, filter berdasarkan sheet (billper/billdu/billtri)
        """
        try:
            self.ensure_connection()
            conn = self.get_connection()
            embedding_text = self._format_vector(embedding_vector)

            # Filter sheet kalau pengguna minta data dari sheet tertentu
            sheet_filter = ""
            params: tuple = (embedding_text, sales_code)
            if sheet_name and sheet_name.lower() != 'all':
                sheet_filter = "AND LOWER(sheet_name) = %s"
                params = (embedding_text, sales_code, sheet_name.lower())

            query = f"""
                SELECT
                    id, sheet_name, snd, customer_name, address, pic_name, phone_number,
                    status_pembayaran, saldo, jenis_tagihan, sales_code, sales_name,
                    ps_agency, datel, sto,
                    VEC_COSINE_DISTANCE(embedding_vector, VEC_FROM_TEXT(%s)) AS distance
                FROM embeddings
                WHERE sales_code = %s
                {sheet_filter}
                ORDER BY distance ASC
                LIMIT {int(limit)}
            """

            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, params)
            results = cast(List[Dict], cursor.fetchall())
            cursor.close()

            logger.debug(f"[DB] Vector search: {len(results)} hasil ditemukan.")
            return results

        except MySQLError as e:
            logger.error(f"[DB] Vector search gagal: {e}")
            raise

    def search_by_customer_name_exact(
        self,
        customer_name: str,
        sales_code: str
    ) -> List[Dict]:
        """
        Cari pelanggan berdasarkan nama — menggunakan LIKE (case-insensitive).

        Kenapa LIKE dan bukan exact match? Karena user mungkin ketik
        "Apotek Sehat" padahal nama lengkapnya "Apotek Sehat Mandiri".
        LIKE %keyword% lebih fleksibel untuk pencarian nama.

        Ini dipakai sebagai langkah PERTAMA sebelum fallback ke semantic search.
        Kalau nama persis ketemu di sini, tidak perlu panggil OpenAI untuk embedding.
        """
        try:
            query = """
                SELECT
                    id, sheet_name, snd, customer_name, address, pic_name, phone_number,
                    status_pembayaran, saldo, jenis_tagihan, sales_code, sales_name,
                    ps_agency, datel, sto
                FROM embeddings
                WHERE sales_code = %s
                  AND LOWER(customer_name) LIKE LOWER(%s)
                ORDER BY customer_name ASC
                LIMIT 10
            """
            # Tambahkan wildcard di kiri dan kanan nama yang dicari
            search_pattern = f"%{customer_name.strip()}%"
            result = self.execute_query(query, (sales_code, search_pattern))
            logger.debug(f"[DB] Exact name search '{customer_name}': {len(result)} hasil.")
            return result

        except MySQLError as e:
            logger.error(f"[DB] Exact name search gagal: {e}")
            raise

    def search_by_snd_exact(
        self,
        snd: str,
        sales_code: str
    ) -> Optional[Dict]:
        """
        Cari pelanggan berdasarkan nomor SND (nomor layanan) secara tepat.

        SND harus cocok persis karena ini adalah identifier unik pelanggan.
        Berbeda dengan pencarian nama yang pakai LIKE, SND harus 13 digit tepat.

        Returns:
            Dict satu record kalau ketemu, None kalau tidak ada
        """
        try:
            query = """
                SELECT
                    id, sheet_name, snd, customer_name, address, pic_name, phone_number,
                    status_pembayaran, saldo, jenis_tagihan, sales_code, sales_name,
                    ps_agency, datel, sto
                FROM embeddings
                WHERE sales_code = %s
                  AND snd = %s
                LIMIT 1
            """
            result = self.execute_query(query, (sales_code, snd.strip()))
            logger.debug(f"[DB] SND search '{snd}': {'ditemukan' if result else 'tidak ditemukan'}.")
            return result[0] if result else None

        except MySQLError as e:
            logger.error(f"[DB] SND search gagal: {e}")
            raise

    def snd_exists_globally(self, snd: str) -> bool:
        """
        Cek apakah SND ada di database tanpa filter sales_code (B4).
        Dipakai untuk membedakan pesan error:
        - SND ada di DB tapi milik sales lain → "ditangani oleh sales lain"
        - SND tidak ada di DB sama sekali → "SND tidak valid / tidak ada"
        """
        try:
            result = self.execute_query(
                "SELECT 1 FROM embeddings WHERE snd = %s LIMIT 1",
                (snd.strip(),)
            )
            return bool(result)
        except MySQLError as e:
            logger.error(f"[DB] SND global check gagal: {e}")
            return False

    # =========================================================================
    # DATA TAGIHAN — Ambil daftar dan statistik pelanggan
    # =========================================================================

    def get_unpaid_customers(
        self,
        sales_code: str,
        limit: int = 15,
        sheet_name: Optional[str] = None
    ) -> List[Dict]:
        """
        Ambil daftar pelanggan yang belum lunas, diurutkan dari saldo terbesar.

        Inilah query yang paling sering dipakai — saat sales tanya
        "Siapa saja pelanggan saya yang belum lunas?"

        Args:
            sales_code : kode sales yang sedang login
            limit      : berapa banyak yang mau ditampilkan (default 10)
            sheet_name : kalau diisi, filter berdasarkan sheet tertentu
                         ('billper', 'billdu', 'billtri', atau None untuk semua)
        """
        try:
            # Build sheet filter dengan LIKE agar resilient terhadap perubahan format nama sheet
            sheet_filter = ""
            sheet_param  = None

            if sheet_name and sheet_name.lower() != 'all':
                sheet_filter = "AND LOWER(sheet_name) LIKE %s"
                sheet_param  = f"%{sheet_name.lower()}%"

            # Step 1: Ambil sales_name dari registry dulu secara terpisah.
            # Ini perlu karena beberapa KCONTACT format MD|...|SC.../MC...|
            # menyimpan kode SC/MC bukan MB/MN di kolom sales_code embeddings,
            # padahal nama sales di NAMA SA/AR/AM tetap sama.
            sales_name_registered = None
            try:
                name_result = self.execute_query(
                    "SELECT sales_name FROM sales_registry WHERE sales_code = %s AND is_active = TRUE LIMIT 1",
                    (sales_code,)
                )
                if name_result:
                    sales_name_registered = name_result[0].get('sales_name')
            except Exception:
                pass  # Kalau gagal, fallback ke sales_code saja

            # Step 2: Query dengan OR sales_name jika berhasil dapat nama.
            if sales_name_registered:
                params: tuple = (
                    (sales_code, sales_name_registered, sheet_param)
                    if sheet_param else
                    (sales_code, sales_name_registered)
                )

                query = f"""
                    SELECT
                        sheet_name, snd, sales_code, sales_name, ps_agency,
                        customer_name, pic_name, phone_number,
                        status_pembayaran, saldo, datel, sto, jenis_tagihan
                    FROM embeddings
                    WHERE (sales_code = %s OR sales_name = %s)
                      AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                      {sheet_filter}
                    ORDER BY CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2)) DESC
                    LIMIT {int(limit)}
                """
            else:
                params = (sales_code, sheet_param) if sheet_param else (sales_code,)
                query = f"""
                    SELECT
                        sheet_name, snd, sales_code, sales_name, ps_agency,
                        customer_name, pic_name, phone_number,
                        status_pembayaran, saldo, datel, sto, jenis_tagihan
                    FROM embeddings
                    WHERE sales_code = %s
                      AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                      {sheet_filter}
                    ORDER BY CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2)) DESC
                    LIMIT {int(limit)}
                """

            conn   = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, params)
            rows = cast(List[Dict], cursor.fetchall())
            cursor.close()

            logger.debug(
                f"[DB] Pelanggan belum lunas untuk {sales_code}"
                f"{f' (sheet: {sheet_name})' if sheet_name else ''}: {len(rows)} data."
            )
            return rows

        except MySQLError as e:
            logger.error(f"[DB] Ambil data belum lunas gagal: {e}")
            raise

    def get_paid_customers(
        self,
        sales_code: str,
        limit: int = 50,
        sheet_name: Optional[str] = None
    ) -> List[Dict]:
        """
        Ambil daftar pelanggan yang sudah LUNAS, diurutkan berdasarkan nama.

        Dipanggil saat user menanyakan semua pelanggan (lunas + belum lunas)
        untuk menampilkan bagian yang sudah membayar secara lengkap (S17).

        Strukturnya identik dengan get_unpaid_customers() namun filter
        status_pembayaran = 'LUNAS', bukan 'BELUM LUNAS'.
        """
        try:
            sheet_filter = ""
            sheet_param: Optional[str] = None
            if sheet_name and sheet_name.lower() != 'all':
                sheet_filter = "AND LOWER(sheet_name) LIKE %s"
                sheet_param  = f"%{sheet_name.lower()}%"

            # Lookup sales_name dari registry untuk support format MD|...|SC/MC
            sales_name_registered = None
            try:
                name_result = self.execute_query(
                    "SELECT sales_name FROM sales_registry WHERE sales_code = %s AND is_active = TRUE LIMIT 1",
                    (sales_code,)
                )
                if name_result:
                    sales_name_registered = name_result[0].get('sales_name')
            except Exception:
                pass

            if sales_name_registered:
                params: tuple = (
                    (sales_code, sales_name_registered, sheet_param)
                    if sheet_param else
                    (sales_code, sales_name_registered)
                )
                query = f"""
                    SELECT
                        sheet_name, snd, sales_code, sales_name, ps_agency,
                        customer_name, pic_name, phone_number,
                        status_pembayaran, saldo, datel, sto, jenis_tagihan
                    FROM embeddings
                    WHERE (sales_code = %s OR sales_name = %s)
                      AND TRIM(UPPER(status_pembayaran)) = 'LUNAS'
                      {sheet_filter}
                    ORDER BY customer_name ASC
                    LIMIT {int(limit)}
                """
            else:
                params = (sales_code, sheet_param) if sheet_param else (sales_code,)
                query = f"""
                    SELECT
                        sheet_name, snd, sales_code, sales_name, ps_agency,
                        customer_name, pic_name, phone_number,
                        status_pembayaran, saldo, datel, sto, jenis_tagihan
                    FROM embeddings
                    WHERE sales_code = %s
                      AND TRIM(UPPER(status_pembayaran)) = 'LUNAS'
                      {sheet_filter}
                    ORDER BY customer_name ASC
                    LIMIT {int(limit)}
                """

            conn   = self.get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(query, params)
            rows = cast(List[Dict], cursor.fetchall())
            cursor.close()

            logger.debug(
                f"[DB] Pelanggan lunas untuk {sales_code}"
                f"{f' (sheet: {sheet_name})' if sheet_name else ''}: {len(rows)} data."
            )
            return rows

        except MySQLError as e:
            logger.error(f"[DB] Ambil data lunas gagal: {e}")
            raise

    def count_unpaid_by_sheet(
        self,
        sales_code: str,
        sheet_name: str
    ) -> int:
        """
        Hitung berapa pelanggan belum lunas di satu sheet tertentu.

        Diperlukan untuk tampilkan breakdown per sheet di ringkasan:
        - Billper: X pelanggan
        - Billdu: Y pelanggan
        - Billtri: Z pelanggan
        """
        try:
            query = """
                SELECT COUNT(*) AS total
                FROM embeddings
                WHERE sales_code = %s
                  AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                  AND LOWER(sheet_name) LIKE %s
            """
            result = self.execute_query(query, (sales_code, f"%{sheet_name.lower()}%"))
            return int(result[0]['total']) if result else 0

        except MySQLError as e:
            logger.error(f"[DB] Count per sheet gagal: {e}")
            return 0

    def count_unpaid_all_sheets(self, sales_code: str) -> Dict:
        """
        Hitung belum lunas di semua sheet sekaligus, kembalikan sebagai dict.

        Returns dict dengan format:
        {
            'billper': 5,
            'billdu': 3,
            'billtri': 8
        }
        """
        result = {}
        for sheet in ['billper', 'billdu', 'billtri']:
            result[sheet] = self.count_unpaid_by_sheet(sales_code, sheet)
        return result

    def count_all_by_sheet(self, sales_code: str) -> Dict:
        """
        Hitung TOTAL record (semua status) per sheet — untuk ringkasan distribusi.

        Berbeda dengan count_unpaid_all_sheets yang hanya hitung belum lunas,
        fungsi ini menghitung semua record di setiap sheet (lunas + belum lunas).
        Dipakai di pipeline_ringkasan_umum untuk menampilkan distribusi portfolio.

        Returns dict dengan format:
        {
            'billper': 10,
            'billdu': 8,
            'billtri': 6
        }
        """
        result = {}
        for sheet in ['billper', 'billdu', 'billtri']:
            try:
                query = """
                    SELECT COUNT(*) AS total
                    FROM embeddings
                    WHERE sales_code = %s
                      AND LOWER(sheet_name) LIKE %s
                """
                res = self.execute_query(query, (sales_code, f"%{sheet}%"))
                result[sheet] = int(res[0]['total']) if res else 0
            except MySQLError as e:
                logger.error(f"[DB] Count all by sheet '{sheet}' gagal: {e}")
                result[sheet] = 0
        return result

    def get_summary_stats(self, sales_code: str) -> Dict:
        """
        Ambil statistik lengkap untuk satu sales — dipakai di tombol 'Ringkasan Saya'.

        Query ini mengambil semua angka yang dibutuhkan untuk format C1.1:
        - Total pelanggan (semua status)
        - Berapa yang lunas & belum lunas
        - Total saldo tertunggak
        - Pelanggan dengan tagihan terbesar

        Returns dict dengan semua angka statistik.
        """
        try:
            # Ambil agregat utama dalam satu query
            query_main = """
                SELECT
                    COUNT(*) AS total_pelanggan,
                    SUM(CASE WHEN TRIM(UPPER(status_pembayaran)) = 'LUNAS' THEN 1 ELSE 0 END) AS total_lunas,
                    SUM(CASE WHEN TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS' THEN 1 ELSE 0 END) AS total_belum_lunas,
                    SUM(CASE WHEN TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                        THEN CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2))
                        ELSE 0 END) AS total_saldo_tertunggak
                FROM embeddings
                WHERE sales_code = %s
            """
            main = self.execute_query(query_main, (sales_code,))
            stats = main[0] if main else {}

            total = int(stats.get('total_pelanggan') or 0)
            belum_lunas = int(stats.get('total_belum_lunas') or 0)
            lunas = int(stats.get('total_lunas') or 0)
            total_saldo = float(stats.get('total_saldo_tertunggak') or 0)

            # Hitung persentase (jaga-jaga kalau total = 0 supaya tidak error ZeroDivision)
            persen_belum_lunas = round(belum_lunas / total * 100, 1) if total > 0 else 0
            persen_lunas = round(lunas / total * 100, 1) if total > 0 else 0

            # Cari pelanggan dengan tagihan terbesar
            query_terbesar = """
                SELECT customer_name, saldo, sheet_name
                FROM embeddings
                WHERE sales_code = %s
                  AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                ORDER BY CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2)) DESC
                LIMIT 1
            """
            terbesar_result = self.execute_query(query_terbesar, (sales_code,))
            terbesar = terbesar_result[0] if terbesar_result else None

            # Breakdown per sheet (count belum lunas tiap sheet)
            sheet_counts = self.count_unpaid_all_sheets(sales_code)
            per_sheet = {}
            for sheet, count in sheet_counts.items():
                persen_sheet = round(count / total * 100, 1) if total > 0 else 0
                per_sheet[sheet] = {'count': count, 'persen': persen_sheet}

            return {
                'total': total,
                'lunas': lunas,
                'belum_lunas': belum_lunas,
                'persen_belum_lunas': persen_belum_lunas,
                'persen_lunas': persen_lunas,
                'total_saldo': total_saldo,
                'terbesar': terbesar,
                'per_sheet': per_sheet
            }

        except MySQLError as e:
            logger.error(f"[DB] Get summary stats gagal: {e}")
            raise

    def get_unpaid_summary_stats(self, sales_code: str) -> Dict:
        """
        Statistik khusus untuk query 'berapa pelanggan belum lunas?' (format C1.2).

        Bedanya dengan get_summary_stats():
        - Persentase per sheet dihitung dari total BELUM LUNAS, bukan total keseluruhan
        - Contoh: jika ada 10 belum lunas dan 7 dari billper → billper = 70%
        """
        try:
            query = """
                SELECT
                    COUNT(*) AS total_belum_lunas,
                    SUM(CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2))) AS total_saldo
                FROM embeddings
                WHERE sales_code = %s
                  AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
            """
            result = self.execute_query(query, (sales_code,))
            data = result[0] if result else {}

            total_bl = int(data.get('total_belum_lunas') or 0)
            total_saldo = float(data.get('total_saldo') or 0)

            # Ambil total keseluruhan untuk hitung persen dari total
            query_total = "SELECT COUNT(*) AS total FROM embeddings WHERE sales_code = %s"
            total_result = self.execute_query(query_total, (sales_code,))
            total = int(total_result[0]['total']) if total_result else 0
            persen_dari_total = round(total_bl / total * 100, 1) if total > 0 else 0

            # Pelanggan dengan saldo terbesar
            query_terbesar = """
                SELECT customer_name, saldo, sheet_name
                FROM embeddings
                WHERE sales_code = %s
                  AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                ORDER BY CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2)) DESC
                LIMIT 1
            """
            terbesar_result = self.execute_query(query_terbesar, (sales_code,))
            terbesar = terbesar_result[0] if terbesar_result else None

            # Breakdown per sheet — persentase dari total belum lunas
            sheet_counts = self.count_unpaid_all_sheets(sales_code)
            per_sheet = {}
            for sheet, count in sheet_counts.items():
                persen = round(count / total_bl * 100, 1) if total_bl > 0 else 0
                per_sheet[sheet] = {'count': count, 'persen': persen}

            return {
                'total_belum_lunas': total_bl,
                'persen_dari_total': persen_dari_total,
                'total_saldo': total_saldo,
                'terbesar': terbesar,
                'per_sheet': per_sheet
            }

        except MySQLError as e:
            logger.error(f"[DB] Get unpaid summary gagal: {e}")
            raise

    def get_saldo_summary(
        self,
        sales_code: str,
        sheet_name: Optional[str] = None
    ) -> Dict:
        """
        Ringkasan saldo — untuk query 'berapa total saldo saya?' (format C1.3).

        Menampilkan top 3 pelanggan dengan tagihan terbesar.
        Jika sheet_name diisi ('billtri', 'billdu', 'billper'), hanya data
        sheet tersebut yang dihitung — untuk query per sheet seperti
        'berapa total saldo billtri saya?'.
        """
        try:
            sheet_filter = ""
            params_base: tuple = (sales_code,)
            if sheet_name and sheet_name.lower() != 'all':
                sheet_filter = "AND LOWER(sheet_name) LIKE %s"
                params_base = (sales_code, f"%{sheet_name.lower()}%")

            query_total = f"""
                SELECT
                    SUM(CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2))) AS total_saldo,
                    COUNT(*) AS jumlah_belum_lunas
                FROM embeddings
                WHERE sales_code = %s
                  AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                  {sheet_filter}
            """
            result = self.execute_query(query_total, params_base)
            data   = result[0] if result else {}

            query_lunas = f"""
                SELECT COUNT(*) AS jumlah_lunas
                FROM embeddings
                WHERE sales_code = %s
                  AND TRIM(UPPER(status_pembayaran)) = 'LUNAS'
                  {sheet_filter}
            """
            lunas_result = self.execute_query(query_lunas, params_base)
            jumlah_lunas = int(lunas_result[0]['jumlah_lunas']) if lunas_result else 0

            query_top3 = f"""
                SELECT customer_name, saldo, sheet_name
                FROM embeddings
                WHERE sales_code = %s
                  AND TRIM(UPPER(status_pembayaran)) = 'BELUM LUNAS'
                  {sheet_filter}
                ORDER BY CAST(COALESCE(NULLIF(saldo, ''), '0') AS DECIMAL(15,2)) DESC
                LIMIT 3
            """
            top3_result = self.execute_query(query_top3, params_base)

            return {
                'total_saldo': float(data.get('total_saldo') or 0),
                'jumlah_belum_lunas': int(data.get('jumlah_belum_lunas') or 0),
                'jumlah_lunas': jumlah_lunas,
                'top3': top3_result
            }

        except MySQLError as e:
            logger.error(f"[DB] Get saldo summary gagal: {e}")
            raise

    # =========================================================================
    # VALIDASI — Cek data sebelum proses
    # =========================================================================

    def sales_code_exists(self, sales_code: str) -> bool:
        """
        Cek apakah sales_code ada di tabel embeddings.

        Dipanggil saat user register — kalau kode tidak ada di data,
        artinya bukan sales dari perusahaan ini (atau kode salah ketik).
        """
        try:
            query = """
                SELECT 1 FROM embeddings
                WHERE sales_code = %s
                LIMIT 1
            """
            result = self.execute_query(query, (sales_code,))
            exists = len(result) > 0
            logger.debug(f"[DB] Cek sales_code '{sales_code}': {'ada' if exists else 'tidak ada'}.")
            return exists

        except MySQLError as e:
            logger.error(f"[DB] Cek sales_code gagal: {e}")
            return False

    def get_sales_info_from_embeddings(self, sales_code: str) -> Optional[Dict]:
        """
        Ambil nama dan ps_agency sales dari data embedding.

        Dipanggil saat registrasi berhasil — supaya bot bisa auto-fill
        'Halo, [Nama Sales]! Anda terdaftar sebagai [Agency].'
        tanpa perlu user ketik manual.
        """
        try:
            query = """
                SELECT sales_name, ps_agency, datel
                FROM embeddings
                WHERE sales_code = %s
                  AND sales_name IS NOT NULL
                  AND sales_name != 'N/A'
                  AND sales_name != ''
                LIMIT 1
            """
            result = self.execute_query(query, (sales_code,))
            return result[0] if result else None

        except MySQLError as e:
            logger.error(f"[DB] Ambil info sales gagal: {e}")
            return None

    # =========================================================================
    # MANAJEMEN USER — Registrasi, cek, dan logout
    # =========================================================================

    def register_user(
        self,
        chat_id: str,
        sales_code: str,
        sales_name: str,
        ps_agency: Optional[str] = None,
        datel: Optional[str] = None
    ) -> bool:
        """
        Daftarkan user baru ke tabel sales_registry.

        Mapping chat_id → sales_code bersifat immutable (1:1 permanen).
        - chat_id yang sama + sales_code yang sama → re-login (update is_logged_in)
        - chat_id yang sama + sales_code berbeda   → DITOLAK (identity switching)
        - sales_code sudah dipakai chat_id lain    → DITOLAK

        Returns:
            True kalau berhasil
        Raises:
            ValueError jika ada percobaan identity switching
        """
        try:
            # Guard 1: Cek apakah chat_id ini sudah punya registrasi aktif
            existing_by_chatid = self.execute_query(
                "SELECT sales_code FROM sales_registry WHERE chat_id = %s AND is_active = TRUE",
                (chat_id,)
            )

            if existing_by_chatid:
                existing_code = existing_by_chatid[0]['sales_code']
                if existing_code == sales_code:
                    # chat_id sama, sales_code sama → re-login setelah logout
                    conn   = self.get_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE sales_registry
                        SET is_logged_in = TRUE, last_login_at = CURRENT_TIMESTAMP
                        WHERE chat_id = %s AND is_active = TRUE
                    """, (chat_id,))
                    conn.commit()
                    cursor.close()
                    logger.info(f"[DB] Re-login: {chat_id} ({sales_code}).")
                    return True
                else:
                    # chat_id sama, sales_code BERBEDA → tolak
                    logger.warning(
                        f"[DB] BLOCKED identity switch: chat_id {chat_id} "
                        f"({existing_code} → {sales_code})."
                    )
                    raise ValueError(
                        f"Chat ID ini sudah terdaftar dengan kode {existing_code}. "
                        f"Hubungi supervisor untuk mengajukan perubahan identitas."
                    )

            # Guard 2: Cek apakah sales_code sudah dipakai chat_id lain
            existing_by_code = self.execute_query(
                "SELECT chat_id FROM sales_registry WHERE sales_code = %s AND is_active = TRUE",
                (sales_code,)
            )

            if existing_by_code:
                logger.warning(f"[DB] BLOCKED: sales_code {sales_code} sudah terdaftar akun lain.")
                raise ValueError(
                    f"Kode sales {sales_code} sudah terdaftar oleh akun lain. "
                    f"Hubungi supervisor jika ini adalah kesalahan."
                )

            # Insert baru
            data = {
                'chat_id': chat_id,
                'sales_code': sales_code,
                'sales_name': sales_name,
                'ps_agency': ps_agency or 'N/A',
                'datel': datel or 'N/A',
                'is_active': True,
                'is_logged_in': True,
                'registration_method': 'self-register'
            }
            self.execute_insert('sales_registry', data)
            logger.info(f"[DB] ✓ User baru terdaftar: {chat_id} ({sales_code}).")
            return True

        except ValueError:
            raise
        except MySQLError as e:
            logger.error(f"[DB] Registrasi gagal: {e}")
            raise

    def get_user(self, chat_id: str) -> Optional[Dict]:
        """
        Ambil info user yang sedang aktif login.
        Hanya return data jika is_active = TRUE DAN is_logged_in = TRUE.
        User yang logout return None sampai mereka /start kembali.
        """
        try:
            query = """
                SELECT * FROM sales_registry
                WHERE chat_id = %s AND is_active = TRUE AND is_logged_in = TRUE
            """
            result = self.execute_query(query, (chat_id,))
            return result[0] if result else None

        except MySQLError as e:
            logger.error(f"[DB] Ambil data user gagal: {e}")
            raise

    def get_registered_user(self, chat_id: str) -> Optional[Dict]:
        """
        Cek apakah chat_id pernah terdaftar (is_active = TRUE),
        TANPA memfilter is_logged_in.
        Dipakai untuk membedakan 'sudah logout' vs 'belum pernah daftar'.
        """
        try:
            result = self.execute_query(
                "SELECT * FROM sales_registry WHERE chat_id = %s AND is_active = TRUE",
                (chat_id,)
            )
            return result[0] if result else None
        except MySQLError as e:
            logger.error(f"[DB] Cek registrasi gagal: {e}")
            raise

    def login_user(self, chat_id: str) -> Optional[Dict]:
        """
        Set is_logged_in = TRUE untuk user yang sebelumnya logout.
        Dipakai oleh handler /start untuk re-login otomatis tanpa re-register.

        Returns:
            Dict data user jika berhasil, None jika chat_id tidak terdaftar
        """
        try:
            result = self.execute_query(
                "SELECT * FROM sales_registry WHERE chat_id = %s AND is_active = TRUE",
                (chat_id,)
            )

            if not result:
                return None

            conn   = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sales_registry
                SET is_logged_in = TRUE, last_login_at = CURRENT_TIMESTAMP
                WHERE chat_id = %s AND is_active = TRUE
            """, (chat_id,))
            conn.commit()
            cursor.close()

            logger.info(f"[DB] Login: {chat_id} ({result[0].get('sales_code')}).")
            return result[0]

        except MySQLError as e:
            logger.error(f"[DB] Login gagal: {e}")
            raise

    def logout_user(self, chat_id: str) -> bool:
        """
        Soft logout — akhiri sesi tanpa menghapus data registrasi.
        is_logged_in diset FALSE sehingga user harus /start untuk aktif kembali,
        tapi tidak bisa /register ulang dengan sales_code berbeda.

        Returns:
            True kalau berhasil, False kalau user tidak ditemukan atau sudah logout
        """
        try:
            conn   = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE sales_registry
                SET is_logged_in = FALSE, last_logout_at = CURRENT_TIMESTAMP
                WHERE chat_id = %s AND is_active = TRUE AND is_logged_in = TRUE
            """, (chat_id,))
            conn.commit()
            rows_affected = cursor.rowcount
            cursor.close()

            if rows_affected > 0:
                logger.info(f"[DB] ✓ User logout: chat_id {chat_id} (sesi diakhiri, data tetap ada).")
                return True
            else:
                logger.warning(f"[DB] Logout: chat_id {chat_id} tidak ditemukan atau sudah logout.")
                return False

        except MySQLError as e:
            logger.error(f"[DB] Logout gagal: {e}")
            raise

    # =========================================================================
    # LOGGING — Catat semua aktivitas untuk audit dan penelitian
    # =========================================================================

    def log_conversation(
        self,
        chat_id: str,
        sales_code: str,
        user_query: str,
        bot_response: str,
        response_time_ms: int,
        query_type: str = 'general',
        retrieved_documents_count: int = 0
    ) -> int:
        """
        Catat percakapan ke tabel conversation_log.

        Log ini penting untuk dua hal:
        1. Audit trail — rekam jejak siapa tanya apa
        2. Data penelitian — sumber metrik untuk evaluasi di BAB IV
           (latency, jumlah dokumen di-retrieve, dll.)

        Response time dan info teknis TIDAK ditampilkan ke user,
        hanya disimpan di sini untuk keperluan penelitian.
        """
        try:
            data = {
                'chat_id': chat_id,
                'sales_code': sales_code,
                'user_query': user_query,
                'bot_response': bot_response,
                'response_time_ms': response_time_ms,
                'query_type': query_type,
                'retrieved_documents_count': retrieved_documents_count,
                'error_occurred': False
            }
            log_id = self.execute_insert('conversation_log', data)
            logger.debug(f"[DB] Percakapan dicatat, ID: {log_id}")
            return log_id

        except MySQLError as e:
            logger.error(f"[DB] Log percakapan gagal: {e}")
            raise

    def log_access_control(
        self,
        chat_id: str,
        sales_code: Optional[str],
        access_type: str,
        reason: str,
        resource: str
    ) -> int:
        """
        Catat setiap percobaan akses ke tabel access_control_log.

        access_type bisa: 'granted', 'denied', 'unregistered'
        reason menjelaskan kenapa akses diberikan atau ditolak, misal:
        - "Akses granted untuk sales_code PSB1234"
        - "Akses denied: sales_code MD5678 tidak punya data"
        - "Akses denied: chat_id 12345 belum register"
        - "Akses denied: sales_code AMRBS0000 sudah tidak aktif"
        """
        try:
            data = {
                'chat_id': chat_id,
                'sales_code': sales_code,
                'access_type': access_type,
                'reason': reason,
                'accessed_resource': resource
            }
            log_id = self.execute_insert('access_control_log', data)
            return log_id

        except MySQLError as e:
            logger.error(f"[DB] Log akses kontrol gagal: {e}")
            raise

    # =========================================================================
    # DESTRUCTOR — Bersihkan koneksi saat objek dihapus
    # =========================================================================

    def __del__(self):
        """Tutup koneksi database otomatis saat objek dihapus dari memori."""
        self.disconnect()


# =========================================================================
# MAIN — Untuk test koneksi dan verifikasi database
# =========================================================================

def main():
    """
    Test function — jalankan langsung untuk verifikasi semua method bekerja.
    Gunakan: python tidb_client.py
    """
    print("=" * 60)
    print("TEST: TiDB Client — Verifikasi Koneksi dan Method")
    print("=" * 60)

    try:
        client = TiDBClient()

        # Test 1: Cek jumlah embedding
        result = client.execute_query("SELECT COUNT(*) AS total FROM embeddings")
        total_embeddings = result[0]['total']
        print(f"\n[OK] Total embeddings di DB: {total_embeddings}")

        # Test 2: Cek dimensi vektor (harus 1536)
        dim_result = client.execute_query(
            "SELECT DIM(embedding_vector) AS dim FROM embeddings LIMIT 1"
        )
        if dim_result:
            print(f"[OK] Dimensi embedding vector: {dim_result[0]['dim']}")

        # Test 3: Cek distribusi sales_code
        code_result = client.execute_query(
            "SELECT sales_code, COUNT(*) AS cnt FROM embeddings "
            "GROUP BY sales_code ORDER BY cnt DESC LIMIT 5"
        )
        print(f"[OK] Top 5 sales_code: {[r['sales_code'] for r in code_result]}")

        # Test 4: Cek kalau sales_code generik sudah bersih
        dirty_check = client.execute_query(
            "SELECT COUNT(*) AS cnt FROM embeddings "
            "WHERE sales_code IN ('PSB', 'AMRBS', 'MD')"
        )
        dirty_count = dirty_check[0]['cnt']
        if dirty_count == 0:
            print("[OK] ✓ Tidak ada sales_code generik (PSB/AMRBS/MD) — IBAC aman.")
        else:
            print(f"[WARNING] Masih ada {dirty_count} record dengan sales_code generik!")

        print("\n" + "=" * 60)
        print("[OK] Semua test selesai.")
        print("=" * 60)
        client.disconnect()

    except Exception as e:
        print(f"\n[ERROR] Test gagal: {e}")
        raise


if __name__ == '__main__':
    main()