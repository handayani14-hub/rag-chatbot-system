# Temporary script - extract conversation_log, access_control_log, sales_registry from TiDB
# Used to compile thesis BAB IV scenario evidence.

import json
import os
import sys
from pathlib import Path

# Adjust path so we can import config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import Config
import mysql.connector


def main():
    conn = mysql.connector.connect(
        host=Config.TIDB_HOST,
        port=Config.TIDB_PORT,
        user=Config.TIDB_USER,
        password=Config.TIDB_PASSWORD,
        database=Config.TIDB_DATABASE,
        ssl_ca=Config.TIDB_SSL_CA,
        ssl_verify_cert=True,
        charset="utf8mb4",
    )
    out_dir = Path(__file__).resolve().parent.parent / "logs"
    out_dir.mkdir(exist_ok=True)

    try:
        cur = conn.cursor(dictionary=True)
        try:
            for table, fname in [
                ("conversation_log", "export_conversation_log.json"),
                ("access_control_log", "export_access_control_log.json"),
                ("sales_registry", "export_sales_registry.json"),
            ]:
                try:
                    cur.execute(f"SELECT * FROM {table} ORDER BY 1")
                    rows = cur.fetchall()
                except Exception as e:
                    print(f"[ERROR] {table}: {e}")
                    continue
                # JSON-safe serialization
                def safe(v):
                    if isinstance(v, (bytes, bytearray)):
                        try:
                            return v.decode("utf-8", errors="replace")
                        except Exception:
                            return str(v)
                    # datetime / Decimal / etc.
                    if hasattr(v, "isoformat"):
                        return v.isoformat()
                    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
                        return v
                    return str(v)

                clean = [{k: safe(v) for k, v in r.items()} for r in rows]
                path = out_dir / fname
                path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[OK] {table}: {len(clean)} rows -> {path}")
        finally:
            cur.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
