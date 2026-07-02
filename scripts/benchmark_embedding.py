"""
Skrip benchmark latensi embedding per-kueri.
Mengukur waktu pemanggilan generate_embedding() secara langsung
menggunakan model dan konfigurasi yang sama dengan sistem produksi.

Jalankan dari root project:
    python scripts/benchmark_embedding.py
"""

import sys
import time
import statistics

sys.path.insert(0, 'src')

from embedding_generator import EmbeddingGenerator
from config import Config

# Sampel kueri yang merepresentasikan input pengguna nyata
SAMPLE_QUERIES = [
    "siapa saja pelanggan saya yang belum lunas?",
    "LAUNDRY BAGAS",
    "berapa total saldo tertunggak saya?",
    "pelanggan saya yang sudah jatuh tempo",
    "tampilkan daftar tagihan billper saya",
    "cari pelanggan CV MITRA CIPTA",
    "show me my unpaid customers",
    "apa status tagihan butik jabir?",
    "berapa jumlah tagihan billtri saya?",
    "siapa PIC PT CAHAYA TEKSTIL?",
]

def run_benchmark():
    print("=" * 60)
    print("  BENCHMARK LATENSI EMBEDDING PER-KUERI")
    print(f"  Model : {Config.OPENAI_EMBEDDING_MODEL}")
    print(f"  Dim   : {Config.OPENAI_EMBEDDING_DIM}")
    print(f"  N     : {len(SAMPLE_QUERIES)} kueri")
    print("=" * 60)

    gen = EmbeddingGenerator()

    results = []  # (query, latency_ms)

    for i, query in enumerate(SAMPLE_QUERIES, 1):
        print(f"\n[{i:02d}/{len(SAMPLE_QUERIES)}] Mengukur: '{query[:55]}'")
        t0 = time.perf_counter()
        try:
            _ = gen.generate_embedding(query)
            latency_ms = (time.perf_counter() - t0) * 1000
            results.append((query, latency_ms))
            print(f"         Latensi : {latency_ms:.1f} ms")
        except Exception as e:
            print(f"         ERROR   : {e}")

    if not results:
        print("\nTidak ada hasil yang berhasil diukur.")
        return

    latencies = [r[1] for r in results]

    print("\n" + "=" * 60)
    print("  HASIL STATISTIK")
    print("=" * 60)
    print(f"  Jumlah sampel     : {len(latencies)} kueri")
    print(f"  Rata-rata (mean)  : {statistics.mean(latencies):.1f} ms")
    print(f"  Median            : {statistics.median(latencies):.1f} ms")
    print(f"  Standar deviasi   : {statistics.stdev(latencies):.1f} ms" if len(latencies) > 1 else "")
    print(f"  Minimum           : {min(latencies):.1f} ms")
    print(f"  Maksimum          : {max(latencies):.1f} ms")
    print()
    print("  Detail per kueri:")
    print(f"  {'No':<4} {'Latensi (ms)':>14}  Kueri")
    print(f"  {'--':<4} {'------------':>14}  -----")
    for i, (q, lat) in enumerate(results, 1):
        print(f"  {i:<4} {lat:>14.1f}  {q[:50]}")

    print("\n" + "=" * 60)
    print("  CATATAN")
    print("=" * 60)
    print("  * Latensi di atas mencakup: serialisasi teks, round-trip")
    print("    HTTP ke API endpoint, pemrosesan model, deseralisasi")
    print("    respons JSON, dan ekstraksi vektor.")
    print("  * Angka ini merepresentasikan latensi embedding per-kueri")
    print("    saat inferensi (bukan batch ingestion).")
    print("  * Variasi antar-pengukuran dipengaruhi kondisi jaringan")
    print("    dan beban server API pada saat pengujian.")
    print("=" * 60)


if __name__ == "__main__":
    run_benchmark()
