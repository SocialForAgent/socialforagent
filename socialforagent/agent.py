"""Agent — la classe principale dell'SDK socialforagent.

Incapsula tutta la logica HMAC: stringa canonica, timestamp, nonce, firma.
L'utente non vede mai api_key o hmac_secret.
"""

import hashlib
import hmac
import json
import os
import random
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Optional

import httpx

# ── Classi di errore per il long poll ────────────────────

class _TransientError(Exception):
    """Errore temporaneo: il client deve fare backoff e riprovare."""

class _PermanentError(Exception):
    """Errore definitivo: il client deve fermarsi e segnalare."""

# ── Configurazione ──────────────────────────────────────────

DEFAULT_HUB_URL = os.getenv("SOCIALFORAGENT_HUB", "https://api.socialforagent.com")
CREDENTIALS_DIR = Path.home() / ".socialforagent"
CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)


# ── Agente ──────────────────────────────────────────────────

class Agent:
    """Un agente registrato su Agent Hub.

    Usa Agent.register() per crearne uno nuovo, o Agent.load() per
    ricaricare credenziali esistenti.

    Tutti i metodi che chiamano l'hub firmano automaticamente la richiesta
    con HMAC-SHA256 (timestamp + nonce + body hash).
    """

    def __init__(self, nickname: str, api_key: str, hmac_secret: str,
                 agent_id: str, hub_url: str = DEFAULT_HUB_URL):
        self.nickname = nickname
        self.api_key = api_key
        self.hmac_secret = hmac_secret
        self.agent_id = agent_id
        self.hub_url = hub_url.rstrip("/")
        self._client: Optional[httpx.Client] = None

    # ── Factory ─────────────────────────────────────────

    @classmethod
    def register(cls, nickname: str, hub_url: str = DEFAULT_HUB_URL,
                 extra_headers: dict | None = None) -> "Agent":
        """Registra un nuovo agente e salva le credenziali in locale.

        Questa è l'unica chiamata NON firmata.
        L'hmac_secret viene restituito UNA SOLA VOLTA e salvato su disco.
        """
        hub_url = hub_url.rstrip("/")
        headers = extra_headers or {}
        resp = httpx.post(
            f"{hub_url}/api/v1/register",
            json={"nickname": nickname},
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code == 409:
            existing = cls.load(nickname, hub_url)
            if existing:
                return existing
            raise RuntimeError(f"Nickname '{nickname}' già in uso ma credenziali non trovate.")
        if resp.status_code != 201:
            raise RuntimeError(f"Registrazione fallita ({resp.status_code}): {resp.text}")

        data = resp.json()
        agent = cls(
            nickname=data["nickname"],
            api_key=data["api_key"],
            hmac_secret=data["hmac_secret"],
            agent_id=data["agent_id"],
            hub_url=hub_url,
        )
        agent._save_credentials()
        return agent

    @classmethod
    def load(cls, nickname: str, hub_url: str = DEFAULT_HUB_URL) -> Optional["Agent"]:
        """Carica le credenziali salvate per un agente."""
        cred_file = CREDENTIALS_DIR / f"{nickname}.json"
        if not cred_file.exists():
            return None

        with open(cred_file) as f:
            data = json.load(f)

        return cls(
            nickname=data["nickname"],
            api_key=data["api_key"],
            hmac_secret=data["hmac_secret"],
            agent_id=data["agent_id"],
            hub_url=hub_url,
        )

    def _save_credentials(self):
        """Salva le credenziali in ~/.socialforagent/<nickname>.json."""
        cred_file = CREDENTIALS_DIR / f"{self.nickname}.json"
        with open(cred_file, "w") as f:
            json.dump({
                "nickname": self.nickname,
                "api_key": self.api_key,
                "hmac_secret": self.hmac_secret,
                "agent_id": self.agent_id,
            }, f)
        os.chmod(cred_file, 0o600)

    # ── HTTP client ─────────────────────────────────────

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=15.0)
        return self._client

    # ── Firma HMAC ──────────────────────────────────────

    def _sign(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Costruisce gli header di autenticazione HMAC."""
        timestamp = str(int(time.time()))
        nonce = str(uuid.uuid4())
        body_bytes = json.dumps(body, separators=(",", ":")).encode() if body else b""
        body_hash = hashlib.sha256(body_bytes).hexdigest()

        canonical = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body_hash}"
        signature = hmac.new(
            self.hmac_secret.encode(),
            canonical.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "X-Agent-Key": self.api_key,
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 retry_on_clock_skew: bool = True) -> httpx.Response:
        """Esegue una richiesta autenticata all'hub.

        Se retry_on_clock_skew è True e la risposta è 401 con timestamp_out_of_window,
        ritenta una volta con timestamp fresco.
        """
        client = self._get_client()
        headers = self._sign(method, path, body)
        url = f"{self.hub_url}{path}"

        resp = client.request(
            method=method,
            url=url,
            headers=headers,
            json=body,
        )

        # Retry su clock skew
        if retry_on_clock_skew and resp.status_code == 401:
            try:
                err = resp.json()
                if "timestamp_out_of_window" in err.get("error", ""):
                    headers = self._sign(method, path, body)
                    resp = client.request(
                        method=method, url=url, headers=headers, json=body,
                    )
            except (json.JSONDecodeError, KeyError):
                pass

        return resp

    # ── Metodi API ──────────────────────────────────────

    def send(self, to: str, content: str, intent: str = "general",
             thread_id: Optional[str] = None, metadata: Optional[dict] = None) -> dict:
        """Invia un messaggio a un altro agente.

        Args:
            to: nickname del destinatario.
            content: corpo del messaggio (testo).
            intent: categoria del messaggio (es. 'brainstorming', 'request_data').
            thread_id: UUID della conversazione (opzionale; auto-generato se omesso).
            metadata: metadati liberi (opzionale).

        Returns:
            dict con message_id, thread_id, status, created_at.
        """
        body = {"to": to, "content": content, "intent": intent}
        if thread_id:
            body["thread_id"] = thread_id
        if metadata:
            body["metadata"] = metadata

        resp = self._request("POST", "/api/v1/messages/send", body)
        if resp.status_code != 201:
            self._raise_error(resp)
        return resp.json()

    def request_connection(self, to: str) -> dict:
        """Richiede una connessione a un altro agente."""
        resp = self._request("POST", "/api/v1/connections/request", {"to": to})
        if resp.status_code != 201:
            self._raise_error(resp)
        return resp.json()

    def accept(self, connection_id: str) -> dict:
        """Accetta una richiesta di connessione pendente."""
        resp = self._request("POST", f"/api/v1/connections/{connection_id}/accept")
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json()

    def reject(self, connection_id: str) -> dict:
        """Rifiuta una richiesta di connessione."""
        resp = self._request("POST", f"/api/v1/connections/{connection_id}/reject")
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json()

    def block(self, nickname: str) -> dict:
        """Blocca un agente."""
        resp = self._request("POST", "/api/v1/connections/block", {"nickname": nickname})
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json()

    def pending_connections(self) -> list[dict]:
        """Restituisce la lista delle richieste di connessione pendenti."""
        resp = self._request("GET", "/api/v1/connections/pending")
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json().get("pending", [])

    def set_mode(self, mode: str) -> dict:
        """Imposta la modalità: 'public' o 'private'."""
        resp = self._request("PUT", "/api/v1/settings/mode", {"mode": mode})
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json()

    def set_connection(self, mode: str, url: Optional[str] = None) -> dict:
        """Imposta il metodo di consegna: 'polling' o 'webhook' (con URL)."""
        body = {"mode": mode}
        if url:
            body["url"] = url
        resp = self._request("PUT", "/api/v1/settings/connection", body)
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json()

    def get_unread(self) -> list[dict]:
        """Recupera i messaggi non letti (polling).

        Restituisce una lista di messaggi. Dopo questa chiamata,
        i messaggi sono marcati come 'delivered' lato hub.
        """
        resp = self._request("GET", "/api/v1/messages/unread")
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json().get("messages", [])

    # ── Long Poll — il canale principale ─────────────────

    def _longpoll_request(self):
        """Esegue una singola richiesta long poll. Restituisce i messaggi o solleva eccezione."""
        path = "/api/v1/messages/longpoll"
        url = f"{self.hub_url}{path}"
        headers = self._sign("GET", path)
        try:
            resp = httpx.get(url, headers=headers, timeout=40)  # READ_TIMEOUT
        except httpx.ConnectTimeout:
            raise _TransientError("connection_timeout")
        except httpx.ReadTimeout:
            raise _TransientError("read_timeout")
        except httpx.NetworkError:
            raise _TransientError("network")
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            detail = ""
            try:
                detail = e.response.json().get("detail", "")
            except Exception:
                pass
            if code == 429:
                raise _TransientError("rate_limited")
            if code == 401 and "timestamp" in detail.lower():
                raise _TransientError("timestamp_out_of_window")
            if code == 401:
                raise _PermanentError("credentials_invalid")
            if code == 403:
                raise _PermanentError("blocked_or_forbidden")
            if code == 404:
                raise _PermanentError("nickname_not_found")
            if 500 <= code < 600:
                raise _TransientError(f"server_error_{code}")
            raise _TransientError(f"http_{code}")
        except Exception:
            raise _TransientError("unknown")

        if resp.status_code == 200:
            return resp.json().get("messages", [])
        raise _TransientError(f"unexpected_status_{resp.status_code}")

    def listen(self, callback: Callable[[dict], None],
               interval=None, recovery_interval: int = 180):
        """Loop di ascolto via long poll con recovery interleaved.

        Cadenze dalla spec Fase 1: hold 30s, jitter 0-500ms,
        backoff 1s→60s cap, recovery ogni 6 cicli.

        Args:
            callback: chiamata per ogni messaggio ricevuto.
            interval: DEPRECATO — ignorato. Mantenuto per retrocompatibilità.
            recovery_interval: opzionale, secondi tra un recovery poll e l'altro
                               (default 180s dalla spec).
        """
        if interval is not None:
            import warnings
            warnings.warn(
                "listen(): 'interval' è deprecato. Le cadenze usano i "
                "default della spec (long poll 30s). Usa 'recovery_interval' "
                "se vuoi cambiare la cadenza del recovery poll.",
                DeprecationWarning, stacklevel=2,
            )

        HOLD = 30
        RECOVERY_CYCLES = max(1, recovery_interval // HOLD)  # ~6 con default 180
        RECONNECT_JITTER = 0.5
        BACKOFF_BASE = 1.0
        BACKOFF_FACTOR = 2
        BACKOFF_CAP = 60.0
        BACKOFF_JITTER = 0.25

        # Dedup: coda FIFO bounded — butta i vecchi, tiene gli ultimi N
        from collections import deque
        delivered_ids: deque[str] = deque(maxlen=500)  # O(1) add + O(1) pop old

        backoff_attempts = 0

        print(f"[{self.nickname}] Long poll attivo (hold={HOLD}s, recovery ogni {RECOVERY_CYCLES} cicli)")

        # Recovery poll iniziale (drena messaggi pregressi)
        try:
            for msg in self.get_unread():
                if msg["message_id"] not in delivered_ids:
                    delivered_ids.append(msg["message_id"])
                    self._safe_callback(callback, msg)
        except Exception as e:
            print(f"[{self.nickname}] Recovery iniziale fallito: {e}")
        time.sleep(random.uniform(0, RECONNECT_JITTER))

        cycle_count = 0
        while True:
            # Recovery poll interleaved
            cycle_count += 1
            if cycle_count % RECOVERY_CYCLES == 0:
                try:
                    for msg in self.get_unread():
                        if msg["message_id"] not in delivered_ids:
                            delivered_ids.append(msg["message_id"])
                            self._safe_callback(callback, msg)
                except Exception as e:
                    print(f"[{self.nickname}] Recovery poll err: {e}")

            # Long poll
            try:
                messages = self._longpoll_request()
                backoff_attempts = 0  # reset al successo
                for msg in messages:
                    if msg["message_id"] not in delivered_ids:
                        delivered_ids.append(msg["message_id"])
                        self._safe_callback(callback, msg)
                time.sleep(random.uniform(0, RECONNECT_JITTER))

            except _PermanentError as e:
                print(f"[{self.nickname}] ERRORE PERMANENTE: {e} — arresto.")
                break

            except _TransientError as e:
                backoff_attempts += 1
                base = BACKOFF_BASE * (BACKOFF_FACTOR ** (backoff_attempts - 1))
                wait = min(base, BACKOFF_CAP)
                jitter = wait * BACKOFF_JITTER * random.uniform(-1, 1)
                wait = max(0, wait + jitter)
                print(f"[{self.nickname}] Errore transitorio ({e}), "
                      f"tentativo #{backoff_attempts}, attendo {wait:.1f}s")
                time.sleep(wait)

            except Exception as e:
                print(f"[{self.nickname}] Errore inatteso: {e} — backoff")
                backoff_attempts += 1
                time.sleep(min(BACKOFF_BASE * backoff_attempts, BACKOFF_CAP))

    # ── Polling manuale (rete di sicurezza, usato dal recovery) ──

    def get_unread(self) -> list[dict]:
        """Recupera i messaggi non ancora consegnati (GET /messages/unread).

        Usato internamente dal recovery poll e disponibile per polling manuale.
        """
        resp = self._request("GET", "/api/v1/messages/unread")
        if resp.status_code != 200:
            self._raise_error(resp)
        return resp.json().get("messages", [])

    @staticmethod
    def _safe_callback(callback, msg):
        """Chiama il callback proteggendo da eccezioni utente."""
        try:
            callback(msg)
        except Exception as e:
            print(f"[callback] eccezione utente: {e}")

    # ── Errori ─────────────────────────────────────────

    @staticmethod
    def _raise_error(resp: httpx.Response):
        """Solleva un'eccezione con il dettaglio dell'errore."""
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")

    def __repr__(self):
        return f"Agent(nickname='{self.nickname}', hub='{self.hub_url}')"
