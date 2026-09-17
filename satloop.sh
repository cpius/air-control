#!/bin/zsh
# Back-to-back Saturn clips with the hold loop; re-acquire with cloudcheck only when a clip reports no planet.
export ASIAIR_HOST=192.168.1.35 EAF_MIN=15000 EAF_MAX=98000
LOG=../telemetry/2026-09-16_session.log
J1="-46.0,599.6,-475.0,55.0"        # satvideo Jacobian, bin 1, pier west
J2E="23.0,-299.8,237.5,-27.5"       # cloudcheck Jacobian, bin 2, pier-east form (it negates on west)
N=${1:-30}; SECS=${2:-300}; UNTIL=${3:-0320}
for i in $(seq 1 $N); do
  now=$(date '+%H%M')
  if [ "$now" -gt "$UNTIL" ] && [ "$now" -lt "1200" ]; then echo "$(date '+%T') LOOP past $UNTIL, stopping" | tee -a $LOG; break; fi
  echo "$(date '+%T') LOOP clip $i/$N, $SECS s" | tee -a $LOG
  for try in 1 2 3; do
    OUT=$(python3 -u satvideo.py --seconds $SECS --roi 1000 --exp-ms 40 --gain 350 --exp-max 100 --gain-max 450 --jacobian "$J1" --deadband 100 --min-gap 4 2>&1 | grep -v -i 'warning\|receive .* B \|heartbeat/status\|info  rpc\|info  image\|drain\|info  mount\|working=None' )
    if echo "$OUT" | grep -q RECORDED; then break; fi
    if echo "$OUT" | grep -q -E 'not in the window|no planet|ABORT|refus'; then break; fi
    echo "$(date '+%T') LOOP transient failure (try $try): $(echo "$OUT" | grep -E 'FAILED after|Error|Traceback' | tail -1 | cut -c1-90) -> 20 s pause" | tee -a $LOG; sleep 20
  done
  echo "$OUT" | grep -E 'exposure test|RECORDED|hold:|exposure used|not in|ABORT|refus|Traceback|Error|correction' | tail -8 | sed 's/^/   /' | tee -a $LOG
  if ! echo "$OUT" | grep -q RECORDED; then
    echo "$(date '+%T') LOOP no clip -> re-acquiring with cloudcheck" | tee -a $LOG
    python3 -u cloudcheck.py --ra 0.7167 --dec 1.991 --name Saturn --no-solve --search-ra 12 --search-dec 9 --jacobian "$J2E" --jacobian-pier east --every 60 --max-checks 3 --planet-peak 20000 --planet-area 30 2>&1 | grep -E 'VERDICT|centred|sync|search|lost|COVERED|EXIT' | tail -5 | sed 's/^/   /' | tee -a $LOG
  fi
done
echo "$(date '+%T') LOOP done" | tee -a $LOG
