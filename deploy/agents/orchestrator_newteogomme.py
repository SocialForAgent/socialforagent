#!/usr/bin/env python3
"""
NewTeogomme Orchestrator - Ciclo operativo di supervisione di Giorgia (ElevenLabs).
Esegui senza argomenti per il ciclo completo, oppure specifica i passi:

  python3 orchestrator.py              # ciclo completo
  python3 orchestrator.py --fetch      # solo raccolta trascrizioni
  python3 orchestrator.py --analyze    # solo analisi
  python3 orchestrator.py --escalate   # solo escalation email
  python3 orchestrator.py --check      # solo controllo risposte Teo
  python3 orchestrator.py --callback   # solo richiamate
"""

import json, os, sys, re, time, subprocess
import smtplib, imaplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

# Import registro appuntamenti (integrato da Giorgia con mentoring Laura - 20/06/2026)
import manage_appointments as appt_mgr

# Import analyze_transcript (creato da allievo10, mentoring maestro12)
from analyze_transcript import analyze_transcript

# --- Config ---
DATA_DIR = Path("/opt/data/allievo10")
APPOINTMENTS_FILE = DATA_DIR / "appointments.json"
PROCESSED_FILE = DATA_DIR / "processed_calls.json"
PENDING_FILE = DATA_DIR / "pending_requests.json"
KNOWLEDGE_FILE = DATA_DIR / "knowledge_state.json"
PROMPT_DIR = DATA_DIR / "prompt_default"
LATEST_PROMPT_FILE = PROMPT_DIR / "LATEST"
SMS_KEY_FILE = DATA_DIR / ".sms_auth_key"
SMS_SECRET_FILE = DATA_DIR / ".sms_auth_secret"
LOG_FILE = DATA_DIR / "orchestrator.log"
SMS_TEMPLATES_FILE = DATA_DIR / "sms_templates.json"
ENV_FILE = Path("/opt/data/.env")

# Email credentials (same as gateway)
EMAIL_ADDRESS = "giorgianewteogomme@gmail.com"
EMAIL_IMAP_HOST = "imap.gmail.com"
EMAIL_SMTP_HOST = "smtp.gmail.com"
EMAIL_SMTP_PORT = 587

GIORGIA_AGENT_ID = "agent_6801kv7t4mr3e56bkne424fxqzhn"
GIORGIA_PHONE_ID = "phnum_1401k4cwa3w0es6ta548v7pfd1vy"
GIORGIA_PHONE = "+390282398320"
TEO_EMAIL = "newteogomme@icloud.com"
SUPERVISOR_EMAIL = "prontosinistri@gmail.com"
FROM_EMAIL = "giorgianewteogomme@gmail.com"

# Himalaya CLI per invio e ricezione email da terminale
HIMALAYA_BIN = "/opt/data/home/.local/bin/himalaya"
PAOLO_EMAIL = "prontosinistri@gmail.com"  # destinatario escalation

# === BLACKLIST NUMERI: mai processare, mai richiamare ===
# Paolo (supervisore) e altri numeri di test/collaudo
BLOCKED_NUMBERS = [
    "+393931485839",  # Paolo (supervisore)
]

def _is_blocked_number(phone: str) -> bool:
    """Verifica se un numero e nella blacklist (match esatto o suffisso)."""
    if not phone:
        return False
    clean = "".join(c for c in phone if c.isdigit() or c == "+")
    for blocked in BLOCKED_NUMBERS:
        blocked_clean = "".join(c for c in blocked if c.isdigit() or c == "+")
        if clean == blocked_clean:
            return True
        # Match sulle ultime 4 cifre (per numeri mascherati)
        if len(clean) >= 4 and len(blocked_clean) >= 4:
            if clean[-4:] == blocked_clean[-4:]:
                return True
    return False


# Il terminale Erminio li maschera con asterischi anche nel codice sorgente.
# Leggere sempre i numeri dai file JSON (dove sono salvati corretti dall'API).

FALLBACK_PHRASES = [
    "chiedo al mio responsabile",
    "chiedo al responsabile",
    "lo chiedo al mio",
    "non lo so",
    "non saprei",
    # Promesse di richiamata (tutte le varianti/coniugazioni) - regola n.1 Teo:
    # se Giorgia promette di richiamare, il cliente VA SEMPRE richiamato.
    "richiam",        # richiamo, richiamarla, richiamerò, la richiamo, richiamo a questo numero
    "ricontatt",      # ricontatterò, ricontatto, la ricontatto, la ricontatterò
    "richiamer",      # richiamerò, richiameremo
    "contatter",      # la contatterò, contatteremo
    # Promesse di verifica/controllo (Giorgia non sa e deve verificare)
    "faccio verificare",
    "preferisco fare una verifica",
    "devo verificare",
    "fare una verifica",
    "controllare i nostri sistemi",
    "controllo i nostri sistemi",
    "verifico e",
    "verifica precisa",
    "darle una risposta sicura",
    "darle una risposta precisa",
    "appena avrò verificato",
    "appena ho verificato",
]

# Frasi che indicano che il cliente chiede esplicitamente di parlare
# con un operatore umano o con Ziliani (regola #7)
OPERATOR_REQUEST_PHRASES = [
"parlare con un operatore",
"parlare con operatore",
"parlare con una persona",
"parlare con una persona reale",
"parlare con un umano",
"parlare con ziliani",
"parlare con il titolare",
"parlare con titolare",
"parlare col titolare",
"parlare col responsabile",
"parlo con ziliani",
"passarmi ziliani",
"passarmi un operatore",
"passarmi una persona",
"passarmi il titolare",
"mi passi un operatore",
"mi passi ziliani",
"mi passi una persona",
"mi passi il titolare",
"mi passi il responsabile",
"voglio parlare con una persona",
"voglio parlare con un operatore",
"voglio parlare con ziliani",
"voglio parlare col titolare",
"voglio parlare col responsabile",
"voglio parlare con il titolare",
"operatore umano",
"non sei una persona",
"sei un robot",
"sei un assistente virtuale",
"sei un'intelligenza artificiale",
"voce registrata",
"non voglio parlare con te",
"famme parla' co' ziliani",
]

# Frasi che indicano che Giorgia ha inventato azioni che non può fare
# (es. "ho registrato", "prenotato", "confermo" senza avere strumenti reali)
FAKE_ACTION_PHRASES = [
    "ho registrato",
    "ho prenotato",
    "registrato la prenotazione",
    "registrato il suo appuntamento",
    "confermo l'appuntamento",
    "appuntamento confermato",
    "le mando un promemoria",
    "le invio una conferma",
]

# Frasi che indicano che Giorgia non ha capito il cliente
CONFUSION_PHRASES = [
    "non ho capito",
    "non capisco",
    "può chiarire",
    "puoi chiarire",
    "potrebbe chiarire",
    "non ho compreso",
]

# Frasi che indicano che il cliente vuole un appuntamento ma Giorgia
# NON deve fissarlo — deve solo chiedere quando e promettere richiamata.
APPOINTMENT_PHRASES = [
    "appuntamento",
    "prenotare",
    "prenotazione",
    "quando posso venire",
    "quando posso passare",
    "fissare un appuntamento",
    "prendere appuntamento",
    "cambio gomme",      # il cambio gomme richiede appuntamento
    "cambio stagionale",  # idem
    # Aggiunte 20/06/2026 (mentoring Laura):
    "allora l aspetto",
    "a che ora",
    "che giorno",
    "prendo appuntamento",
    "vengo da voi",
    "passo in officina",
    "ci vediamo",
    "martedi",
    "mercoledi",
    "giovedi",
    "venerdi",
    "lunedi",
    "sabato",
    "fissiamo per",
    "segnato per",
    "confermato per",
]


def get_greeting(capitalize=False):
    """Restituisce il saluto appropriato in base all'ora italiana.
    - 06:00-13:00: buongiorno
    - 13:00-18:00: buon pomeriggio
    - 18:00-06:00: buonasera
    Se capitalize=True, la prima lettera è maiuscola (per inizio frase).
    """
    now = datetime.now(ZoneInfo("Europe/Rome"))
    hour = now.hour
    if 6 <= hour < 13:
        greeting = "buongiorno"
    elif 13 <= hour < 18:
        greeting = "buon pomeriggio"
    else:
        greeting = "buonasera"
    return greeting.capitalize() if capitalize else greeting


def load_api_key() -> str:
    """Carica la API key ElevenLabs dal file .env."""
    if not ENV_FILE.exists():
        sys.exit("ERRORE: /opt/data/.env non trovato")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("ELEVENLABS_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("ERRORE: ELEVENLABS_API_KEY non trovata in .env")


def load_env_value(key: str) -> str:
    """Carica un valore generico dal file .env (ritorna '' se assente)."""
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _get_email_password() -> str:
    """Carica la password email dal .env (lazy load)."""
    return load_env_value("EMAIL_PASSWORD")


def send_email_smtp(subject: str, body: str, html_body: str = None) -> bool:
    """Invia email tramite Gmail SMTP (senza himalaya).
    Se html_body e' fornito, invia come multipart/alternative (testo + HTML).
    Altrimenti invia come plain text.
    Ritorna True se inviata con successo."""
    try:
        password = _get_email_password()
        if not password:
            print("  ERRORE: EMAIL_PASSWORD non trovata in .env")
            return False
        
        if html_body:
            msg = MIMEMultipart("alternative")
            msg["From"] = FROM_EMAIL
            msg["To"] = TEO_EMAIL
            msg["Cc"] = SUPERVISOR_EMAIL
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
            msg["From"] = FROM_EMAIL
            msg["To"] = TEO_EMAIL
            msg["Cc"] = SUPERVISOR_EMAIL
            msg["Subject"] = subject
        
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, password)
            server.sendmail(FROM_EMAIL, [TEO_EMAIL, SUPERVISOR_EMAIL], msg.as_string())
        return True
    except Exception as e:
        print(f"  ERRORE SMTP: {e}")
        return False


def check_inbox_imap() -> list:
    """Controlla la inbox Gmail per email da TEO_EMAIL o SUPERVISOR_EMAIL.
    Ritorna lista di dict: [{\"id\": str, \"subject\": str, \"from\": str, \"date\": str}, ...]."""
    try:
        password = _get_email_password()
        if not password:
            return []
        mail = imaplib.IMAP4_SSL(EMAIL_IMAP_HOST, timeout=30)
        mail.login(EMAIL_ADDRESS, password)
        mail.select("INBOX")
        status, messages = mail.search(None, f'OR FROM "{TEO_EMAIL}" FROM "{SUPERVISOR_EMAIL}"')
        if status != "OK":
            mail.logout()
            return []
        msg_ids = messages[0].split()
        envelopes = []
        import email as em
        from email.header import decode_header
        for mid in reversed(msg_ids[-20:]):  # ultime 20
            status, data = mail.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK":
                continue
            raw = data[0][1]
            msg = em.message_from_bytes(raw)
            # Decodifica il subject (da =?UTF-8?Q?...?= a testo leggibile)
            raw_subj = msg.get("Subject", "")
            decoded_subj = ""
            for part, charset in decode_header(raw_subj):
                if isinstance(part, bytes):
                    decoded_subj += part.decode(charset or "utf-8", errors="replace")
                else:
                    decoded_subj += part
            envelopes.append({
                "id": mid.decode(),
                "subject": decoded_subj,
                "from": msg.get("From", ""),
                "date": msg.get("Date", ""),
            })
        mail.logout()
        return envelopes
    except Exception as e:
        print(f"  ERRORE IMAP (list): {e}")
        return []


def read_email_imap(email_id: str) -> str:
    """Legge il corpo di una email specifica tramite IMAP.
    Ritorna il testo del body (o stringa vuota)."""
    try:
        password = _get_email_password()
        if not password:
            return ""
        mail = imaplib.IMAP4_SSL(EMAIL_IMAP_HOST, timeout=30)
        mail.login(EMAIL_ADDRESS, password)
        mail.select("INBOX")
        status, data = mail.fetch(email_id.encode(), "(RFC822)")
        if status != "OK":
            mail.logout()
            return ""
        import email as em
        msg = em.message_from_bytes(data[0][1])
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")
                        break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")
        mail.logout()
        return body.strip()
    except Exception as e:
        print(f"  ERRORE IMAP (read {email_id}): {e}")
        return ""


def load_sms_credentials() -> tuple:
    """Carica le credenziali SMSTools.it dai file sicuri.
    Ritorna (auth_key, auth_secret)."""
    key = SMS_KEY_FILE.read_text().strip() if SMS_KEY_FILE.exists() else ""
    secret = SMS_SECRET_FILE.read_text().strip() if SMS_SECRET_FILE.exists() else ""
    return key, secret


def send_sms_via_smstools(to_number: str, text: str) -> dict:
    """Invia un SMS tramite SMSTools.it API.
    to_number: numero con prefisso internazionale senza '+' (es. '393931485839')
    text: corpo del messaggio (max 160 caratteri standard, multi-SMS automatico)
    Ritorna dict con {'ok': True, 'sms_id': ...} o {'ok': False, 'error': ...}"""
    import urllib.request, urllib.error, base64, urllib.parse

    auth_key, auth_secret = load_sms_credentials()
    if not auth_key or not auth_secret:
        return {"ok": False, "error": "Credenziali SMS non configurate"}

    # Pulisci il numero: rimuovi +, spazi, trattini, asterischi (per sicurezza)
    clean_number = to_number.strip().lstrip('+').replace(' ', '').replace('-', '').replace('*', '')

    if not clean_number.isdigit():
        return {"ok": False, "error": f"Numero non valido: '{to_number}' -> '{clean_number}'"}
    if len(clean_number) < 10:
        return {"ok": False, "error": f"Numero troppo corto: {clean_number}"}

    # Prepara l'auth Basic
    credentials = f"{auth_key}:{auth_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()

    # Prepara il body form-urlencoded
    body = urllib.parse.urlencode({
        "from": "NewTeogomme",
        "to": clean_number,
        "text": text,
        "sandbox": "false"
    }).encode()

    req = urllib.request.Request(
        "https://api.smstools.it/rest/api/sms/send",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded}"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if result.get("smsInserted", 0) > 0:
            sms_list = result.get("sms", [])
            sms_id = sms_list[0].get("id") if sms_list else None
            return {"ok": True, "sms_id": sms_id}
        else:
            error_msg = result.get("errorMsg", "Errore sconosciuto SMSTools")
            return {"ok": False, "error": error_msg}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def get_default_model() -> tuple:
    """Legge il modello/provider LLM di default dalla config Hermes.
    Ritorna (model, provider). Fallback a DeepSeek se la config non e' leggibile."""
    try:
        import yaml
        # Prima prova la config Hermes
        for cfg_path in [
            Path("/opt/hermes/config.yaml"),
            Path.home() / ".hermes" / "config.yaml",
            Path("/opt/data/config.yaml"),
        ]:
            if cfg_path.exists():
                cfg = yaml.safe_load(cfg_path.read_text()) or {}
                m = cfg.get("model", {})
                if isinstance(m, dict):
                    provider = m.get("provider", "deepseek")
                    model = m.get("default", "deepseek-v4-pro")
                    return model, provider
    except Exception:
        pass
    # Fallback: DeepSeek e' il provider configurato per NewTeogomme
    return "deepseek-v4-pro", "deepseek"


def analyze_call_with_llm(transcript: str, call_type: str = "inbound") -> dict:
    """Analisi semantica della chiamata tramite LLM.
    call_type: 'inbound' (prima chiamata) o 'callback' (richiamata).

    Ritorna dict con escalate, reason, question, summary, sms_promised, sms_type, _llm_ok."""
    import urllib.request, urllib.error

    model, provider = get_default_model()
    deepseek_key = load_env_value("DEEPSEEK_API_KEY")
    anthropic_key = load_env_value("ANTHROPIC_API_KEY")

    system_prompt = (
        "Sei il supervisore di Giorgia, agente vocale AI di NewTeogomme (gommista). "
        "Analizza la trascrizione tra Giorgia [agent] e un cliente [user].\n\n"
        "REGOLE (escalate=true se ALMENO UNA e' vera):\n"
        "1. Il cliente chiede ESPLICITAMENTE di parlare con operatore/persona reale/titolare/responsabile.\n"
        "2. Giorgia NON ha saputo rispondere a una domanda del cliente (info aziendale mancante).\n"
        "3. Giorgia ha CONFERMATO un servizio che NewTeogomme NON fa. "
        "NewTeogomme FA SOLO: pneumatici, cambio gomme, equilibratura, convergenza, "
        "gonfiaggio azoto, cerchi (centratura/verniciatura/diamantatura). "
        "NewTeogomme NON fa: meccanica, carrozzeria, revisioni auto, aria condizionata, "
        "elettrauto, freni, ammortizzatori, tagliandi. "
        "Se il cliente chiede un servizio NON nella lista e Giorgia dice 'si lo facciamo' -> escalate=true.\n"
        "4. Il cliente e' rimasto INSODDISFATTO o senza una risposta concreta.\n"
        "5. Il cliente segnala SINISTRO, INCIDENTE o SOCCORSO STRADALE.\n\n"
        "NON e' escalation se:\n"
        "- Giorgia ha gia' dato tutte le info corrette e il cliente e' soddisfatto.\n"
        "- Giorgia dice \"verifico e richiamo\" DOPO aver gia' gestito la richiesta "
        "(es. appuntamento preso, verifica solo disponibilita'). E' procedura standard.\n"
        "- Il cliente chiede SOLO informazioni gia' nella KB (orari, indirizzo). richieste di APPUNTAMENTO (cambio gomme, convergenza) DEVONO attivare escalate=true.\n\n"
        "TRACCIAMENTO SMS (sms_promised=true):\n"
        "- Se Giorgia promette di inviare SMS/messaggio, imposta sms_promised=true.\n"
        "- Se il contenuto e' gia' stato dato a voce, puoi tenere escalate=false.\n\n"
        "Rispondi SOLO con JSON:\n"
        '{"escalate": true/false, "reason": "motivo", "question": "domanda cliente", '
        '"sms_promised": true/false, "sms_type": "cambio_stagionale" o vuoto, '
        '"summary": "riassunto", "nome_cliente": "nome o vuoto", "targa": "targa o vuoto"}'
    )

    # ── Branch DeepSeek (OpenAI-compatibile) ──
    if provider == "deepseek" and deepseek_key:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Trascrizione:\n\n{transcript}\n\nRestituisci JSON."}
            ],
            "temperature": 0.1,
            "max_tokens": 1000,
            "response_format": {"type": "json_object"}
        }
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {deepseek_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            text_out = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            result = json.loads(text_out.strip())
            result["_llm_ok"] = True
            result.setdefault("escalate", False)
            result.setdefault("reason", "")
            result.setdefault("question", "")
            result.setdefault("summary", "")
            return result
        except Exception as e:
            print(f"  DeepSeek LLM error ({type(e).__name__}): {e} - uso fallback frasi-spia")
            return {"_llm_ok": False}

    # ── Branch Anthropic (backup) ──
    if provider == "anthropic" and anthropic_key:
        payload = {
            "model": model,
            "max_tokens": 500,
            "system": system_prompt,
            "messages": [{"role": "user", "content": f"Trascrizione:\n\n{transcript}"}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "x-api-key": anthropic_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            content = data.get("content", [])
            text_out = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            m = re.search(r"\{.*\}", text_out, re.S)
            if not m:
                return {"_llm_ok": False}
            result = json.loads(m.group(0))
            result["_llm_ok"] = True
            result.setdefault("escalate", False)
            result.setdefault("reason", "")
            result.setdefault("question", "")
            result.setdefault("summary", "")
            return result
        except Exception as e:
            print(f"  Anthropic LLM error ({type(e).__name__}): {e} - uso fallback frasi-spia")
            return {"_llm_ok": False}

    # ── Nessun LLM disponibile ──
    return {"_llm_ok": False}



def api_get(path: str) -> dict:
    """Chiamata GET alle API ElevenLabs."""
    import urllib.request, urllib.error
    url = f"https://api.elevenlabs.io/v1/{path}"
    req = urllib.request.Request(url, headers={"xi-api-key": load_api_key()})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:1000]
        print(f"  API ERROR {e.code}: {body}")
        return {"error": e.code, "body": body}


def api_patch(path: str, data: dict) -> dict:
    """Chiamata PATCH alle API ElevenLabs."""
    import urllib.request, urllib.error
    url = f"https://api.elevenlabs.io/v1/{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": load_api_key(),
        "Content-Type": "application/json"
    }, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:1000]
        print(f"  API ERROR {e.code}: {body}")
        return {"error": e.code, "body": body}


def api_post(path: str, data: dict) -> dict:
    """Chiamata POST alle API ElevenLabs."""
    import urllib.request, urllib.error
    url = f"https://api.elevenlabs.io/v1/{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": load_api_key(),
        "Content-Type": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:1000]
        print(f"  API ERROR {e.code}: {body}")
        return {"error": e.code, "body": body}


def load_latest_prompt() -> dict:
    """Carica l'ultima versione del prompt predefinito di Giorgia."""
    if not LATEST_PROMPT_FILE.exists():
        return {"version": "v1", "prompt": "Sei un \"Assistente\"...", "llm": "gemini-3.5-flash"}
    version = LATEST_PROMPT_FILE.read_text().strip()
    prompt_file = PROMPT_DIR / f"{version}.json"
    if prompt_file.exists():
        return json.loads(prompt_file.read_text())
    return {"version": version, "prompt": ""}


def get_default_prompt() -> str:
    """Restituisce solo il testo del prompt predefinito corrente."""
    return load_latest_prompt().get("prompt", "")


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(path)  # scrittura atomica


# ============================================================
# FASE 1: RACCOLTA TRASCRIZIONI
# ============================================================

def fetch_conversations():
    """Recupera le conversazioni del numero NewTeogomme e salva quelle nuove.
    Usa phone_number_id per beccare TUTTE le chiamate, anche se gestite da agenti diversi."""
    print("=== FASE 1: RACCOLTA TRASCRIZIONI ===")

    processed = load_json(PROCESSED_FILE)
    processed_ids = set(processed.get("processed_conversation_ids", []))

    # Recupera conversazioni (le più recenti in cima)
    all_convs = []
    cursor = None
    page = 0
    empty_pages = 0  # pagine consecutive senza novità

    while True:
        page += 1
        path = f"convai/conversations?phone_number_id={GIORGIA_PHONE_ID}&page_size=20"
        if cursor:
            path += f"&cursor={cursor}"

        resp = api_get(path)
        if "error" in resp:
            print(f"  Errore API: {resp}")
            break

        convs = resp.get("conversations", [])

        # Conta quante sono già processate in questa pagina
        known_in_page = sum(1 for c in convs if c["conversation_id"] in processed_ids)
        all_convs.extend(convs)
        print(f"  Pagina {page}: {len(convs)} conversazioni (nuove: {len(convs) - known_in_page})")

        # Stop intelligente: se 5 pagine consecutive senza nuove, fermati
        if known_in_page == len(convs) and len(convs) > 0:
            empty_pages += 1
            if empty_pages >= 5:
                print(f"  Stop: {empty_pages} pagine senza nuove conversazioni")
                break
        else:
            empty_pages = 0

        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    new_count = 0
    for conv in all_convs:
        conv_id = conv["conversation_id"]
        if conv_id in processed_ids:
            continue

        # FILTRO CRITICO: l'API ElevenLabs restituisce conversazioni di ALTRI agenti
        # che condividono lo stesso SIP trunk. Dobbiamo processare SOLO le chiamate
        # di Giorgia, non quelle di KilometroZero o altri agenti sullo stesso trunk.
        conv_agent_id = conv.get("agent_id", "")
        if conv_agent_id != GIORGIA_AGENT_ID:
            # Marca come processata per non ri-fetcharla ogni ciclo
            processed_ids.add(conv_id)
            continue

        # Recupera transcript completo
        detail = api_get(f"convai/conversations/{conv_id}")
        if "error" in detail:
            continue

        transcript = detail.get("transcript", [])
        # Estrai numero chiamante dal metadata
        metadata = detail.get("metadata", {})
        phone_call = metadata.get("phone_call", {})
        caller_number = phone_call.get("external_number", "") or phone_call.get("caller_number", "")
        caller_name = ""

        # Fallback: cerca nel transcript
        if not caller_number:
            for msg in transcript:
                if msg.get("role") == "user":
                    caller_number = msg.get("phone_number", "") or msg.get("caller_phone_number", "")
                    if caller_number:
                        break

        # Costruisci testo completo
        full_text = ""
        for msg in transcript:
            role = msg.get("role", "")
            content = msg.get("message", "") or msg.get("content", "")
            full_text += f"[{role}] {content}\n"

        # IMPORTANTE: non acquisire chiamate ancora in corso o senza contenuto.
        # Se le marcassimo come processate ora, quando si completano (con il
        # transcript reale) verrebbero saltate per sempre -> chiamata persa.
        # Le lasciamo NON processate: verranno catturate in un giro successivo.
        conv_status = (conv.get("status") or "").lower()
        if conv_status not in ("done", "completed", "ended", "processed", "failed"):
            print(f"  IN CORSO ({conv_status or 'sconosciuto'}): {conv_id} - rimando al prossimo giro")
            continue
        if not full_text.strip():
            print(f"  VUOTA: {conv_id} - transcript ancora vuoto, rimando al prossimo giro")
            continue

        # Validazione: rifiuta numeri mascherati

        if '*' in caller_number:
            print(f"  ATTENZIONE: numero mascherato per {conv_id}, lo prendo dall'API")
            caller_number = phone_call.get("external_number", "")
        if not caller_number or '*' in caller_number:
            caller_number = "(ANONIMO)"
        # === BLACKLIST CHECK: salta numeri bloccati ===
        if _is_blocked_number(caller_number):
            print(f"  SALTATO (numero bloccato): {conv_id} | {caller_number}")
            continue
        # (filtro per phone_number_id di Giorgia o per tipo INBOUND)
        direction = phone_call.get("direction", "")

        call_type = "inbound"
        if direction == "outbound" or direction == "outgoing":
            call_type = "callback"

        # Salva la trascrizione (scrittura atomica: prima tmp, poi rinomina)
        conv_dir = DATA_DIR / "transcripts"
        conv_dir.mkdir(exist_ok=True)
        transcript_file = conv_dir / f"{conv_id}.txt"
        tmp_file = conv_dir / f"{conv_id}.tmp"
        tmp_file.write_text(full_text)
        tmp_file.rename(transcript_file)  # atomico sullo stesso filesystem

        # Registra come processato
        processed_ids.add(conv_id)
        processed.setdefault("calls", []).append({
            "conversation_id": conv_id,
            "start_time": conv.get("start_time_unix_secs"),
            "duration": conv.get("call_duration_secs"),
            "caller_number": caller_number,
            "caller_name": caller_name,
            "status": conv.get("status"),
            "transcript_file": str(transcript_file),
            "call_type": call_type,
            "processed": False,
        })
        new_count += 1
        print(f"  NUOVA: {conv_id} | {caller_number} | {len(transcript)} messaggi")

    processed["processed_conversation_ids"] = list(processed_ids)
    processed["last_processed_at"] = datetime.now(timezone.utc).isoformat()
    processed["total_processed"] = len(processed_ids)
    save_json(PROCESSED_FILE, processed)

    print(f"  Nuove trascrizioni: {new_count}")
    return new_count


# ============================================================
# FASE 2: ANALISI
# ============================================================

def _analyze_fallback_phrases(text: str) -> dict:
    """Fallback (solo se l'LLM non e' disponibile): rilevamento a frasi-spia.
    Ritorna {"escalate": bool, "question": str, "reason": str}."""
    
    def _norm(s: str) -> str:
        """Normalizza testo: rimuove filler, punteggiatura, spazi multipli."""
        s = re.sub(r'\b(eh|uhm|ehm|eee+|mah|cioè|ah|ok|okay)\b', '', s.lower())
        s = re.sub(r'[,;:!?.]', '', s)
        return re.sub(r'\s+', ' ', s).strip()
    
    tl = _norm(text)
    found_fallback = any(_norm(p) in tl for p in FALLBACK_PHRASES)
    confusion_count = sum(_norm(p) in tl for p in CONFUSION_PHRASES)
    found_confusion = confusion_count > 0
    found_fake = any(_norm(p) in tl for p in FAKE_ACTION_PHRASES)
    found_operator = any(_norm(p) in tl for p in OPERATOR_REQUEST_PHRASES)
    found_appointment = any(_norm(p) in tl for p in APPOINTMENT_PHRASES)

    if not (found_fallback or found_confusion or found_fake or found_operator):
        # Se trovato solo appuntamento senza altri problemi, NON escalare
        if found_appointment:
            return {"escalate": False, "question": "", "reason": "appuntamento_normale",
                    "_register_appointment": True}
        return {"escalate": False, "question": "", "reason": "nessun problema rilevato (frasi-spia)"}

    reason = []
    if found_fallback:
        reason.append("fallback")
    if found_confusion:
        reason.append(f"confusione({confusion_count})")
    if found_fake:
        reason.append("azioni_inventate")
    if found_operator:
        reason.append("cliente_chiede_operatore")
    if found_appointment:
        reason.append("appuntamento")

    # Estrai domanda del cliente — prendi il PRIMO messaggio [user] sostanzioso
    # che precede immediatamente un fallback dell'agente. Ignora risposte brevi
    # (<4 parole) che sono solo convenevoli ("va bene", "grazie", "ok").
    # Blocca al primo match per non catturare follow-up pieni di filler.
    lines = text.split("\n")
    question = ""
    substantive_user_line = ""
    for i, line in enumerate(lines):
        if "[user]" in line.lower() or "[human]" in line.lower():
            content = line.split("]", 1)[1].strip() if "]" in line else line.strip()
            if len(content.split()) >= 4 or "?" in content:
                substantive_user_line = content
        if "[agent]" in line.lower() and substantive_user_line:
            remaining = "\n".join(lines[i:])
            if any(_norm(p) in _norm(remaining) for p in FALLBACK_PHRASES):
                question = substantive_user_line  # PRIMO messaggio sostanzioso prima del fallback
                break  # FERMO al primo match: i follow-up successivi sono pieni di filler
    if not question:
        # Fallback: se found_operator, prendi la prima frase utente con richiesta operatore
        if found_operator:
            for line in lines:
                if "[user]" in line.lower():
                    content = line.split("]", 1)[1].strip() if "]" in line else line.strip()
                    if any(_norm(p) in _norm(content) for p in OPERATOR_REQUEST_PHRASES):
                        question = content
                        break
        # Fallback: se found_appointment, prendi la prima frase utente con appuntamento
        if not question and found_appointment:
            for line in lines:
                if "[user]" in line.lower():
                    content = line.split("]", 1)[1].strip() if "]" in line else line.strip()
                    # Salta conferme ("appuntamento confermato", "ok", "va bene", "perfetto")
                    nc = _norm(content)
                    if any(w in nc for w in ["confermato", "va bene", "perfetto grazie", "ok grazie"]):
                        continue
                    if any(_norm(p) in _norm(content) for p in APPOINTMENT_PHRASES):
                        question = content
                        break
        # Fallback: se found_fake, prendi l'ultimo messaggio utente sostanzioso
        if not question and found_fake:
            for line in reversed(lines):
                if "[user]" in line.lower():
                    content = line.split("]", 1)[1].strip() if "]" in line else line.strip()
                    if len(content.split()) >= 4 or "?" in content:
                        question = content
                        break
        # Fallback: se found_confusion, prendi l'ultimo messaggio utente sostanzioso
        if not question and found_confusion:
            for line in reversed(lines):
                if "[user]" in line.lower():
                    content = line.split("]", 1)[1].strip() if "]" in line else line.strip()
                    if len(content.split()) >= 4 or "?" in content:
                        question = content
                        break
    if not question:
        question = "(domanda non individuata automaticamente)"

    return {"escalate": True, "question": question, "reason": ",".join(reason)}


def analyze_transcripts():
    """Analizza le trascrizioni nuove tramite LLM (analisi semantica) e individua
    le chiamate che richiedono escalation. Fallback alle frasi-spia se l'LLM e' giu'."""
    print("=== FASE 2: ANALISI (LLM) ===")

    processed = load_json(PROCESSED_FILE)
    calls = processed.get("calls", [])

    unanswered = []
    for call in calls:
        if call.get("analyzed"):
            continue

        conv_id = call["conversation_id"]
        transcript_file = Path(call["transcript_file"])
        if not transcript_file.exists():
            continue

        text = transcript_file.read_text()
        call_type = call.get("call_type", "inbound")

        # 0) kb_filter: intercetta domande comuni PRIMA del LLM (0 token)
        from kb_filter import check_kb
        kb_result = check_kb(text)

        if kb_result["action"] == "force_escalate":
            call["analyzed"] = True
            call["analysis_method"] = "kb_filter"
            call["analysis_reason"] = f"KB OVERRIDE: {kb_result['trigger']}"
            call["outcome"] = "domanda_aperta"
            call["question"] = kb_result.get("trigger", "")
            call["escalated"] = True
            continue

        if kb_result["action"] == "auto_reply":
            call["analyzed"] = True
            call["analysis_method"] = "kb_filter"
            call["analysis_reason"] = f"KB AUTO-REPLY: {kb_result['trigger']}"
            call["outcome"] = "risposta_ok"
            call["question"] = kb_result.get("trigger", "")
            call["auto_reply_text"] = kb_result.get("text", "")
            continue

        # 1) Analisi semantica via LLM (metodo primario)
        verdict = analyze_call_with_llm(text, call_type=call_type)
        if verdict.get("_llm_ok"):
            method = "LLM"
        else:
            # 2) Fallback: analyze_transcript (frasi-spia con priorita' corretta)
            result = analyze_transcript(text)
            method = "analyze_transcript(fallback)"
            # Mappa formato analyze_transcript → formato verdict
            escalate = result["outcome"] != "risposta_ok"
            verdict = {
                "escalate": escalate,
                "question": result["question"],
                "reason": result["reason"],
            }
            if result["outcome"] == "cliente_chiede_operatore":
                verdict["operator_request"] = True
            if result["outcome"] == "fake_action":
                verdict["fake_action"] = True

        # Salva sempre il riassunto/valutazione per avere un quadro completo
        call["analyzed"] = True
        call["analysis_method"] = method
        if verdict.get("summary"):
            call["summary"] = verdict["summary"]
        call["analysis_reason"] = verdict.get("reason", "")
        if verdict.get("targa"):
            call["targa"] = verdict["targa"]
        if verdict.get("nome_cliente"):
            call["nome_cliente"] = verdict["nome_cliente"]

        # Rileva promessa SMS (anche se escalate=false, l'SMS va inviato)
        sms_promised = verdict.get("sms_promised", False)
        sms_type = verdict.get("sms_type", "")
        if sms_promised:
            call["sms_promised"] = True
            call["sms_type"] = sms_type
            print(f"  SMS PROMESSO: {conv_id} | tipo: {sms_type}")


        # NUOVO (20/06/2026): registra appuntamento se rilevato
        _try_register_appointment(verdict, call, method)

        if not verdict.get("escalate"):
            # Se non serve escalation ma SMS e' promesso, segna comunque per invio SMS
            if sms_promised:
                call["outcome"] = "sms_da_inviare"
                call["question"] = verdict.get("question") or "Richiesta SMS"
                unanswered.append(call)
            else:
                call["outcome"] = "completata"
            continue

        question = verdict.get("question") or "(domanda non individuata)"
        call["outcome"] = "domanda_aperta"
        call["question"] = question

        # Per le callback: linka al ticket originale per tracciare la catena
        # e eredita la domanda dal ticket padre se non individuata
        if call_type == "callback":
            pending = load_json(PENDING_FILE)
            for req in pending.get("requests", []):
                if req.get("callback_conv_id") == conv_id:
                    call["ticket_chain_id"] = req.get("ticket_chain_id", req["id"])
                    call["ticket_chain_depth"] = req.get("ticket_chain_depth", 1)
                    # Eredita la domanda dal padre se la callback non ha prodotto una nuova
                    if "non individuata" in question.lower() and req.get("question"):
                        question = req["question"]
                        call["question"] = question
                    break
            if not call.get("ticket_chain_id"):
                call["ticket_chain_id"] = f"chain_{conv_id}"

        unanswered.append(call)
        print(f"  DOMANDA APERTA [{method}]: {conv_id} | {call.get('caller_number','?')} | "
              f"\"{question[:90]}\" | motivo: {verdict.get('reason','')[:60]}")

    save_json(PROCESSED_FILE, processed)
    print(f"  Domande aperte trovate: {len(unanswered)}")
    return unanswered


# ============================================================
# REGISTRAZIONE APPUNTAMENTI (integrato 20/06/2026, mentoring Laura)
# ============================================================

def _try_register_appointment(verdict, call, method):
    """Registra un appuntamento se il verdict indica che ne e' stato fissato uno."""
    reason = verdict.get("reason", "")
    if "appuntamento" not in reason:
        return

    caller = call.get("caller_number", "")
    conv_id = call.get("conversation_id", "")
    nome = verdict.get("nome_cliente", "") or call.get("nome_cliente", "") or ""
    targa = verdict.get("targa", "") or call.get("targa", "") or ""

    dett = verdict.get("dettagli_appuntamento", {})
    data_app = dett.get("data", "")
    ora_app = dett.get("ora", "")
    servizio = dett.get("servizio", "non specificato")

    if not data_app or not ora_app:
        question = verdict.get("question", "")
        data_app = data_app or _extract_date_from_text(question)
        ora_app = ora_app or _extract_time_from_text(question)

    if not data_app or not ora_app:
        print(f"  APPUNTAMENTO rilevato ma data/ora non estraibili: {conv_id}")
        call["appuntamento_parziale"] = True
        return

    try:
        result = appt_mgr.aggiungi_appuntamento(
            data_app=data_app,
            ora_app=ora_app,
            servizio=servizio,
            nome=nome or "Cliente",
            cognome="",
            telefono=caller,
            targa=targa,
            note=f"Da chiamata {conv_id} [{method}]",
            conv_id=conv_id,
        )
        appt_id = result.get("id", "?")
        print(f"  APPUNTAMENTO REGISTRATO: {appt_id} | {data_app} {ora_app} | "
              f"{servizio} | {nome or caller}")
        call["appointment_registered"] = True
        call["appointment_id"] = appt_id
    except ValueError as e:
        print(f"  APPUNTAMENTO NON REGISTRATO (slot occupato): {e}")
        call["appointment_error"] = str(e)
    except Exception as e:
        print(f"  APPUNTAMENTO ERRORE: {e}")


def _extract_date_from_text(text):
    """Estrae data YYYY-MM-DD da testo."""
    import re, datetime
    mesi = {"gennaio":1,"febbraio":2,"marzo":3,"aprile":4,"maggio":5,"giugno":6,
            "luglio":7,"agosto":8,"settembre":9,"ottobre":10,"novembre":11,"dicembre":12}
    oggi = datetime.date.today()
    anno = oggi.year
    m = re.search(r"(\d{1,2})\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s*(\d{4})?", text.lower())
    if m:
        try:
            return datetime.date(int(m.group(3)) if m.group(3) else anno, mesi[m.group(2)], int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if m:
        try:
            return datetime.date(int(m.group(3)) if m.group(3) else anno, int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _extract_time_from_text(text):
    """Estrae orario HH:MM da testo."""
    import re
    m = re.search(r"(\d{1,2})[:\.](\d{2})", text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"alle\s+(\d{1,2})\s*e\s*mezz[ao]", text.lower())
    if m:
        return f"{int(m.group(1)):02d}:30"
    m = re.search(r"alle\s+(\d{1,2})", text.lower())
    if m:
        return f"{int(m.group(1)):02d}:00"
    return ""



def _humanize_reason(reason: str) -> str:
    """Converte il codice motivo in una frase unica leggibile, stile Laura.
    Es: 'fallback,appuntamento' → 'Il Cliente Ha Richiesto Un Appuntamento E Giorgia Ha Promesso Di Richiamarlo'"""
    parts = [p.strip() for p in reason.split(",") if p.strip()]
    
    # Priorità: operatore > appuntamento > fallback/altro
    if "cliente_chiede_operatore" in parts:
        return "Il Cliente Ha Chiesto Di Parlare Con Un Operatore"
    if "appuntamento" in parts and "fallback" in parts:
        return "Il Cliente Ha Richiesto Un Appuntamento E Giorgia Ha Promesso Di Richiamarlo"
    if "appuntamento" in parts:
        return "Il Cliente Ha Richiesto Un Appuntamento"
    if "azioni_inventate" in parts:
        return "Giorgia Ha Dato Informazioni Non Verificate E Ha Promesso Di Richiamare Il Cliente"
    if "confusione" in reason:
        return "Giorgia Non Ha Compreso La Domanda E Ha Promesso Di Richiamare Il Cliente"
    if "fallback" in parts or not parts:
        return "Giorgia Ha Promesso Di Verificare E Richiamare Il Cliente"
    return "Giorgia Non Ha Saputo Rispondere Alla Richiesta"


def _build_email_html(question, caller, conv_id, start_time, reason, is_operator_request=False, targa="", nome_cliente=""):
    """Costruisce il corpo HTML dell'email di escalation.
    Ritorna un dict {'plain': str, 'html': str}."""
    dt = datetime.fromtimestamp(start_time or 0, tz=timezone.utc).strftime("%d/%m/%Y %H:%M")
    reason_clean = reason.replace(",", ", ").replace("_", " ").title() if reason else "N/D"

    # Colori in base al tipo
    if is_operator_request:
        accent = "#DC2626"      # rosso urgente
        badge_text = "RICHIESTA OPERATORE"
        icon = "&#x1F514;"      # campanella
        title = "Cliente chiede di parlare con un operatore"
        subtitle = "Il cliente non vuole parlare con l'agente automatico e richiede una persona reale."
    else:
        accent = "#EA580C"      # arancione
        badge_text = "DOMANDA SENZA RISPOSTA"
        icon = "&#x2753;"      # punto interrogativo
        title = "Domanda cliente senza risposta"
        subtitle = "Giorgia non ha saputo rispondere a questa richiesta del cliente."

    reason_human = _humanize_reason(reason)

    # --- Plain text (stile Laura) ---
    plain_parts = [
        f"{title}",
        f"{subtitle}",
        "",
        f"💬 DOMANDA / RICHIESTA",
        f"{question}",
        f"📞 NUMERO CLIENTE",
        f"{caller}",
        f"🕒 DATA / ORA",
        f"{dt}",
    ]
    if targa:
        plain_parts.append(f"🚗 TARGA VEICOLO")
        plain_parts.append(f"{targa}")
    if nome_cliente:
        plain_parts.append(f"👤 NOME E COGNOME")
        plain_parts.append(f"{nome_cliente}")
    plain_parts += [
        f"📋 ID CONVERSAZIONE",
        f"{conv_id}",
        f"🔍 MOTIVO RILEVATO",
        f"{reason_human}",
        "",
        "Grazie,",
        "Erminio (supervisore di Giorgia)",
    ]
    plain = "\n".join(plain_parts)

    # --- HTML ---
    targa_nome_html = ""
    if targa or nome_cliente:
        targa_nome_html = f'''        <tr>
          <td style="padding:14px 18px;border-bottom:1px solid #e4e4e7;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="50%" style="vertical-align:top;">
                  <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">
                    &#x1F697; Targa Veicolo
                  </div>
                  <div style="font-size:15px;color:#18181b;font-weight:600;">
                    {targa}
                  </div>
                </td>
                <td width="50%" style="vertical-align:top;">
                  <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">
                    &#x1F464; Nome e Cognome
                  </div>
                  <div style="font-size:15px;color:#18181b;font-weight:600;">
                    {nome_cliente}
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>
'''

    cta_text = (
        '<p style="font-size:14px;color:#18181b;font-weight:600;margin:0 0 8px 0;">'
        '&#x26A0;&#xFE0F; Il cliente va ricontattato da una persona reale (Teo o operatore).'
        '</p>'
        if is_operator_request else
        '<p style="font-size:14px;color:#52525b;margin:0 0 8px 0;">'
        "Puoi rispondere a questa email con l'informazione mancante."
        '</p>'
    )

    now_str = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y alle %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:20px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">

  <!-- HEADER -->
  <tr>
    <td style="background:#1a1a2e;padding:28px 32px;text-align:center;">
      <div style="font-size:26px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">
        &#x1F697; NEWTEOGOMME
      </div>
      <div style="font-size:13px;color:#94a3b8;margin-top:4px;">Supervisione Agente Giorgia</div>
    </td>
  </tr>

  <!-- BADGE + TITLE -->
  <tr>
    <td style="padding:32px 32px 16px 32px;text-align:center;">
      <span style="display:inline-block;background:{accent};color:#fff;font-size:11px;font-weight:700;padding:4px 14px;border-radius:20px;letter-spacing:1px;text-transform:uppercase;">
        {icon} {badge_text}
      </span>
      <h1 style="font-size:20px;color:#18181b;margin:16px 0 6px 0;font-weight:700;line-height:1.3;">
        {title}
      </h1>
      <p style="font-size:14px;color:#71717a;margin:0;line-height:1.5;">
        {subtitle}
      </p>
    </td>
  </tr>

  <!-- DIVIDER -->
  <tr>
    <td style="padding:0 32px;">
      <div style="border-top:1px solid #e4e4e7;"></div>
    </td>
  </tr>

  <!-- DATA CARD -->
  <tr>
    <td style="padding:24px 32px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#fafafa;border-radius:8px;border:1px solid #e4e4e7;">

        <!-- Domanda -->
        <tr>
          <td style="padding:14px 18px;border-bottom:1px solid #e4e4e7;">
            <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">
              &#x1F4AC; Domanda / Richiesta
            </div>
            <div style="font-size:15px;color:#18181b;line-height:1.5;font-weight:500;">
              {question}
            </div>
          </td>
        </tr>

        <!-- Numero cliente -->
        <tr>
          <td style="padding:14px 18px;border-bottom:1px solid #e4e4e7;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="50%" style="vertical-align:top;">
                  <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">
                    &#x1F4DE; Numero Cliente
                  </div>
                  <div style="font-size:15px;color:#18181b;font-weight:600;">
                    {caller}
                  </div>
                </td>
                <td width="50%" style="vertical-align:top;">
                  <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">
                    &#x1F552; Data / Ora
                  </div>
                  <div style="font-size:15px;color:#18181b;">
                    {dt}
                  </div>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        {targa_nome_html}
        <!-- ID conversazione -->
        <tr>
          <td style="padding:14px 18px;border-bottom:1px solid #e4e4e7;">
            <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">
              &#x1F4CB; ID Conversazione
            </div>
            <div style="font-size:13px;color:#52525b;font-family:'SF Mono',Monaco,'Cascadia Code',monospace;word-break:break-all;">
              {conv_id}
            </div>
          </td>
        </tr>

        <!-- Motivo -->
        <tr>
          <td style="padding:14px 18px;">
            <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px;">
              &#x1F50D; Motivo Rilevato
            </div>
            <div style="font-size:14px;color:#52525b;line-height:1.4;">
              {reason_human}
            </div>
          </td>
        </tr>

      </table>
    </td>
  </tr>

  <!-- CTA -->
  <tr>
    <td style="padding:0 32px 24px 32px;text-align:center;">
      {cta_text}
      <a href="tel:{caller}" style="display:inline-block;background:{accent};color:#fff;font-size:14px;font-weight:700;padding:12px 28px;border-radius:8px;text-decoration:none;margin-top:8px;">
        &#x1F4DE; Chiama Cliente
      </a>
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td style="background:#fafafa;padding:16px 32px;text-align:center;border-top:1px solid #e4e4e7;">
      <div style="font-size:12px;color:#a1a1aa;">
        Erminio &middot; Supervisore automatico di Giorgia &middot; NewTeogomme
      </div>
      <div style="font-size:11px;color:#a1a1aa;margin-top:2px;">
        Email inviata il {now_str}
      </div>
      <div style="font-size:10px;color:#d4d4d8;margin-top:2px;">
        Questa email &egrave; stata generata automaticamente dall'orchestrator.
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    return {"plain": plain, "html": html}


# ============================================================
# FASE 3B: ESCALATION A PAOLO (Himalaya)
# ============================================================

def escalate_to_paolo():
    """Invia email a Paolo per le domande senza risposta usando Himalaya CLI."""
    print("=== FASE 3B: ESCALATION A PAOLO ===")

    processed = load_json(PROCESSED_FILE)
    pending = load_json(PENDING_FILE)
    existing_questions = {r["question"] for r in pending.get("requests", [])}

    sent = 0
    for call in processed.get("calls", []):
        if call.get("outcome") != "domanda_aperta":
            continue
        if call.get("escalated"):
            continue

        question = call.get("question", "")
        caller = call.get("caller_number", "N/A")
        conv_id = call["conversation_id"]

        if question in existing_questions:
            call["escalated"] = True
            call["escalation_note"] = "duplicate"
            continue

        subject = f"Domanda cliente senza risposta – {question[:60]}"
        body = (
            f"Buongiorno Paolo,\n\n"
            f"Il cliente {caller} ha chiamato e Giorgia non ha saputo rispondere.\n\n"
            f"Domanda del cliente:\n{question}\n\n"
            f"ID chiamata: {conv_id}\n"
            f"Motivo: {call.get('analysis_reason', 'N/A')}\n\n"
            f"Rispondi a questa email con la risposta da dare al cliente.\n"
        )

        try:
            result = subprocess.run(
                [HIMALAYA_BIN, "template", "send"],
                input=f"From: {FROM_EMAIL}\nTo: {PAOLO_EMAIL}\nSubject: {subject}\n\n{body}",
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PATH": f"{os.environ.get('PATH','')}:/opt/data/home/.local/bin"}
            )
            if result.returncode == 0:
                print(f"  EMAIL INVIATA: {subject}")
                sent += 1
            else:
                print(f"  ERRORE INVIO: {result.stderr[:200]}")
                continue
        except Exception as e:
            print(f"  ERRORE: {e}")
            continue

        call["escalated"] = True

        pending.setdefault("requests", []).append({
            "id": f"req_{conv_id}",
            "question": question,
            "caller_number": caller,
            "conversation_id": conv_id,
            "status": "inviata_a_paolo",
            "email_subject": subject,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "answer": None,
            "kb_doc_id": None,
            "callback_done": False,
        })
        existing_questions.add(question)

    save_json(PROCESSED_FILE, processed)
    save_json(PENDING_FILE, pending)
    print(f"  Email inviate: {sent}")
    return sent


# ============================================================
# FASE 3: ESCALATION A TEO
# ============================================================

def escalate_to_teo():
    """Invia email a Teo per le domande senza risposta."""
    print("=== FASE 3: ESCALATION A TEO ===")

    processed = load_json(PROCESSED_FILE)
    pending = load_json(PENDING_FILE)
    existing_questions = {r["question"] for r in pending.get("requests", [])}

    sent = 0
    for call in processed.get("calls", []):
        if call.get("outcome") != "domanda_aperta":
            continue
        if call.get("escalated"):
            continue

        question = call.get("question", "")
        caller = call.get("caller_number", "N/A")
        conv_id = call["conversation_id"]

        # Evita duplicati (stessa domanda da stesso numero entro 30 min)
        if question in existing_questions:
            # Notifica Pablo di un duplicato (senza rispedire a Teo)
            dup_subject = "Nuova chiamata stessa richiesta"
            dup_dt = datetime.fromtimestamp(call.get("start_time", 0) or 0, tz=timezone.utc).strftime("%d/%m/%Y %H:%M")
            
            dup_plain = f"""DUPLICATO - Nuova chiamata con stessa richiesta

DOMANDA: {question}
NUMERO: {caller}
CONVERSAZIONE: {conv_id}
DATA/ORA: {dup_dt}

Questa email e' solo per notifica. L'escalation originale a Teo e' gia' stata inviata.

-- Erminio (automatico)"""
            
            dup_html = f"""<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#f4f4f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f4f5;padding:20px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">
<tr>
  <td style="background:#1a1a2e;padding:28px 32px;text-align:center;">
    <div style="font-size:26px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">&#x1F697; NEWTEOGOMME</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:4px;">Supervisione Agente Giorgia</div>
  </td>
</tr>
<tr>
  <td style="padding:32px 32px 16px 32px;text-align:center;">
    <span style="display:inline-block;background:#F59E0B;color:#fff;font-size:11px;font-weight:700;padding:4px 14px;border-radius:20px;letter-spacing:1px;text-transform:uppercase;">&#x1F4E2; DUPLICATO</span>
    <h1 style="font-size:18px;color:#18181b;margin:16px 0 6px 0;font-weight:700;">Nuova chiamata con stessa richiesta</h1>
    <p style="font-size:14px;color:#71717a;margin:0;">Un'altra chiamata e' arrivata mentre la richiesta e' gia' in attesa di risposta da Teo.</p>
  </td>
</tr>
<tr><td style="padding:0 32px;"><div style="border-top:1px solid #e4e4e7;"></div></td></tr>
<tr>
  <td style="padding:24px 32px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#fafafa;border-radius:8px;border:1px solid #e4e4e7;">
      <tr><td style="padding:14px 18px;border-bottom:1px solid #e4e4e7;">
        <div style="font-size:11px;font-weight:700;color:#a1a1aa;text-transform:uppercase;margin-bottom:4px;">&#x1F4AC; Domanda</div>
        <div style="font-size:15px;color:#18181b;font-weight:500;">{question}</div>
      </td></tr>
      <tr><td style="padding:14px 18px;border-bottom:1px solid #e4e4e7;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="50%"><div style="font-size:11px;font-weight:700;color:#a1a1aa;">&#x1F4DE; Numero</div><div style="font-size:15px;color:#18181b;font-weight:600;">{caller}</div></td>
            <td width="50%"><div style="font-size:11px;font-weight:700;color:#a1a1aa;">&#x1F552; Ora</div><div style="font-size:15px;color:#18181b;">{dup_dt}</div></td>
          </tr>
        </table>
      </td></tr>
      <tr><td style="padding:14px 18px;">
        <div style="font-size:11px;font-weight:700;color:#a1a1aa;">&#x1F4CB; ID</div>
        <div style="font-size:13px;color:#52525b;font-family:monospace;">{conv_id}</div>
      </td></tr>
    </table>
  </td>
</tr>
<tr>
  <td style="background:#fafafa;padding:16px 32px;text-align:center;border-top:1px solid #e4e4e7;">
    <div style="font-size:12px;color:#a1a1aa;">Erminio &middot; Supervisore Giorgia &middot; NewTeogomme</div>
    <div style="font-size:10px;color:#d4d4d8;margin-top:2px;">L'escalation originale a Teo e' gia' stata inviata. Questa e' solo una notifica.</div>
  </td>
</tr>
</table></td></tr></table></body></html>"""
            
            if send_email_smtp(dup_subject, dup_plain, dup_html):
                print(f"  NOTIFICA DUPLICATO: {conv_id[:30]}")
            call["escalated"] = True
            call["escalation_note"] = "duplicate_notified"
            continue

        reason = call.get("analysis_reason", "")
        is_operator_request = "cliente_chiede_operatore" in reason

        # Subject pulito stile Laura: solo il titolo, niente domanda
        if is_operator_request:
            subject = "Cliente chiede di parlare con un operatore"
        else:
            subject = "Domanda cliente senza risposta"

        # Costruisci corpo HTML con template grafico
        email_parts = _build_email_html(
            question=question,
            caller=caller,
            conv_id=conv_id,
            start_time=call.get("start_time", 0),
            reason=reason,
            is_operator_request=is_operator_request,
            targa=call.get("targa", ""),
            nome_cliente=call.get("nome_cliente", ""),
        )

        # Invia email tramite SMTP Gmail (multipart plain+HTML)
        if send_email_smtp(subject, email_parts["plain"], email_parts["html"]):
            print(f"  EMAIL INVIATA: {subject}")
            sent += 1
        else:
            continue

        call["escalated"] = True

        # Eredita ticket_chain per tracciare escalation multiple
        ticket_chain_id = call.get("ticket_chain_id", f"chain_{conv_id}")
        ticket_chain_depth = call.get("ticket_chain_depth", 0) + 1

        # Registra in pending
        pending.setdefault("requests", []).append({
            "id": f"req_{conv_id}",
            "question": question,
            "caller_number": caller,
            "conversation_id": conv_id,
            "status": "inviata_a_teo",
            "email_subject": subject,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "answer": None,
            "kb_doc_id": None,
            "callback_done": False,
            "ticket_chain_id": ticket_chain_id,
            "ticket_chain_depth": ticket_chain_depth,
        })
        existing_questions.add(question)

    save_json(PROCESSED_FILE, processed)
    save_json(PENDING_FILE, pending)
    print(f"  Email inviate: {sent}")
    return sent


# ============================================================
# FASE 4B: RICEZIONE RISPOSTE PAOLO (Himalaya)
# ============================================================

def check_paolo_responses():
    """Controlla inbox per risposte di Paolo e matcha con pending."""
    print("=== FASE 4B: RICEZIONE RISPOSTE PAOLO ===")

    pending = load_json(PENDING_FILE)
    reqs = pending.get("requests", [])

    # AUTO-CLOSE dopo 3 giorni
    auto_closed = 0
    now = datetime.now(timezone.utc)
    for req in reqs:
        if req.get("status") == "inviata_a_paolo":
            sent_at_str = req.get("sent_at", "")
            if sent_at_str:
                try:
                    sent_at = datetime.fromisoformat(sent_at_str)
                    if (now - sent_at).total_seconds() / 86400 >= 3:
                        req["status"] = "chiusa"
                        req["auto_closed"] = True
                        req["auto_closed_reason"] = "Chiusa dopo 3 giorni senza risposta"
                        req["closed_at"] = now.isoformat()
                        auto_closed += 1
                except (ValueError, TypeError):
                    pass

    if auto_closed:
        save_json(PENDING_FILE, pending)
        print(f"  Auto-close: {auto_closed}")

    awaiting = [r for r in reqs if r["status"] == "inviata_a_paolo"]
    if not awaiting:
        print("  Nessuna risposta in attesa")
        return 0

    # Cerca risposte di Paolo via Himalaya
    try:
        result = subprocess.run(
            [HIMALAYA_BIN, "envelope", "list", "--page-size", "20", f"from {PAOLO_EMAIL}"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": f"{os.environ.get('PATH','')}:/opt/data/home/.local/bin"}
        )
        if result.returncode != 0:
            print(f"  Errore Himalaya: {result.stderr[:200]}")
            return 0
        envelopes = json.loads(result.stdout)
    except Exception as e:
        print(f"  Errore: {e}")
        return 0

    matched = 0
    for env in envelopes:
        msg_id = str(env.get("id"))
        subject = env.get("subject", "")

        # Leggi corpo email
        msg = subprocess.run(
            [HIMALAYA_BIN, "message", "read", msg_id],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": f"{os.environ.get('PATH','')}:/opt/data/home/.local/bin"}
        )
        body = msg.stdout if msg.returncode == 0 else ""

        # Cerca match con domande pending
        for req in awaiting:
            if req["question"].lower() in body.lower() or req["question"][:30].lower() in subject.lower():
                req["status"] = "risposta_ricevuta"
                req["answer"] = body.strip()[:1000]
                req["received_at"] = now.isoformat()
                matched += 1
                print(f"  RISPOSTA TROVATA: {req['id'][:30]}... -> {subject[:60]}")
                break

    save_json(PENDING_FILE, pending)
    print(f"  Risposte ricevute: {matched}")
    return matched


# ============================================================
# FASE 4: RICEZIONE RISPOSTA DI TEO
# ============================================================

def check_teo_responses():
    """Controlla la casella email per risposte di Teo."""
    print("=== FASE 4: RICEZIONE RISPOSTE TEO ===")

    pending = load_json(PENDING_FILE)
    reqs = pending.get("requests", [])
    awaiting = [r for r in reqs if r["status"] == "inviata_a_teo"]

    # AUTO-CLOSE: richieste senza risposta da oltre 3 giorni
    auto_closed = 0
    now = datetime.now(timezone.utc)
    for req in reqs:
        if req.get("status") == "inviata_a_teo":
            sent_at_str = req.get("sent_at", "")
            if sent_at_str:
                try:
                    sent_at = datetime.fromisoformat(sent_at_str)
                    days_waiting = (now - sent_at).total_seconds() / 86400
                    if days_waiting >= 3:
                        req["status"] = "chiusa"
                        req["auto_closed"] = True
                        req["auto_closed_reason"] = (
                            f"Chiusa automaticamente dopo {days_waiting:.0f} giorni senza risposta di Teo"
                        )
                        req["closed_at"] = now.isoformat()
                        auto_closed += 1
                        print(f"  AUTO-CLOSE: {req['id'][:30]} (in attesa da {days_waiting:.0f}gg)")
                except (ValueError, TypeError):
                    pass

    if auto_closed:
        save_json(PENDING_FILE, pending)
        print(f"  Richieste auto-chiuse: {auto_closed}")

    # Ricalcola awaiting dopo eventuali auto-close
    awaiting = [r for r in reqs if r["status"] == "inviata_a_teo"]

    if not awaiting:
        print("  Nessuna risposta in attesa")
        return auto_closed

    # Cerca nella inbox via IMAP Gmail
    envelopes = check_inbox_imap()

    if not envelopes:
        print("  Nessuna email trovata da Teo")
        return auto_closed

    updated = 0
    for env in envelopes[:10]:  # ultime 10 email
        email_id = env.get("id") if isinstance(env, dict) else env
        email_subject = env.get("subject", "") if isinstance(env, dict) else ""

        body = read_email_imap(email_id)
        if not body:
            continue

        # Estrai subject dagli header se non lo abbiamo dal JSON
        if not email_subject:
            for line in body.split("\n"):
                if line.lower().startswith("subject:"):
                    email_subject = line.split(":", 1)[1].strip()
                    break

        # Match per subject: cerca "Re: <subject_originale>"
        clean_subject = email_subject.lower()
        for prefix in ["re:", "duplicato:", "nuova chiamata stessa richiesta –"]:
            clean_subject = clean_subject.replace(prefix, "")
        clean_subject = clean_subject.strip()

        for req in awaiting:
            expected_subject = req.get("email_subject", "").lower().strip()
            # Match bidirezionale: il subject pulito deve combaciare con l'expected
            # (gestisce sia risposte dirette che risposte a notifiche DUPLICATO)
            if expected_subject and (expected_subject in clean_subject or clean_subject in expected_subject):
                # Estrai la risposta: tutto prima del quoted text ("Il giorno...")
                quoted_marker = body.find("\nIl giorno ")
                if quoted_marker > 0:
                    answer = body[:quoted_marker].strip()
                else:
                    # Fallback: prendi le prime righe dopo gli header
                    parts_body = body.split("\n\n", 2)
                    answer = parts_body[-1].strip()[:500] if len(parts_body) > 1 else body.strip()[:500]

                # Rimuovi header email (From, To, Subject, Date)
                answer_clean = []
                for line in answer.split("\n"):
                    if not any(line.lower().startswith(h) for h in ["from:", "to:", "subject:", "date:", "content-type:", "mime-version:"]):
                        answer_clean.append(line)
                answer = "\n".join(answer_clean).strip()[:500]

                req["status"] = "risposta_ricevuta"
                req["answer"] = answer
                req["received_at"] = datetime.now(timezone.utc).isoformat()

                print(f"  RISPOSTA RICEVUTA: {req['id'][:30]} | subject: \"{email_subject[:60]}\"")
                updated += 1
                break  # una risposta per richiesta

    if updated:
        save_json(PENDING_FILE, pending)

    print(f"  Risposte ricevute: {updated}")
    return updated


# ============================================================
# FASE 5: AGGIORNAMENTO KNOWLEDGE BASE
# ============================================================

def update_knowledge_base():
    """Aggiorna il documento KB permanente di Giorgia con le nuove risposte di Teo.
    Unico documento, cresce nel tempo. Mai duplicati."""
    print("=== FASE 5: AGGIORNAMENTO KNOWLEDGE BASE ===")

    pending = load_json(PENDING_FILE)

    # Percorsi
    kb_state_file = DATA_DIR / ".kb_permanent_state.json"
    kb_content_file = DATA_DIR / "knowledge_permanent.txt"

    # Carica stato KB permanente
    kb_state = load_json(kb_state_file) if kb_state_file.exists() else {}
    permanent_doc_id = kb_state.get("permanent_kb_doc_id", "")

    # Carica contenuto attuale KB permanente
    if kb_content_file.exists():
        current_kb_text = kb_content_file.read_text()
    else:
        # Fallback: usa contenuto iniziale dal prompt
        current_kb_text = load_latest_prompt().get("prompt", "")

    reqs = pending.get("requests", [])
    to_process = [r for r in reqs if r["status"] == "risposta_ricevuta" and not r.get("kb_updated")]

    if not to_process:
        # Anche senza nuove risposte, verifica che il doc sia linkato all'agente
        if permanent_doc_id:
            agent_config = api_get(f"convai/agents/{GIORGIA_AGENT_ID}")
            if "error" not in agent_config:
                prompt = agent_config.get("agent", agent_config).get("conversation_config", {}).get("agent", {}).get("prompt", {})
                kb_linked = prompt.get("knowledge_base", [])
                if not kb_linked:
                    print("  KB non linkata all'agente, ripristino link...")
                    _link_kb_to_agent(permanent_doc_id)
        print("  KB aggiornata: 0 nuove informazioni")
        return 0

    # Accumula nuove informazioni
    new_entries = []
    for req in to_process:
        question = req.get("question", "")
        answer = req.get("answer", "")

        # Pulisci la risposta (rimuovi header, firme, quoted text)
        answer_clean = answer
        for marker in ["Il giorno ", "Da: ", "Inviato: ", "Oggetto: ", "Scrive:", "----", ">>>"]:
            idx = answer_clean.find(marker)
            if idx > 0:
                answer_clean = answer_clean[:idx]
        answer_clean = answer_clean.strip()

        if not answer_clean:
            continue

        # Verifica se questa informazione è già presente nella KB
        if answer_clean[:100] in current_kb_text:
            req["kb_updated"] = True
            continue

        # Aggiungi alla KB
        entry = f"\n\n---\nDOMANDA: {question}\nRISPOSTA: {answer_clean}"
        new_entries.append(entry)
        req["kb_updated"] = True
        print(f"  + Nuova info: {question[:70]}")

    if not new_entries:
        save_json(PENDING_FILE, pending)
        print("  KB aggiornata: 0 (nessuna nuova informazione)")
        return 0

    # Aggiorna contenuto locale
    new_kb_text = current_kb_text + "\n".join(new_entries)
    kb_content_file.write_text(new_kb_text)

    # Elimina vecchio doc su ElevenLabs e crea nuovo
    if permanent_doc_id:
        try:
            _delete_kb_doc(permanent_doc_id)
        except Exception as e:
            print(f"  Avviso: impossibile eliminare vecchio doc: {e}")

    new_doc_id = _upload_kb_document("Conoscenza Permanente NewTeogomme", new_kb_text)
    if not new_doc_id:
        print("  ERRORE: upload KB fallito")
        save_json(PENDING_FILE, pending)
        return 0

    # Linka all'agente
    _link_kb_to_agent(new_doc_id)

    # Salva stato
    kb_state = {
        "permanent_kb_doc_id": new_doc_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": len([l for l in new_kb_text.split("\n") if l.startswith("DOMANDA:")]),
    }
    save_json(kb_state_file, kb_state)

    save_json(PENDING_FILE, pending)
    print(f"  KB aggiornata: {len(new_entries)} nuove informazioni (doc: {new_doc_id[:20]}...)")
    return len(new_entries)


def _upload_kb_document(name: str, content: str) -> str:
    """Carica un documento su ElevenLabs KB. Ritorna l'ID o stringa vuota."""
    import urllib.request as _ur
    boundary = '----FormBoundary7MA4YWxkTrZu0gW'
    body = ''
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{name}\r\n'
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="conoscenza.txt"\r\nContent-Type: text/plain\r\n\r\n'
    body += content + '\r\n'
    body += f'--{boundary}--\r\n'

    try:
        kb_req = _ur.Request(
            "https://api.elevenlabs.io/v1/convai/knowledge-base",
            data=body.encode(),
            headers={
                "xi-api-key": load_api_key(),
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            }
        )
        with _ur.urlopen(kb_req, timeout=30) as resp:
            result = json.loads(resp.read())
        return result.get("id", "")
    except Exception as e:
        print(f"  _upload_kb_document error: {e}")
        return ""


def _delete_kb_doc(doc_id: str):
    """Elimina un documento KB da ElevenLabs."""
    import urllib.request as _ur
    del_req = _ur.Request(
        f"https://api.elevenlabs.io/v1/convai/knowledge-base/{doc_id}",
        headers={"xi-api-key": load_api_key()},
        method="DELETE"
    )
    with _ur.urlopen(del_req, timeout=30):
        pass


def _link_kb_to_agent(doc_id: str):
    """Collega il documento KB all'agente Giorgia."""
    import urllib.request as _ur

    # Recupera prompt corrente per preservarlo
    agent_config = api_get(f"convai/agents/{GIORGIA_AGENT_ID}")
    if "error" in agent_config:
        print(f"  _link_kb_to_agent: errore recupero agente: {agent_config}")
        return

    prompt = agent_config.get("agent", agent_config).get("conversation_config", {}).get("agent", {}).get("prompt", {})
    prompt_text = prompt.get("prompt", "")
    llm = prompt.get("llm", "gemini-3.5-flash")

    payload = {
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": prompt_text,
                    "llm": llm,
                    "knowledge_base": [
                        {"id": doc_id, "type": "file", "name": "Conoscenza Permanente NewTeogomme"}
                    ]
                }
            }
        }
    }
    try:
        req = _ur.Request(
            f"https://api.elevenlabs.io/v1/convai/agents/{GIORGIA_AGENT_ID}",
            data=json.dumps(payload).encode(),
            headers={"xi-api-key": load_api_key(), "Content-Type": "application/json"},
            method="PATCH"
        )
        with _ur.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if "error" in result:
            print(f"  _link_kb_to_agent error: {result}")
    except Exception as e:
        print(f"  _link_kb_to_agent error: {e}")


def _get_current_kb_docs() -> list:
    """Recupera i documenti KB attualmente collegati a Giorgia.
    Ritorna una lista di dict {id, type, name} o lista vuota in caso di errore.
    """
    agent_config = api_get(f"convai/agents/{GIORGIA_AGENT_ID}")
    if "error" in agent_config:
        print(f"  _get_current_kb_docs: errore recupero agente: {agent_config}")
        return []
    conv = agent_config.get("agent", agent_config).get("conversation_config", {})
    prompt = conv.get("agent", {}).get("prompt", {})
    kb = prompt.get("knowledge_base", [])
    return [{"id": d.get("id", ""), "type": d.get("type", "file"), "name": d.get("name", "")} for d in kb]


# ============================================================
# FASE 6: AGGIORNAMENTO PROMPT (solo se necessario)
# ============================================================

def update_prompt_if_needed():
    """Analizza le risposte recenti di Teo con LLM. Se contengono nuove
    specifiche permanenti (servizi, prezzi, policy, convenzioni), aggiorna
    il prompt di Giorgia e la KB permanente."""
    print("=== FASE 6: CONSOLIDAMENTO CONOSCENZA ===")

    pending = load_json(PENDING_FILE)
    kb_content_file = DATA_DIR / "knowledge_permanent.txt"

    # Trova risposte recenti non ancora consolidate
    answered = [r for r in pending.get("requests", [])
                if r.get("status") == "risposta_ricevuta" and r.get("answer") and not r.get("consolidated")]

    if not answered:
        print("  Nessuna nuova risposta da consolidare")
        return 0

    # Carica prompt attuale
    prompt_data = load_latest_prompt()
    current_prompt = prompt_data.get("prompt", "")

    # Costruisci il testo da analizzare
    answers_text = ""
    for r in answered[-5:]:  # ultime 5 risposte
        q = r.get("question", "")
        a = r.get("answer", "")
        # Pulisci
        for marker in ["Il giorno ", "Da: ", "Inviato: ", "Oggetto: "]:
            idx = a.find(marker)
            if idx > 0:
                a = a[:idx]
        answers_text += f"DOMANDA CLIENTE: {q}\nRISPOSTA TEO: {a.strip()[:300]}\n\n"

    # Chiedi all'LLM di estrarre specifiche permanenti
    model, provider = get_default_model()
    deepseek_key = load_env_value("DEEPSEEK_API_KEY")
    anthropic_key = load_env_value("ANTHROPIC_API_KEY")

    import urllib.request, urllib.error

    system = (
        "Analizzi le risposte del titolare di NewTeogomme (gommista) e estrai solo "
        "le INFORMAZIONI PERMANENTI da aggiungere alla knowledge base dell'agente vocale Giorgia.\\n\\n"
        "COSA ESTRARRE:\\n"
        "- Servizi che NewTeogomme FA (es. 'facciamo i freni', 'sostituiamo pastiglie')\\n"
        "- Servizi che NewTeogomme NON FA\\n"
        "- Prezzi, tariffe, costi\\n"
        "- Policy (appuntamento si/no, procedure)\\n"
        "- Convenzioni con aziende/enti\\n"
        "- Orari, contatti\\n\\n"
        "COSA IGNORARE:\\n"
        "- Istruzioni operative a Giorgia/Teo ('devi dire che...', 'ricordati di...')\\n"
        "- Informazioni specifiche di un singolo cliente\\n"
        "- Conferme di cose già note\\n\\n"
        "Rispondi SOLO con JSON:\\n"
        '{"new_specs": ["specifica 1", "specifica 2"], "update_prompt": true/false}\\n'
        "Se non ci sono nuove specifiche permanenti, new_specs array vuoto e update_prompt false."
    )

    # ── Branch DeepSeek ──
    if provider == "deepseek" and deepseek_key:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Prompt attuale di Giorgia:\n{current_prompt[:500]}\n\nRisposte recenti di Teo:\n{answers_text}\n\nRestituisci JSON."}
            ],
            "temperature": 0.1,
            "max_tokens": 600,
            "response_format": {"type": "json_object"}
        }
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {deepseek_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            analysis = json.loads(text) if text else {}
        except Exception as e:
            print(f"  Errore LLM consolidamento (DeepSeek): {e}")
            return 0

    # ── Branch Anthropic (backup) ──
    elif provider == "anthropic" and anthropic_key:
        payload = {
            "model": model,
            "max_tokens": 600,
            "system": system,
            "messages": [{
                "role": "user",
                "content": f"Prompt attuale di Giorgia:\n{current_prompt[:500]}\n\nRisposte recenti di Teo:\n{answers_text}"
            }],
        }
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            text = ""
            if "content" in result and result["content"]:
                text = result["content"][0].get("text", "")
            analysis = json.loads(text) if text else {}
        except Exception as e:
            print(f"  Errore LLM consolidamento (Anthropic): {e}")
            return 0

    else:
        print("  LLM non disponibile, salto consolidamento")
        return 0

    new_specs = analysis.get("new_specs", [])
    update_prompt = analysis.get("update_prompt", False)

    if not new_specs:
        # Marca come consolidate comunque
        for r in answered:
            r["consolidated"] = True
        save_json(PENDING_FILE, pending)
        print("  Nessuna nuova specifica permanente trovata")
        return 0

    print(f"  Trovate {len(new_specs)} nuove specifiche permanenti")

    # Aggiorna il prompt di Giorgia
    if update_prompt:
        specs_text = "\n".join(f"- {s}" for s in new_specs)
        new_section = f"\n\nSERVIZI AGGIUNTIVI (appresi nel tempo):\n{specs_text}"

        if "SERVIZI AGGIUNTIVI" in current_prompt:
            # Sostituisci sezione esistente
            idx = current_prompt.find("SERVIZI AGGIUNTIVI")
            idx_end = current_prompt.find("\n\n", idx + 50)
            if idx_end < 0:
                idx_end = len(current_prompt)
            new_prompt_text = current_prompt[:idx] + new_section
        else:
            new_prompt_text = current_prompt + new_section

        # Salva nuova versione prompt
        prompt_dir = PROMPT_DIR
        existing_versions = sorted([
            int(f.stem[1:]) for f in prompt_dir.glob("v*.json") if f.stem[1:].isdigit()
        ])
        new_version = max(existing_versions) + 1 if existing_versions else 5

        new_prompt_data = dict(prompt_data)
        new_prompt_data["prompt"] = new_prompt_text
        new_prompt_data["_consolidated_from"] = [r.get("id", "")[:30] for r in answered]

        new_prompt_file = prompt_dir / f"v{new_version}.json"
        save_json(new_prompt_file, new_prompt_data)
        LATEST_PROMPT_FILE.write_text(f"v{new_version}")

        # Aggiorna agente su ElevenLabs (include knowledge_base per evitare wipe)
        kb_docs = _get_current_kb_docs()
        result = api_patch(f"convai/agents/{GIORGIA_AGENT_ID}", {
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "prompt": new_prompt_text,
                        "llm": prompt_data.get("llm", "gemini-3.5-flash"),
                        "knowledge_base": kb_docs
                    }
                }
            }
        })
        if "error" in result:
            print(f"  ATTENZIONE: errore aggiornamento agente: {result}")
        else:
            print(f"  Prompt aggiornato: v{new_version}.json con {len(new_specs)} specifiche")

    # Marca richieste come consolidate
    for r in answered:
        r["consolidated"] = True
    save_json(PENDING_FILE, pending)

    return len(new_specs)


# ============================================================
# FASE 7: RICHIAMATA
# ============================================================

def _notify_teo_unreachable(req: dict, last_status: str, attempts: int):
    """Invia email a Teo quando un cliente non è raggiungibile dopo 4 tentativi."""
    question = req.get("question", "")[:100]
    caller = req.get("caller_number", "")
    subject = f"CLIENTE NON RAGGIUNGIBILE: {question[:60]}"

    history = req.get("callback_history", [])
    hist_lines = []
    for h in history:
        hist_lines.append(f"  Tentativo {h['attempt']}: status={h['status']}, durata={h['duration']}s")

    body = f"""Teo,

Il cliente {caller} NON è stato raggiunto dopo {attempts} tentativi di richiamata.

Domanda originale: {question}

Storico tentativi:
{chr(10).join(hist_lines)}

Il ticket è stato chiuso come 'cliente_non_raggiungibile'.

-- Erminio (automatico)"""

    if send_email_smtp(subject, body):
        print(f"  Email notifica inviata a Teo: {subject[:60]}")
    else:
        print(f"  ERRORE invio notifica")


def _is_working_hours() -> bool:
    """Restituisce True se siamo in orario lavorativo (ora italiana).
    Lun-Ven: 8:30-12:30 e 14:30-19:00. Sab: 8:30-12:30. Dom: mai."""
    now = datetime.now(ZoneInfo("Europe/Rome"))
    wd = now.weekday()  # 0=Lun ... 6=Dom
    t = now.hour * 60 + now.minute
    if wd == 6:
        return False  # Domenica
    if wd == 5:  # Sabato
        return 510 <= t < 750  # 8:30-12:30
    # Lun-Ven
    return (510 <= t < 750) or (870 <= t < 1140)  # 8:30-12:30 o 14:30-19:00


def _restore_agent_prompt(prompt_text: str, llm: str, first_message: str) -> bool:
    """Ripristina il prompt dell'agente ElevenLabs. Ritorna True se OK.
    Funzione idempotente: puo' essere chiamata piu' volte, anche se il
    prompt e' gia' corretto (nessun effetto collaterale)."""
    try:
        kb_docs = _get_current_kb_docs()
        result = api_patch(f"convai/agents/{GIORGIA_AGENT_ID}", {
            "conversation_config": {
                "agent": {
                    "prompt": {"prompt": prompt_text, "llm": llm, "knowledge_base": kb_docs},
                    "first_message": first_message,
                }
            }
        })
        if "error" in result:
            print(f"  ATTENZIONE: errore ripristino prompt: {result}")
            return False
        print(f"  Prompt ripristinato (da safeguard/finally)")
        return True
    except Exception as e:
        print(f"  ERRORE CRITICO ripristino prompt: {e}")
        return False


def callback_customers():
    """Avvia chiamate in uscita per rispondere ai clienti."""
    print("=== FASE 7: RICHIAMATA CLIENTI ===")

    # GUARDIA ORARIA: mai chiamare fuori orario lavorativo
    if not _is_working_hours():
        print("  Fuori orario lavorativo, nessuna richiamata.")
        return 0

    pending = load_json(PENDING_FILE)
    def _had_successful_callback(req):
        """Controlla se una chiamata precedente era già 'done' — se sì, non riprovare MAI."""
        for h in req.get("callback_history", []):
            if h.get("status") == "done":
                return True
        return False

    reqs = pending.get("requests", [])
    to_callback = []
    now_ts = datetime.now(timezone.utc).timestamp()
    for r in reqs:
        # === BLACKLIST CHECK ===
        caller = r.get("caller_number", "")
        if _is_blocked_number(caller):
            print(f"  SALTATO (numero bloccato): {r["id"][:30]}")
            r["callback_done"] = True
            r["callback_outcome"] = "numero_bloccato"
            continue

        if r.get("callback_done") or r.get("callback_in_progress"):
            continue
        # GUARDIA CRITICA: se una chiamata precedente era 'done', non riprovare.
        # Protegge da bug nel calcolo call_ok (es. call_duration None/0).
        if _had_successful_callback(r):
            print(f"  SALTATO: {r['id'][:30]} già chiamato con successo (history ha 'done')")
            r["callback_done"] = True
            r["callback_in_progress"] = False
            r["status"] = "chiusa"
            r["closed_at"] = datetime.now(timezone.utc).isoformat()
            r["callback_outcome"] = "chiuso_da_guardia_history_done"
            continue
        if r["status"] == "risposta_ricevuta" and r.get("answer"):
            to_callback.append(r)
        elif r["status"] == "in_retry":
            next_retry = r.get("callback_next_retry_at", 0)
            if now_ts >= next_retry:
                to_callback.append(r)

    if not to_callback:
        print("  Nessuna richiamata da effettuare")
        return 0

    # Carica i numeri reali da file (MAI hardcodare)
    numbers_file = DATA_DIR / "client_numbers.json"
    client_numbers = load_json(numbers_file)

    # Salva il prompt corrente di Giorgia per ripristino
    default_prompt_data = load_latest_prompt()
    default_prompt = default_prompt_data.get("prompt", "")
    default_llm = default_prompt_data.get("llm", "gemini-3.5-flash")

    # Recupera configurazione attuale dell'agente
    agent_config = api_get(f"convai/agents/{GIORGIA_AGENT_ID}")
    if "error" in agent_config:
        print(f"  ERRORE nel recupero configurazione agente: {agent_config}")
        return 0

    conv_config = agent_config.get("agent", agent_config).get("conversation_config", {})
    original_first_message = f"New Teo gomme, {get_greeting()}. Come posso aiutarla?"

    called = 0
    for req in to_callback:
        caller_number = req.get("caller_number", "")
        # === BLACKLIST CHECK: non richiamare numeri bloccati ===
        if _is_blocked_number(caller_number):
            print(f"  SALTATO (numero bloccato): {req["id"][:30]}")
            req["callback_done"] = True
            req["callback_outcome"] = "numero_bloccato"
            continue

        conv_id = req.get("conversation_id", "")

        # Leggi il numero reale dal file JSON (MAI hardcodare)
        real_number = client_numbers.get(conv_id, caller_number)
        # Se il numero è mascherato, prova a prenderlo dall'API
        if '*' in real_number:
            detail = api_get(f"convai/conversations/{conv_id}")
            if "error" not in detail:
                real_number = detail.get("metadata", {}).get("phone_call", {}).get("external_number", "")
                if real_number and '*' not in real_number:
                    client_numbers[conv_id] = real_number
                    save_json(numbers_file, client_numbers)

        if not real_number or '*' in real_number:
            print(f"  SALTATO {req['id']}: numero non disponibile")
            continue

        question = req.get("question", "")
        answer = req.get("answer", "")

        # Estrai informazioni pulite dalla risposta di Teo
        # Rimuove header email, firme, quoted text, riferimenti interni
        answer_clean = answer
        for marker in ["Il giorno ", "Da: ", "Inviato: ", "Oggetto: ", "Scrive:", "----"]:
            idx = answer_clean.find(marker)
            if idx > 0:
                answer_clean = answer_clean[:idx]
        answer_clean = answer_clean.strip()[:500]

        # 1. PATCH: modifica il prompt con le informazioni da comunicare
        ticket_chain_depth = req.get("ticket_chain_depth", 1)

        if ticket_chain_depth >= 3:
            # Dopo 3 escalation sulla stessa catena: invita in officina
            callback_prompt = f"""{default_prompt}

IMPORTANTE: stai richiamando un cliente che ha gia' fatto DIVERSE domande a cui abbiamo risposto.

INVITA GENTILMENTE il cliente a passare in officina cosi' possono rispondergli
a tutte le sue domande di persona, senza ulteriori giri di telefono.

NON rispondere a domande specifiche. Di' SOLO che lo invitiamo a passare in
officina negli orari di apertura (Lun-Ven 8:30-19:30, Sab 8:30-13:00)
cosi' possiamo aiutarlo al meglio per tutte le sue esigenze."""
            callback_first_message = f"Buongiorno, la richiamo da New Teo gomme. La invitiamo a passare in officina cosi' possiamo rispondere a tutte le sue domande di persona."
            print(f"  TICKET CHAIN #{ticket_chain_depth}: invito in officina per {req['id'][:30]}")
        else:
            # Inietta le info di Teo ma SOLO il contenuto pulito per il cliente
            callback_prompt = f"""{default_prompt}

IMPORTANTE: stai richiamando un cliente che aveva chiesto: "{question}"

Ecco le informazioni corrette da comunicare al cliente:
{answer_clean}

COMUNICA queste informazioni in modo chiaro. NON aggiungere "verifico e la richiamo"
se hai gia' le informazioni. NON promettere di richiamare una seconda volta.
NON menzionare sistemi interni, agenti AI, o processi operativi."""
            callback_first_message = f"Buongiorno, la richiamo da New Teo gomme per rispondere alla sua domanda."

        kb_docs = _get_current_kb_docs()
        patch_result = api_patch(f"convai/agents/{GIORGIA_AGENT_ID}", {
            "conversation_config": {
                "agent": {
                    "prompt": {"prompt": callback_prompt, "llm": default_llm, "knowledge_base": kb_docs},
                    "first_message": callback_first_message,
                }
            }
        })

        if "error" in patch_result:
            print(f"  ERRORE PATCH agente: {patch_result}")
            continue

        # PATCH riuscita: da qui in poi il prompt DEVE essere ripristinato.
        # try/finally garantisce il ripristino anche su eccezioni, kill del
        # thread, errori API nel polling, o qualsiasi crash imprevisto.
        prompt_patched = True
        print(f"  Prompt aggiornato per callback a {real_number}")

        # Segna callback in corso SUBITO per evitare doppie chiamate
        req["callback_in_progress"] = True
        save_json(PENDING_FILE, pending)

        try:
            # 2. POST: avvia chiamata outbound
            outbound_result = api_post("convai/sip-trunk/outbound-call", {
                "agent_id": GIORGIA_AGENT_ID,
                "agent_phone_number_id": GIORGIA_PHONE_ID,
                "to_number": real_number,
            })

            if "error" in outbound_result:
                print(f"  ERRORE chiamata outbound: {outbound_result}")
                # Il finally ripristinera' il prompt automaticamente
                continue

            call_id = outbound_result.get("call_id", outbound_result.get("conversation_id", ""))
            print(f"  CHIAMATA AVVIATA: {call_id} -> {real_number}")

            # 3. Attendi completamento (polling)
            max_wait = 120  # secondi massimi
            wait_interval = 10
            waited = 0
            call_status = "unknown"

            time.sleep(5)  # attesa iniziale

            while waited < max_wait:
                time.sleep(wait_interval)
                waited += wait_interval

                if call_id:
                    status_resp = api_get(f"convai/conversations/{call_id}")
                    call_status = status_resp.get("status", "unknown")
                else:
                    # Fallback: cerca conversazioni recenti
                    recent = api_get(f"convai/conversations?agent_id={GIORGIA_AGENT_ID}&page_size=5")
                    for c in recent.get("conversations", []):
                        if c.get("phone_call", {}).get("direction") == "outbound":
                            call_status = c.get("status", "unknown")
                            call_id = c.get("conversation_id", "")
                            break

                print(f"  Stato chiamata ({waited}s): {call_status}")

                if call_status in ("done", "failed", "cancelled"):
                    break

        finally:
            # GARANZIA: ripristina SEMPRE il prompt originale, qualsiasi
            # cosa succeda nel blocco try (eccezioni API, timeout, continue).
            # Questa e' la difesa contro bug #1 e #2 dell'analisi callback.
            if prompt_patched:
                _restore_agent_prompt(default_prompt, default_llm, original_first_message)

        # Determina se la chiamata è riuscita
        call_duration = 0
        if call_id:
            detail = api_get(f"convai/conversations/{call_id}")
            if "error" not in detail:
                # .get() usa il default solo se la chiave è ASSENTE, non se è null.
                # L'API ElevenLabs restituisce call_duration_secs: null per outbound → None.
                call_duration = detail.get("call_duration_secs") or 0

        # Una chiamata 'done' è riuscita. Il controllo durata >= 10 era pensato per
        # filtrare agganci immediati ma quelli danno status='failed'/'cancelled', mai 'done'.
        # Con call_duration=None, None >= 10 causava false riprovazioni → doppie chiamate.
        call_ok = call_status == "done" and (call_duration >= 10 or call_duration == 0)

        # Traccia tentativi
        attempts = req.get("callback_attempts", 0) + 1
        req["callback_attempts"] = attempts
        req["callback_last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        if not req.get("callback_history"):
            req["callback_history"] = []
        req["callback_history"].append({
            "attempt": attempts,
            "conv_id": call_id,
            "status": call_status,
            "duration": call_duration,
            "at": req["callback_last_attempt_at"],
        })

        if call_ok:
            # Successo: cliente raggiunto
            req["callback_done"] = True
            req["callback_conv_id"] = call_id
            req["callback_outcome"] = f"status={call_status}, durata={call_duration}s"
            req["status"] = "chiusa"
            req["closed_at"] = datetime.now(timezone.utc).isoformat()
            called += 1
            print(f"  RICHIAMATA OK: {req['id'][:30]} -> {call_status} ({call_duration}s)")
            continue

        # Fallimento: pianifica retry
        req["callback_in_progress"] = False
        max_attempts = 4

        if attempts < 3:
            # Tentativi 1-2: riprova tra 15 minuti
            retry_delay = 15 * 60
        elif attempts == 3:
            # Terzo tentativo fallito: quarto tentativo tra 1 ora
            retry_delay = 60 * 60
        else:
            # Quarto tentativo fallito: notifica Teo
            req["callback_done"] = True
            req["status"] = "cliente_non_raggiungibile"
            req["closed_at"] = datetime.now(timezone.utc).isoformat()
            print(f"  CLIENTE NON RAGGIUNGIBILE: {req['id'][:30]} dopo {attempts} tentativi")
            # Notifica via email
            _notify_teo_unreachable(req, call_status, attempts)
            continue

        req["callback_next_retry_at"] = (
            datetime.now(timezone.utc).timestamp() + retry_delay
        )
        req["status"] = "in_retry"
        print(f"  RETRY #{attempts}: {req['id'][:30]} riprovo tra {retry_delay//60}min (status={call_status}, durata={call_duration}s)")

    save_json(PENDING_FILE, pending)
    print(f"  Richiamate effettuate: {called}")
    return called


# ============================================================
# FASE 8: INVIO SMS AI CLIENTI
# ============================================================

def load_sms_template(key: str) -> str:
    """Carica template SMS pre-approvato da sms_templates.json."""
    try:
        templates = load_json(SMS_TEMPLATES_FILE)
        tmpl = templates.get(key, {})
        return tmpl.get("it", "")
    except Exception:
        return ""  # Nessun template = nessun SMS
def send_sms_to_customers():
    """Invia SMS ai clienti quando Giorgia ha promesso di farlo.
    Due scenari:
    A) SMS auto (sms_da_inviare): Giorgia ha gia' dato le info, promesso SMS -> invia subito
    B) SMS con risposta: Teo ha risposto, invia la risposta via SMS"""
    print("=== FASE 8: INVIO SMS CLIENTI ===")

    processed = load_json(PROCESSED_FILE)
    pending = load_json(PENDING_FILE)
    numbers_file = DATA_DIR / "client_numbers.json"
    client_numbers = load_json(numbers_file)

    sent = 0

    # -- Scenario A: SMS automatici (cambio stagionale, prenotazioni, etc.) --
    for call in processed.get("calls", []):
        if call.get("outcome") != "sms_da_inviare":
            continue
        if call.get("sms_sent"):
            continue

        conv_id = call["conversation_id"]
        caller_number = call.get("caller_number", "")

        # Recupera numero reale da client_numbers.json
        real_number = client_numbers.get(conv_id, caller_number)
        if not real_number or '*' in real_number:
            print(f"  SALTATO {conv_id[:30]}: numero non disponibile")
            continue

        # Usa template pre-approvato
        sms_text = load_sms_template("cambio_stagionale")
        if not sms_text:
            continue

        # Anti-duplicato SMSTools: aggiungi riferimento univoco al link
        # SMSTools rifiuta messaggi identici in rapida successione (DUPLICATESMS).
        # Aggiungiamo ?r=<hash breve del conv_id> per rendere ogni SMS unico.
        short_ref = conv_id[:4] + conv_id[-4:]
        if "?" in sms_text:
            sms_text = sms_text + "&r=" + short_ref
        else:
            sms_text = sms_text + "?r=" + short_ref

        print(f"  INVIO SMS a {real_number}: {sms_text[:80]}...")
        result = send_sms_via_smstools(real_number, sms_text)

        if result.get("ok"):
            call["sms_sent"] = True
            call["sms_id"] = result.get("sms_id")
            call["sms_text"] = sms_text
            call["sms_sent_at"] = datetime.now(timezone.utc).isoformat()
            sent += 1
            print(f"  SMS INVIATO: id={result.get('sms_id')} -> {real_number}")
        else:
            print(f"  ERRORE SMS: {result.get('error')}")

    # -- Scenario B: saltato (solo SMS cambio stagionale) --
    # Dopo escalation Teo, non si invia SMS. Solo template cambio_stagionale.

    save_json(PROCESSED_FILE, processed)
    save_json(PENDING_FILE, pending)
    print(f"  SMS inviati: {sent}")
    return sent


# ============================================================
# MAIN
# ============================================================

def ensure_agent_prompt_is_default():
    """Verifica che il prompt dell'agente Giorgia su ElevenLabs corrisponda
    al file prompt_default/LATEST. Se non corrisponde, lo ripristina."""
    try:
        default_data = load_latest_prompt()
        default_prompt = default_data.get("prompt", "")
        default_llm = default_data.get("llm", "gemini-3.5-flash")
        default_first_msg = f"New Teo gomme, {get_greeting()}. Come posso aiutarla?"

        agent_config = api_get(f"convai/agents/{GIORGIA_AGENT_ID}")
        if "error" in agent_config:
            return

        conv = agent_config.get("agent", agent_config).get("conversation_config", {}).get("agent", {})
        current_prompt = conv.get("prompt", {})
        current_prompt_text = current_prompt.get("prompt", "") if isinstance(current_prompt, dict) else ""
        current_first_msg = conv.get("first_message", "")

        prompt_ok = current_prompt_text.strip() == default_prompt.strip()
        first_msg_ok = current_first_msg == default_first_msg

        if prompt_ok and first_msg_ok:
            return  # Già corretto

        print(f"=== SAFEGUARD: prompt agente non corrisponde a LATEST, ripristino... ===")
        patch_body = {
            "conversation_config": {
                "agent": {
                    "prompt": {
                        "prompt": default_prompt,
                        "llm": default_llm,
                        "knowledge_base": current_prompt.get("knowledge_base", [])  # Preserva KB link
                    },
                    "first_message": default_first_msg,
                }
            }
        }
        result = api_patch(f"convai/agents/{GIORGIA_AGENT_ID}", patch_body)
        if "error" in result:
            print(f"  ERRORE safeguard: {result}")
        else:
            print(f"  Prompt agente ripristinato da {LATEST_PROMPT_FILE}")
    except Exception as e:
        print(f"  Safeguard prompt: errore ({e}) - continuo")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="NewTeogomme Orchestrator")
    parser.add_argument("--fetch", action="store_true", help="Solo raccolta trascrizioni")
    parser.add_argument("--analyze", action="store_true", help="Solo analisi")
    parser.add_argument("--escalate", action="store_true", help="Solo escalation")
    parser.add_argument("--check", action="store_true", help="Solo controllo risposte")
    parser.add_argument("--kb", action="store_true", help="Solo update knowledge base")
    parser.add_argument("--callback", action="store_true", help="Solo richiamate")
    parser.add_argument("--sms", action="store_true", help="Solo invio SMS")
    parser.add_argument("--safeguard", action="store_true", help="Solo verifica/ripristino prompt (healthcheck rapido)")
    args = parser.parse_args()

    # --safeguard e' un healthcheck leggero: NON prende il lock, NON fa heartbeat
    if args.safeguard:
        ensure_agent_prompt_is_default()
        return

    # Se nessun argomento, esegui ciclo completo
    run_all = not any([args.fetch, args.analyze, args.escalate, args.check, args.kb, args.callback, args.sms])

    # SAFEGUARD: impedisci esecuzioni sovrapposte (lock file PID-aware v2)
    lock_file = DATA_DIR / ".orchestrator.lock"
    if lock_file.exists():
        lock_age = time.time() - lock_file.stat().st_mtime
        try:
            locked_pid = int(lock_file.read_text().strip())
            os.kill(locked_pid, 0)      # signal 0 = check esistenza
            process_alive = True
        except (OSError, ProcessLookupError, ValueError):
            process_alive = False
        if process_alive and lock_age < 600:   # vivo + <10 min → busy
            print(f"Lock attivo (PID {locked_pid}), esco.")
            return
        elif process_alive:                    # vivo + >=10 min → stuck su API
            print(f"Lock STALE (PID {locked_pid} vivo da {lock_age:.0f}s), non accumulo.")
            return
        else:                                  # morto → lock orfano
            print(f"Lock orfano (PID {locked_pid} morto), pulisco e procedo.")
            lock_file.unlink()
    lock_file.write_text(str(os.getpid()))

    # Heartbeat: scrivi timestamp DOPO lock check (solo se si procede davvero)
    heartbeat_file = DATA_DIR / ".orchestrator_heartbeat.json"
    heartbeat = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running"
    }
    save_json(heartbeat_file, heartbeat)

    try:
        _main_loop(run_all, args)
    finally:
        if lock_file.exists():
            lock_file.unlink()


def _main_loop(run_all, args):
    # SAFEGUARD: verifica e ripristina prompt agente
    ensure_agent_prompt_is_default()

    if run_all or args.fetch:
        fetch_conversations()
    if run_all or args.analyze:
        analyze_transcripts()
    if run_all or args.escalate:
        escalate_to_teo()
    if run_all or args.check:
        check_paolo_responses()
    if run_all or args.kb:
        update_knowledge_base()
    if run_all:
        update_prompt_if_needed()
    if run_all or args.callback:
        callback_customers()
    if run_all or args.sms:
        send_sms_to_customers()

    print("\n=== CICLO COMPLETATO ===")

    # Heartbeat: ciclo completato con successo
    heartbeat_file = DATA_DIR / ".orchestrator_heartbeat.json"
    heartbeat = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok"
    }
    save_json(heartbeat_file, heartbeat)


def _summarize_cycle(output: str) -> str:
    """Estrae dall'output del ciclo solo gli eventi salienti da notificare.
    Ritorna stringa vuota se non e' successo nulla di rilevante (cosi' il
    cron job no_agent resta silenzioso e non spamma l'utente ogni minuto)."""
    import re as _re
    events = []

    # Nuove trascrizioni
    m = _re.search(r"Nuove trascrizioni:\s*(\d+)", output)
    if m and int(m.group(1)) > 0:
        events.append(f"{m.group(1)} nuova/e chiamata/e")
    # Domande aperte / escalation email
    m = _re.search(r"Email inviate:\s*(\d+)", output)
    if m and int(m.group(1)) > 0:
        events.append(f"{m.group(1)} email a Teo")
    # Risposte ricevute
    m = _re.search(r"Risposte ricevute:\s*(\d+)", output)
    if m and int(m.group(1)) > 0:
        events.append(f"{m.group(1)} risposta/e da Teo")
    # KB aggiornata
    m = _re.search(r"KB aggiornata:\s*(\d+)", output)
    if m and int(m.group(1)) > 0:
        events.append(f"{m.group(1)} doc KB aggiornati")
    # Richiamate
    m = _re.search(r"Richiamate effettuate:\s*(\d+)", output)
    if m and int(m.group(1)) > 0:
        events.append(f"{m.group(1)} cliente/i richiamato/i")
    # SMS
    m = _re.search(r"SMS inviati:\s*(\d+)", output)
    if m and int(m.group(1)) > 0:
        events.append(f"{m.group(1)} SMS inviato/i")

    # Errori espliciti -> sempre notificati
    errors = [ln.strip() for ln in output.splitlines()
              if "ERRORE" in ln or "Errore API" in ln or "Traceback" in ln]

    if not events and not errors:
        return ""

    parts = []
    if events:
        parts.append("NewTeogomme - attivita: " + ", ".join(events))
    if errors:
        parts.append("ERRORI rilevati:\n" + "\n".join(f"  {e}" for e in errors[:10]))
    return "\n".join(parts)


if __name__ == "__main__":
    import sys, io, traceback
    from datetime import datetime, timezone

    # Cattura TUTTO l'output del ciclo in un buffer; NON scrive su stdout reale.
    # Lo stdout reale viene usato solo per un riepilogo conciso degli eventi,
    # cosi' che come cron job no_agent il job sia silenzioso quando non
    # succede nulla e notifichi solo quando c'e' qualcosa di importante.
    real_stdout = sys.stdout
    buffer = io.StringIO()
    sys.stdout = buffer

    crashed = None
    try:
        main()
    except Exception:
        crashed = traceback.format_exc()
        print(crashed)
    finally:
        sys.stdout = real_stdout
        output = buffer.getvalue()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # Log completo su file (storico, per debug/watchdog)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(output)
                f.write(f"\n=== {now} ===\n")
        except Exception:
            pass

        # Su stdout SOLO il riepilogo eventi (vuoto = job silenzioso)
        summary = _summarize_cycle(output)
        if crashed and not summary:
            summary = "NewTeogomme - ERRORE orchestrator:\n" + crashed.strip().splitlines()[-1]
        if summary:
            print(summary)
