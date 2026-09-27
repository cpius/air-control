#!/bin/zsh
export ASIAIR_HOST=192.168.1.35
export SKYSURVEY_OUT=$HOME/ASICAP/skysurvey-west.jsonl
export SKYSURVEY_HTML=$HOME/ASICAP/sky-survey-west.html
cd /Users/madsdorup/ASICAP/air-control
echo "########## RE-SCOPED high probe: walls 190/180/170/150 + 2 zenith checks" >> $HOME/ASICAP/west-ladder.log
python3 -u high_probe.py >> $HOME/ASICAP/west-ladder.log 2>&1
