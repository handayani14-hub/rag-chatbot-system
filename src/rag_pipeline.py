# src/rag_pipeline.py
"""
RAG Pipeline — Alur Retrieve → Augment → Generate untuk chatbot Billie.
Mengambil data relevan dari TiDB sebagai konteks sebelum LLM menjawab,
dengan 4 pipeline sesuai skenario query: daftar, cari by nama, cari by
SND, dan query umum.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

from config import Config
from embedding_generator import EmbeddingGenerator
from tidb_client import TiDBClient

logger = logging.getLogger(__name__)

# Batas tampilan untuk satu sheet (billper/billdu/billtri)
MAX_DISPLAY_ITEMS = 15
# Untuk Keseluruhan (semua sheet), tampilkan semua tanpa batas kecuali hard cap ini
MAX_DISPLAY_ITEMS_ALL = 100


class RAGPipeline:
    """
    Kelas utama yang mengorkestrasi alur Retrieve → Augment → Generate.

    Setiap pipeline method mengikuti pola yang sama:
    1. Retrieve  : ambil data relevan dari TiDB (dengan filter sales_code)
    2. Augment   : format data sebagai konteks teks untuk LLM
    3. Generate  : kirim ke OpenAI dan dapatkan jawaban dalam bahasa natural

    Access control sudah terintegrasi di setiap pipeline —
    semua query ke database selalu difilter berdasarkan sales_code
    milik user yang sedang login, bukan dari input user.
    """

    def __init__(self):
        # TiDB client untuk semua operasi database
        self.db = TiDBClient()

        # Embedding generator untuk konversi teks ke vektor
        self.embedding_gen = EmbeddingGenerator()

        # OpenAI client — endpoint bisa diarahkan ke Maia Router
        self.llm = OpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_BASE_URL
        )
        self.model = Config.OPENAI_MODEL

    # =========================================================================
    # HELPER — Fungsi-fungsi pembantu yang dipakai oleh semua pipeline
    # =========================================================================

    def _format_currency(self, value: Any) -> str:
        """Ubah angka ke format Rupiah Indonesia. Contoh: 1500000 → 'Rp 1.500.000'"""
        try:
            amount = float(value)
            return f"Rp {amount:,.0f}".replace(',', '.')
        except (TypeError, ValueError):
            try:
                amount = float(str(value).replace('.', '').replace(',', '.'))
                return f"Rp {amount:,.0f}".replace(',', '.')
            except (TypeError, ValueError):
                return "Rp 0"

    def _clean_value(self, value: Any, fallback: str = "Tidak tersedia") -> str:
        """
        Bersihkan nilai yang kosong atau None.
        Berguna supaya di output bot tidak muncul 'None' atau 'nan'.
        """
        if value is None:
            return fallback
        text = str(value).strip()
        if not text or text.lower() in ("nan", "none", "null", "n/a", ""):
            return fallback
        return text

    def _normalize_sheet_type(self, value: Any) -> str:
        """Normalisasi sheet_name DB (mis. 'Inkasso_billdu_20241031') → 'BILLDU'."""
        if not value:
            return "Tidak tersedia"
        text = str(value).strip().lower()
        if text in ("nan", "none", "null", "n/a", ""):
            return "Tidak tersedia"
        if 'billper' in text:
            return 'BILLPER'
        if 'billdu' in text:
            return 'BILLDU'
        if 'billtri' in text:
            return 'BILLTRI'
        return "Tidak tersedia"

    def _format_context_from_docs(self, documents: List[Dict]) -> str:
        """
        Ubah list dokumen hasil retrieval menjadi teks konteks untuk LLM.

        Kenapa perlu diformat dulu? Karena LLM butuh teks yang terstruktur
        supaya bisa memahami dan memformat jawaban dengan benar.
        Format ini sengaja dibuat verbose (detail) supaya LLM punya
        cukup informasi untuk menjawab berbagai jenis pertanyaan.
        """
        if not documents:
            return "Tidak ada data yang ditemukan."

        parts = []
        for idx, doc in enumerate(documents, start=1):
            jenis = self._normalize_sheet_type(doc.get('jenis_tagihan'))
            if jenis == "Tidak tersedia":
                jenis = self._normalize_sheet_type(doc.get('sheet_name'))
            parts.append(
                f"Data {idx}:\n"
                f"  Nama Usaha/Pelanggan : {self._clean_value(doc.get('customer_name'))}\n"
                f"  Alamat               : {self._clean_value(doc.get('address'))}\n"
                f"  SND                  : {self._clean_value(doc.get('snd'))}\n"
                f"  PIC                  : {self._clean_value(doc.get('pic_name'))}\n"
                f"  Nomor Telepon PIC    : {self._clean_value(doc.get('phone_number'))}\n"
                f"  Status Pembayaran    : {self._clean_value(doc.get('status_pembayaran'))}\n"
                f"  Saldo                : {self._format_currency(doc.get('saldo', 0))}\n"
                f"  Jenis Tagihan        : {jenis}\n"
                f"  STO                  : {self._clean_value(doc.get('sto'))}\n"
                f"  DATEL                : {self._clean_value(doc.get('datel'))}"
            )

        return "\n\n".join(parts)

    def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 800
    ) -> str:
        """
        Panggil OpenAI API dengan system prompt dan user prompt.

        System prompt = instruksi untuk LLM (formatnya seperti apa, aturan apa)
        User prompt   = pertanyaan + data konteks

        Temperature 0.2 supaya jawaban konsisten dan tidak terlalu 'kreatif' —
        kita tidak mau LLM mengarang data yang tidak ada di konteks.
        """
        try:
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ],
                temperature=0.2,
                max_tokens=max_tokens
            )
            return (response.choices[0].message.content or "").strip()

        except Exception as e:
            logger.error(f"[RAG] Panggilan LLM gagal: {e}")
            return "Maaf, saya tidak bisa memproses permintaan ini saat ini. Coba lagi nanti."

    # =========================================================================
    # PIPELINE 1 — Daftar pelanggan belum lunas
    # =========================================================================

    def pipeline_daftar(
        self,
        sales_code: str,
        sheet_name: Optional[str] = None
    ) -> Tuple[str, List[Dict], int]:
        """
        Pipeline untuk menampilkan daftar pelanggan yang belum lunas.

        Dipanggil saat user klik tombol sheet (Billper/Billdu/Billtri/Keseluruhan)
        setelah memilih 'Cari Pelanggan' dari menu utama.

        Alur:
        1. Ambil data belum lunas dari DB (dengan filter sheet_name jika ada)
        2. Kalau hasil > MAX_DISPLAY_ITEMS → ambil top-N saldo terbesar
        3. Format dengan LLM menjadi teks yang rapi

        Args:
            sales_code : kode sales yang sedang login
            sheet_name : 'billper', 'billdu', 'billtri', atau None (semua sheet)

        Returns:
            Tuple berisi:
            - response (str)    : teks jawaban dari LLM
            - documents (list)  : list data pelanggan yang diambil
            - total_count (int) : total pelanggan belum lunas (sebelum di-limit)
        """
        logger.info(f"[RAG] pipeline_daftar — sales: {sales_code}, sheet: {sheet_name or 'semua'}")

        # Untuk Keseluruhan (sheet_name=None), ambil lebih banyak data
        is_all_sheets = sheet_name is None
        db_limit = MAX_DISPLAY_ITEMS_ALL if is_all_sheets else 50

        semua_data = self.db.get_unpaid_customers(
            sales_code=sales_code,
            limit=db_limit,
            sheet_name=sheet_name
        )
        total_count = len(semua_data)

        # Kalau tidak ada data sama sekali
        if total_count == 0:
            label_sheet = sheet_name.upper() if sheet_name and sheet_name != 'all' else "semua kategori"
            return (
                f"✅ Tidak ada pelanggan yang belum lunas untuk tagihan <b>{label_sheet}</b>.\n\n"
                f"Semua pelanggan Anda sudah lunas. Kerja bagus! 🎉",
                [],
                0
            )

        # Keseluruhan → tampilkan semua; sheet tertentu → cap 15
        cap = MAX_DISPLAY_ITEMS_ALL if is_all_sheets else MAX_DISPLAY_ITEMS
        documents = semua_data[:cap]
        jumlah_tampil = len(documents)

        # Format compact langsung di Python — tidak perlu LLM untuk daftar sederhana
        tampilkan_jenis = sheet_name is None or sheet_name.lower() == 'all'
        lines = []
        for i, doc in enumerate(documents, start=1):
            nama = self._clean_value(doc.get('customer_name'), 'Nama Usaha')
            snd  = self._clean_value(doc.get('snd'), '-')
            if tampilkan_jenis:
                jenis = self._clean_value(doc.get('jenis_tagihan') or doc.get('sheet_name'), '')
                jenis_label = f" <i>({jenis.upper()})</i>" if jenis else ""
                lines.append(f"{i}. {nama} - <code>{snd}</code>{jenis_label}")
            else:
                lines.append(f"{i}. {nama} - <code>{snd}</code>")

        response = "\n".join(lines)
        response += f"\n\n<b>Total: {total_count} pelanggan belum lunas</b>"

        if total_count > cap:
            response += f"\n<i>(Menampilkan {jumlah_tampil} dari {total_count} pelanggan)</i>"

        response += "\n\n💡 Ketik nama usaha atau SND untuk informasi lebih lengkap."

        return response, documents, total_count

    # =========================================================================
    # PIPELINE 2 — Cari status satu pelanggan berdasarkan nama
    # =========================================================================

    def pipeline_status_by_name(
        self,
        user_query: str,
        sales_code: str,
        customer_name: str
    ) -> Tuple[str, List[Dict], str]:
        """
        Pipeline untuk cari detail tagihan pelanggan berdasarkan nama.

        Strateginya dua tahap — ini adalah pola 'exact first, semantic fallback':
        1. Coba exact/LIKE match dulu (cepat, tidak butuh embedding)
        2. Kalau tidak ketemu, baru pakai semantic search (butuh embedding)

        Kenapa seperti ini? Karena nama pelanggan sering unik dan spesifik,
        jadi LIKE match biasanya sudah cukup. Semantic search hanya dipakai
        kalau nama yang diketik user benar-benar berbeda jauh dari yang di DB
        (misal typo besar atau pakai nama singkatan).

        Args:
            user_query    : kalimat lengkap yang dikirim user
            sales_code    : kode sales yang sedang login
            customer_name : nama pelanggan yang diekstrak dari query

        Returns:
            Tuple berisi:
            - response (str)   : teks jawaban
            - documents (list) : data yang ditemukan
            - match_type (str) : 'exact', 'semantic', atau 'not_found'
        """
        logger.info(f"[RAG] pipeline_status_by_name — nama: '{customer_name}', sales: {sales_code}")

        # === TAHAP 1: Exact/LIKE match ===
        exact_results = self.db.search_by_customer_name_exact(customer_name, sales_code)

        if exact_results:
            logger.debug(f"[RAG] Exact match ditemukan: {len(exact_results)} hasil.")
            # Kalau ada lebih dari satu hasil (misal nama yang sama di beda sheet),
            # tampilkan semua supaya user bisa pilih yang tepat
            context  = self._format_context_from_docs(exact_results)
            response = self._generate_status_response(user_query, context, len(exact_results))
            return response, exact_results, "exact"

        # === TAHAP 2: Semantic search sebagai fallback ===
        logger.debug(f"[RAG] Exact match tidak ditemukan, coba semantic search...")
        try:
            query_embedding = self.embedding_gen.generate_embedding(customer_name)
            semantic_results = self.db.search_by_vector(
                embedding_vector=query_embedding,
                sales_code=sales_code,
                limit=3  # Ambil 3 kandidat terdekat untuk suggestion
            )
        except Exception as e:
            logger.error(f"[RAG] Semantic fallback gagal: {e}")
            semantic_results = []

        if semantic_results:
            # Cek relevansi: hanya tampilkan jika hasil benar-benar mirip.
            # Distance TiDB COSINE: 0 = identik, 2 = berlawanan.
            # Threshold 0.35 menyaring nama yang benar-benar berbeda
            # (mencegah false match saat pelanggan tidak ada di data sales ini).
            SEMANTIC_THRESHOLD = 0.35
            min_distance = min(
                float(r.get('distance', 1.0)) for r in semantic_results
            )

            if min_distance <= SEMANTIC_THRESHOLD:
                logger.debug(
                    f"[RAG] Semantic match diterima (min_distance={min_distance:.3f} "
                    f"<= {SEMANTIC_THRESHOLD})."
                )
                context  = self._format_context_from_docs(semantic_results)
                response = self._generate_status_response(
                    user_query, context, len(semantic_results), is_semantic=True
                )
                return response, semantic_results, "semantic"
            else:
                logger.debug(
                    f"[RAG] Semantic match ditolak (min_distance={min_distance:.3f} "
                    f"> {SEMANTIC_THRESHOLD}) — kemungkinan bukan pelanggan sales ini."
                )

        # === Tidak ketemu sama sekali ===
        response = (
            f"Data pelanggan <b>{customer_name}</b> tidak ditemukan "
            f"dalam cakupan tanggung jawab Anda.\n\n"
            f"💡 <i>Kemungkinan:\n"
            f"• Pelanggan ini ditangani oleh sales lain\n"
            f"• Periksa kembali ejaan nama pelanggan</i>"
        )
        return response, [], "not_found"

    def _generate_status_response(
        self,
        user_query: str,
        context: str,
        result_count: int,
        is_semantic: bool = False
    ) -> str:
        """
        Generate respons status pelanggan pakai LLM.
        Helper internal — dipanggil oleh pipeline_status_by_name.
        """
        catatan_semantic = (
            "\n\nCatatan: Data ini adalah hasil pencarian semantik karena nama persis "
            "tidak ditemukan. Mungkin ini yang dimaksud?"
            if is_semantic else ""
        )

        system_prompt = """Kamu adalah asisten sales bernama Billie untuk informasi tagihan pelanggan B2B.

Tugasmu: tampilkan detail tagihan pelanggan dengan format berikut (WAJIB persis seperti ini):

[Nama Usaha/Pelanggan]
SND: [nomor SND]
PIC: [nama PIC, atau 'Tidak tersedia']
Nomor Telepon PIC: [nomor telepon, atau 'Tidak tersedia']
STO: [kode STO, atau 'Tidak tersedia']
Status: [LUNAS / BELUM LUNAS]
Saldo: Rp [nominal]
Jenis Tagihan: [BILLPER / BILLDU / BILLTRI]
Alamat: [alamat usaha, atau 'Tidak tersedia']

Aturan:
- Baris pertama adalah Nama Usaha/Pelanggan (TANPA label "Nama:")
- Kalau ada lebih dari 1 pelanggan di data, tampilkan semua dengan format yang sama
- Pisahkan setiap pelanggan dengan garis '---'
- Jangan tampilkan Sales Code atau info internal sistem
- Kalau data tidak ada, tulis 'Tidak tersedia' bukan 'None' atau 'N/A'
- Jawab dalam bahasa Indonesia"""

        user_prompt = f"""Data tagihan pelanggan:

{context}

Pertanyaan user: {user_query}

Tampilkan detail tagihan berdasarkan data di atas.{catatan_semantic}"""

        return self._call_llm(system_prompt, user_prompt)

    # =========================================================================
    # PIPELINE 3 — Cari pelanggan berdasarkan nomor SND
    # =========================================================================

    def pipeline_status_by_snd(
        self,
        user_query: str,
        sales_code: str,
        snd: str
    ) -> Tuple[str, Optional[Dict]]:
        """
        Pipeline untuk cari detail tagihan berdasarkan nomor SND (nomor layanan).

        SND adalah identifier unik pelanggan — 13 digit angka.
        Ini adalah pencarian yang paling presisi karena tidak ada ambiguitas:
        satu SND = satu pelanggan.

        Kalau SND ada di data tapi sales_code tidak cocok, hasilnya None
        (artinya pelanggan itu milik sales lain — access control bekerja).

        Args:
            user_query : kalimat asli dari user
            sales_code : kode sales yang sedang login
            snd        : nomor SND yang ingin dicari (13 digit)

        Returns:
            Tuple berisi:
            - response (str)       : teks jawaban
            - record (Dict | None) : data pelanggan kalau ditemukan
        """
        logger.info(f"[RAG] pipeline_status_by_snd — SND: {snd}, sales: {sales_code}")

        record = self.db.search_by_snd_exact(snd=snd, sales_code=sales_code)

        # Kalau SND ditemukan dan milik sales ini
        if record:
            context = self._format_context_from_docs([record])

            system_prompt = """Kamu adalah asisten sales bernama Billie untuk informasi tagihan pelanggan B2B.

Tampilkan detail tagihan dengan format:

📋 STATUS TAGIHAN PELANGGAN

SND            : [nomor SND]
Nama Usaha     : [nama pelanggan]
PIC            : [nama PIC]
Telepon PIC    : [nomor telepon PIC]
Status         : [LUNAS / BELUM LUNAS]
Saldo          : [nominal dalam Rupiah]
Jenis Tagihan  : [jenis]
Alamat         : [alamat usaha]

Aturan:
- Isi setiap field dari data yang tersedia
- Kalau kosong, tulis 'Tidak tersedia'
- Jangan tambahkan Sales Code atau info internal
- Jawab dalam bahasa Indonesia"""

            user_prompt = f"""Data pelanggan dengan SND {snd}:

{context}

Tampilkan detail tagihan pelanggan ini."""

            response = self._call_llm(system_prompt, user_prompt)
            return response, record

        # SND tidak ditemukan — bedakan: ada di DB tapi sales lain, atau tidak ada sama sekali (B4)
        snd_clean = snd.strip()
        is_valid_format = snd_clean.isdigit() and 9 <= len(snd_clean) <= 13

        if not is_valid_format:
            # Format SND tidak valid (di luar 9-13 digit)
            response = (
                f"⚠️ <b>Format SND tidak valid</b>\n\n"
                f"Nomor <code>{snd}</code> ({len(snd_clean)} digit) bukan SND yang valid.\n\n"
                f"💡 <i>SND berupa <b>9-13 digit angka</b>. "
                f"Periksa kembali nomor yang Anda masukkan.</i>"
            )
        elif self.db.snd_exists_globally(snd_clean):
            # SND ada di sistem tapi bukan milik sales ini
            response = (
                f"🔒 <b>SND di luar cakupan Anda</b>\n\n"
                f"Nomor SND <code>{snd}</code> terdaftar di sistem, "
                f"namun bukan dalam tanggung jawab Anda.\n\n"
                f"💡 <i>Nomor SND ini ditangani oleh sales lain. "
                f"Hubungi koordinator Anda jika butuh akses.</i>"
            )
        else:
            # SND tidak ada sama sekali di database
            response = (
                f"❌ <b>SND tidak ditemukan</b>\n\n"
                f"Nomor <code>{snd}</code> tidak terdaftar di sistem.\n\n"
                f"💡 <i>Kemungkinan:\n"
                f"• Periksa kembali nomor SND yang Anda masukkan\n"
                f"• Data mungkin belum diperbarui dari Google Sheets</i>"
            )

        return response, None

    # =========================================================================
    # PIPELINE 4 — Query umum (semantic search + out-of-scope handling)
    # =========================================================================

    def pipeline_general(
        self,
        user_query: str,
        sales_code: str
    ) -> Tuple[str, List[Dict]]:
        """
        Pipeline fallback untuk query yang tidak masuk kategori spesifik.

        Cara kerjanya:
        1. Query diubah ke vektor embedding
        2. Cari dokumen paling relevan di TiDB pakai cosine similarity
        3. Kalau similarity score-nya terlalu rendah (dokumen tidak relevan),
           artinya pertanyaan di luar cakupan — tampilkan pesan khusus
        4. Kalau ada dokumen relevan, generate jawaban normal

        Skenario yang masuk ke sini:
        - Pertanyaan yang tidak cocok keyword manapun di detect_query_intent
        - Pertanyaan campuran Bahasa Indonesia + Inggris (Skenario 20)
        - Pertanyaan dengan konteks yang tidak biasa

        Args:
            user_query : pertanyaan dari user
            sales_code : kode sales yang sedang login

        Returns:
            Tuple berisi:
            - response (str)   : teks jawaban
            - documents (list) : dokumen yang diambil (kosong kalau out-of-scope)
        """
        logger.info(f"[RAG] pipeline_general — query: '{user_query[:50]}...', sales: {sales_code}")

        # Konversi query ke vektor embedding
        try:
            query_embedding = self.embedding_gen.generate_embedding(user_query)
        except Exception as e:
            logger.error(f"[RAG] Gagal generate embedding: {e}")
            return "Maaf, sistem sedang mengalami gangguan. Silakan coba lagi.", []

        # Ambil dokumen paling mirip dari TiDB
        documents = self.db.search_by_vector(
            embedding_vector=query_embedding,
            sales_code=sales_code,
            limit=5
        )

        # Kalau tidak ada dokumen sama sekali → pasti out-of-scope
        if not documents:
            return _out_of_scope_message(), []

        # Cek relevansi: cosine distance di TiDB = 0 (identik) sampai 2 (berbeda 180°)
        # Distance > 0.8 biasanya artinya dokumen tidak terlalu relevan dengan query
        # Nilai threshold ini bisa disesuaikan setelah testing
        distances     = [float(doc.get('distance', 1.0)) for doc in documents]
        avg_distance  = sum(distances) / len(distances)
        MIN_RELEVANCE = 0.7  # Kalau rata-rata distance > ini, anggap out-of-scope

        if avg_distance > MIN_RELEVANCE:
            logger.debug(
                f"[RAG] Average distance {avg_distance:.3f} > threshold {MIN_RELEVANCE} "
                f"— query kemungkinan di luar cakupan."
            )
            return _out_of_scope_message(), []

        # Ada dokumen yang relevan — generate jawaban
        context = self._format_context_from_docs(documents)

        system_prompt = """Kamu adalah asisten sales bernama Billie untuk informasi tagihan pelanggan B2B.

Jawab pertanyaan user berdasarkan data yang tersedia.

Aturan:
- Gunakan hanya informasi dari data konteks
- Kalau informasi tidak tersedia di data, katakan 'Data tidak tersedia'
- Jangan membuat informasi atau asumsi yang tidak ada di data
- Format jawaban singkat dan cocok untuk dibaca di Telegram
- Jawab dalam bahasa Indonesia (boleh ada kata Inggris kalau memang relevan)
- Selalu sertakan SND sebagai identitas utama pelanggan
- JANGAN gunakan tanda bintang (**) atau underscore (_) untuk formatting. Gunakan teks biasa saja."""

        user_prompt = f"""Data tagihan yang relevan:

{context}

Pertanyaan: {user_query}

Jawab berdasarkan data di atas."""

        response = self._call_llm(system_prompt, user_prompt)
        return response, documents

    # =========================================================================
    # PIPELINE RINGKASAN — Untuk tombol 'Ringkasan Saya' dan intent ringkasan
    # =========================================================================

    def pipeline_ringkasan_umum(self, sales_code: str) -> str:
        """
        Generate ringkasan umum data tagihan untuk satu sales (format C1.1).

        Dipanggil ketika user klik tombol '📊 Ringkasan Saya' atau
        tanya 'bagaimana ringkasan tagihan saya?'
        """
        logger.info(f"[RAG] pipeline_ringkasan_umum — sales: {sales_code}")

        stats = self.db.get_summary_stats(sales_code)

        if stats['total'] == 0:
            return "Tidak ada data tagihan yang ditemukan untuk akun Anda."

        total     = stats['total']
        lunas     = stats['lunas']
        bl        = stats['belum_lunas']
        pct_lunas = stats['persen_lunas']
        pct_bl    = stats['persen_belum_lunas']
        total_sal = self._format_currency(stats['total_saldo'])

        # Tagihan terbesar
        info_terbesar = "Tidak tersedia"
        if stats.get('terbesar'):
            t = stats['terbesar']
            nama  = self._clean_value(t.get('customer_name'))
            saldo = self._format_currency(t.get('saldo', 0))
            sheet = self._clean_value(t.get('sheet_name'))
            info_terbesar = f"{nama} - {saldo} ({sheet})"

        # Rincian per sheet: total pelanggan per sheet / total all
        per_sheet_total = self.db.count_all_by_sheet(sales_code)
        sheet_order = [('billper', 'Billper'), ('billdu', 'Billdu'), ('billtri', 'Billtri')]
        rincian = []
        for i, (key, label) in enumerate(sheet_order):
            count        = per_sheet_total.get(key, 0)
            persen_sheet = round(count / total * 100, 1) if total > 0 else 0
            prefix       = "└─" if i == len(sheet_order) - 1 else "├─"
            rincian.append(f"{prefix} {label}: {count} pelanggan ({persen_sheet}%)")

        return (
            f"📊 <b>RINGKASAN TAGIHAN ANDA</b>\n\n"
            f"Total Pelanggan: <b>{total}</b>\n"
            f"├─ Lunas: {lunas} ({pct_lunas}%)\n"
            f"└─ Belum Lunas: {bl} ({pct_bl}%)\n\n"
            f"Total Saldo Tertunggak: <b>{total_sal}</b>\n\n"
            f"Tagihan Terbesar: {info_terbesar}\n\n"
            f"<b>Rincian per Jenis Tagihan:</b>\n"
            + "\n".join(rincian) +
            "\n\n════════════════════════════════\n\n"
            "<i>Perlu detail lebih lanjut? Gunakan menu Cari Pelanggan.</i>"
        )

    def pipeline_ringkasan_belum_lunas(self, sales_code: str) -> str:
        """
        Ringkasan khusus belum lunas (format C1.2).
        Persentase per sheet dihitung dari TOTAL BELUM LUNAS, bukan total keseluruhan.
        """
        logger.info(f"[RAG] pipeline_ringkasan_belum_lunas — sales: {sales_code}")

        stats = self.db.get_unpaid_summary_stats(sales_code)

        if stats['total_belum_lunas'] == 0:
            return "✅ Tidak ada pelanggan yang belum lunas saat ini. Semua sudah lunas!"

        total_bl  = stats['total_belum_lunas']
        persen    = stats['persen_dari_total']
        total_sal = self._format_currency(stats['total_saldo'])

        # Tagihan terbesar
        info_terbesar = "Tidak tersedia"
        if stats.get('terbesar'):
            t = stats['terbesar']
            nama  = self._clean_value(t.get('customer_name'))
            saldo = self._format_currency(t.get('saldo', 0))
            sheet = self._clean_value(t.get('sheet_name'))
            info_terbesar = f"{nama} - {saldo} ({sheet})"

        # Rincian per sheet dengan tree format (├─ └─)
        per_sheet   = stats.get('per_sheet', {})
        sheet_order = [('billper', 'Billper'), ('billdu', 'Billdu'), ('billtri', 'Billtri')]
        rincian = []
        for i, (key, label) in enumerate(sheet_order):
            count  = per_sheet.get(key, {}).get('count', 0)
            persen_sheet = per_sheet.get(key, {}).get('persen', 0)
            prefix = "└─" if i == len(sheet_order) - 1 else "├─"
            rincian.append(f"{prefix} {label}: {count} pelanggan ({persen_sheet}%)")

        return (
            f"📊 <b>RINGKASAN BELUM LUNAS</b>\n\n"
            f"Total Pelanggan Belum Lunas: <b>{total_bl} ({persen}%)</b>\n\n"
            f"Total Saldo Tertunggak: <b>{total_sal}</b>\n\n"
            f"Tagihan Terbesar: {info_terbesar}\n\n"
            f"<b>Rincian per Jenis Tagihan:</b>\n"
            + "\n".join(rincian) +
            "\n\n════════════════════════════════\n\n"
            "<i>Tindak lanjut penagihan segera untuk mencegah isolasi layanan.</i>"
        )

    def pipeline_ringkasan_saldo(
        self,
        sales_code: str,
        sheet_name: Optional[str] = None
    ) -> str:
        """
        Ringkasan saldo (format C1.3) — top 3 pelanggan dengan saldo terbesar.
        Jika sheet_name diisi, hanya menghitung saldo untuk sheet tersebut.
        """
        logger.info(
            f"[RAG] pipeline_ringkasan_saldo — sales: {sales_code}, "
            f"sheet: {sheet_name or 'semua'}"
        )

        stats = self.db.get_saldo_summary(sales_code, sheet_name=sheet_name)

        sheet_label = sheet_name.upper() if sheet_name else "SEMUA KATEGORI"

        if stats['jumlah_belum_lunas'] == 0:
            return (
                f"✅ Tidak ada saldo tertunggak"
                f"{f' untuk {sheet_label}' if sheet_name else ''}. "
                f"Semua pelanggan sudah lunas!"
            )

        total_sal = self._format_currency(stats['total_saldo'])
        jumlah_bl = stats['jumlah_belum_lunas']
        jumlah_lunas = stats.get('jumlah_lunas', 0)

        top3_lines = ""
        for i, item in enumerate(stats.get('top3', []), start=1):
            nama  = self._clean_value(item.get('customer_name'))
            saldo = self._format_currency(item.get('saldo', 0))
            jenis = self._normalize_sheet_type(item.get('sheet_name'))
            jenis_label = f" ({jenis})" if jenis != "Tidak tersedia" else ""
            top3_lines += f"\n{i}. {nama} - {saldo}{jenis_label}"

        header = (
            f"💰 <b>TOTAL SALDO TERTUNGGAK — {sheet_label}</b>"
            if sheet_name else
            f"💰 <b>TOTAL SALDO TERTUNGGAK</b>"
        )

        return (
            f"{header}\n\n"
            f"Total Saldo: <b>{total_sal}</b>\n\n"
            f"<b>Rincian Status:</b>\n"
            f"├─ Belum Lunas: {total_sal} ({jumlah_bl} pelanggan)\n"
            f"└─ Lunas: Rp 0 ({jumlah_lunas} pelanggan)\n\n"
            f"<b>Pelanggan dengan Saldo Terbesar:</b>"
            f"{top3_lines}"
        )

    # =========================================================================
    # PIPELINE MULTI-SHEET — Gabungkan data dari beberapa sheet sekaligus
    # =========================================================================

    def pipeline_daftar_multi_sheet(
        self,
        sales_code: str,
        sheet_names: List[str]
    ) -> Tuple[str, List[Dict], int]:
        """
        Tampilkan daftar belum lunas dari beberapa sheet sekaligus (gabungan).

        Dipanggil saat user minta data dari lebih dari satu sheet, misal:
        - "gabungkan daftar belum lunas dari billper dan billdu"
        - "dari billper dan billdu, mana yang paling besar tagihannya?"

        Data dari setiap sheet diambil terpisah lalu digabung dan diurutkan
        berdasarkan saldo terbesar, sehingga tagihan terbesar muncul di atas.

        Args:
            sales_code  : kode sales yang sedang login
            sheet_names : list sheet yang diminta, misal ['billper', 'billdu']

        Returns:
            Tuple (response, documents, total_count)
        """
        logger.info(
            f"[RAG] pipeline_daftar_multi_sheet — sales: {sales_code}, "
            f"sheets: {sheet_names}"
        )

        all_data: List[Dict] = []
        for sheet in sheet_names:
            sheet_data = self.db.get_unpaid_customers(
                sales_code=sales_code,
                limit=MAX_DISPLAY_ITEMS_ALL,
                sheet_name=sheet
            )
            all_data.extend(sheet_data)

        total_count = len(all_data)

        if total_count == 0:
            sheets_label = " + ".join(s.upper() for s in sheet_names)
            return (
                f"✅ Tidak ada pelanggan belum lunas untuk {sheets_label}.\n\n"
                f"Semua pelanggan Anda sudah lunas! 🎉",
                [],
                0
            )

        # Urutkan gabungan berdasarkan saldo terbesar
        def _parse_saldo(doc: Dict) -> float:
            try:
                v = doc.get('saldo', 0)
                return float(str(v or 0).replace('.', '').replace(',', '.'))
            except (ValueError, TypeError):
                return 0.0

        all_data.sort(key=_parse_saldo, reverse=True)

        cap       = MAX_DISPLAY_ITEMS_ALL
        documents = all_data[:cap]

        sheets_label = " + ".join(s.upper() for s in sheet_names)
        lines = []
        for i, doc in enumerate(documents, start=1):
            nama  = self._clean_value(doc.get('customer_name'), 'Nama Usaha')
            snd   = self._clean_value(doc.get('snd'), '-')
            jenis = self._normalize_sheet_type(doc.get('jenis_tagihan'))
            if jenis == "Tidak tersedia":
                jenis = self._normalize_sheet_type(doc.get('sheet_name'))
            label = f" <i>({jenis})</i>" if jenis != "Tidak tersedia" else ""
            lines.append(f"{i}. {nama} - <code>{snd}</code>{label}")

        response = "\n".join(lines)
        response += f"\n\n<b>Total: {total_count} pelanggan belum lunas ({sheets_label})</b>"

        if total_count > cap:
            response += f"\n<i>(Menampilkan {cap} dari {total_count} pelanggan)</i>"

        response += "\n\n💡 Ketik nama usaha atau SND untuk informasi lebih lengkap."

        return response, documents, total_count

    # =========================================================================
    # PIPELINE LAMA — Tetap ada untuk backward compatibility
    # =========================================================================

    def pipeline(
        self,
        user_query: str,
        sales_code: str,
        query_type: str = "general",
        top_k: int = 5
    ) -> Tuple[str, List[Dict]]:
        """
        Pipeline generik (warisan dari versi sebelumnya).

        Tetap dipertahankan karena beberapa bagian kode lama masih memanggilnya.
        Untuk pengembangan baru, sebaiknya gunakan pipeline yang spesifik.
        """
        if query_type == "daftar_pelanggan":
            response, documents, _ = self.pipeline_daftar(sales_code)
            return response, documents

        # Untuk query lainnya gunakan pipeline_general
        return self.pipeline_general(user_query, sales_code)


# =========================================================================
# HELPER MODULE-LEVEL — Fungsi standalone yang dipakai di dalam modul ini
# =========================================================================

def _get_sheet_label(sheet_name: Optional[str]) -> str:
    """Ubah kode sheet menjadi label yang rapi untuk ditampilkan ke user."""
    labels = {
        'billper': '📄 BILLPER (Periode Berjalan)',
        'billdu':  '⏰ BILLDU (Jatuh Tempo)',
        'billtri': '📅 BILLTRI (3 Bulan Terakhir)',
        None:      '📋 KESELURUHAN (Semua Kategori)',
        'all':     '📋 KESELURUHAN (Semua Kategori)'
    }
    key = sheet_name.lower() if sheet_name else None
    return labels.get(key, f"📄 {(sheet_name or 'SEMUA').upper()}")


def _out_of_scope_message() -> str:
    """
    Pesan standar saat pertanyaan user di luar cakupan sistem.
    Dipisah jadi fungsi sendiri supaya pesannya konsisten di semua pipeline.
    """
    return (
        "Maaf, saya hanya dapat membantu untuk informasi tagihan pelanggan "
        "yang berada di bawah tanggung jawab Anda. 😊\n\n"
        "Untuk pertanyaan lain, silakan hubungi atasan Anda.\n\n"
        "💡 <i>Contoh yang bisa saya bantu:\n"
        "• 'Siapa pelanggan saya yang belum lunas?'\n"
        "• 'Berapa saldo CV Maju Jaya?'\n"
        "• 'Status tagihan 3315000012345?'</i>"
    )


# =========================================================================
# MAIN — Test pipeline secara manual
# =========================================================================

if __name__ == '__main__':
    print("=" * 60)
    print("TEST: RAG Pipeline — Verifikasi Koneksi dan Pipeline")
    print("=" * 60)

    try:
        rag = RAGPipeline()
        print("[OK] RAGPipeline berhasil diinisialisasi.")
        print(f"[OK] Model LLM : {rag.model}")
        print(f"[OK] DB        : TiDBClient terhubung")

        # Test ambil satu sales_code yang ada
        result = rag.db.execute_query(
            "SELECT sales_code FROM embeddings LIMIT 1"
        )
        if result:
            test_code = result[0]['sales_code']
            print(f"\n[INFO] Menggunakan sales_code: {test_code} untuk test...")

            # Test pipeline_daftar
            print("\n[TEST] pipeline_daftar...")
            response, docs, total = rag.pipeline_daftar(test_code, 'billper')
            print(f"  Total: {total} | Docs: {len(docs)}")
            print(f"  Preview: {response[:100]}...")

        print("\n[OK] Test RAG Pipeline selesai.")

    except Exception as e:
        print(f"\n[ERROR] Test gagal: {e}")
        import traceback
        traceback.print_exc()