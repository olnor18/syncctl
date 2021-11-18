#!/bin/bash
set -o nounset -o errexit

# jq 'to_entries | map({key: .value|tostring, value: .key}) | from_entries' images/mappings.json

trap "jobs -p | xargs --no-run-if-empty kill" EXIT
kubectl -n support port-forward service/image-registry-service 5000:80 >/dev/null &
sleep 1

sync_image() {
  skopeo sync --all --dest-tls-verify=false --src dir --dest docker "images/${1}/${2}" "localhost:5000/${1}/${3%/*}/"
}

tag_image() {
  skopeo copy --dest-tls-verify=false --src-tls-verify=false "docker://localhost:5000/${1}/${2}" "docker://localhost:5000/${1}/${3}"
}

while read -r line; do
  host="$(cut -f1 -d / <<< "${line}")"
  digest="$(awk '{print $1}' <<< "${line}")"
  digest="${digest#$host/}"
  tag="$(awk '{print $2}' <<< "${line}")"
  tag="${tag#$host/}"
  sync_image "${host}" "${digest}" "${digest}"
  if [ "${tag}" != "null" ]; then
    tag_image "${host}" "${digest}" "${tag}"
  fi
done < <(jq -rc 'to_entries | .[] | "\(.key) \(.value)"' images/mappings.json)
