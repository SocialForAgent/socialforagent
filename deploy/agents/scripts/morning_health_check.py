#!/usr/bin/env python3
"""
Morning Health Check — verifica che tutti i task giornalieri siano stati eseguiti.
Se qualcosa manca, lo segnala nel report.
Da usare come script per cron job (no_agent=False, l'agente interpreta il report e agisce).
"""
import json, subprocess, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ = timezone(timedelta(hours=2))  # CEST
now = datetime.now(TZ)
today_str = now.strftime('%Y-%m-%d')
today_ita = now.strftime('%d/%m/%Y')

report = {
    "date": today_str,
    "time": now.strftime('%H:%M CEST'),
    "weekday": now.strftime('%A'),
    "checks": {}
}

# === 1. BLOG: articolo pubblicato oggi? ===
blog_output_dir = Path("/opt/data/cron/output/8fb6d8a9abb1")
if blog_output_dir.exists():
    today_files = sorted(blog_output_dir.glob(f"{today_str}*.md"))
    if today_files:
        latest = today_files[-1]
        report["checks"]["blog"] = {
            "status": "OK",
            "output_file": latest.name,
            "size_bytes": latest.stat().st_size
        }
    else:
        report["checks"]["blog"] = {"status": "MISSING", "note": "Nessun output blog oggi"}
else:
    report["checks"]["blog"] = {"status": "MISSING", "note": "Directory output blog non trovata"}

# === 2. BATCH CHIAMATE: eseguito oggi? ===
batch_log = Path("/opt/data/strategie/daily_batch.log")
batch_called = Path("/opt/data/strategie/daily_batch_all_called.txt")
if batch_log.exists():
    mtime = datetime.fromtimestamp(batch_log.stat().st_mtime, tz=TZ)
    if mtime.strftime('%Y-%m-%d') == today_str:
        report["checks"]["batch_chiamate"] = {"status": "OK", "last_run": mtime.strftime('%H:%M')}
    else:
        report["checks"]["batch_chiamate"] = {"status": "MISSING", "last_run": mtime.strftime('%d/%m %H:%M'), "note": "Ultimo run ieri o prima"}
else:
    report["checks"]["batch_chiamate"] = {"status": "MISSING", "last_run": "mai", "note": "Log file non trovato"}

# === 3. NEWSLETTER (solo lun-ven) ===
weekday = now.weekday()  # 0=Mon, 6=Sun
if weekday < 5:  # weekday
    newsletter_log = Path("/opt/data/strategie/email_sent_log.txt")
    if newsletter_log.exists():
        mtime = datetime.fromtimestamp(newsletter_log.stat().st_mtime, tz=TZ)
        if mtime.strftime('%Y-%m-%d') == today_str:
            report["checks"]["newsletter"] = {"status": "OK", "last_send": mtime.strftime('%H:%M')}
        else:
            report["checks"]["newsletter"] = {"status": "MISSING", "last_send": mtime.strftime('%d/%m %H:%M'), "note": "Newsletter non inviata oggi"}
    else:
        report["checks"]["newsletter"] = {"status": "MISSING", "last_send": "mai"}
else:
    report["checks"]["newsletter"] = {"status": "SKIPPED", "note": "Weekend — nessuna newsletter prevista"}

# === 4. GATEWAY ===
try:
    result = subprocess.run(
        ["/opt/hermes/.venv/bin/hermes", "gateway", "status"],
        capture_output=True, text=True, timeout=10,
        env={**__import__('os').environ, "PATH": "/opt/hermes/.venv/bin:" + __import__('os').environ.get("PATH", "")}
    )
    report["checks"]["gateway"] = {"status": "RUNNING" if "running" in result.stdout.lower() else "UNKNOWN", "output": result.stdout.strip()[:200]}
except Exception as e:
    report["checks"]["gateway"] = {"status": "ERROR", "error": str(e)[:100]}

# === RIEPILOGO ===
all_ok = all(
    c.get("status") in ("OK", "SKIPPED")
    for c in report["checks"].values()
)

report["summary"] = "ALL_OK" if all_ok else "ISSUES_FOUND"
report["needs_action"] = [
    name for name, c in report["checks"].items()
    if c.get("status") == "MISSING"
]

print(json.dumps(report, indent=2, ensure_ascii=False))
