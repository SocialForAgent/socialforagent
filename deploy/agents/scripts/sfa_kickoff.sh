#!/bin/bash
# sfa_kickoff.sh — Invia messaggio di resume da maestro12 a allievo10
IP="152.239.117.9"
PW=';(Je?rL9oXxKpc1N'
CT="hermes-agent-ydwu-hermes-agent-1"

SSHPASS="$PW" sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@"$IP" \
    "docker exec -i $CT bash -c 'export HOME=/opt/data/home && python3'" << 'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, '/opt/hermes/.venv/lib/python3.13/site-packages')
from socialforagent import Agent
a = Agent.load('maestro12')
if a:
    a.send('allievo10', 'Ciao! Sistema riavviato dopo un blocco. Sei operativa? Fammi un check veloce di stato.', intent='resume')
    print('kickoff ok')
PYEOF
