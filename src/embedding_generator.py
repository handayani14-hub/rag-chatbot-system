# src/embedding_generator.py
"""
Embedding Generator
Purpose: Generate vector embeddings menggunakan OpenAI API
Project: RAG Chatbot untuk Akses Informasi Tagihan Pelanggan
Author: Hani Handayani
"""

import logging
import time
import pandas as pd
from openai import OpenAI
from config import Config
from typing import List

# Setup logger
logger = logging.getLogger(__name__)


class EmbeddingGenerator:
    """
    Class untuk generate vector embeddings dari text data.
    
    Features:
    - Convert text ke vector embeddings menggunakan OpenAI
    - Batch processing untuk efficiency
    - Error handling dan retry logic
    - Caching untuk avoid duplicate requests
    """
    
    def __init__(self):
        """
        Initialize OpenAI client dengan API key dari .env
        Support untuk OpenAI dan Maia Router
        """
        try:
            # Check if using custom base URL (e.g., Maia Router)
            if hasattr(Config, 'OPENAI_BASE_URL') and Config.OPENAI_BASE_URL:
                self.client = OpenAI(
                    api_key=Config.OPENAI_API_KEY,
                    base_url=Config.OPENAI_BASE_URL
                )
                logger.info(f"[EMBEDDING] [OK] Using custom base URL: {Config.OPENAI_BASE_URL}")
            else:
                self.client = OpenAI(api_key=Config.OPENAI_API_KEY)
            
            self.model = Config.OPENAI_EMBEDDING_MODEL
            self.embedding_dim = Config.OPENAI_EMBEDDING_DIM
            
            logger.info(f"[EMBEDDING] [OK] OpenAI client initialized with model: {self.model}")
            logger.info(f"[EMBEDDING] Embedding dimension: {self.embedding_dim}")
            
        except Exception as e:
            logger.error(f"[EMBEDDING] ✗ Failed to initialize OpenAI client: {e}")
            raise
    
    @staticmethod
    def create_text_representation(row: pd.Series, sheet_type: str) -> str:
        """
        Convert DataFrame row ke text representation untuk embedding.
        
        Text format:
        "SND: {snd}, Status: {status}, Pelanggan: {customer}, Sales: {sales}, 
         Wilayah: {datel}, Tagihan: {jenis_tagihan}, Saldo: {saldo}"
        
        Args:
            row (pd.Series): Single row dari DataFrame
            sheet_type (str): Jenis sheet ('billper', 'billdu', 'billtri')
        
        Returns:
            str: Text representation untuk embedding
        """
        try:
            # Extract key fields dengan fallback ke 'N/A'
            snd = str(row.get('SND', 'N/A')).strip()
            status = str(row.get('LUNAS', 'N/A')).strip()
            customer = str(row.get('CUSTOMER_NAME', 'N/A')).strip()
            sales_code = str(row.get('sales_code', 'N/A')).strip()
            sales_name = str(row.get('sales_name', 'N/A')).strip()
            datel = str(row.get('DATEL', 'N/A')).strip()
            jenis = str(row.get('JENIS_TAGIHAN', 'N/A')).strip()
            saldo = str(row.get('SALDO', 'N/A')).strip()
            
            # Create structured text representation
            text = (
                f"Nomor Layanan: {snd}. "
                f"Status Pembayaran: {status}. "
                f"Nama Pelanggan: {customer}. "
                f"Kode Sales: {sales_code}. "
                f"Nama Sales: {sales_name}. "
                f"Wilayah: {datel}. "
                f"Jenis Tagihan: {jenis}. "
                f"Saldo: {saldo}. "
                f"Tipe Data: {sheet_type}."
            )
            
            return text
            
        except Exception as e:
            logger.error(f"[EMBEDDING] Error creating text representation: {e}")
            return "Error creating representation"
    
    def generate_embedding(self, text: str, retry_count: int = 3) -> List[float]:
        """
        Generate single embedding untuk text menggunakan OpenAI API.
        
        Args:
            text (str): Text untuk di-embed
            retry_count (int): Berapa kali retry jika gagal
        
        Returns:
            List[float]: Vector embedding (length 1536)
        
        Raises:
            Exception: Jika gagal setelah retry attempts
        """
        for attempt in range(retry_count):
            try:
                response = self.client.embeddings.create(
                    model=self.model,
                    input=text,
                    dimensions=self.embedding_dim
                )
                
                embedding = response.data[0].embedding
                
                # Verify embedding length
                if len(embedding) != self.embedding_dim:
                    logger.warning(
                        f"[EMBEDDING] Embedding length mismatch. "
                        f"Expected: {self.embedding_dim}, Got: {len(embedding)}"
                    )
                logger.debug(f"[EMBEDDING] [OK] Generated embedding for text: {text[:50]}...")
                return embedding
                
            except Exception as e:
                if attempt < retry_count - 1:
                    wait_time = 2 ** attempt  # Exponential backoff
                    logger.warning(
                        f"[EMBEDDING] Retry {attempt + 1}/{retry_count} "
                        f"after {wait_time}s. Error: {e}"
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(f"[EMBEDDING] Failed to generate embedding after {retry_count} attempts: {e}")
                    raise
        
        raise RuntimeError("Failed to generate embedding")
    
    def generate_embeddings_batch(
        self,
        texts: List[str],
        batch_size: int = 100,
        show_progress: bool = True,
        progress_callback=None
    ) -> List[List[float]]:
        """
        Generate embeddings untuk multiple texts.
        
        Batch processing untuk efficiency dan rate limiting compliance.
        
        Args:
            texts (List[str]): List of texts untuk di-embed
            batch_size (int): Ukuran batch (default: 100)
            show_progress (bool): Show progress logging
            progress_callback (callable): Optional callback untuk progress tracking
        
        Returns:
            List[List[float]]: List of embeddings
        """
        embeddings = []
        total = len(texts)
        
        if show_progress:
            logger.info(f"[EMBEDDING] Starting batch embedding for {total} texts")
        
        for i in range(0, total, batch_size):
            batch = texts[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (total + batch_size - 1) // batch_size
            
            if show_progress:
                logger.info(f"[EMBEDDING] Processing batch {batch_num}/{total_batches} ({len(batch)} texts)")
            
            # Generate embedding untuk setiap text di batch
            for idx, text in enumerate(batch):
                try:
                    embedding = self.generate_embedding(text)
                    embeddings.append(embedding)
                    
                    # Update progress callback setiap item
                    if progress_callback:
                        progress_callback(i + idx + 1)
                    
                except Exception as e:
                    logger.error(f"[EMBEDDING] Failed to embed text: {e}")
                    # Add zero vector sebagai fallback
                    embeddings.append([0.0] * self.embedding_dim)
                    if progress_callback:
                        progress_callback(i + idx + 1)
        
        if show_progress:
            logger.info(f"[EMBEDDING] [OK] Generated {len(embeddings)} embeddings successfully")
        return embeddings
    
    def process_dataframe(
        self,
        df: pd.DataFrame,
        sheet_type: str,
        batch_size: int = 100,
        progress_callback=None
    ) -> pd.DataFrame:
        """
        Add embedding column ke DataFrame.
        
        Args:
            df (pd.DataFrame): DataFrame dengan billing data
            sheet_type (str): Jenis sheet ('billper', 'billdu', 'billtri')
            batch_size (int): Batch size untuk processing
            progress_callback (callable): Optional callback untuk progress tracking
        
        Returns:
            pd.DataFrame: DataFrame dengan embedding column ditambah
        """
        # Step 1: Create text representations
        df_copy = df.copy()
        df_copy['text_representation'] = df_copy.apply(
            lambda row: self.create_text_representation(row, sheet_type),
            axis=1
        )
        
        # Step 2: Get list of texts
        texts = df_copy['text_representation'].tolist()
        
        # Step 3: Generate embeddings dengan progress tracking
        embeddings = self.generate_embeddings_batch(
            texts,
            batch_size=batch_size,
            show_progress=False,  # Suppress default logging
            progress_callback=progress_callback
        )
        
        # Step 4: Add embedding column
        df_copy['embedding_vector'] = embeddings
        
        return df_copy
    
    @staticmethod
    def embedding_to_string(embedding: List[float]) -> str:
        """
        Convert embedding vector ke string format untuk database storage.
        Format: "[0.1,0.2,0.3,...]"
        
        Args:
            embedding (List[float]): Vector embedding
        
        Returns:
            str: String representation
        """
        # Round ke 4 decimal places untuk efficiency
        rounded = [round(x, 4) for x in embedding]
        return '[' + ','.join(str(x) for x in rounded) + ']'
    
    @staticmethod
    def string_to_embedding(embedding_str: str) -> List[float]:
        """
        Convert string format ke embedding vector.
        
        Args:
            embedding_str (str): String representation "[0.1,0.2,...]"
        
        Returns:
            List[float]: Vector embedding
        """
        # Remove brackets dan split
        cleaned = embedding_str.strip('[]')
        embedding = [float(x) for x in cleaned.split(',')]
        return embedding


def main():
    """Test function untuk verify embedding generator works"""
    try:
        generator = EmbeddingGenerator()
        
        # Test 1: Single embedding
        print("[TEST] Generating single embedding...")
        test_text = "Nomor Layanan: 3315000001. Status Pembayaran: BELUM LUNAS. Nama Pelanggan: PT ABC Corp."
        embedding = generator.generate_embedding(test_text)
        print(f"  [OK] Embedding generated, dimension: {len(embedding)}")
        
        # Test 2: Batch embeddings
        print("\n[TEST] Generating batch embeddings...")
        test_texts = [
            "Nomor Layanan: 3315000001",
            "Nomor Layanan: 3315000002",
            "Nomor Layanan: 3315000003"
        ]
        embeddings = generator.generate_embeddings_batch(test_texts)
        print(f"  [OK] Generated {len(embeddings)} embeddings")
        
        # Test 3: Text representation
        print("\n[TEST] Creating text representation...")
        test_row = pd.Series({
            'SND': '3315000001',
            'LUNAS': 'BELUM LUNAS',
            'CUSTOMER_NAME': 'PT ABC',
            'sales_code': 'MB20100',
            'sales_name': 'Andi',
            'DATEL': 'Bandung',
            'JENIS_TAGIHAN': 'Reguler',
            'SALDO': 100000
        })
        text = EmbeddingGenerator.create_text_representation(test_row, 'billper')
        print(f" [OK] Text representation created: {text[:50]}...")
        
        print("\n[TEST] [OK] Embedding Generator Test PASSED")
        
    except Exception as e:
        print(f"[TEST] [ERROR] Embedding Generator Test FAILED: {e}")
        raise


if __name__ == '__main__':
    main()