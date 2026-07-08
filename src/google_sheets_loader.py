# src/google_sheets_loader.py
"""
Google Sheets Data Loader
Purpose: Fetch data dari Google Spreadsheet dan normalize
Project: RAG Chatbot untuk Akses Informasi Tagihan Pelanggan
"""

import pandas as pd
import logging
import re
from typing import Dict, List, Tuple
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from config import Config

# Setup logger
logger = logging.getLogger(__name__)


class GoogleSheetsLoader:
    """
    Class untuk load dan manage data dari Google Sheets.
    
    Features:
    - Load data dari multiple sheets
    - Normalize dan clean data
    - Handle missing values
    - Extract sales codes dari KCONTACT column
    """
    
    def __init__(self):
        """
        Initialize Google Sheets API client.
        Menggunakan service account credentials dari .env
        """
        try:
            # Define scopes yang dibutuhkan
            SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
            
            # Load service account credentials dari JSON file
            self.credentials = Credentials.from_service_account_file(
                Config.GOOGLE_SHEETS_API_KEY,
                scopes=SCOPES
            )
            
            # Build Google Sheets API client
            self.service = build('sheets', 'v4', credentials=self.credentials)
            self.spreadsheet_id = Config.GOOGLE_SPREADSHEET_ID
            
            logger.info("[LOADER] [OK] Google Sheets API initialized successfully")
            
        except Exception as e:
            logger.error(f"[LOADER] [ERROR] Failed to initialize Google Sheets API: {e}")
            raise
    
    def load_sheet_data(self, sheet_name: str) -> pd.DataFrame:
        """
        Load data dari specific sheet di Google Spreadsheet.
        
        Args:
            sheet_name (str): Nama sheet (contoh: 'BILLPER-APRIL')
        
        Returns:
            pd.DataFrame: DataFrame berisi data dari sheet
        
        Raises:
            Exception: Jika gagal load data
        """
        try:
            # Build range string (A:Z untuk ambil semua columns)
            range_name = f"'{sheet_name}'!A:Z"
            
            logger.info(f"[LOADER] Loading data from sheet: {sheet_name}")
            
            # Call Google Sheets API untuk get values
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=range_name
            ).execute()
            
            values = result.get('values', [])
            
            if not values:
                logger.warning(f"[LOADER] [WARNING] Sheet '{sheet_name}' is empty")
                return pd.DataFrame()
            
            headers = values[0]
            rows = values[1:]  

            normalized_rows = []
            for row in rows:
                if len(row) < len(headers):
                    row = row + [""] * (len(headers) - len(row))
                elif len(row) > len(headers):
                    row = row[:len(headers)]

                normalized_rows.append(row)

            # Convert ke DataFrame
            # Row pertama adalah header
            df = pd.DataFrame(normalized_rows, columns=headers)

            logger.info(f"[LOADER] [OK] Loaded {len(df)} rows from '{sheet_name}'")
            
            return df
            
        except Exception as e:
            logger.error(f"[LOADER] [ERROR] Failed to load sheet '{sheet_name}': {e}")
            raise
    
    def load_all_sheets(self) -> Dict[str, pd.DataFrame]:
        """
        Load data dari semua sheets yang defined di Config.
        
        Returns:
            Dict[str, pd.DataFrame]: Dictionary dengan key=sheet_type, value=DataFrame
                                     Contoh: {'billper': df1, 'billdu': df2, 'billtri': df3}
        """
        all_data = {}
        
        for sheet_type, sheet_name in Config.SHEET_NAMES.items():
            try:
                df = self.load_sheet_data(sheet_name)
                all_data[sheet_type] = df
                
            except Exception as e:
                logger.error(f"[LOADER] [ERROR] Failed to load {sheet_type} from {sheet_name}: {e}")
                # Continue loading other sheets
                continue
        
        logger.info(f"[LOADER] [OK] All sheets loaded. Total sheets: {len(all_data)}")
        return all_data
    
    @staticmethod
    def normalize_data(df: pd.DataFrame, sheet_type: str) -> pd.DataFrame:
        """
        Normalize dan clean data dari sheet.
        
        Operations:
        - Remove whitespace dari columns
        - Handle missing values
        - Standardize status values
        - Extract sales code dari KCONTACT
        
        Args:
            df (pd.DataFrame): Raw DataFrame dari Google Sheets
            sheet_type (str): Jenis sheet ('billper', 'billdu', 'billtri')
        
        Returns:
            pd.DataFrame: Normalized DataFrame
        """
        df = df.copy()
        
        # Step 1: Strip whitespace dari column names
        df.columns = df.columns.str.strip()
        
        # Step 2: Strip whitespace dari semua string values
        for col in df.columns:
            if df[col].dtype == 'object':
                df[col] = df[col].str.strip()
        
        # Step 3: Handle missing values - fill dengan 'N/A'
        df = df.fillna('N/A')
        
        # Step 4: Standardize LUNAS status (should be 'LUNAS' or 'BELUM LUNAS')
        if 'LUNAS' in df.columns:
            df['LUNAS'] = df['LUNAS'].str.upper()
            # Standardize variations
            df['LUNAS'] = df['LUNAS'].replace({
                'Y': 'LUNAS',
                'YES': 'LUNAS',
                'N': 'BELUM LUNAS',
                'NO': 'BELUM LUNAS',
                '': 'BELUM LUNAS'
            })
        
        # Step 5: Add sheet_type column untuk tracking
        df['sheet_type'] = sheet_type
        
        logger.debug(f"[LOADER] Normalized {len(df)} rows for sheet_type={sheet_type}")
        
        return df
    
    @staticmethod
    def extract_sales_code(kcontact_value: str) -> str:
        """
        Extract kode identitas sales dari kolom KCONTACT.

        Pola yang didukung:
        1. |MB20100|/Nama/NoHP/email
        -> MB20100

        2. PSB;ISE228893;Budi;NoHP;Produk
        -> ISE228893

        3. AMRBS;205907;Rafi;PIC Rafi - NoHP;Produk
        -> 205907

        4. MD|MYDB-xxxx|SC18675/MC37653|Nama|NoHP
        -> SC18675/MC37653
        """
        if not kcontact_value or kcontact_value == "N/A":
            return "UNKNOWN"

        text = str(kcontact_value).strip()

        # Format 1: |MB20100|/...
        if text.startswith("|"):
            parts = [p.strip() for p in text.split("|") if p.strip()]
            if parts:
                return parts[0]

        # Format 2: PSB;ISE228893;...
        if text.startswith("PSB;"):
            parts = [p.strip() for p in text.split(";") if p.strip()]
            if len(parts) > 1:
                return parts[1]

        # Format 3: AMRBS;205907;...
        if text.startswith("AMRBS;"):
            parts = [p.strip() for p in text.split(";") if p.strip()]
            if len(parts) > 1:
                return parts[1]

        # Format 4: MD|MYDB-xxxx|SC18675/MC37653|...
        if text.startswith("MD|"):
            parts = [p.strip() for p in text.split("|") if p.strip()]
            if len(parts) > 2:
                return parts[2]

        # Fallback lama
        for sep in [";", "|", ",", "/"]:
            if sep in text:
                parts = [p.strip() for p in text.split(sep) if p.strip()]
                if parts:
                    return parts[0]

        return text[:20] if text else "UNKNOWN"
    
    @staticmethod
    def extract_pic_name(kcontact_value: str) -> str:
        """
        Extract nama PIC perusahaan/pelanggan dari kolom KCONTACT.

        Contoh:
        RBS/R2/AM CINDY TRIANA RULYTA/0821556944615/KEMAS ARIEF FAISAL/087875177313/1S 50MB/legal@kecilin.id
        -> KEMAS ARIEF FAISAL
        """
        if not kcontact_value or kcontact_value == "N/A":
            return "N/A"

        text = str(kcontact_value).strip()

        # Format RBS/R2/AM SALES/NO SALES/PIC CUSTOMER/NO CUSTOMER/...
        if "/" in text:
            parts = [p.strip() for p in text.split("/") if p.strip()]

            # Ambil bagian sebelum nomor telepon pelanggan.
            # Dalam contoh: index 4 = KEMAS ARIEF FAISAL
            for i, part in enumerate(parts):
                if re.fullmatch(r"(?:\+62|62|0)8[0-9]{8,13}", part):
                    if i >= 1:
                        candidate = parts[i - 1]

                        phone_count_before = sum(
                            1 for p in parts[:i]
                            if re.fullmatch(r"(?:\+62|62|0)8[0-9]{8,13}", p)
                        )

                        if phone_count_before >= 1:
                            # Format panjang: sales_name/sales_phone/pic_name/pic_phone/...
                            # Nama PIC adalah teks sebelum nomor telepon ke-2
                            return candidate[:150]

                        elif phone_count_before == 0 and re.fullmatch(r"\|[A-Z0-9]+\|", parts[0]):
                            # Format |sales_code|/pic_name/pic_phone/...
                            # Hanya ada satu nomor telepon, bagian pertama adalah kode sales
                            # dalam tanda pipa — teks sebelum nomor tersebut adalah nama PIC
                            return candidate[:150]

            return "N/A"

        if ";" in text:
            parts = [p.strip() for p in text.split(";") if p.strip()]

            # Cek apakah ada bagian yang dimulai dengan "PIC "
            # Format: AMRBS;940102;SALES_NAME;PIC Customer Name - 08xxx;PRODUK
            for part in parts:
                pic_match = re.match(r"PIC\s+(.+?)\s*-\s*(?:\+62|62|0)8[0-9]{8,13}", part)
                if pic_match:
                    return pic_match.group(1).strip()[:150]

            # Fallback: format lama PSB;ISE228893;Budi;087464457467;...
            if len(parts) >= 3:
                return parts[2][:150]

        return "N/A"


    @staticmethod
    def extract_sales_name(kcontact_value: str) -> str:
        """
        Extract sales name dari KCONTACT column.
        Mendukung separator: |, koma, dan titik koma.

        Args:
            kcontact_value (str): Value dari KCONTACT column
        
        Returns:
            str: Extracted sales name
        """
        if not kcontact_value or kcontact_value == 'N/A':
            return 'Unknown'
        
        # Split by pipe (|) atau comma
        parts = (
            str(kcontact_value)
            .replace(',', '|')
            .replace(';', '|')
            .split('|')
        )
        
        parts = [p.strip() for p in parts if p.strip()]
        
        # Jika formatnya "AMRBS;101812;Erik:PIC ERik..."
        # Biasanya nama singkat ada di index ke-2
        if len(parts) >= 3:
            return parts[2][:100]  # Truncate jika terlalu panjang


        # Look untuk part yang bukan sales code
        for part in parts:
            part = part.strip()
            if not part.startswith(('MB', 'MN')):
                return part[:100]  # Truncate jika terlalu panjang
        
        # Jika semua parts adalah code, return N/A
        return 'N/A'
    
    @staticmethod
    def enrich_data(df: pd.DataFrame) -> pd.DataFrame:
        """
        Enrich DataFrame dengan extracted columns dari KCONTACT.
        
        Tambahan columns:
        - sales_code: Extracted dari KCONTACT
        - sales_name: Extracted dari KCONTACT
        
        Args:
            df (pd.DataFrame): Normalized DataFrame
        
        Returns:
            pd.DataFrame: Enriched DataFrame
        """
        df = df.copy()
        
        # Extract sales code dan name dari KCONTACT
        if 'KCONTACT' in df.columns:
            df['sales_code'] = df['KCONTACT'].apply(GoogleSheetsLoader.extract_sales_code)
            df['sales_name'] = df['KCONTACT'].apply(GoogleSheetsLoader.extract_sales_name)
        else:
            # Jika KCONTACT tidak ada, set default
            df['sales_code'] = 'UNKNOWN'
            df['sales_name'] = 'Unknown'
        
        logger.debug(f"[LOADER] Enriched data with {df['sales_code'].nunique()} unique sales codes")
        
        return df
    
    @staticmethod
    def extract_phone(kcontact_value: str) -> str:
        """
        Extract nomor telepon PIC dari kolom KCONTACT.
        Mendukung format nomor Indonesia: 08..., 62..., +62...
        """
        if not kcontact_value or kcontact_value == "N/A":
            return "N/A"

        text = str(kcontact_value)

        # Prioritas: ambil nomor dari bagian "PIC ... - 08xxx" jika ada
        if ";" in text:
            for part in text.split(";"):
                pic_match = re.search(
                    r"PIC\s+.+?-\s*((?:\+62|62|0)8[0-9]{8,13})", part
                )
                if pic_match:
                    return pic_match.group(1)

        # Fallback: ambil nomor telepon pertama yang ditemukan
        match = re.search(r"(?:\+62|62|0)8[0-9]{8,13}", text)
        if match:
            return match.group(0)

        return "N/A"

    @staticmethod
    def process_all_data(data_dict: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """
        Process semua sheet data (normalize + enrich).
        
        Args:
            data_dict (Dict[str, pd.DataFrame]): Dictionary dari load_all_sheets()
        
        Returns:
            Dict[str, pd.DataFrame]: Processed data dictionary
        """
        processed = {}
        
        for sheet_type, df in data_dict.items():
            if df.empty:
                logger.warning(f"[LOADER] Skipping empty dataframe for {sheet_type}")
                continue
            
            # Normalize
            df_normalized = GoogleSheetsLoader.normalize_data(df, sheet_type)
            
            # Enrich
            df_enriched = GoogleSheetsLoader.enrich_data(df_normalized)
            
            # Extract PIC name dan phone number dari KCONTACT
            df_enriched["pic_name"] = df_enriched["KCONTACT"].apply(GoogleSheetsLoader.extract_pic_name)
            df_enriched["phone_number"] = df_enriched["KCONTACT"].apply(GoogleSheetsLoader.extract_phone)
            
            processed[sheet_type] = df_enriched
            logger.info(f"[LOADER] [OK] Processed {sheet_type}: {len(df_enriched)} rows")
        
        return processed
    
    def get_unique_sales_codes(self, data_dict: Dict[str, pd.DataFrame]) -> List[str]:
        """
        Get list of unique sales codes dari all sheets.
        
        Args:
            data_dict (Dict[str, pd.DataFrame]): Processed data dictionary
        
        Returns:
            List[str]: List of unique sales codes
        """
        all_codes = set()
        
        for df in data_dict.values():
            if 'sales_code' in df.columns:
                codes = df['sales_code'].unique()
                all_codes.update(codes)
        
        # Remove 'UNKNOWN' if present
        all_codes.discard('UNKNOWN')
        
        return sorted(list(all_codes))


def main():
    """Test function untuk verify loader works"""
    try:
        loader = GoogleSheetsLoader()
        
        # Load semua sheets
        print("[TEST] Loading all sheets...")
        data = loader.load_all_sheets()
        
        # Process data
        print("[TEST] Processing data...")
        processed = loader.process_all_data(data)
        
        # Get unique sales codes
        print("[TEST] Extracting sales codes...")
        sales_codes = loader.get_unique_sales_codes(processed)
        
        # Display summary
        print("\n[TEST] [OK] Data Loading Test PASSED")
        print(f"  - Sheets loaded: {len(processed)}")
        for sheet_type, df in processed.items():
            print(f"    • {sheet_type}: {len(df)} rows")
        print(f"  - Unique sales codes: {len(sales_codes)}")
        print(f"    Sample codes: {sales_codes[:5]}")
        
    except Exception as e:
        print(f"[TEST] [ERROR] Data Loading Test FAILED: {e}")
        raise


if __name__ == '__main__':
    main()