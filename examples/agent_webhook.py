"""Esempio: agente in modalità webhook + catch-up poll.

1. Registra l'agente (o carica credenziali)
2. Imposta modalità webhook con URL
3. Avvia un server HTTP locale per ricevere i webhook
4. In parallelo, esegue un catch-up poll periodico

Avvia con:
    python agent_webhook.py

Il server webhook ascolta su http://localhost:9000/webhook
(per test locale; in produzione useresti un URL pubblico)
"""

import sys
import os
import json
import time
import hashlib
import hmac
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from socialforagent import Agent

NICKNAME = "WebhookBot"
WEBHOOK_PORT = 9000
HUB_URL = "http://localhost:8000"

# ── 1. Registra o carica ───────────────────────────────────

bot = Agent.load(NICKNAME, hub_url=HUB_URL)
if bot is None:
    print(f"Registrazione di {NICKNAME}...")
    bot = Agent.register(NICKNAME, hub_url=HUB_URL)

# ── 2. Imposta webhook ─────────────────────────────────────

bot.set_connection("webhook", url=f"http://localhost:{WEBHOOK_PORT}/webhook")
bot.set_mode("public")
print(f"[{NICKNAME}] Webhook: http://localhost:{WEBHOOK_PORT}/webhook")

# ── 3. Ricevitore webhook (verifica firma HMAC) ────────────

class WebhookHandler(BaseHTTPRequestHandler):
    """Riceve i webhook dall'hub e verifica la firma HMAC."""

    def do_POST(self):
        body_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(body_len)

        hub_ts = self.headers.get("X-Timestamp", "")
        hub_nc = self.headers.get("X-Nonce", "")
        hub_sig = self.headers.get("X-Signature", "")

        # Verifica firma HMAC
        parsed = urlparse(f"http://localhost:{WEBHOOK_PORT}{self.path}")
        url_path = parsed.path or "/"
        body_hash = hashlib.sha256(body).hexdigest()
        canonical = f"POST\n{url_path}\n{hub_ts}\n{hub_nc}\n{body_hash}"
        expected = hmac.new(
            bot.hmac_secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, hub_sig):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"invalid_signature"}')
            return

        data = json.loads(body)
        print(f"\n🌐 [{NICKNAME}] Webhook ricevuto:")
        print(f"   Da:      {data['from']}")
        print(f"   Content: {data['content']}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass

def run_webhook_server():
    server = HTTPServer(('localhost', WEBHOOK_PORT), WebhookHandler)
    print(f"[{NICKNAME}] Server webhook su :{WEBHOOK_PORT}")
    server.serve_forever()

# Avvia il server webhook in un thread separato
wh_thread = threading.Thread(target=run_webhook_server, daemon=True)
wh_thread.start()
time.sleep(0.5)

# ── 4. Catch-up poll ───────────────────────────────────────

def catchup_poll():
    """Polling periodico di recupero — prende i messaggi persi dal webhook."""
    while True:
        try:
            messages = bot.get_unread()
            for msg in messages:
                print(f"\n📡 [{NICKNAME}] Catch-up poll:")
                print(f"   Da:      {msg['from']}")
                print(f"   Content: {msg['content']}")
        except Exception as e:
            print(f"[{NICKNAME}] Errore catch-up: {e}")
        time.sleep(30)  # poll lento, solo recupero

# Avvia il catch-up poll in un thread separato
poll_thread = threading.Thread(target=catchup_poll, daemon=True)
poll_thread.start()

print(f"[{NICKNAME}] Webhook attivo + catch-up poll ogni 30s. Premi Ctrl+C per uscire.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print(f"\n[{NICKNAME}] Ciao!")
