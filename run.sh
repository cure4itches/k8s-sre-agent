#!/usr/bin/env bash
# 사용법: ./run.sh setup | chaos <INC-x ...|reset>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
  setup) bash "${ROOT}/infra/setup.sh" ;;
  chaos) shift; bash "${ROOT}/infra/chaos.sh" "$@" ;;
  *) echo "사용법: $0 setup | chaos <INC-1..INC-7|INC-2B|reset>" >&2; exit 1 ;;
esac
