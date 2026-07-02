# src/access_control.py
"""
Access Control - User Registration & RBAC
"""

import logging
from typing import Optional, Dict, Tuple
from tidb_client import TiDBClient
from google_sheets_loader import GoogleSheetsLoader

logger = logging.getLogger(__name__)


class AccessControl:
    """Manage user registration dan access control"""
    
    def __init__(self, db: TiDBClient):
        self.db = db
        self.loader = GoogleSheetsLoader()
    
    def validate_sales_code(self, sales_code: str) -> Tuple[bool, Optional[str]]:
        """
        Validate apakah sales_code exists di data.

        Returns:
            Tuple[bool, str]: (is_valid, sales_name)
        """
        try:
            # Load data untuk extract unique sales codes
            data = self.loader.load_all_sheets()
            processed = self.loader.process_all_data(data)

            all_sales_codes = self.loader.get_unique_sales_codes(processed)

            if sales_code in all_sales_codes:
                # Find sales name
                for df in processed.values():
                    match = df[df['sales_code'] == sales_code]

                    if not match.empty:
                        row = match.iloc[0]

                        # Prioritas utama: nama sales dari kolom NAMA SA/AR/AM
                        sales_name = str(row.get('NAMA SA/AR/AM', '')).strip()

                        # Fallback jika kolom kosong/tidak tersedia
                        if not sales_name or sales_name == 'N/A':
                            sales_name = str(row.get('sales_name', '')).strip()

                        # Fallback terakhir
                        if not sales_name or sales_name == 'N/A':
                            sales_name = f"Sales {sales_code}"

                        logger.info(f"[AC] Valid sales code: {sales_code} ({sales_name})")
                        return True, sales_name

            logger.warning(f"[AC] Invalid sales code: {sales_code}")
            return False, None

        except Exception as e:
            logger.error(f"[AC] Error validating sales code: {e}")
            return False, None
    
    def register_user(
        self,
        chat_id: str,
        sales_code: str
    ) -> Tuple[bool, str]:
        """Register user ke database

        Returns:
            Tuple[bool, str]: (success, message)
        """
        # Step 1: Validate sales code
        is_valid, sales_name = self.validate_sales_code(sales_code)

        if not is_valid:
            msg = f"Kode sales '{sales_code}' tidak valid. Cek kembali kode Anda."
            logger.warning(f"[AC] Registration failed for {chat_id}: {msg}")
            return False, msg

        # Step 2: Register ke database
        try:
            self.db.register_user(chat_id, sales_code, sales_name or "Unknown", "N/A")
            msg = f"Registrasi berhasil! Halo {sales_name}, selamat datang."
            logger.info(f"[AC] User registered: {chat_id} ({sales_code})")
            return True, msg

        except ValueError as e:
            # Identity switching attempt — pesan sudah diformulasikan di tidb_client
            logger.warning(f"[AC] Blocked registration for {chat_id}: {e}")
            return False, str(e)

        except Exception as e:
            logger.error(f"[AC] Error registering user: {e}")
            msg = "Terjadi error saat registrasi. Silakan coba lagi."
            return False, msg
    
    def get_user_access(self, chat_id: str) -> Tuple[bool, Optional[Dict]]:
        """Check user access dan retrieve user info
        
        Returns:
            Tuple[bool, Dict]: (is_authorized, user_info)
        """
        try:
            user = self.db.get_user(chat_id)
            
            if user:
                logger.info(f"[AC] Access granted for {chat_id}")
                return True, user
            else:
                logger.warning(f"[AC] Access denied for {chat_id} (not registered)")
                return False, None
                
        except Exception as e:
            logger.error(f"[AC] Error checking access: {e}")
            return False, None
    
    def log_access_attempt(
        self,
        chat_id: str,
        sales_code: Optional[str],
        access_type: str,
        reason: str
    ):
        """Log access attempt untuk audit"""
        try:
            self.db.log_access_control(
                chat_id=chat_id,
                sales_code=sales_code,
                access_type=access_type,
                reason=reason,
                resource="chatbot_query"
            )
        except Exception as e:
            logger.error(f"[AC] Error logging access: {e}")