#!/bin/bash
# bridge_watchdog.sh — Monitora e auto-riavvia i bridge SFA
# Versione semplificata: check + restart se morti o bloccati

LOG="/opt/data/logs/bridge_watchdog.log"
NOW=$(date -u '+%Y-%m-%d %H:%M:%S')

log() { echo "[$NOW] $1" >> "$LOG"; }

check_one() {
    local ip=$1 pw=$2 ct=$3 dir=$4 role=$5

    # Controlla se il bridge è vivo
    local alive
    alive=$(SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@"$ip" \
        "docker exec $ct pgrep -f 'bridge.py' 2>/dev/null | wc -l" 2>/dev/null)
    alive=$(echo -n "$alive" | tr -dc '0-9')

    if [ -z "$alive" ] || [ "$alive" = "0" ]; then
        log "$role: BRIDGE MORTO — riavvio"
        SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
            "docker exec -d $ct bash -c 'cd $dir && nohup /opt/hermes/.venv/bin/python3 bridge.py config.json >> bridge.log 2>&1 &'" 2>/dev/null
        return
    fi

    # Controlla ultimo RECV/SEND
    local last_ts
    last_ts=$(SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
        "docker exec $ct grep -E '\[RECV\]|\[SEND\]' $dir/bridge.log 2>/dev/null | tail -1 | awk '{print \$1, \$2}'" 2>/dev/null)

    if [ -z "$last_ts" ]; then
        return  # primo avvio, nessuna attività ancora
    fi

    local last_epoch now_epoch idle_min
    last_epoch=$(date -d "$last_ts" +%s 2>/dev/null || echo 0)
    now_epoch=$(date +%s)
    idle_min=$(( (now_epoch - last_epoch) / 60 ))

    # Controlla timeout Hermes recente
    local has_timeout
    local last_to_ts
    last_to_ts=$(SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
        "docker exec $ct grep -E 'timeout|Nessuna risposta' $dir/bridge.log 2>/dev/null | tail -1 | awk '{print \$1, \$2}'" 2>/dev/null)
    has_timeout=0
    if [ -n "$last_to_ts" ]; then
        local to_epoch to_min
        to_epoch=$(date -d "$last_to_ts" +%s 2>/dev/null || echo 0)
        to_min=$(( (now_epoch - to_epoch) / 60 ))
        [ "$to_min" -lt 8 ] && has_timeout=1
    fi

    # Decisione
    if [ "$idle_min" -ge 30 ]; then
        log "$role: IDLE ${idle_min}min — riavvio forzato"
        SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
            "docker exec $ct kill -9 \$(docker exec $ct pgrep -f bridge.py) 2>/dev/null; sleep 1" 2>/dev/null
        SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
            "docker exec -d $ct bash -c 'cd $dir && nohup /opt/hermes/.venv/bin/python3 bridge.py config.json >> bridge.log 2>&1 &'" 2>/dev/null
    elif [ "$idle_min" -ge 5 ] && [ "$has_timeout" -eq 1 ]; then
        log "$role: IDLE ${idle_min}min + timeout — riavvio"
        SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
            "docker exec $ct kill -9 \$(docker exec $ct pgrep -f bridge.py) 2>/dev/null; sleep 1" 2>/dev/null
        SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
            "docker exec -d $ct bash -c 'cd $dir && nohup /opt/hermes/.venv/bin/python3 bridge.py config.json >> bridge.log 2>&1 &'" 2>/dev/null
    elif [ "$idle_min" -ge 15 ]; then
        log "$role: IDLE ${idle_min}min — warning"
    fi
}

echo "[$NOW] === Watchdog ===" >> "$LOG"

VPS1_PW='gsWrw-2ef'"'"'Z?-6dn'
VPS2_PW=';(Je?rL9oXxKpc1N'

check_one "187.124.160.176" "$VPS1_PW" "hermes-agent-ojuj-hermes-agent-1" "/opt/sfa-allievo10" "allievo10"
check_one "152.239.117.9" "$VPS2_PW" "hermes-agent-ydwu-hermes-agent-1" "/opt/sfa-maestro12" "maestro12"
