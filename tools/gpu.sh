#!/usr/bin/env bash
#
# gpu.sh — the single GPU runner for sliceme campaigns.
#
# Only the verifier is given this tool. Workers never touch the GPU; T0 CPU
# remains the inner development loop (docs/orchestration.md §8).
#
# It wraps a command with:
#   * an exclusive flock, so two verifiers never share the device;
#   * a foreign-process gate, so an unrelated training job is not disturbed;
#   * a per-tier timeout (T1 short, T2 long).
#
# Usage:
#   tools/gpu.sh [--tier T1|T2] [--wait SECONDS] -- COMMAND [ARGS...]
#   tools/gpu.sh --tier T1 -- bazel test //dev/kernels:rms_norm_fd_test
#
# Environment:
#   SLICEME_GPU_LOCK          lock file (default: ${TMPDIR:-/tmp}/sliceme-gpu.lock)
#   SLICEME_GPU_ALLOW_FOREIGN set to 1 to run even when foreign processes hold the GPU
#   SLICEME_GPU_T1_TIMEOUT    T1 timeout seconds (default 900)
#   SLICEME_GPU_T2_TIMEOUT    T2 timeout seconds (default 3600)
set -euo pipefail

TIER="T1"
WAIT_SECONDS="${SLICEME_GPU_WAIT:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier) TIER="${2:?--tier needs a value}"; shift 2 ;;
    --tier=*) TIER="${1#*=}"; shift ;;
    --wait) WAIT_SECONDS="${2:?--wait needs a value}"; shift 2 ;;
    --wait=*) WAIT_SECONDS="${1#*=}"; shift ;;
    --) shift; break ;;
    *) break ;;
  esac
done

case "$TIER" in
  T1) TIMEOUT="${SLICEME_GPU_T1_TIMEOUT:-900}" ;;
  T2) TIMEOUT="${SLICEME_GPU_T2_TIMEOUT:-3600}" ;;
  *) echo "gpu.sh: unknown tier '$TIER' (want T1 or T2)" >&2; exit 2 ;;
esac

if [[ $# -eq 0 ]]; then
  echo "gpu.sh: no command given" >&2
  exit 2
fi

LOCK="${SLICEME_GPU_LOCK:-${TMPDIR:-/tmp}/sliceme-gpu.lock}"
mkdir -p "$(dirname "$LOCK")"

exec 9>"$LOCK"
if ! flock -w "$WAIT_SECONDS" 9; then
  echo "gpu.sh: GPU busy (another verifier holds $LOCK)" >&2
  exit 75 # EX_TEMPFAIL
fi

# Foreign-process gate: refuse to share the device with a process we did not
# start, unless explicitly allowed.  nvidia-smi is optional; skip the gate when
# it (or the driver) is unavailable.
if [[ "${SLICEME_GPU_ALLOW_FOREIGN:-0}" != "1" ]] && command -v nvidia-smi >/dev/null 2>&1; then
  FOREIGN="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null \
    | tr -d ' ' | grep -E '^[0-9]+$' | grep -v -x "$$" || true)"
  if [[ -n "$FOREIGN" ]]; then
    echo "gpu.sh: foreign process(es) on the GPU: $FOREIGN" >&2
    echo "gpu.sh: set SLICEME_GPU_ALLOW_FOREIGN=1 to override" >&2
    exit 75
  fi
fi

echo "gpu.sh: tier=$TIER timeout=${TIMEOUT}s lock=$LOCK" >&2

# ``timeout`` is coreutils; fall back to running without it on Minimal systems.
if command -v timeout >/dev/null 2>&1; then
  timeout --signal=TERM --kill-after=30 "$TIMEOUT" "$@"
  STATUS=$?
else
  "$@"
  STATUS=$?
fi

if [[ $STATUS -eq 124 || $STATUS -eq 137 ]]; then
  echo "gpu.sh: command exceeded the ${TIER} timeout (${TIMEOUT}s)" >&2
fi
exit $STATUS
