#!/bin/bash
# bridge_watchdog.sh v3 — Monitora e auto-riavvia i bridge SFA
# Fix: evita restart loop, traccia ultimo riavvio, manda nudge dopo restart

LOG="/opt/data/logs/bridge_watchdog.log"
STATE="/opt/data/logs/bridge_watchdog_state.txt"
NOW=$(date -u '+%Y-%m-%d %H:%M:%S')
NOW_EPOCH=$(date +%s)

log() { echo "[$NOW] $1" >> "$LOG"; }

# Carica ultimo restart da state file
declare -A LAST_RESTART
if [ -f "$STATE" ]; then
    while IFS='=' read -r key val; do
        LAST_RESTART["$key"]="$val"
    done < "$STATE"
fi

check_one() {
    local ip=$1 pw=$2 ct=$3 dir=$4 role=$5

    # Controlla se il bridge è vivo
    local alive
    alive=$(SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 root@"$ip" \
        "docker exec $ct pgrep -f 'bridge.py' 2>/dev/null | wc -l" 2>/dev/null)
    alive=$(echo -n "$alive" | tr -dc '0-9')

    if [ -z "$alive" ] || [ "$alive" = "0" ]; then
        log "$role: BRIDGE MORTO — riavvio"
        do_restart "$ip" "$pw" "$ct" "$dir" "$role"
        return
    fi

    # Ultimo RECV/SEND nel log
    local last_ts
    last_ts=$(SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
        "docker exec $ct grep -E '\[RECV\]|\[SEND\]' $dir/bridge.log 2>/dev/null | tail -1 | awk '{print \$1, \$2}'" 2>/dev/null)

    if [ -z "$last_ts" ]; then
        return
    fi

    local last_epoch idle_min
    last_epoch=$(date -d "$last_ts" +%s 2>/dev/null || echo 0)
    idle_min=$(( (NOW_EPOCH - last_epoch) / 60 ))

    # Ultimo timeout Hermes
    local has_timeout=0
    local last_to_ts
    last_to_ts=$(SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
        "docker exec $ct grep -E 'timeout|Nessuna risposta' $dir/bridge.log 2>/dev/null | tail -1 | awk '{print \$1, \$2}'" 2>/dev/null)
    if [ -n "$last_to_ts" ]; then
        local to_epoch to_min
        to_epoch=$(date -d "$last_to_ts" +%s 2>/dev/null || echo 0)
        to_min=$(( (NOW_EPOCH - to_epoch) / 60 ))
        [ "$to_min" -lt 8 ] && has_timeout=1
    fi

    # === GUARDIA ANTI-LOOP: non riavviare se già riavviato negli ultimi 30 minuti ===
    local last_restart=${LAST_RESTART["$role"]:-0}
    local since_restart=$(( NOW_EPOCH - last_restart ))
    local cooldown=$(( 30 * 60 ))  # 30 minuti

    if [ "$since_restart" -lt "$cooldown" ]; then
        # Dentro la finestra di cooldown — non toccare
        return
    fi

    # === DECISIONE ===
    if [ "$idle_min" -ge 30 ]; then
        log "$role: IDLE ${idle_min}min — riavvio forzato"
        do_restart "$ip" "$pw" "$ct" "$dir" "$role"
    elif [ "$idle_min" -ge 5 ] && [ "$has_timeout" -eq 1 ]; then
        log "$role: IDLE ${idle_min}min + timeout — riavvio"
        do_restart "$ip" "$pw" "$ct" "$dir" "$role"
    fi
}

do_restart() {
    local ip=$1 pw=$2 ct=$3 dir=$4 role=$5
    SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
        "docker exec $ct kill -9 \$(docker exec $ct pgrep -f bridge.py) 2>/dev/null; sleep 1" 2>/dev/null
    SSHPASS="$pw" sshpass -e ssh -o StrictHostKeyChecking=no root@"$ip" \
        "docker exec -d $ct bash -c 'cd $dir && nohup /opt/hermes/.venv/bin/python3 bridge.py config.json >> bridge.log 2>&1 &'" 2>/dev/null
    
    # Registra timestamp restart
    LAST_RESTART["$role"]=$NOW_EPOCH
    save_state
    log "$role: riavvio completato"
}

save_state() {
    > "$STATE"
    for k in "${!LAST_RESTART[@]}"; do
        echo "${k}=${LAST_RESTART[$k]}" >> "$STATE"
    done
}

echo "[$NOW] === Watchdog v3 ===" >> "$LOG"

VPS1_PW='gsWrw-2ef'"'"'Z?-6dn'
VPS2_PW=';(Je?rL9oXxKpc1N'

check_one "187.124.160.176" "$VPS1_PW" "hermes-agent-ojuj-hermes-agent-1" "/opt/sfa-allievo10" "allievo10"
check_one "152.239.117.9" "$VPS2_PW" "hermes-agent-ydwu-hermes-agent-1" "/opt/sfa-maestro12" "maestro12"

save_state
