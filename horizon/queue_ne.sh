#!/bin/zsh
export ASIAIR_HOST=192.168.1.35
export SKYSURVEY_OUT=$HOME/ASICAP/skysurvey-west.jsonl
export SKYSURVEY_HTML=$HOME/ASICAP/sky-survey-west.html
export LADDER_EXP=10
cd /Users/madsdorup/ASICAP/air-control
while pgrep -f "high_probe.py" > /dev/null; do sleep 5; done
echo "" >> $HOME/ASICAP/west-ladder.log
echo "########## APPENDED: north-east edge, az 35 and 40" >> $HOME/ASICAP/west-ladder.log
python3 -u ladder_run.py 35 40 >> $HOME/ASICAP/west-ladder.log 2>&1
