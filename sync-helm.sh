#!/bin/bash
# This sync script populates the helm-server with the data located in ~/helm-chart-repo
set -o nounset -o errexit

# There is only a single docker volume; the one used by KinD.
vol_id=$(docker volume list -q)
namespace="support"; pvc="helm-registry-data-pvc";
vol_name=$(kubectl -n ${namespace} get pvc ${pvc} -o jsonpath={.spec.volumeName}) 

pvc_location="/var/lib/docker/volumes/${vol_id}/_data/local-path-provisioner/${vol_name}_${namespace}_${pvc}"
helm_repo_location="distributed-technologies.github.io/helm-charts"
# In case there is a mistake in the above pvc location, or if it does not exist, 
# make sure to only create the dictories inside the pvc.
$(cd "${pvc_location}"; mkdir -p "${helm_repo_location}")
helm_repo_data_dir="${pvc_location}/${helm_repo_location}"

echo "Synching the helm repo in ~/helm-chart-repo to the helm-server pvc."
rsync -v -aP --delete ~/helm-chart-repo/ "$helm_repo_data_dir"
