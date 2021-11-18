#!/bin/bash
set -o nounset -o errexit

repo_folder="${HOME}/RTP-dev"

trap "jobs -p | xargs --no-run-if-empty kill" EXIT
kubectl -n support port-forward service/git-server-service 1234:80 >/dev/null & 
sleep 1

cd "${repo_folder}"
git push http://localhost:1234/git-test-project.git



