-- =============================================================
-- database/schema.sql
-- TiDB Schema — RAG Chatbot dengan IBAC
-- Database: RAG
--
-- CARA JALANKAN:
-- 1. Connect ke TiDB dengan MySQL client
-- 2. USE RAG;
-- 3. Jalankan seluruh file ini
--
-- URUTAN EKSEKUSI: Jalankan sesuai urutan tabel di bawah.
-- Hapus database lama sebelum jika ingin mulai dari nol:
--   DROP DATABASE IF EXISTS RAG;
--   CREATE DATABASE RAG CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   USE RAG;
-- =============================================================


-- =============================================================
-- TABLE 1: embeddings
-- Menyimpan data pelanggan beserta vector embedding untuk RAG.
-- Sumber data: Google Sheets (billper, billdu, billtri).
-- Access control diterapkan di sini via kolom sales_code.
-- =============================================================

CREATE TABLE IF NOT EXISTS embeddings (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- Sumber data
    sheet_name          VARCHAR(50) NOT NULL,       -- billper / billdu / billtri

    -- Identitas layanan
    snd                 VARCHAR(20) NOT NULL,        -- Nomor layanan pelanggan (13 digit)

    -- Identitas sales (basis IBAC)
    sales_code          VARCHAR(50) NOT NULL,        -- Kode unik sales
    sales_name          VARCHAR(100),               -- Nama sales

    -- Agency
    ps_agency           VARCHAR(100),               -- Nama agency sales

    -- Data pelanggan
    customer_name       VARCHAR(255) NOT NULL,       -- Nama usaha / perusahaan
    address             VARCHAR(500),               -- Alamat usaha / pelanggan
    pic_name            VARCHAR(150),               -- Nama PIC perusahaan
    phone_number        VARCHAR(30),                -- Nomor telepon PIC

    -- Wilayah
    datel               VARCHAR(50),                -- Daerah Telekomunikasi
    sto                 VARCHAR(50),                -- STO

    -- Data tagihan
    jenis_tagihan       VARCHAR(50),                -- billper / billdu / billtri
    status_pembayaran   VARCHAR(50),                -- LUNAS / BELUM LUNAS
    saldo               DECIMAL(15, 2),             -- Nominal tagihan

    -- Vector embedding (1536 dimensi untuk text-embedding-3-small)
    embedding_vector    VECTOR(1536) NOT NULL,

    -- Metadata tambahan
    metadata            JSON,

    -- Timestamps
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Indexes
    INDEX idx_sales_code        (sales_code),
    INDEX idx_snd               (snd),
    INDEX idx_sheet_name        (sheet_name),
    INDEX idx_status            (status_pembayaran),
    INDEX idx_datel             (datel),

    -- Cegah duplikasi record yang sama pada sheet yang sama
    UNIQUE KEY unique_snd_sheet (snd, sheet_name)

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- =============================================================
-- TABLE 2: sales_registry
-- Registrasi akun sales Telegram ke sistem.
-- Mapping: chat_id Telegram → sales_code (1:1, immutable).
--
-- Desain keamanan:
-- - is_active    : kontrol admin (deaktivasi permanen oleh admin)
-- - is_logged_in : kontrol sesi user (logout/login harian)
-- - Logout TIDAK menghapus record (soft session management)
-- - Re-register dengan sales_code berbeda DITOLAK di application layer
-- =============================================================

CREATE TABLE IF NOT EXISTS sales_registry (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- Identitas Telegram
    chat_id             VARCHAR(50) NOT NULL UNIQUE,    -- Telegram chat ID

    -- Identitas sales (dari KCONTACT / embeddings)
    sales_code          VARCHAR(50) NOT NULL UNIQUE,    -- Kode sales (1 chat_id = 1 sales_code)
    sales_name          VARCHAR(100) NOT NULL,          -- Nama sales
    ps_agency           VARCHAR(100),                   -- Agency sales
    datel               VARCHAR(50),                    -- Wilayah

    -- Status akun (dikelola admin)
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = dinonaktifkan admin

    -- Status sesi (dikelola user sendiri)
    is_logged_in        BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = sedang logout
    last_login_at       TIMESTAMP NULL,
    last_logout_at      TIMESTAMP NULL,

    -- Informasi registrasi
    registration_method VARCHAR(50) DEFAULT 'self-register',  -- self-register / admin

    -- Timestamps
    registered_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity_at    TIMESTAMP NULL,

    -- Indexes
    INDEX idx_chat_id   (chat_id),
    INDEX idx_sales_code(sales_code),
    INDEX idx_is_active (is_active),
    INDEX idx_logged_in (is_logged_in)

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- =============================================================
-- TABLE 3: conversation_log
-- Rekam semua percakapan user dengan bot.
-- Dua fungsi: audit trail + data penelitian (metrik BAB IV).
-- Response time dan detail teknis TIDAK ditampilkan ke user,
-- hanya disimpan di sini untuk evaluasi performa RAG.
-- =============================================================

CREATE TABLE IF NOT EXISTS conversation_log (
    id                          BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- Identitas user
    chat_id                     VARCHAR(50) NOT NULL,
    sales_code                  VARCHAR(50),            -- NULL jika belum register

    -- Isi percakapan
    user_query                  TEXT NOT NULL,
    bot_response                TEXT,
    query_type                  VARCHAR(50),            -- intent: general / status_pelanggan / ringkasan / dll

    -- Detail proses RAG (untuk penelitian)
    retrieved_documents_count   INT DEFAULT 0,
    similarity_scores           JSON,

    -- Metrik performa (untuk BAB IV)
    response_time_ms            INT,
    error_occurred              BOOLEAN DEFAULT FALSE,
    error_message               VARCHAR(500),

    -- Timestamp
    timestamp                   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Indexes
    INDEX idx_chat_id   (chat_id),
    INDEX idx_sales_code(sales_code),
    INDEX idx_timestamp (timestamp),
    INDEX idx_query_type(query_type),
    INDEX idx_error     (error_occurred)

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- =============================================================
-- TABLE 4: access_control_log
-- Log keamanan untuk setiap percobaan akses ke sistem.
-- PENTING: Tabel ini harus append-only (tidak boleh di-UPDATE/DELETE).
-- Jalankan REVOKE di bawah setelah tabel dibuat.
-- =============================================================

CREATE TABLE IF NOT EXISTS access_control_log (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,

    -- Identitas
    chat_id         VARCHAR(50),
    sales_code      VARCHAR(50),

    -- Detail akses
    access_type     VARCHAR(50),        -- granted / denied / unregistered /
                                        -- login / logout / register /
                                        -- blocked_identity_switch
    reason          VARCHAR(255),
    accessed_resource VARCHAR(255),

    -- Timestamp
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Indexes
    INDEX idx_chat_id       (chat_id),
    INDEX idx_sales_code    (sales_code),
    INDEX idx_access_type   (access_type),
    INDEX idx_timestamp     (timestamp)

) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Setelah tabel dibuat, jalankan ini untuk enforce append-only:
-- (Ganti 'chatbot_app_user' dengan nama user DB yang dipakai aplikasi)
--
-- REVOKE UPDATE, DELETE ON RAG.access_control_log FROM 'chatbot_app_user'@'%';
-- GRANT SELECT, INSERT ON RAG.access_control_log TO 'chatbot_app_user'@'%';


-- =============================================================
-- VERIFIKASI: Jalankan ini untuk cek semua tabel berhasil dibuat
-- =============================================================

SELECT
    TABLE_NAME,
    TABLE_ROWS,
    ROUND(DATA_LENGTH / 1024, 1)  AS data_kb,
    ROUND(INDEX_LENGTH / 1024, 1) AS index_kb
FROM
    INFORMATION_SCHEMA.TABLES
WHERE
    TABLE_SCHEMA = 'RAG'
ORDER BY
    TABLE_NAME;
