"""
clock_sync.py — Compensazione del clock drift senza permessi di sistema.

Perche': su VPS OpenVZ/LXC (e dentro i container) NON puoi cambiare l'orologio.
Se l'host e' indietro di 2-3 ore, l'HMAC fallisce con 401 e non puoi fixare
l'ora da dentro. Soluzione: non tocchiamo l'orologio e non allarghiamo la
window HMAC. Misuriamo l'offset rispetto al SERVER (header HTTP `Date`,
leggibile anche da una risposta 401) e lo applichiamo ai timestamp che
firmano l'HMAC.

  - offset  = il "trimmer" di compensazione (secondi da sommare al clock locale)
  - jsonl   = il "database" con lo storico per ogni agente
  - re-sync = periodico + automatico al primo 401 (l'offset si auto-corregge)
"""

import json
import time
import email.utils
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


class ClockSync:
    def __init__(self, base_url, handle, state_dir="state",
                 resync_interval=300, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.handle = handle
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.offset_file = self.state_dir / "clock_offset.json"
        self.log_file = self.state_dir / "clock_drift_log.jsonl"
        self.resync_interval = resync_interval
        self.timeout = timeout
        self.offset = 0.0
        self._last_sync_mono = 0.0
        self._load()

    # --- tempo corretto (usa QUESTI, non time.time()/datetime.now()) --------
    def time(self) -> float:
        """Epoch corretto. Mettilo al posto di time.time() per firmare l'HMAC."""
        return time.time() + self.offset

    def now_utc(self) -> datetime:
        """datetime UTC corretto, per log e stampe dell'agente."""
        return datetime.fromtimestamp(self.time(), tz=timezone.utc)

    # --- misura / aggiorna offset ------------------------------------------
    def sync(self, force=False) -> float:
        """Misura l'offset dal Date header del server (HEAD sulla root).
        Throttlata: rifa' la misura solo ogni resync_interval secondi."""
        if not force and (time.monotonic() - self._last_sync_mono) < self.resync_interval:
            return self.offset
        server_epoch, local_epoch = self._probe()
        if server_epoch is not None:
            self._apply(server_epoch, local_epoch)
        return self.offset  # se la probe fallisce, tieni l'ultimo offset noto

    def sync_from_response(self, response) -> bool:
        """Impara l'offset dall'header Date di una risposta GIA' ricevuta
        (tipicamente la 401 stessa). Ritorna True se aggiornato."""
        date_hdr = getattr(response, "headers", {}).get("Date")
        if not date_hdr:
            return False
        self._apply(self._parse_http_date(date_hdr), time.time())
        return True

    # --- interni ------------------------------------------------------------
    def _probe(self):
        before = time.time()
        try:
            req = urllib.request.Request(self.base_url + "/", method="HEAD")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                date_hdr = r.headers.get("Date")
            after = time.time()
        except urllib.error.HTTPError as e:          # anche 4xx/401 porta Date
            date_hdr = e.headers.get("Date") if e.headers else None
            after = time.time()
        except Exception:
            return None, None
        if not date_hdr:
            return None, None
        # punto medio della richiesta: compensa la latenza di rete
        return self._parse_http_date(date_hdr), (before + after) / 2

    @staticmethod
    def _parse_http_date(date_hdr):
        dt = email.utils.parsedate_to_datetime(date_hdr)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    def _apply(self, server_epoch, local_epoch):
        delta = (server_epoch - local_epoch) - self.offset
        self.offset = server_epoch - local_epoch
        self._last_sync_mono = time.monotonic()
        rec = {
            "handle": self.handle,
            "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "local_epoch": round(local_epoch, 3),
            "server_epoch": round(server_epoch, 3),
            "offset_seconds": round(self.offset, 3),
            "delta_since_last": round(delta, 3),
        }
        self.offset_file.write_text(json.dumps(rec, indent=2))
        with self.log_file.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def _load(self):
        try:
            self.offset = float(json.loads(self.offset_file.read_text())["offset_seconds"])
        except Exception:
            self.offset = 0.0


# --- prova rapida -----------------------------------------------------------
if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://socialforagent.com"
    c = ClockSync(base_url=url, handle="test")
    c.sync(force=True)
    print(f"offset misurato : {c.offset:+.3f} s")
    print(f"ora locale      : {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    print(f"ora corretta    : {c.now_utc().strftime('%H:%M:%S')} UTC")
