#!/usr/bin/env bash
# Opens Grafana on the mon host at http://localhost:3000 through SSH (Grafana is
# never exposed to the internet). Ctrl-C closes the tunnel.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_tools
load_outputs
[[ "${Profile:-}" == "full" ]] || die "the minimal profile has no mon host (cloud/up.sh --profile full)"
echo "Grafana: http://localhost:3000  (anonymous viewing works; admin password: $(ssh_to mon 'cat ~/grafana-admin-password'))"
echo "Dashboards: 'L4 Maglev Load Balancer — AWS' and 'L4 Maglev — AWS as referee'. Ctrl-C to close."
ssh "${SSH_OPTS[@]}" -o ControlMaster=no -o ControlPath=none -N \
    -L 3000:127.0.0.1:3000 "ubuntu@$(public_ip mon)"
