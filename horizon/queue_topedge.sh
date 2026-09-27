#!/bin/zsh
export ASIAIR_HOST=192.168.1.35
export SKYSURVEY_OUT=$HOME/ASICAP/skysurvey-west.jsonl
export SKYSURVEY_HTML=$HOME/ASICAP/sky-survey-west.html
cd /Users/madsdorup/ASICAP/air-control
LOG=$HOME/ASICAP/west-ladder.log
# wait for the NE run to APPEAR (avoid firing before it starts), then finish
while ! grep -q "north-east edge" $LOG; do sleep 5; done
while pgrep -f "ladder_run.py 35" > /dev/null; do sleep 5; done
sleep 8                                   # let the mount session close fully
echo "" >> $LOG
echo "########## APPENDED: top edge of the southern wall, az 190 alt 65/70/75" >> $LOG
python3 - >> $LOG 2>&1 <<'PY'
import sys, time
sys.path[:0] = ['/Users/madsdorup/ASICAP/air-control/lib', '/Users/madsdorup/ASICAP/air-control/horizon']
from skysurvey import Rig, shot, fmt
import survey_report
rig = Rig()
for alt in (65.0, 70.0, 75.0):
    r = shot(rig, 190.0, alt, exp=10.0, cap=75.0, label=f"southern wall top edge, alt {alt:.0f}")
    print("   " + fmt(r), flush=True)
    survey_report.render()
print("TOP EDGE DONE", flush=True)
PY
