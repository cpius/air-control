#!/bin/zsh
# Back-to-back Saturn clips with satvideo's hold loop -- with nothing else driving the
# Air, and with every clip checked in the FILE before the next one starts.
#
#   ./satloop.sh --clips 30 --seconds 300 --until 0320
#   ./satloop.sh --check-control          # just: is anything but the ASIAIR app on 4400/4700?
#
# 2026-09-16/17: six 300 s clips logged as RECORDED were empty sky. Pkill-by-name
# restarts had left loops overlapping, and nothing looked at the files until morning.
#
# Before every child that talks to the Air (each satvideo try, each cloudcheck):
#   sole control -- `lsof -nP -i @<host>` may show nothing on ports 4400/4700 but the
#   ASIAIR app (by executable path) and PIDs given with --allow-pid. Anything else stops
#   the loop with the PID and command line. Kill it BY PID; never pkill by name.
# At start: a lock file refuses a second satloop.sh (a loop sleeping between children
#   holds no socket, so lsof cannot see it).
# After satvideo exits 0 (RECORDED) or 6 (frames stalled): clipcheck.py reads the
#   new AVI on the Air's SMB share, read-only (thumbnail + 20-frame means). EMPTY or
#   UNCHECKABLE stops the loop; PARTIAL re-acquires first.
#
# Children run in the background, tracked by PID. Stopping the loop (Ctrl-C, or
# `kill <loop PID>`) sends the child SIGTERM by PID; satvideo still issues stop_record_avi.
# Everything a child prints goes to telemetry/satloop/<night>/; a filtered view streams
# here; LOOP lines also go to --log.

SELF=${0:A}                             # (inside a function zsh's $0 is the function name)
HERE=${SELF:h}
ROOT=${HERE:h}
NIGHT=$(date -v-12H +%Y-%m-%d)

CLIPS=30; SECS=300; UNTIL=0320
HOST=${ASIAIR_HOST:-192.168.1.35}
RA=0.7167; DEC=1.991                    # Saturn, JNow, as synced on 2026-09-16 -- update per night
J_VIDEO="-46.0,599.6,-475.0,55.0"       # satvideo Jacobian, bin 1, pier west
J_CHECK="23.0,-299.8,237.5,-27.5"       # cloudcheck Jacobian, bin 2, pier-east form (it negates on west)
APP_PREFIX=/Applications/ASIAIR.app/    # the ASIAIR app's executable lives under here
LOG=$ROOT/telemetry/${NIGHT}_session.log
typeset -a ALLOW
CHECK_ONLY=0
RETRY_PAUSE=20
VIDEO_ARGS=""                           # extra satvideo flags, appended (argparse: the last one wins)
PULSE=0                                 # 1: no gotos anywhere (register not trustworthy) -- planetcentre/planetsearch
EAST="0.239,0.971"                      # --pulse: sky east on the sensor (camangle.py)
MOONS_EVERY=0                           # >0: a satmoons.py set after every Nth verified clip
MOONS_ARGS=""

usage() {
  sed -n '2,24p' $SELF | sed 's/^# \{0,1\}//'
  cat <<EOF

flags (defaults in brackets):
  --clips N            clips to record [$CLIPS]
  --seconds S          length of each clip [$SECS]
  --until HHMM         no new clip after this time (before noon) [$UNTIL]
  --host IP            the Air [$HOST]
  --ra H --dec D       re-acquisition target for cloudcheck, JNow [$RA $DEC]
  --jacobian-video J   satvideo --jacobian [$J_VIDEO]
  --jacobian-check J   cloudcheck --jacobian [$J_CHECK]
  --allow-pid PID      also tolerate this PID on 4400/4700 (repeatable), e.g. a powerlog.py you started
  --log FILE           LOOP lines are appended here [$LOG]
  --retry-pause S      wait after a satvideo error before retrying (5 GHz connect refusals last ~10 s) [$RETRY_PAUSE]
  --check-control      run the sole-control check once and exit (0 clear, 1 not)
  --video-args "ARGS"  extra satvideo flags, appended after the defaults (the last occurrence wins)
  --pulse              no gotos (register reset/untrusted): planetcentre.py before every clip, satvideo
                       --hold-mode pulse, re-acquire = planetcentre.py, else planetsearch.py --centre
  --east X,Y           --pulse: sky east on the sensor, from camangle.py [$EAST]
  --moons-every N      after every Nth verified clip, a satmoons.py set (deep frames for the moons) [$MOONS_EVERY]
  --moons-args "ARGS"  satmoons.py flags, e.g. "--frames 15 --exp 1.0 --gain 300 --east=X,Y --target-east 50"
EOF
}

while (( $# )); do
  case $1 in
    --clips) CLIPS=$2; shift 2;;
    --seconds) SECS=$2; shift 2;;
    --until) UNTIL=$2; shift 2;;
    --host) HOST=$2; shift 2;;
    --ra) RA=$2; shift 2;;
    --dec) DEC=$2; shift 2;;
    --jacobian-video) J_VIDEO=$2; shift 2;;
    --jacobian-check) J_CHECK=$2; shift 2;;
    --allow-pid) ALLOW+=($2); shift 2;;
    --log) LOG=$2; shift 2;;
    --retry-pause) RETRY_PAUSE=$2; shift 2;;
    --check-control) CHECK_ONLY=1; shift;;
    --video-args) VIDEO_ARGS=$2; shift 2;;
    --pulse) PULSE=1; shift;;
    --east) EAST=$2; shift 2;;
    --moons-every) MOONS_EVERY=$2; shift 2;;
    --moons-args) MOONS_ARGS=$2; shift 2;;
    -h|--help) usage; exit 0;;
    *) print -u2 "unknown argument: $1 (see --help)"; exit 2;;
  esac
done
export ASIAIR_HOST=$HOST EAF_MIN=15000 EAF_MAX=98000

CLIPDIR=$ROOT/telemetry/satloop/$NIGHT
mkdir -p ${LOG:h}
say() { print -r -- "$(date '+%T') $*" | tee -a $LOG; }

# -- sole control ---------------------------------------------------------------------
# Prints every process with a socket to the Air. Returns 1 if any process other than
# the ASIAIR app, the PIDs passed as arguments and --allow-pid holds port 4400 or 4700.
sole_control() {
  local -a allowed; allowed=("$@" "${ALLOW[@]}")
  local -A ports
  local pid port exe line bad=0
  command -v lsof >/dev/null || { say "LOOP cannot check sole control: lsof not found"; return 1; }
  while read -r pid port; do
    [[ -n $pid ]] && ports[$pid]="${ports[$pid]:+${ports[$pid]},}$port"
  done < <(lsof -nP -i @$HOST 2>/dev/null | awk -v h="$HOST" 'NR > 1 {
             for (i = 9; i <= NF; i++) { k = index($i, "->" h ":"); if (k) print $2, substr($i, k + length(h) + 3) } }')
  if (( ${#ports} == 0 )); then
    say "CONTROL clear: no process on this Mac holds a socket to $HOST"
    return 0
  fi
  for pid in ${(k)ports}; do
    exe=$(ps -o comm= -p $pid 2>/dev/null)
    line="PID $pid ports ${ports[$pid]}: $(ps -o etime=,args= -p $pid 2>/dev/null | cut -c1-160)"
    if [[ $exe == ${APP_PREFIX}* ]]; then
      say "CONTROL ok   the ASIAIR app, $line"
    elif (( ${allowed[(Ie)$pid]} )); then
      say "CONTROL ok   allowed, $line"
    elif [[ ",${ports[$pid]}," != *,4400,* && ",${ports[$pid]}," != *,4700,* ]]; then
      say "CONTROL note not on 4400/4700, $line"
    else
      say "CONTROL FOREIGN $line"
      bad=1
    fi
  done
  if (( bad )); then
    say "CONTROL something else is talking to the Air's control ports. Stop it by PID (kill <PID>), never pkill by name;"
    say "CONTROL if it is meant to run alongside (e.g. powerlog.py), pass --allow-pid <PID>."
    return 1
  fi
  return 0
}

if (( CHECK_ONLY )); then
  sole_control
  exit $?
fi
mkdir -p $CLIPDIR

# -- one loop at a time -----------------------------------------------------------------
LOCK=$ROOT/telemetry/satloop.lock
if [[ -f $LOCK ]]; then
  other=$(<$LOCK)
  if [[ -n $other ]] && kill -0 $other 2>/dev/null && [[ "$(ps -o args= -p $other 2>/dev/null)" == *satloop.sh* ]]; then
    say "LOOP ABORT: another satloop.sh is running as PID $other -- stop it first with: kill $other"
    exit 1
  fi
  say "LOOP stale lock (PID ${other:-?} is gone), taking it over"
fi
print $$ > $LOCK

# -- children, by PID -------------------------------------------------------------------
CHILD=""
NOISE='warning|receive .* B |heartbeat/status|info  rpc|info  image|info  mount|drain'
cleanup() {
  trap - INT TERM
  if [[ -n $CHILD ]] && kill -0 $CHILD 2>/dev/null; then
    say "LOOP stopping: SIGTERM to child PID $CHILD (satvideo stops the recorder on its way out)"
    kill -TERM $CHILD 2>/dev/null
    for n in {1..40}; do kill -0 $CHILD 2>/dev/null || break; sleep 0.5; done
    if kill -0 $CHILD 2>/dev/null; then
      say "LOOP child PID $CHILD still running after 20 s -> SIGKILL; check that the Air is not still recording"
      kill -KILL $CHILD 2>/dev/null
    fi
  fi
  CHILD=""
  [[ -f $LOCK && "$(<$LOCK)" == "$$" ]] && rm -f $LOCK
}
trap 'cleanup; say "LOOP interrupted"; exit 130' INT
trap 'cleanup; say "LOOP terminated"; exit 143' TERM
trap 'cleanup' EXIT

# run_child <logfile> <command...>: background, PID in $CHILD, filtered live view, returns its status
run_child() {
  local logfile=$1; shift
  print -r -- "\$ $*" >> $logfile
  "$@" > >(tee -a $logfile | grep --line-buffered -v -E "$NOISE" | sed -l 's/^/   /') 2>&1 &
  CHILD=$!
  wait $CHILD
  local rc=$?
  CHILD=""
  sleep 0.3                                   # let tee/grep flush before the next LOOP line
  return $rc
}

guard() {
  sole_control && return 0
  say "LOOP ABORT before $1: the Air is not under this loop's sole control"
  exit 1
}

centre_pulse() {
  guard "planetcentre"
  local cl=$CLIPDIR/$(date '+%H%M%S')_clip${1}_centre.log
  run_child $cl python3 -u $HERE/planetcentre.py --east=$EAST --arcsec-per-px ${SCALE:-0.110}
  local rc=$?
  say "LOOP planetcentre exit $rc: $(grep -E 'pass [0-9]|NOT in' $cl | tail -1 | cut -c1-120)"
  if (( rc == 2 )); then
    guard "planetsearch"
    local sl=$CLIPDIR/$(date '+%H%M%S')_clip${1}_search.log
    say "LOOP planet not in the field -> defocused spiral search (log $sl)"
    EAF_MIN=40000 EAF_MAX=70000 run_child $sl python3 -u $HERE/planetsearch.py --eaf-focus $EAF_FOCUS --eaf-search $((EAF_FOCUS - 15000)) --rings 2 --centre --east=$EAST --step-ra ${STEP_RA:-9} --step-dec ${STEP_DEC:-12} --disc-radius ${DISC_R:-2295}
    say "LOOP planetsearch: $(grep -E 'FOUND|not found' $sl | tail -1 | cut -c1-120)"
    run_child $cl python3 -u $HERE/planetcentre.py --east=$EAST --arcsec-per-px ${SCALE:-0.110}
    rc=$?
    say "LOOP planetcentre after the search exit $rc"
  fi
  return $rc
}

reacquire() {
  if (( PULSE )); then centre_pulse $1; return; fi
  guard "cloudcheck"
  local cl=$CLIPDIR/$(date '+%H%M%S')_clip${1}_cloudcheck.log
  say "LOOP re-acquiring with cloudcheck (log $cl)"
  run_child $cl python3 -u $HERE/cloudcheck.py --ra $RA --dec $DEC --name Saturn --no-solve --search-ra 12 --search-dec 9 \
    --jacobian "$J_CHECK" --jacobian-pier east --every 60 --max-checks 3 --planet-peak 20000 --planet-area 30
  local rc=$?
  say "LOOP cloudcheck exit $rc: $(grep -E 'VERDICT|EXIT' $cl | tail -1 | cut -c1-150)"
}

typeset -a CHECKED                          # clips already verified: clipcheck must never take one for the new clip
typeset -A MEANING
MEANING=(0 "recorded, planet held" 1 "error" 2 "bad arguments" 3 "no planet before recording" 4 "too dim (cloud)" 5 "stopped: planet lost"
         6 "stopped: frames stalled" 130 "interrupted" 143 "terminated")

PULSE_ARGS=""; (( PULSE )) && PULSE_ARGS="--hold-mode pulse --east=$EAST"
EAF_FOCUS=${EAF_FOCUS:-60411}
say "LOOP start: PID $$, $CLIPS clips x $SECS s until $UNTIL, host $HOST, logs in $CLIPDIR"
for i in {1..$CLIPS}; do
  now=$(date '+%H%M')
  # minutes since noon, so an evening --until (2315) works as well as an after-midnight one (0320)
  # (2026-09-26: the old test "now > UNTIL && now < 1200" never fired for 2315 and clip 7 started at 23:16)
  nm=$(( (10#${now:0:2} * 60 + 10#${now:2:2} + 720) % 1440 )); um=$(( (10#${UNTIL:0:2} * 60 + 10#${UNTIL:2:2} + 720) % 1440 ))
  if (( nm >= um )); then say "LOOP past $UNTIL, stopping"; break; fi
  say "LOOP clip $i/$CLIPS, $SECS s"
  rc=1
  for try in 1 2 3; do
    guard "clip $i try $try"
    since=$(date '+%Y-%m-%d-%H%M%S')
    cl=$CLIPDIR/${since##*-}_clip${i}_try${try}_satvideo.log
    (( PULSE )) && centre_pulse $i
    run_child $cl python3 -u $HERE/satvideo.py --seconds $SECS --roi 1000 --exp-ms 40 --gain 350 --exp-max 100 --gain-max 450 \
      --jacobian "$J_VIDEO" --deadband 100 --min-gap 4 ${(z)PULSE_ARGS} ${(z)VIDEO_ARGS}
    rc=$?
    say "LOOP clip $i try $try: satvideo exit $rc (${MEANING[$rc]:-unknown}) ; log $cl"
    grep -E 'exposure test|ABORT|confirm|RECORDED|STOPPED|hold:|exposure used|Traceback|Error' $cl | tail -8 | sed 's/^/   /' >> $LOG   # already streamed above
    if (( rc == 2 )); then say "LOOP STOP: satvideo rejected its arguments (exit 2) -- fix the command, retrying cannot help"; exit 2; fi
    (( rc == 1 )) || break
    say "LOOP transient failure: $(grep -E 'FAILED after|Error|Traceback' $cl | tail -1 | cut -c1-110) -> $RETRY_PAUSE s pause"
    sleep $RETRY_PAUSE
  done

  verdict=""
  if (( rc == 0 || rc == 6 )); then
    ck=$CLIPDIR/${since##*-}_clip${i}_clipcheck.log
    say "LOOP checking the new clip in the file on the Air (log $ck)"
    excl=(); for c in $CHECKED; do excl+=(--exclude "$c"); done
    run_child $ck python3 -u $HERE/clipcheck.py --air --host $HOST --since $since $excl ${(z)CLIPCHECK_ARGS}   # e.g. "--roi 800": the frame size to assume when the Air has not rewritten the AVI header yet
    crc=$?
    clip=$(sed -n 's/^.* clip \(.*\.avi\)  [0-9.]* GB$/\1/p' $ck | tail -1)
    [[ -n $clip ]] && CHECKED+=("$clip")
    say "LOOP $(grep VERDICT $ck | tail -1 | sed 's/^.*VERDICT/VERDICT/' | cut -c1-160)"
    case $crc in
      0) verdict=planet;;
      3) verdict=partial;;
      2) say "LOOP STOP: clip $i is EMPTY in the file although satvideo exited $rc -- not recording blind"; exit 3;;
      *) say "LOOP STOP: could not verify clip $i on the Air (clipcheck exit $crc) -- not continuing unverified"; exit 4;;
    esac
  fi
  if (( MOONS_EVERY > 0 && i % MOONS_EVERY == 0 )) && [[ $verdict == planet ]]; then
    guard "satmoons"
    ml=$CLIPDIR/$(date '+%H%M%S')_clip${i}_satmoons.log
    say "LOOP moon set after clip $i (log $ml)"
    run_child $ml python3 -u $HERE/satmoons.py ${(z)MOONS_ARGS}
    say "LOOP moon set exit $?: $(grep -E 'stacked|wrote' $ml | tail -2 | tr '\n' ' ' | cut -c1-200)"
  fi
  if [[ $verdict != planet ]]; then
    reacquire $i
  fi
done
say "LOOP done"
