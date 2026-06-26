#!/bin/bash
# sfa_resume_nudge.sh — Se entrambi i bridge sono vivi e idle da >10min, manda nudge

LOG="/opt/data/logs/sfa_resume_nudge.log"
NOW=$(date -u '+%Y-%m-%d %H:%M:%S')
NOW_EPOCH=$(date +%s)

log() { echo "[$NOW] $1" >> "$LOG"; }

VPS2_IP="152.239.117.9"
VPS2_PW=';(Je?rL9oXxKpc1N'
VPS2_CT="hermes-agent-ydwu-hermes-agent-1"

# Ultimo SEND del maestro
last=$(SSHPASS="$VPS2_PW" sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@"$VPS2_IP" \
    "docker exec $VPS2_CT grep '\[SEND\]' /opt/sfa-maestro12/bridge.log 2>/dev/null | tail -1 | awk '{print \$1, \$2}'" 2>/dev/null)

if [ -z "$last" ]; then
    exit 0
fi

last_epoch=$(date -d "$last" +%s 2>/dev/null || echo 0)
idle_min=$(( (NOW_EPOCH - last_epoch) / 60 ))

# Solo se idle tra 10 e 20 minuti (evita spam)
if [ "$idle_min" -ge 10 ] && [ "$idle_min" -le 20 ]; then
    SSHPASS="$VPS2_PW" sshpass -e ssh -o StrictHostKeyChecking=no root@"$VPS2_IP" \
        "docker exec -i $VPS2_CT bash -c 'export HOME=/opt/data/home && python3'" << 'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, '/opt/hermes/.venv/lib/python3.13/site-packages')
from socialforagent import Agent
a = Agent.load('maestro12')
if a:
    a.send('allievo10', 'Come stai procedendo? Ci sono dubbi o blocchi?', intent='nudge')
PYEOF
    log "nudge inviato (idle ${idle_min}min)"
fi
