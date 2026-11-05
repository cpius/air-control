#!/bin/zsh
# Saturn clips back to back with no gotos (safe past transit): planetcentre.py, then satvideo.py with the
# pulse hold -- the loop that recorded 17 ADC clips on 2026-11-03, with the camera angle as a flag.
#
#   planetary/cliploop.sh --east 0.997,0.075 --clips 10 --seconds 150 --tag sat1
#
# Stops after 3 clips in a row that did not record. Ctrl-C stops it (satvideo still sends stop_record_avi).
HOST=${ASIAIR_HOST:-192.168.1.39}
EAST="0.997,0.075"; SCALE=0.162; CLIPS=10; SECS=150; TAG=clips; DIAM=18.3
EXP=50; GAIN=400; EXPMAX=55; GAINMAX=450; PRE_NORTH=0; ROI=800; HOLD="--hold-mode pulse"; LEAD=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --east) EAST=$2; shift 2;;
    --scale) SCALE=$2; shift 2;;
    --clips) CLIPS=$2; shift 2;;
    --seconds) SECS=$2; shift 2;;
    --tag) TAG=$2; shift 2;;
    --pre-north) PRE_NORTH=$2; shift 2;;
    --roi) ROI=$2; shift 2;;
    --no-hold) HOLD="--no-hold"; shift;;
    --lead-north) LEAD=$2; shift 2;;   # arcsec: after centring, put the planet this far NORTH of centre so a southward drift carries it across the window   # 2026-11-04 22:00: the pulse hold drove Saturn OUT twice; centre before each short clip instead
    *) echo "unknown flag $1"; exit 2;;
  esac
done
cd ${0:A:h:h}
LOG=$HOME/ASICAP/telemetry/$(date +%Y-%m-%d)/cliploop_$TAG.log
export ASIAIR_HOST=$HOST
{
  if [[ $PRE_NORTH != 0 ]]; then
    echo "=== catching up the drift: pointing ${PRE_NORTH}\" north"
    python3 -u -c "import sys; sys.path.insert(0, 'lib'); from slowpulse import Nudger; print(Nudger('$HOST').nudge(0, $PRE_NORTH))"
  fi
  fails=0
  for i in $(seq 1 $CLIPS); do
    echo "=== $TAG clip $i of $CLIPS  $(date +%T)"
    python3 -u planetary/planetcentre.py --east=$EAST --arcsec-per-px $SCALE --exp 0.05 --gain 350 --passes 5 --tol 12 2>&1 \
      | grep --line-buffered -E "pass [0-9]|exit|NOT"
    if [[ $LEAD != 0 ]]; then
      python3 -u -c "import sys; sys.path.insert(0, 'lib'); from slowpulse import Nudger; print('lead', Nudger('$HOST', log=lambda *x: None).nudge(0, -$LEAD))"
    fi
    python3 -u planetary/satvideo.py --seconds $SECS --roi $ROI --exp-ms $EXP --gain $GAIN ${=HOLD} --east=$EAST \
        --arcsec-per-px $SCALE --planet-diam-arcsec $DIAM --exp-max $EXPMAX --gain-max $GAINMAX 2>&1 \
      | grep --line-buffered -E "exposure used|RECORDED|RESULT|hold:|ABORT|refus|Traceback|Error|stop_record"
    if tail -8 $LOG | grep -q "RESULT recorded"; then
      fails=0
    else
      fails=$((fails+1)); echo "=== clip $i did not record (fail $fails)"
      [[ $fails -ge 3 ]] && { echo "=== 3 failures in a row -- stopping"; break; }
      sleep 20
    fi
  done
  echo "=== loop finished $(date +%T)"
} 2>&1 | tee -a $LOG
