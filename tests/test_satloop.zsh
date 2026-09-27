#!/bin/zsh
# Exercise satloop.sh's control flow with stub children in a throwaway copy of the
# tree -- no Air, no network (the sole-control check runs against 127.0.0.1).
#
#   tests/test_satloop.zsh
#
# One stub stands in for satvideo.py, cloudcheck.py and clipcheck.py; each call
# takes the next exit code from codes.<name> and appends "<name> <code> <pid>" to calls.
HERE=${0:A:h}
T=$(mktemp -d ${TMPDIR:-/tmp}/satloop_test.XXXXXX)
mkdir -p $T/air-control/planetary $T/telemetry
cp $HERE/../planetary/satloop.sh $T/air-control/planetary/
cat > $T/stub.py <<'EOF'
import os, signal, sys, time
name = os.path.splitext(os.path.basename(sys.argv[0]))[0]
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))))
path = os.path.join(root, "codes." + name)
codes = open(path).read().split() if os.path.exists(path) else []
code = int(codes[0]) if codes else 0
open(path, "w").write("\n".join(codes[1:]))
def note(s):
    with open(os.path.join(root, "calls"), "a") as f:
        f.write(s + "\n")
note("%s %d %d" % (name, code, os.getpid()))
def term(sig, frm):
    print("%s: SIGTERM -> stop_record_avi (cleanup)" % name, flush=True)
    note("%s TERM %d" % (name, os.getpid()))
    sys.exit(143)
signal.signal(signal.SIGTERM, term)
print("%s: pid %d, will exit %d" % (name, os.getpid(), code), flush=True)
if name == "clipcheck":
    n = sum(1 for l in open(os.path.join(root, "calls")) if l.startswith("clipcheck "))
    ex = [sys.argv[i + 1] for i, v in enumerate(sys.argv) if v == "--exclude"]
    print("12:00:00    +2.0s  clip 2026-09-17-00000%d-Alpheratz-Bin1 -10.0C.avi  1.87 GB" % n, flush=True)
    print("excluded: %s" % " | ".join(ex), flush=True)
t = time.time()
while time.time() - t < float(os.environ.get("STUB_SECONDS", "0.3")):
    time.sleep(0.05)
print({"satvideo": {0: "RECORDED 2.0s ; planet in 10 of 10 fresh frames", 1: "Traceback (most recent call last):\nTimeoutError: timed out",
                    3: "ABORT: no planet in the window", 5: "STOPPED EARLY after 1.0s: planet lost"},
       "clipcheck": {0: "VERDICT PLANET: stub", 1: "VERDICT UNCHECKED: stub", 2: "VERDICT EMPTY: stub", 3: "VERDICT PARTIAL: stub"},
       "cloudcheck": {0: "VERDICT VISIBLE (check 1): stub"}}[name].get(code, "exit %d" % code), flush=True)
sys.exit(code)
EOF
cp $T/stub.py $T/air-control/planetary/satvideo.py; cp $T/stub.py $T/air-control/planetary/cloudcheck.py; cp $T/stub.py $T/air-control/planetary/clipcheck.py

LOOP=$T/air-control/planetary/satloop.sh
pass=0; fail=0
prep() {        # prep "<satvideo codes>" "<clipcheck codes>" "<cloudcheck codes>"
  rm -f $T/codes.*(N) $T/calls $T/out $T/telemetry/satloop.lock
  print -l ${=1} > $T/codes.satvideo; print -l ${=2} > $T/codes.clipcheck; print -l ${=3} > $T/codes.cloudcheck
}
calls() { [[ -f $T/calls ]] && awk '{print $1}' $T/calls | paste -sd' ' - ; }
check() {       # check <name> <exit> <wanted exit> <wanted call sequence or -> <text>...
  local name=$1 got=$2 want=$3 seq=$4; shift 4
  local why=""
  [[ $got == $want ]] || why+="exit $got, wanted $want; "
  [[ $seq == - || "$(calls)" == $seq ]] || why+="calls '$(calls)', wanted '$seq'; "
  for s in "$@"; do grep -qF -- "$s" $T/out || why+="missing '$s'; "; done
  [[ -f $T/telemetry/satloop.lock ]] && why+="lock file left behind; "
  if [[ -z $why ]]; then
    print "PASS  $name"; (( pass++ ))
  else
    print "FAIL  $name: $why"; (( fail++ )); sed 's/^/      | /' $T/out
  fi
}
run() { $LOOP --host 127.0.0.1 --log $T/loop.log --retry-pause 1 --seconds 2 --until 1159 "$@" > $T/out 2>&1; }   # --until 1159: the night-only stop rule never fires

prep "0 0" "0 0" ""
run --clips 2; check two-good-clips $? 0 "satvideo clipcheck satvideo clipcheck" "LOOP done" "VERDICT PLANET"

prep "0 0 0" "0 0 0" ""
run --clips 3; check later-checks-exclude-verified-clips $? 0 "satvideo clipcheck satvideo clipcheck satvideo clipcheck" \
  "excluded: 2026-09-17-000001-Alpheratz-Bin1 -10.0C.avi | 2026-09-17-000002-Alpheratz-Bin1 -10.0C.avi"

prep "0" "2" ""
run --clips 5; check empty-file-stops-the-loop $? 3 "satvideo clipcheck" "LOOP STOP: clip 1 is EMPTY"

prep "0" "1" ""
run --clips 5; check uncheckable-file-stops-the-loop $? 4 "satvideo clipcheck" "could not verify clip 1"

prep "5 0" "0" "0"
run --clips 2; check planet-lost-reacquires $? 0 "satvideo cloudcheck satvideo clipcheck" "stopped: planet lost" "re-acquiring with cloudcheck"

prep "3 0" "0" "0"
run --clips 2; check no-planet-reacquires $? 0 "satvideo cloudcheck satvideo clipcheck" "no planet before recording"

prep "2 0" "0" ""
run --clips 3; check usage-error-stops-at-once $? 2 "satvideo" "rejected its arguments"

prep "1 1 0" "0" ""
run --clips 1; check transient-error-retries $? 0 "satvideo satvideo satvideo clipcheck" "clip 1 try 3: satvideo exit 0" "transient failure"

prep "0 0" "3 0" "0"
run --clips 2; check partial-file-reacquires $? 0 "satvideo clipcheck cloudcheck satvideo clipcheck" "VERDICT PARTIAL"

# stopping the loop reaches the running child by PID
prep "0" "0" ""
STUB_SECONDS=30 $LOOP --host 127.0.0.1 --log $T/loop.log --seconds 30 --until 1159 --clips 1 > $T/out 2>&1 &
lp=$!
for n in {1..100}; do [[ -f $T/calls ]] && break; sleep 0.1; done
sleep 0.5
kill -TERM $lp; wait $lp; rc=$?
child=$(awk '$1 == "satvideo" && $2 != "TERM" {print $3; exit}' $T/calls)
check sigterm-reaches-child-by-pid $rc 143 "satvideo satvideo" "SIGTERM to child PID $child" "stop_record_avi (cleanup)" "LOOP terminated"
if [[ -n $child ]] && kill -0 $child 2>/dev/null; then print "FAIL  child PID $child survived the loop"; (( fail++ )); kill $child; fi

# a second loop is refused while the first runs
prep "0" "0" ""
STUB_SECONDS=30 $LOOP --host 127.0.0.1 --log $T/loop.log --seconds 30 --until 1159 --clips 1 > $T/out1 2>&1 &
lp=$!
for n in {1..100}; do [[ -f $T/calls ]] && break; sleep 0.1; done
$LOOP --host 127.0.0.1 --log $T/loop.log --seconds 2 --until 1159 --clips 1 > $T/out 2>&1; rc=$?
if [[ $rc == 1 ]] && grep -qF "another satloop.sh is running as PID $lp" $T/out && [[ "$(calls)" == "satvideo" ]]; then
  print "PASS  second-loop-refused"; (( pass++ ))
else
  print "FAIL  second-loop-refused: exit $rc, calls '$(calls)'"; (( fail++ )); sed 's/^/      | /' $T/out
fi
kill -TERM $lp; wait $lp

# something else on port 4400 of the "Air" (127.0.0.1): no child may start
prep "0" "0" ""
python3 -c '
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", 4400)); s.listen(1)
c = socket.create_connection(("127.0.0.1", 4400)); a = s.accept(); time.sleep(20)' &
sp=$!
sleep 1
run --clips 1; check foreign-process-blocks-every-child $? 1 "" "CONTROL FOREIGN PID $sp" "LOOP ABORT before clip 1 try 1"
kill $sp; wait $sp 2>/dev/null

print "\n$pass passed, $fail failed  (scratch tree $T)"
(( fail == 0 )) && rm -rf $T
exit $(( fail > 0 ))
