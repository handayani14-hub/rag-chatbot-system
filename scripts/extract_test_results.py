"""
Script Auto-Extract Hasil Pengujian Formal dari conversation_log & access_control_log.

Menghubungkan langsung ke TiDB Cloud, mengambil data setelah timestamp tertentu,
lalu menghasilkan:
  1. Tabel ringkasan per skenario (console output)
  2. File JSON untuk analisis lanjutan
  3. File Markdown siap copy-paste ke BAB IV

Jalankan:
    python scripts/extract_test_results.py --after "2026-06-18 10:00:00"

Ganti timestamp sesuai waktu mulai pengujian formal.
"""

import argparse
import json
import os
import sys
import statistics
from datetime import datetime

sys.path.insert(0, 'src')

from tidb_client import TiDBClient


# ============================================================================
# PEMETAAN SKENARIO
# Setiap skenario dipetakan ke: query_type yang diharapkan + keyword di input
# ============================================================================

SCENARIO_MAP = [
    {
        "code": "S1",
        "name": "Daftar pelanggan belum lunas (billper)",
        "category": "Core",
        "match": lambda r: (
            r["query_type"] == "daftar_pelanggan"
            and any(k in (r.get("bot_response") or "").lower() for k in ["billper", "salon galih"])
            and "sheet selector" not in (r.get("bot_response") or "").lower()
        ),
        "input_keyword": "cari pelanggan",
    },
    {
        "code": "S2",
        "name": "Variasi bahasa dan parafrasa",
        "category": "Core",
        "match": lambda r: (
            r["query_type"] == "daftar_pelanggan"
            and "nunggak" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S3",
        "name": "Variasi periode tagihan",
        "category": "Core",
        "match": lambda r: (
            "april" in (r.get("user_query") or "").lower()
            and "periode" in (r.get("bot_response") or "").lower()
        ),
    },
    {
        "code": "S4",
        "name": "Pencarian berdasarkan nama pelanggan",
        "category": "Core",
        "match": lambda r: (
            r["query_type"] == "status_pelanggan"
            and "laundry bagas" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S5",
        "name": "Pencarian berdasarkan SND",
        "category": "Core",
        "match": lambda r: (
            "131123873518" in (r.get("user_query") or "")
            and r["query_type"] in ("status_pelanggan_snd", "status_pelanggan")
        ),
    },
    {
        "code": "S6",
        "name": "Pertanyaan dengan typo",
        "category": "Edge",
        "match": lambda r: "pelanggna" in (r.get("user_query") or "").lower(),
    },
    {
        "code": "S7",
        "name": "Tagihan jatuh tempo (billdu)",
        "category": "Core",
        "match": lambda r: (
            "jatuh tempo" in (r.get("user_query") or "").lower()
            and r["query_type"] == "daftar_pelanggan"
        ),
    },
    {
        "code": "S8",
        "name": "Konteks waktu ambigu",
        "category": "Edge",
        "match": lambda r: (
            "paling lama" in (r.get("user_query") or "").lower()
            or r["query_type"] == "query_waktu"
        ),
    },
    {
        "code": "S9",
        "name": "Permintaan agregasi",
        "category": "Core",
        "match": lambda r: (
            r["query_type"] == "ringkasan_saldo"
            and "total" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S10",
        "name": "Akses pengguna belum terdaftar",
        "category": "Core (Security)",
        "match_type": "access_log",
        "match": lambda r: (
            r["access_type"] == "denied"
            and "belum terdaftar" in (r.get("reason") or "").lower()
        ),
    },
    {
        "code": "S11",
        "name": "Kode identitas tidak valid",
        "category": "Core (Security)",
        "match_type": "access_log",
        "match": lambda r: (
            r["access_type"] == "denied"
            and "tidak valid" in (r.get("reason") or "").lower()
        ),
    },
    {
        "code": "S12",
        "name": "Pendaftaran ulang (re-login)",
        "category": "Core",
        "match_type": "access_log",
        "match": lambda r: (
            r["access_type"] == "granted"
            and "re-login" in (r.get("reason") or "").lower()
        ),
    },
    {
        "code": "S13",
        "name": "Single-sheet (billtri)",
        "category": "Core",
        "match": lambda r: (
            r["query_type"] == "daftar_pelanggan"
            and "billtri" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S14",
        "name": "Two-sheet (billper + billdu)",
        "category": "Core (Complex)",
        "match": lambda r: (
            r["query_type"] == "daftar_pelanggan"
            and "gabungkan" in (r.get("user_query") or "").lower()
            and "billper" in (r.get("user_query") or "").lower()
            and "billdu" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S15",
        "name": "Three-sheet (Ringkasan Saya)",
        "category": "Core (Complex)",
        "match": lambda r: r["query_type"] in ("ringkasan", "ringkasan_belum_lunas"),
    },
    {
        "code": "S16",
        "name": "Out-of-scope",
        "category": "Edge",
        "match": lambda r: (
            "cuaca" in (r.get("user_query") or "").lower()
            or (r["query_type"] == "general" and "di luar" in (r.get("bot_response") or "").lower())
        ),
    },
    {
        "code": "S17",
        "name": "Volume besar (lunas + belum lunas)",
        "category": "Edge",
        "match": lambda r: (
            "maupun yang lunas" in (r.get("user_query") or "").lower()
            or "baik yang" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S18",
        "name": "Rapid-fire",
        "category": "Edge",
        "match": None,  # Dideteksi dari interval timestamp
    },
    {
        "code": "S19",
        "name": "Multi-turn (pronoun)",
        "category": "Edge",
        "match": lambda r: (
            r["query_type"] == "s19_pronoun"
            or "tagihannya" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S20",
        "name": "Bilingual",
        "category": "Edge",
        "match": lambda r: (
            "show me" in (r.get("user_query") or "").lower()
            or "unpaid" in (r.get("user_query") or "").lower()
        ),
    },
    {
        "code": "S21",
        "name": "Sapaan (Greeting)",
        "category": "Tambahan",
        "match": lambda r: r["query_type"] == "sapaan",
    },
    {
        "code": "S22",
        "name": "Multi-SND",
        "category": "Tambahan",
        "match": lambda r: r["query_type"] == "status_pelanggan_multi_snd",
    },
    {
        "code": "S23",
        "name": "Akses lintas-sales (IBAC)",
        "category": "Tambahan (Security)",
        "match": lambda r: (
            "2232252002" in (r.get("user_query") or "")
        ),
    },
    {
        "code": "S24",
        "name": "SND tidak terdaftar",
        "category": "Tambahan",
        "match": lambda r: (
            "0000000001" in (r.get("user_query") or "")
            or ("tidak ditemukan" in (r.get("bot_response") or "").lower()
                and "tidak terdaftar" in (r.get("bot_response") or "").lower())
        ),
    },
    {
        "code": "S25",
        "name": "Query atribut spesifik (PIC)",
        "category": "Tambahan",
        "match": lambda r: (
            "pic" in (r.get("user_query") or "").lower()
            and "cahaya" in (r.get("user_query") or "").lower()
        ),
    },
]


def fetch_data(db, after_ts):
    """Ambil data dari TiDB setelah timestamp tertentu."""
    conv_query = """
        SELECT id, chat_id, sales_code, user_query, bot_response,
               query_type, retrieved_documents_count, similarity_scores,
               response_time_ms, error_occurred, error_message, timestamp
        FROM conversation_log
        WHERE timestamp >= %s
        ORDER BY timestamp ASC
    """
    conv_rows = db.execute_query(conv_query, (after_ts,))

    access_query = """
        SELECT id, chat_id, sales_code, access_type, reason,
               accessed_resource, timestamp
        FROM access_control_log
        WHERE timestamp >= %s
        ORDER BY timestamp ASC
    """
    access_rows = db.execute_query(access_query, (after_ts,))

    return conv_rows, access_rows


def match_scenarios(conv_rows, access_rows):
    """Cocokkan setiap row ke skenario yang sesuai."""
    results = {}

    for scenario in SCENARIO_MAP:
        code = scenario["code"]
        match_fn = scenario.get("match")
        source = scenario.get("match_type", "conv_log")

        if match_fn is None:
            results[code] = {
                "scenario": scenario,
                "matched_rows": [],
                "note": "Deteksi manual (lihat interval timestamp)"
            }
            continue

        matched = []
        rows = access_rows if source == "access_log" else conv_rows

        for row in rows:
            try:
                if match_fn(row):
                    matched.append(row)
            except (KeyError, TypeError):
                continue

        results[code] = {
            "scenario": scenario,
            "matched_rows": matched,
        }

    return results


def detect_rapid_fire(conv_rows):
    """Deteksi S18: cari cluster 4+ pesan dengan interval < 30 detik."""
    clusters = []
    if len(conv_rows) < 4:
        return clusters

    for i in range(len(conv_rows) - 3):
        try:
            ts = []
            for j in range(4):
                t = conv_rows[i + j].get("timestamp")
                if isinstance(t, str):
                    t = datetime.fromisoformat(t)
                ts.append(t)

            span = (ts[3] - ts[0]).total_seconds()
            if span <= 60:
                cluster_rows = [conv_rows[i + j] for j in range(4)]
                intervals = [(ts[j+1] - ts[j]).total_seconds() for j in range(3)]
                clusters.append({
                    "rows": cluster_rows,
                    "span_seconds": span,
                    "intervals": intervals,
                })
        except (TypeError, ValueError):
            continue

    return clusters


def generate_report(results, conv_rows, access_rows):
    """Generate report ke console."""
    print()
    print("=" * 80)
    print("  LAPORAN HASIL PENGUJIAN FORMAL — CHATBOT BILLIE")
    print(f"  Dihasilkan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    print(f"\n  Data ditemukan: {len(conv_rows)} conversation_log + {len(access_rows)} access_control_log\n")

    # === Rapid-fire detection (harus sebelum tabel agar S18 status benar) ===
    rf_clusters = detect_rapid_fire(conv_rows)
    if rf_clusters:
        results["S18"]["matched_rows"] = rf_clusters[0]["rows"]

    # === Tabel ringkasan ===
    print(f"  {'Kode':<6} {'Nama Skenario':<42} {'Kategori':<20} {'Match':>5} {'Latency':>10} {'Status':<8}")
    print(f"  {'----':<6} {'------------------------------------------':<42} {'--------':<20} {'-----':>5} {'-------':>10} {'------':<8}")

    lulus = 0
    gagal = 0
    total = len(SCENARIO_MAP)

    for scenario in SCENARIO_MAP:
        code = scenario["code"]
        name = scenario["name"][:40]
        cat = scenario["category"]
        r = results.get(code, {})
        matched = r.get("matched_rows", [])
        n = len(matched)

        if matched:
            latencies = [m.get("response_time_ms", 0) for m in matched if m.get("response_time_ms")]
            avg_lat = f"{statistics.mean(latencies):.0f}ms" if latencies else "-"
        else:
            avg_lat = "-"

        status = "FOUND" if n > 0 else "EMPTY"
        if n > 0:
            lulus += 1
        else:
            gagal += 1

        print(f"  {code:<6} {name:<42} {cat:<20} {n:>5} {avg_lat:>10} {status:<8}")

    print(f"\n  Terdeteksi: {lulus}/{total} skenario cocok dengan data log")
    if gagal > 0:
        print(f"  {gagal} skenario TIDAK ditemukan — cek apakah sudah dijalankan")

    # === Rapid-fire detail ===
    if rf_clusters:
        print(f"\n  S18 Rapid-fire: {len(rf_clusters)} cluster terdeteksi")
        for i, c in enumerate(rf_clusters[:3]):
            print(f"    Cluster {i+1}: {c['span_seconds']:.1f}s span, intervals: {c['intervals']}")

    # === Statistik waktu respons ===
    all_times = [r.get("response_time_ms", 0) for r in conv_rows if r.get("response_time_ms")]
    if all_times:
        print(f"\n  === Statistik Waktu Respons Keseluruhan ===")
        print(f"  Jumlah     : {len(all_times)} respons")
        print(f"  Rata-rata  : {statistics.mean(all_times):.0f} ms")
        print(f"  Median     : {statistics.median(all_times):.0f} ms")
        print(f"  Minimum    : {min(all_times)} ms")
        print(f"  Maksimum   : {max(all_times)} ms")
        if len(all_times) > 1:
            print(f"  Std. Dev   : {statistics.stdev(all_times):.0f} ms")

    # === Distribusi per query_type ===
    from collections import defaultdict
    by_type = defaultdict(list)
    for r in conv_rows:
        qt = r.get("query_type", "unknown")
        rt = r.get("response_time_ms")
        if rt:
            by_type[qt].append(rt)

    print(f"\n  === Waktu Respons per Query Type ===")
    print(f"  {'Query Type':<35} {'n':>4} {'Avg (ms)':>10} {'Med (ms)':>10} {'Min':>8} {'Max':>8}")
    print(f"  {'-'*35} {'--':>4} {'-'*10} {'-'*10} {'-'*8} {'-'*8}")
    for qt in sorted(by_type.keys()):
        times = by_type[qt]
        avg = statistics.mean(times)
        med = statistics.median(times)
        print(f"  {qt:<35} {len(times):>4} {avg:>10.0f} {med:>10.0f} {min(times):>8} {max(times):>8}")

    # === Access control summary ===
    from collections import Counter
    ac_types = Counter(r.get("access_type") for r in access_rows)
    print(f"\n  === Access Control Log ===")
    for at, cnt in ac_types.most_common():
        print(f"    {at}: {cnt} event")

    print("\n" + "=" * 80)

    return results


def save_json(results, conv_rows, access_rows, output_dir):
    """Simpan hasil ke JSON."""
    os.makedirs(output_dir, exist_ok=True)

    # Conversation log
    conv_path = os.path.join(output_dir, "conversation_log.json")
    serializable_conv = []
    for r in conv_rows:
        row = dict(r)
        for k, v in row.items():
            if isinstance(v, datetime):
                row[k] = v.isoformat()
        serializable_conv.append(row)

    with open(conv_path, 'w', encoding='utf-8') as f:
        json.dump(serializable_conv, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {conv_path} ({len(conv_rows)} rows)")

    # Access control log
    ac_path = os.path.join(output_dir, "access_control_log.json")
    serializable_ac = []
    for r in access_rows:
        row = dict(r)
        for k, v in row.items():
            if isinstance(v, datetime):
                row[k] = v.isoformat()
        serializable_ac.append(row)

    with open(ac_path, 'w', encoding='utf-8') as f:
        json.dump(serializable_ac, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {ac_path} ({len(access_rows)} rows)")

    # Ringkasan per skenario
    summary = []
    for scenario in SCENARIO_MAP:
        code = scenario["code"]
        r = results.get(code, {})
        matched = r.get("matched_rows", [])
        latencies = [m.get("response_time_ms", 0) for m in matched if m.get("response_time_ms")]

        summary.append({
            "code": code,
            "name": scenario["name"],
            "category": scenario["category"],
            "matched_count": len(matched),
            "avg_latency_ms": round(statistics.mean(latencies)) if latencies else None,
            "queries": [m.get("user_query", "") for m in matched],
        })

    summary_path = os.path.join(output_dir, "scenario_summary.json")
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {summary_path}")


def save_markdown(results, conv_rows, access_rows, output_dir):
    """Generate tabel Markdown ringkasan untuk BAB IV."""
    os.makedirs(output_dir, exist_ok=True)
    md_path = os.path.join(output_dir, "hasil_ringkasan.md")

    lines = []
    lines.append("# Ringkasan Hasil Pengujian Formal\n")
    lines.append(f"> Diekstrak otomatis pada {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"> Total data: {len(conv_rows)} conversation + {len(access_rows)} access control\n\n")

    # Tabel rekapitulasi
    lines.append("## Rekapitulasi Status per Skenario\n")
    lines.append("| Kode | Nama Skenario | Kategori | Latency (ms) | Status |")
    lines.append("|------|---------------|----------|-------------|--------|")

    for scenario in SCENARIO_MAP:
        code = scenario["code"]
        r = results.get(code, {})
        matched = r.get("matched_rows", [])
        latencies = [m.get("response_time_ms", 0) for m in matched if m.get("response_time_ms")]
        avg_lat = f"{statistics.mean(latencies):.0f}" if latencies else "-"
        status = "Lulus" if matched else "Tidak terdeteksi"
        lines.append(f"| {code} | {scenario['name']} | {scenario['category']} | {avg_lat} | {status} |")

    # Statistik keseluruhan
    all_times = [r.get("response_time_ms", 0) for r in conv_rows if r.get("response_time_ms")]
    if all_times:
        lines.append(f"\n## Statistik Waktu Respons\n")
        lines.append(f"| Metrik | Nilai |")
        lines.append(f"|---|---|")
        lines.append(f"| Jumlah respons | {len(all_times)} |")
        lines.append(f"| Rata-rata | {statistics.mean(all_times):.0f} ms |")
        lines.append(f"| Median | {statistics.median(all_times):.0f} ms |")
        lines.append(f"| Minimum | {min(all_times)} ms |")
        lines.append(f"| Maksimum | {max(all_times)} ms |")

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"  Saved: {md_path}")


def main():
    parser = argparse.ArgumentParser(description="Extract formal test results from TiDB")
    parser.add_argument(
        "--after",
        required=True,
        help='Timestamp mulai pengujian formal, format: "YYYY-MM-DD HH:MM:SS"'
    )
    parser.add_argument(
        "--output",
        default="logs/evaluasi",
        help="Direktori output (default: logs/evaluasi)"
    )
    args = parser.parse_args()

    after_ts = args.after
    print(f"\n  Filter: data setelah {after_ts}")

    # Koneksi ke TiDB
    print("  Menghubungkan ke TiDB Cloud...")
    db = TiDBClient()

    # Ambil data
    print("  Mengambil data...")
    conv_rows, access_rows = fetch_data(db, after_ts)

    if not conv_rows and not access_rows:
        print(f"\n  TIDAK ADA DATA setelah {after_ts}.")
        print("  Pastikan timestamp benar dan pengujian sudah dijalankan.")
        return

    # Match ke skenario
    results = match_scenarios(conv_rows, access_rows)

    # Generate report
    results = generate_report(results, conv_rows, access_rows)

    # Save files
    print(f"\n  Menyimpan hasil ke {args.output}/...")
    save_json(results, conv_rows, access_rows, args.output)
    save_markdown(results, conv_rows, access_rows, args.output)

    print(f"\n  Selesai! File tersimpan di: {args.output}/")
    print("=" * 80)


if __name__ == "__main__":
    main()
