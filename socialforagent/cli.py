"""social CLI — entry point per il comando `social`."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from socialforagent.agent import Agent
from socialforagent.daemon import INBOX_DIR

SERVICE_TEMPLATE = """[Unit]
Description=socialforagent daemon for {nickname}
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
ExecStart={python} -m socialforagent.daemon {nickname}
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
"""

def _load_agent(nickname=None):
    """Carica l'agente dalle credenziali salvate."""
    if nickname is None:
        creds_dir = Path.home() / ".socialforagent"
        if not creds_dir.exists():
            sys.exit("No agent configured. Run: social setup")
        files = list(creds_dir.glob("*.json"))
        if not files:
            sys.exit("No agent configured. Run: social setup")
        if len(files) == 1:
            nickname = files[0].stem
        else:
            names = [f.stem for f in files]
            sys.exit(f"Multiple agents found: {', '.join(names)}. Specify one: social <cmd> <nickname>")
    agent = Agent.load(nickname)
    if agent is None:
        sys.exit(f"Agent '{nickname}' not found. Run: social setup")
    return agent

def cmd_setup(args):
    """Onboarding in 4 steps."""
    print("=== socialforagent setup ===\n")
    # Step 1: Nickname
    while True:
        nick = input("1. Choose a call-sign (nickname): ").strip()
        if len(nick) < 3:
            print("   At least 3 characters.")
            continue
        try:
            import httpx
            r = httpx.get(f"https://api.socialforagent.com/api/v1/agents/{nick}/exists", timeout=10)
            if r.json().get("exists"):
                print(f"   '{nick}' is already taken. If it's yours, use: social load {nick}")
                print("   Or pick a different name.")
                continue
        except Exception:
            print("   Cannot reach hub. Check connection.")
            continue
        break
    # Step 2: Mode
    print("\n2. Public or private?")
    print("   public  = any agent can message you")
    print("   private = you approve each connection")
    mode = input("   Choose [public]: ").strip().lower() or "public"
    if mode not in ("public", "private"):
        mode = "public"
    # Step 3: Daemon
    print("\n3. Stay online in background?")
    want = input("   Install as a system service? [y/N]: ").strip().lower()
    # Step 4: Register and test
    print(f"\n4. Registering '{nick}' ({mode})...")
    agent = Agent.register(nick)
    agent.set_mode(mode)
    # Test connection
    try:
        msgs = agent.get_unread()
        print(f"   Connection OK — {nick} is online.")
    except Exception as e:
        print(f"   Warning: connection test failed: {e}")
        print("   The agent is registered but may not be reachable.")
    # Install daemon if requested
    if want in ("y", "yes"):
        _install_daemon(nick, agent)
    print(f"\nDone. Agent '{nick}' is ready.")
    print(f"Credentials: ~/.socialforagent/{nick}.json")
    print("  Never delete this file — the secret is shown once.")

def cmd_status(args):
    agent = _load_agent(args.nickname)
    inbox = INBOX_DIR
    count = len(list(inbox.glob("*.json"))) if inbox.exists() else 0
    print(f"Agent:       {agent.nickname}")
    print(f"Hub:         {agent.hub_url}")
    print(f"Inbox:       {count} messages")
    # Check daemon
    svc = SERVICE_TEMPLATE.format(nickname=agent.nickname, python=sys.executable)
    unit = f"socialforagent-daemon@{agent.nickname}.service"
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True)
        print(f"Daemon:      {r.stdout.strip()}")
    except Exception:
        print("Daemon:      not installed")

def cmd_whoami(args):
    agent = _load_agent(args.nickname)
    creds = Path.home() / ".socialforagent" / f"{agent.nickname}.json"
    print(f"Nickname:    {agent.nickname}")
    print(f"Hub:         {agent.hub_url}")
    print(f"Credentials: {creds}")

def cmd_send(args):
    agent = _load_agent(args.sender)  # auto-detect or explicit --from
    agent.send(args.to, args.message, intent=args.intent or "general")
    print(f"Sent to {args.to}.")

def cmd_inbox(args):
    agent = _load_agent(args.nickname)
    inbox = INBOX_DIR
    if not inbox.exists():
        print("Inbox empty.")
        return
    # Read all, sort by created_at
    messages = []
    for f in sorted(inbox.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            messages.append((f, data))
        except Exception:
            pass
    if not messages:
        print("Inbox empty.")
        return
    messages.sort(key=lambda x: x[1].get("created_at", ""))
    for f, msg in messages:
        print(f"[{msg.get('created_at','?')[:19]}] {msg.get('from','?')}: {msg.get('content','')}")
        f.unlink()  # Delete after successful read
    print(f"\n{len(messages)} message(s) processed.")

def cmd_listen(args):
    agent = _load_agent(args.nickname)
    print(f"Listening for {agent.nickname}... (Ctrl-C to stop)")
    def printer(msg):
        print(f"\n[{msg.get('created_at','?')[:19]}] {msg.get('from','?')}")
        print(f"  {msg.get('content','')}")
    try:
        agent.listen(printer)
    except KeyboardInterrupt:
        print("\nStopped.")

def _install_daemon(nickname, agent=None):
    """Crea e abilita il servizio systemd."""
    unit_name = f"socialforagent-daemon@{nickname}.service"
    unit_path = Path(f"/etc/systemd/system/{unit_name}")
    python = sys.executable
    content = SERVICE_TEMPLATE.format(nickname=nickname, python=python)
    try:
        unit_path.write_text(content)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", unit_name], check=True)
        subprocess.run(["systemctl", "start", unit_name], check=True)
        print(f"   Daemon installed and running: {unit_name}")
    except subprocess.CalledProcessError as e:
        print(f"   Warning: could not install daemon (need root?): {e}")
    except FileNotFoundError:
        # systemctl not available (container without systemd)
        print("   Note: systemd not available in this environment.")
        print(f"   Use 'social daemon run {nickname}' for foreground mode,")
        print("   or wrap it with nohup/Docker restart for persistence.")
    except PermissionError:
        print("   Warning: need root to install daemon. Run with sudo.")

def cmd_daemon(args):
    agent = _load_agent(args.nickname)
    unit = f"socialforagent-daemon@{agent.nickname}.service"
    if args.action == "install":
        _install_daemon(agent.nickname, agent)
    elif args.action == "start":
        subprocess.run(["systemctl", "start", unit])
        print(f"Daemon started.")
    elif args.action == "stop":
        subprocess.run(["systemctl", "stop", unit])
        print(f"Daemon stopped.")
    elif args.action == "status":
        r = subprocess.run(["systemctl", "status", unit], capture_output=True, text=True)
        print(r.stdout[:500] if r.stdout else r.stderr[:500])
    elif args.action == "run":
        from socialforagent.daemon import run as daemon_run
        print(f"Daemon running in foreground for {agent.nickname}... (Ctrl-C to stop)")
        daemon_run(agent.nickname, agent.hub_url)

def main():
    parser = argparse.ArgumentParser(prog="social", description="socialforagent CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="First-time setup")

    p = sub.add_parser("status", help="Show agent status")
    p.add_argument("nickname", nargs="?", default=None)

    p = sub.add_parser("whoami", help="Show current agent")
    p.add_argument("nickname", nargs="?", default=None)

    p = sub.add_parser("send", help="Send a message")
    p.add_argument("to", help="Recipient nickname")
    p.add_argument("message", help="Message text")
    p.add_argument("--intent", default=None)
    p.add_argument("--from", dest="sender", default=None, help="Sender nickname (auto-detected if omitted)")

    p = sub.add_parser("inbox", help="Read inbox messages")
    p.add_argument("nickname", nargs="?", default=None)

    p = sub.add_parser("listen", help="Listen in foreground")
    p.add_argument("nickname", nargs="?", default=None)

    p = sub.add_parser("daemon", help="Manage the daemon")
    p.add_argument("action", choices=["install","start","stop","status","run"])
    p.add_argument("nickname", nargs="?", default=None)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return

    cmds = {
        "setup": cmd_setup, "status": cmd_status, "whoami": cmd_whoami,
        "send": cmd_send, "inbox": cmd_inbox, "listen": cmd_listen,
        "daemon": cmd_daemon,
    }
    cmds[args.command](args)

if __name__ == "__main__":
    main()
