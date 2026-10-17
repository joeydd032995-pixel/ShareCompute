#!/bin/bash
# usage: gate.sh NAME [extra coordinator args...]   (env SC_PARALLEL_LOAD is inherited)
S=/tmp/claude-0/-home-user-ShareCompute/e401796b-35ef-50f7-9cbc-9a4f7927fdf6/scratchpad
name=${1:?name}; shift
rm -rf "${S:?}/gate-${name:?}"
cd /home/user/ShareCompute
timeout 1500 python3 scripts/split_cluster.py loopback \
  --worker-binary /tmp/claude-0/sc-build/bin/sc-rpc-worker --probe-binary /tmp/claude-0/sc-build/bin/sc-split-probe \
  --model models/split-capacity.gguf --out "$S/gate-$name" --tokens 16 --timeout 1200 \
  --reference native/split/reference-capacity.json "$@" >"$S/gate-$name.log" 2>&1
echo "$name exit $?"
