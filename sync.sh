#!/bin/bash
set -o nounset -o errexit
namespace="support"

sync_image() {
  skopeo sync --all --dest-tls-verify=false --src dir --dest docker "images/${1}/${2}" "localhost:5000/${1}/${3}"
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
    if [[ "$digest" == */* ]]; then
      sync_image "${host}" "${digest}" "${digest%/*}/"
    else
      sync_image "${host}" "${digest}" ""
    fi
    if [ "${tag}" != "null" ]; then
      tag_image "${host}" "${digest}" "${tag}"
    fi
  done < <(jq -rc 'to_entries | .[] | "\(.key) \(.value)"' images/mappings.json)

  jobs -p | xargs --no-run-if-empty kill
  trap -- EXIT
}

sync_helm() {
  pvc="helm-registry-data-pvc"

  helm_repo_data_dir="/var/lib/docker/volumes/minikube/_data/hostpath-provisioner/${namespace}/${pvc}"

  echo "Synching the helm charts repo to the helm-server pvc"
  rsync -v -aP --delete helm-chart-repo/ "$helm_repo_data_dir"
}

# Credit: https://stackoverflow.com/a/37840948
urldecode() {
  "${*//+/ }"
  echo -e "${_//%/\\x}"
}

sync_git() {
  pvc="git-server-data-pvc"

  git_repo_data_dir="/var/lib/docker/volumes/minikube/_data/hostpath-provisioner/${namespace}/${pvc}"
  repo="$(jq -r '.flux_repository.flux_repository' -r manifest.json | cut -f 3- -d / | cut -f 2 -d @)"
  git_repo_dir="${git_repo_data_dir}/$(urldecode "${repo}")"
  if [ ! -d "${git_repo_dir}" ]; then
    mkdir -p "${git_repo_dir}"
    git -C "${git_repo_dir}" init --bare
    chown -R 1000:1000 "${git_repo_data_dir}/$(cut -f1 -d / <<< "${repo}")"
  fi

  trap "jobs -p | xargs --no-run-if-empty kill" EXIT
  kubectl -n support port-forward service/git-server-service 1234:80 >/dev/null &
  sleep 1

  git -C flux push --force "http://localhost:1234/${repo}"

  jobs -p | xargs --no-run-if-empty kill
  trap -- EXIT
}

main() {
  sync_images
  sync_helm
  sync_git
}

main "$@"
