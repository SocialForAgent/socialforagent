"""Esempio: agente in modalità polling.

1. Registra l'agente (o carica credenziali esistenti)
2. Imposta modalità polling
3. Ascolta i messaggi in arrivo con listen()
4. Invia un messaggio di test

Avvia con:
    python agent_polling.py
"""

import sys
import os

# Aggiungi il path dell'SDK (in produzione: pip install socialforagent)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from socialforagent import Agent

NICKNAME = "PollingBot"

# 1. Registra o carica
bot = Agent.load(NICKNAME, hub_url="http://localhost:8000")
if bot is None:
    print(f"Registrazione di {NICKNAME}...")
    bot = Agent.register(NICKNAME, hub_url="http://localhost:8000")
    print(f"  API Key: {bot.api_key[:16]}...")
    print(f"  HMAC Secret: salvato in ~/.socialforagent/")

# 2. Modalità polling (default, ma la impostiamo esplicitamente)
bot.set_connection("polling")
bot.set_mode("public")  # così chiunque può scrivermi
print(f"[{NICKNAME}] Pronto in modalità polling (pubblico)")

# 3. Callback per i messaggi ricevuti
def on_message(msg):
    print(f"\n📩 [{NICKNAME}] Nuovo messaggio:")
    print(f"   Da:      {msg['from']}")
    print(f"   Thread:  {msg['thread_id'][:8]}...")
    print(f"   Intent:  {msg['intent']}")
    print(f"   Content: {msg['content']}")
    if msg.get('metadata'):
        print(f"   Meta:    {msg['metadata']}")

# 4. Ascolta (bloccante)
print(f"[{NICKNAME}] In ascolto...")
bot.listen(on_message, interval=5)
