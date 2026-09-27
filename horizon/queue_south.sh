#!/bin/zsh
export ASIAIR_HOST=192.168.1.35
export SKYSURVEY_OUT=$HOME/ASICAP/skysurvey-west.jsonl
export SKYSURVEY_HTML=$HOME/ASICAP/sky-survey-west.html
export LADDER_EXP=10
cd /Users/madsdorup/ASICAP/air-control
while pgrep -f "ladder_run.py 230" > /dev/null; do sleep 5; done
echo "" >> $HOME/ASICAP/west-ladder.log
echo "########## APPENDED: 195 (wall edge), then 180, 170" >> $HOME/ASICAP/west-ladder.log
python3 -u horizon/ladder_run.py 195 180 170 >> $HOME/ASICAP/west-ladder.log 2>&1
