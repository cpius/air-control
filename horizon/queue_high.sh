#!/bin/zsh
export ASIAIR_HOST=192.168.1.35
export SKYSURVEY_OUT=$HOME/ASICAP/skysurvey-west.jsonl
export SKYSURVEY_HTML=$HOME/ASICAP/sky-survey-west.html
cd /Users/madsdorup/ASICAP/air-control
while pgrep -f "ladder_run.py 195" > /dev/null; do sleep 5; done
echo "" >> $HOME/ASICAP/west-ladder.log
echo "########## APPENDED: high-altitude probe, alt 70/80/85" >> $HOME/ASICAP/west-ladder.log
python3 -u horizon/high_probe.py >> $HOME/ASICAP/west-ladder.log 2>&1
