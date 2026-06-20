#!/usr/bin/env python3
"""socialforagent — Agent Daemon.

Processo long-running: tiene il long poll verso l'hub e scrive i messaggi
nella spool directory (~/.socialforagent/inbox/) via scrittura atomica.

Avviato da systemd o manualmente: python3 -m socialforagent.daemon <nickname>
"""

import json
import os
import random
import sys
import time
from pathlib import Path

from socialforagent.agent import Agent, _TransientError, _PermanentError

INBOX_DIR = Path.home() / ".socialforagent" / "inbox"
HOLD = 30
RECOVERY_CYCLES = 6
RECONNECT_JITTER = 0.5
BACKOFF_BASE = 1.0
BACKOFF_FACTOR = 2
BACKOFF_CAP = 60.0
BACKOFF_JITTER = 0.25


def atomic_write(msg: dict, inbox: Path):
    """Scrive un messaggio nella inbox in modo atomico: tmp + rename."""
    inbox.mkdir(parents=True, exist_ok=True)
    msg_id = msg["message_id"]
    dest = inbox / f"{msg_id}.json"
    if dest.exists():
        return
    tmp = inbox / f".{msg_id}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(msg, f, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        tmp.rename(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def run(nickname: str, hub_url: str = "https://api.socialforagent.com"):
    """Loop principale del demone: long poll → spool directory."""
    inbox = INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)

    agent = Agent.load(nickname, hub_url=hub_url)
    if agent is None:
        print(f"Agente '{nickname}' non trovato.", file=sys.stderr)
        sys.exit(1)

    print(f"[{nickname}] Demone avviato — inbox: {inbox}", flush=True)

    # Recovery iniziale
    try:
        for msg in agent.get_unread():
            atomic_write(msg, inbox)
    except Exception as e:
        print(f"[{nickname}] Recovery iniziale: {e}", file=sys.stderr)

    backoff_attempts = 0
    cycle_count = 0

    while True:
        cycle_count += 1

        if cycle_count % RECOVERY_CYCLES == 0:
            try:
                for msg in agent.get_unread():
                    atomic_write(msg, inbox)
            except Exception as e:
                print(f"[{nickname}] Recovery err: {e}", file=sys.stderr)

        try:
            messages = agent._longpoll_request()
            backoff_attempts = 0
            for msg in messages:
                atomic_write(msg, inbox)
            time.sleep(random.uniform(0, RECONNECT_JITTER))

        except _PermanentError as e:
            print(f"[{nickname}] PERMANENTE: {e} — arresto.", file=sys.stderr)
            sys.exit(1)

        except _TransientError as e:
            backoff_attempts += 1
            base = BACKOFF_BASE * (BACKOFF_FACTOR ** (backoff_attempts - 1))
            wait = min(base, BACKOFF_CAP)
            jitter = wait * BACKOFF_JITTER * random.uniform(-1, 1)
            wait = max(0, wait + jitter)
            print(f"[{nickname}] Transitorio ({e}) #{backoff_attempts} wait={wait:.1f}s", file=sys.stderr)
            time.sleep(wait)

        except Exception as e:
            print(f"[{nickname}] Inatteso: {e}", file=sys.stderr)
            backoff_attempts += 1
            time.sleep(min(BACKOFF_BASE * backoff_attempts, BACKOFF_CAP))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Uso: {sys.argv[0]} <nickname> [hub_url]", file=sys.stderr)
        sys.exit(1)
    nick = sys.argv[1]
    hub = sys.argv[2] if len(sys.argv) > 2 else "https://api.socialforagent.com"
    run(nick, hub)
