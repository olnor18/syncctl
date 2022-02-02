#!/bin/bash
set -o nounset -o errexit

sync_image() {
  skopeo sync --all --dest-tls-verify=false --src dir --dest docker "images/${1}/${2}" "localhost:5000/${1}/${3%/*}/"
}

tag_image() {
  skopeo copy --dest-tls-verify=false --src-tls-verify=false "docker://localhost:5000/${1}/${2}" "docker://localhost:5000/${1}/${3}"
}

sync_images() {
  trap "jobs -p | xargs --no-run-if-empty kill" EXIT
  kubectl -n support port-forward service/image-registry-service 5000:80 >/dev/null &
  sleep 1

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

  jobs -p | xargs --no-run-if-empty kill
  trap -- EXIT
}

sync_helm() {
  namespace="support"
  pvc="helm-registry-data-pvc"
  vol_name="$(kubectl -n ${namespace} get pvc ${pvc} -o jsonpath="{.spec.volumeName}")"

  helm_repo_data_dir="$(compgen -G "/var/lib/docker/volumes/*/_data/local-path-provisioner/${vol_name}_${namespace}_${pvc}")"

  echo "Synching the helm charts repo to the helm-server pvc"
  rsync -v -aP --delete helm-chart-repo/ "$helm_repo_data_dir"
}

sync_git() {
  trap "jobs -p | xargs --no-run-if-empty kill" EXIT
  kubectl -n support port-forward service/git-server-service 1234:80 >/dev/null &
  sleep 1

  git -C yggdrasil push --force http://localhost:1234/git-test-project.git

  jobs -p | xargs --no-run-if-empty kill
  trap -- EXIT
}


main() {
  sync_images
  sync_helm
  sync_git
}

main "$@"
