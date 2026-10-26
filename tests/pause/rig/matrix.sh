#!/usr/bin/env bash
# Run every pause, resume and cancel scenario against one rig, one after another.
#
#   RIG=<rig dir> RIG_PORT=<port> ./matrix.sh grbl|fluidnc <label> <evidence dir>
#
# <label> names this run (for example the OctoPrint version) and ends up in the evidence file names.  Each scenario's
# output is written to <evidence dir>/<scenario>_<label>.txt; `python3 digest.py <evidence dir>` summarises them.
# The rig's OctoPrint must already be running with the plugin and octoprint_grblsim.py installed (about 25 minutes).
set -euo pipefail
side=${1:?grbl or fluidnc}; label=${2:?label}; evidence=${3:?evidence dir}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
: "${RIG:?}" "${RIG_PORT:?}"
mkdir -p "$evidence"
export EVIDENCE="$evidence" LABEL="$label"

if [ "$side" = grbl ]; then
  port=GRBL_SIM; p=G
  park='{"grbl_parking":true,"grbl_homing":true}'; nodetect='{"grbl_homing":true}'; nohoming='{"grbl_parking":true}'
  plain='{}'; laser='{"grbl_parking":true,"grbl_homing":true,"grbl_laser":true}'; spin=$park
else
  port=FLUIDNC_SIM; p=F
  park='{"fluidnc_spindle":"pwm","fluidnc_homing":"XYZ","fluidnc_parking":true}'
  nodetect='{"fluidnc_spindle":"pwm","fluidnc_homing":"XYZ"}'; nohoming='{"fluidnc_spindle":"pwm","fluidnc_parking":true}'
  plain='{"fluidnc_spindle":"pwm"}'; laser='{"fluidnc_homing":"XYZ","fluidnc_parking":true}'
  spin='{"fluidnc_spindle":"pwm","fluidnc_homing":"XYZ","fluidnc_parking":true,"fluidnc_spinup_ms":2000}'
fi
on='{"pauseParking":true}'; hold='{"pauseMode":"hold"}'

run() { name=$1; shift; (cd "$here" && python3 scenarios.py "$@" > "$evidence/${name}_${label}.txt" 2>&1); echo "$name done"; }

run "${p}_park_on"        scenario "${p}_park_on" "port=$port" "sim=$park" "plugin=$on"
run "${p}_park_nodetect"  scenario "${p}_park_nodetect" "port=$port" "sim=$nodetect" "plugin=$on"
run "${p}_park_nohoming"  scenario "${p}_park_nohoming" "port=$port" "sim=$nohoming" "plugin=$on"
run "${p}_drain"          scenario "${p}_drain" "port=$port" "sim=$plain"
run "${p}_drain_inch"     scenario "${p}_drain_inch" "port=$port" "sim=$plain" gfile=pk_inch.gcode pre=10
run "${p}_hold"           scenario "${p}_hold" "port=$port" "sim=$plain" "plugin=$hold"
run "${p}_laser"          scenario "${p}_laser" "port=$port" "sim=$laser" "plugin=$on"
run "${p}_park_zabove"    scenario "${p}_park_zabove" "port=$port" "sim=$park" "plugin=$on" gfile=pk_cut_z3.gcode
run "${p}_park_spinup"    scenario "${p}_park_spinup" "port=$port" "sim=$spin" "plugin=$on" gfile=pk_cut_s10k.gcode
run "C_${p}_run"          cancel_scenario "C_${p}_run" run "port=$port" "sim=$plain"
run "C_${p}_pause_drain"  cancel_scenario "C_${p}_pause_drain" pause "port=$port" "sim=$plain"
run "C_${p}_pause_hold"   cancel_scenario "C_${p}_pause_hold" pause "port=$port" "sim=$plain" "plugin=$hold"
run "C_${p}_pause_park"   cancel_scenario "C_${p}_pause_park" pause "port=$port" "sim=$park" "plugin=$on"
run "C_${p}_retract_park" cancel_scenario "C_${p}_retract_park" retract "port=$port" "sim=$park" "plugin=$on"
run "C_${p}_restore_park" cancel_scenario "C_${p}_restore_park" restore "port=$port" "sim=$park" "plugin=$on"
run "${p}_early_resume"   early_resume "${p}_early_resume" "port=$port" "sim=$park" "plugin=$on"
(cd "$here" && python3 scenarios.py reset > "$evidence/reset_${label}.txt" 2>&1)
