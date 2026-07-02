# src/initialize_db.py
"""
Initialize Database - Buat tabel dari database/schema.sql, lalu load data
tagihan dari Google Sheets, generate embeddings, dan insert ke TiDB.
"""

import logging
import os
import sys

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("embedding_generator").setLevel(logging.WARNING)

from google_sheets_loader import GoogleSheetsLoader
from embedding_generator import EmbeddingGenerator
from tidb_client import TiDBClient
from progress_bar import ProgressBar, print_section, print_step, print_success, print_error

logger = logging.getLogger(__name__)


def initialize_database():
    """Full database initialization pipeline"""

    print_section("DATABASE INITIALIZATION")

    try:
        # Step 0: Buat tabel yang diperlukan dari database/schema.sql
        print_step(0, "Checking/creating required tables...")
        db = TiDBClient()

        schema_path = os.path.join(os.path.dirname(__file__), '..', 'database', 'schema.sql')
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema_sql = f.read()

        # Ambil hanya statement CREATE TABLE (lewati komentar dan query verifikasi di akhir file)
        for statement in schema_sql.split(';'):
            statement = statement.strip()
            if not statement.upper().startswith('CREATE TABLE'):
                continue
            table_name = statement.split('EXISTS', 1)[1].strip().split(' ', 1)[0].split('(', 1)[0].strip()
            try:
                db.execute_query(statement)
                print_success(f"Table {table_name} ready")
            except Exception as e:
                logger.warning(f"[INIT] Table {table_name}: {e}")

        db.disconnect()

        # Step 1: Load data dari Google Sheets
        print_step(1, "Loading data dari Google Sheets...")
        loader = GoogleSheetsLoader()
        data = loader.load_all_sheets()
        
        if not data:
            print_error("No data loaded from Google Sheets")
            return False
        
        # Step 2: Process data (normalize + enrich)
        print_step(2, "Processing data...")
        processed_data = loader.process_all_data(data)
        
        # Step 3: Generate embeddings dengan progress bar
        print_step(3, "Generating embeddings...")
        generator = EmbeddingGenerator()
        
        embeddings_to_insert = []
        
        for sheet_type, df in processed_data.items():
            print(f"\nProcessing {sheet_type} ({len(df)} rows)...")
            
            # Create progress bar untuk sheet ini
            progress = ProgressBar(
                len(df),
                prefix=f"{sheet_type}",
                length=30,
                update_every=25,
                show_eta=True
            )
            progress.start()
            
            # Add embeddings column dengan progress tracking
            df_with_embeddings = generator.process_dataframe(df, sheet_type, progress_callback=progress.update)
            progress.finish()
            
            # Prepare data for insertion (silent, tanpa progress)
            for _, row in df_with_embeddings.iterrows():
                # Cari kolom alamat — coba beberapa kemungkinan nama kolom
                # sesuai dengan header di Google Sheets
                address_candidates = [
                    'ALAMAT USAHA', 'ALAMAT', 'ADDRESS',
                    'NAMA USAHA DAN ALAMAT', 'ALAMAT PELANGGAN'
                ]
                address_val = 'N/A'
                for col in address_candidates:
                    val = str(row.get(col, '')).strip()
                    if val and val not in ('', 'N/A', 'nan', 'None'):
                        address_val = val
                        break

                record = {
                    'sheet_name': sheet_type,
                    'snd': str(row.get('SND', 'N/A')),
                    'sales_code': str(row.get('sales_code', 'UNKNOWN')),
                    'sales_name': str(row.get('NAMA SA/AR/AM', 'Unknown')),
                    'ps_agency': str(row.get('PS AGENCY', 'N/A')),
                    'customer_name': str(row.get('CUSTOMER NAME', 'N/A')),
                    'address': address_val,
                    'pic_name': str(row.get('pic_name', 'N/A')),
                    'phone_number': str(row.get('phone_number', 'N/A')),
                    'datel': str(row.get('DATEL', 'N/A')),
                    'sto': str(row.get('STO', 'N/A')),
                    'jenis_tagihan': str(row.get('JENIS_TAGIHAN', 'N/A')),
                    'status_pembayaran': str(row.get('LUNAS', 'N/A')),
                    'saldo': float(row.get('SALDO', 0)) if str(row.get('SALDO', '0')).replace('.','').isdigit() else 0,
                    'embedding_vector': row.get('embedding_vector', []),
                    'kcontact': str(row.get('KCONTACT', '')),
                    'metadata': {'source': sheet_type}
                }
                embeddings_to_insert.append(record)
        
        # Step 4: Insert ke database dengan progress bar
        print_step(4, f"Inserting {len(embeddings_to_insert)} records ke TiDB...")
        db = TiDBClient()
        
        # Progress bar untuk database insertion
        progress_db = ProgressBar(
            len(embeddings_to_insert),
            prefix="Insert",
            length=30,
            update_every=25,
            show_eta=True
        )
        progress_db.start()
        
        rows_inserted = db.insert_embeddings(embeddings_to_insert, progress_callback=progress_db.update)
        progress_db.finish()
        
        if rows_inserted > 0:
            print_success(f"Inserted {rows_inserted} records")
            
            # Verify insertion
            result = db.execute_query("SELECT COUNT(*) as cnt FROM embeddings")
            total_count = result[0]['cnt']
            print_success(f"Total embeddings in database: {total_count}")
            
            db.disconnect()
            return True
        else:
            print_error("No records inserted")
            return False
        
    except Exception as e:
        print_error(f"Database initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = initialize_database()
    
    print_section("RESULT", length=50)
    if success:
        print_success("DATABASE INITIALIZATION COMPLETED SUCCESSFULLY")
    else:
        print_error("DATABASE INITIALIZATION FAILED")
        sys.exit(1)