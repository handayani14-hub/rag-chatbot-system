# src/metrics_collector.py
"""
Metrics Collector - Query conversation_log dan access_control_log untuk
menghasilkan metrik evaluasi sistem: latency, success rate, efektivitas
retrieval, distribusi intent, dan error rate.
"""

import logging
import json
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from statistics import mean, median, stdev

from tidb_client import TiDBClient
from utils import format_currency_short

logger = logging.getLogger(__name__)


class MetricsCollector:
    """
    Kumpulkan dan analisis metrics dari conversation_log & access_control_log
    untuk evaluasi sistem RAG dalam laporan tugas akhir.

    Metrics yang dikumpulkan:
    - Response latency (response_time_ms)
    - Query success rate
    - Jumlah dokumen yang di-retrieve (RAG effectiveness)
    - Access control events (registration, logout, denied)
    - Intent distribution
    - Error rate
    """

    def __init__(self, db: TiDBClient):
        """
        Initialize metrics collector dengan koneksi database.

        Args:
            db (TiDBClient): Instance database client
        """
        self.db = db
        logger.info("[METRICS] Metrics collector initialized")

    # =========================================================================
    # 1. QUERY METRICS — Ambil raw data dari conversation_log
    # =========================================================================

    def get_conversation_logs(
        self,
        hours: int = 24,
        sales_code: Optional[str] = None,
        query_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Query conversation_log dalam rentang waktu tertentu.
        Bisa filter by sales_code atau query_type.

        Args:
            hours (int): Berapa jam terakhir yang diambil (default 24 jam)
            sales_code (str): Filter by sales code tertentu (optional)
            query_type (str): Filter by tipe query (optional)

        Returns:
            List[Dict]: List dari conversation records
        """
        try:
            # Build WHERE clause
            filters = []
            filters.append(f"timestamp >= DATE_SUB(NOW(), INTERVAL {hours} HOUR)")

            if sales_code:
                filters.append(f"sales_code = '{sales_code}'")

            if query_type:
                filters.append(f"query_type = '{query_type}'")

            where_clause = " AND ".join(filters)

            # Query
            query = f"""
                SELECT
                    id, chat_id, sales_code, user_query, bot_response,
                    response_time_ms, query_type, retrieved_documents_count,
                    error_occurred, timestamp
                FROM conversation_log
                WHERE {where_clause}
                ORDER BY timestamp DESC
            """

            results = self.db.execute_query(query)
            logger.info(f"[METRICS] Fetched {len(results)} conversation logs")
            return results

        except Exception as e:
            logger.error(f"[METRICS] Error fetching conversation logs: {e}")
            return []

    def get_access_control_logs(
        self,
        hours: int = 24,
        access_type: Optional[str] = None
    ) -> List[Dict]:
        """
        Query access_control_log dalam rentang waktu tertentu.

        Args:
            hours (int): Berapa jam terakhir
            access_type (str): Filter by 'granted', 'denied', 'unregistered' (optional)

        Returns:
            List[Dict]: List dari access control records
        """
        try:
            filters = []
            filters.append(f"timestamp >= DATE_SUB(NOW(), INTERVAL {hours} HOUR)")

            if access_type:
                filters.append(f"access_type = '{access_type}'")

            where_clause = " AND ".join(filters)

            query = f"""
                SELECT
                    id, chat_id, sales_code, access_type, reason,
                    accessed_resource, timestamp
                FROM access_control_log
                WHERE {where_clause}
                ORDER BY timestamp DESC
            """

            results = self.db.execute_query(query)
            logger.info(f"[METRICS] Fetched {len(results)} access control logs")
            return results

        except Exception as e:
            logger.error(f"[METRICS] Error fetching access logs: {e}")
            return []

    # =========================================================================
    # 2. RESPONSE TIME METRICS — Analisis latency
    # =========================================================================

    def get_response_time_stats(
        self,
        hours: int = 24,
        query_type: Optional[str] = None
    ) -> Dict:
        """
        Hitung statistik response time dari percakapan.
        Metrics: min, max, mean, median, stdev (dalam milliseconds)

        Args:
            hours (int): Rentang waktu analisis
            query_type (str): Filter by specific query type (optional)

        Returns:
            Dict: {
                'min_ms': int,
                'max_ms': int,
                'mean_ms': float,
                'median_ms': float,
                'stdev_ms': float,
                'total_queries': int
            }
        """
        try:
            logs = self.get_conversation_logs(hours, query_type=query_type)

            if not logs:
                return {
                    'min_ms': 0,
                    'max_ms': 0,
                    'mean_ms': 0,
                    'median_ms': 0,
                    'stdev_ms': 0,
                    'total_queries': 0
                }

            # Extract response times
            response_times = [log['response_time_ms'] for log in logs]

            # Hitung statistik
            stats = {
                'min_ms': int(min(response_times)),
                'max_ms': int(max(response_times)),
                'mean_ms': round(mean(response_times), 2),
                'median_ms': median(response_times),
                'stdev_ms': round(stdev(response_times), 2) if len(response_times) > 1 else 0,
                'total_queries': len(response_times)
            }

            logger.info(
                f"[METRICS] Response time stats (last {hours}h): "
                f"mean={stats['mean_ms']}ms, median={stats['median_ms']}ms"
            )
            return stats

        except Exception as e:
            logger.error(f"[METRICS] Error calculating response time stats: {e}")
            return {}

    # =========================================================================
    # 3. QUERY SUCCESS RATE — Berapa persen query berhasil
    # =========================================================================

    def get_query_success_rate(self, hours: int = 24) -> Dict:
        """
        Hitung berapa persen query berhasil tanpa error.

        Returns:
            Dict: {
                'total_queries': int,
                'successful_queries': int,
                'failed_queries': int,
                'success_rate_percent': float
            }
        """
        try:
            logs = self.get_conversation_logs(hours)

            if not logs:
                return {
                    'total_queries': 0,
                    'successful_queries': 0,
                    'failed_queries': 0,
                    'success_rate_percent': 0
                }

            total = len(logs)
            failed = sum(1 for log in logs if log.get('error_occurred', False))
            successful = total - failed

            success_rate = (successful / total * 100) if total > 0 else 0

            result = {
                'total_queries': total,
                'successful_queries': successful,
                'failed_queries': failed,
                'success_rate_percent': round(success_rate, 2)
            }

            logger.info(
                f"[METRICS] Query success rate: {result['success_rate_percent']}% "
                f"({successful}/{total})"
            )
            return result

        except Exception as e:
            logger.error(f"[METRICS] Error calculating success rate: {e}")
            return {}

    # =========================================================================
    # 4. INTENT DISTRIBUTION — Berapa banyak setiap jenis query
    # =========================================================================

    def get_intent_distribution(self, hours: int = 24) -> Dict[str, int]:
        """
        Hitung distribusi query berdasarkan intent.
        Intent: daftar_pelanggan, status_pelanggan, ringkasan_umum,
                ringkasan_belum_lunas, ringkasan_saldo, general

        Returns:
            Dict: {
                'daftar_pelanggan': count,
                'status_pelanggan': count,
                'ringkasan': count,
                ...
            }
        """
        try:
            logs = self.get_conversation_logs(hours)

            if not logs:
                return {}

            # Count by query_type
            distribution = {}
            for log in logs:
                query_type = log.get('query_type', 'unknown')
                distribution[query_type] = distribution.get(query_type, 0) + 1

            logger.info(f"[METRICS] Intent distribution: {distribution}")
            return distribution

        except Exception as e:
            logger.error(f"[METRICS] Error calculating intent distribution: {e}")
            return {}

    # =========================================================================
    # 5. RAG EFFECTIVENESS — Berapa dokumen di-retrieve per query
    # =========================================================================

    def get_retrieval_stats(self, hours: int = 24) -> Dict:
        """
        Analisis effectiveness RAG: berapa dokumen di-retrieve per query.

        Returns:
            Dict: {
                'avg_documents_retrieved': float,
                'min_documents': int,
                'max_documents': int,
                'queries_with_zero_results': int
            }
        """
        try:
            logs = self.get_conversation_logs(hours)

            if not logs:
                return {
                    'avg_documents_retrieved': 0,
                    'min_documents': 0,
                    'max_documents': 0,
                    'queries_with_zero_results': 0
                }

            doc_counts = [log.get('retrieved_documents_count', 0) for log in logs]

            result = {
                'avg_documents_retrieved': round(mean(doc_counts), 2),
                'min_documents': min(doc_counts),
                'max_documents': max(doc_counts),
                'queries_with_zero_results': sum(1 for c in doc_counts if c == 0)
            }

            logger.info(
                f"[METRICS] RAG stats: avg {result['avg_documents_retrieved']} "
                f"docs/query, zero-result queries: {result['queries_with_zero_results']}"
            )
            return result

        except Exception as e:
            logger.error(f"[METRICS] Error calculating retrieval stats: {e}")
            return {}

    # =========================================================================
    # 6. ACCESS CONTROL METRICS — Registration, logout, denied attempts
    # =========================================================================

    def get_access_control_stats(self, hours: int = 24) -> Dict:
        """
        Analisis access control events: registration, logout, denied access.

        Returns:
            Dict: {
                'total_registrations': int,
                'total_logouts': int,
                'total_denied': int,
                'unique_users': int
            }
        """
        try:
            logs = self.get_access_control_logs(hours)

            if not logs:
                return {
                    'total_registrations': 0,
                    'total_logouts': 0,
                    'total_denied': 0,
                    'unique_users': 0
                }

            # Count by access_type
            registrations = sum(1 for log in logs if log.get('access_type') == 'granted')
            logouts = sum(1 for log in logs if 'logout' in log.get('reason', '').lower())
            denied = sum(1 for log in logs if log.get('access_type') == 'denied')

            # Unique users
            unique_users = len(set(log['chat_id'] for log in logs))

            result = {
                'total_registrations': registrations,
                'total_logouts': logouts,
                'total_denied': denied,
                'unique_users': unique_users
            }

            logger.info(f"[METRICS] Access control: {registrations} registrations, "
                       f"{denied} denied attempts")
            return result

        except Exception as e:
            logger.error(f"[METRICS] Error calculating access control stats: {e}")
            return {}

    # =========================================================================
    # 7. GENERATE EVALUATION REPORT — Rangkuman untuk BAB IV
    # =========================================================================

    def generate_evaluation_report(
        self,
        hours: int = 24,
        output_file: Optional[str] = None
    ) -> Dict:
        """
        Generate comprehensive evaluation report untuk thesis.
        Menggabungkan semua metrics di atas jadi satu laporan.

        Args:
            hours (int): Rentang waktu evaluasi (default 24 jam)
            output_file (str): Kalau ada, simpan hasil ke JSON file

        Returns:
            Dict: Report lengkap dengan semua metrics
        """
        logger.info(f"[METRICS] Generating evaluation report (last {hours} hours)...")

        report = {
            'generated_at': datetime.now().isoformat(),
            'period_hours': hours,
            'response_time': self.get_response_time_stats(hours),
            'query_success': self.get_query_success_rate(hours),
            'intent_distribution': self.get_intent_distribution(hours),
            'retrieval_stats': self.get_retrieval_stats(hours),
            'access_control': self.get_access_control_stats(hours),
        }

        # Kalau ada output file, simpan ke sana
        if output_file:
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(report, f, indent=2, ensure_ascii=False)
                logger.info(f"[METRICS] Report saved to {output_file}")
            except Exception as e:
                logger.error(f"[METRICS] Failed to save report: {e}")

        return report

    def print_evaluation_report(
        self,
        hours: int = 24
    ) -> None:
        """
        Generate dan print evaluation report ke terminal (user-friendly format).
        Cocok untuk dilihat saat evaluasi sistem.

        Args:
            hours (int): Rentang waktu evaluasi
        """
        report = self.generate_evaluation_report(hours)

        print("\n" + "=" * 70)
        print("  📊 EVALUATION REPORT - BILLIE RAG CHATBOT")
        print("=" * 70)

        # Period
        print(f"\n⏱️  Period: Last {hours} hour(s)")
        print(f"   Generated: {report['generated_at']}")

        # Response Time
        rt = report['response_time']
        print(f"\n⚡ RESPONSE TIME METRICS")
        print(f"   Min:    {rt.get('min_ms', 0):,} ms")
        print(f"   Max:    {rt.get('max_ms', 0):,} ms")
        print(f"   Mean:   {rt.get('mean_ms', 0):,} ms")
        print(f"   Median: {rt.get('median_ms', 0):,} ms")
        print(f"   StDev:  {rt.get('stdev_ms', 0):,} ms")
        print(f"   Total:  {rt.get('total_queries', 0)} queries")

        # Success Rate
        qs = report['query_success']
        print(f"\n✅ QUERY SUCCESS RATE")
        print(f"   Success: {qs.get('successful_queries', 0)}/{qs.get('total_queries', 0)} "
              f"({qs.get('success_rate_percent', 0)}%)")
        print(f"   Failed:  {qs.get('failed_queries', 0)}")

        # Intent Distribution
        intent = report['intent_distribution']
        if intent:
            print(f"\n🎯 INTENT DISTRIBUTION")
            for intent_name, count in sorted(intent.items(), key=lambda x: x[1], reverse=True):
                print(f"   {intent_name}: {count}")

        # Retrieval Stats
        ret = report['retrieval_stats']
        print(f"\n📚 RAG RETRIEVAL STATS")
        print(f"   Avg Documents/Query: {ret.get('avg_documents_retrieved', 0)}")
        print(f"   Min: {ret.get('min_documents', 0)}, Max: {ret.get('max_documents', 0)}")
        print(f"   Queries with 0 results: {ret.get('queries_with_zero_results', 0)}")

        # Access Control
        ac = report['access_control']
        print(f"\n🔐 ACCESS CONTROL STATS")
        print(f"   Registrations: {ac.get('total_registrations', 0)}")
        print(f"   Logouts: {ac.get('total_logouts', 0)}")
        print(f"   Denied Attempts: {ac.get('total_denied', 0)}")
        print(f"   Unique Users: {ac.get('unique_users', 0)}")

        print("\n" + "=" * 70 + "\n")


# ═════════════════════════════════════════════════════════════════════════
# TEST FUNCTION — Jalankan metrics collector untuk testing
# ═════════════════════════════════════════════════════════════════════════

def main():
    """Test metrics collector"""
    try:
        print("[TEST] Initializing metrics collector...")
        db = TiDBClient()
        collector = MetricsCollector(db)

        print("[TEST] Generating 24-hour evaluation report...")
        collector.print_evaluation_report(hours=24)

        print("[TEST] Generating JSON report...")
        report = collector.generate_evaluation_report(
            hours=24,
            output_file='evaluation_report.json'
        )

        db.disconnect()
        print("[TEST] ✓ Metrics collection test PASSED")

    except Exception as e:
        print(f"[TEST] ✗ Metrics collection test FAILED: {e}")
        raise


if __name__ == '__main__':
    main()
