#!/bin/zsh
export SKYSURVEY_OUT=$HOME/ASICAP/skysurvey-west.jsonl
export SKYSURVEY_HTML=$HOME/ASICAP/horizon-map-west.html
cd /Users/madsdorup/ASICAP/air-control/horizon
while true; do
  python3 -c "import survey_report; survey_report.render()" >/dev/null 2>&1
  sleep 20
done
