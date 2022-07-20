#!/usr/bin/env python3
import yaml
import json
import requests
import git
import os
import hashlib
import subprocess
import shutil
import tarfile
import datetime
import tempfile
import collections
import re
import itertools
import sys
from pathlib import Path
from argparse import ArgumentParser
from logging import basicConfig
from logging import DEBUG
from logging import debug
from typing import Generator

parser = ArgumentParser()
parser.add_argument(
    "-v", "--verbose", action="store_true", help="Causes to print debugging messages about the progress"
)
parser.add_argument(
    "-c", "--config", help="Specify config file to use", default="config.json"
)
parser.add_argument(
    "-m", "--manifest", help="Specify manifest file to use", default="manifest.json"
)
subcommands = parser.add_subparsers(dest="subcommand")
ci_parser = subcommands.add_parser(
    "mirror-flux",
    help="mirror the flux git repository to the local fs",
)
ci_parser = subcommands.add_parser(
    "mirror-charts",
    help="mirror the charts to the local fs",
)
ci_parser = subcommands.add_parser(
    "mirror-images",
    help="mirror the container images to the local fs",
)
ci_parser.add_argument('-i', "--incremental", action='store_true',
                       help='Only mirror images that haven\'t been mirrored in a previous run with -i')
ci_parser = subcommands.add_parser(
    "resolve-images",
    help="resolve container images to digest and update the manifest file",
)
ci_parser = subcommands.add_parser(
    "tar",
    help="create a tarball",
)

def mirror_flux(manifest: str, manifest_file: str, flux_config: dict) -> None:
    if not Path("work/flux").is_dir():
        repo = git.Repo.clone_from(flux_config["repository"], "work/flux")
    else:
        repo = git.Repo('work/flux')
    if "branch" in flux_config:
        repo.git.fetch()
        repo.git.reset('--hard', f"origin/{flux_config['branch']}")
    elif repo.head.object.hexsha != flux_config["commit"]:
        repo.git.fetch()
        repo.git.reset('--hard', flux_config["commit"])

    manifest["flux_repository"] = flux_config
    manifest["flux_repository"]["commit"] = repo.head.object.hexsha
    save_manifest(manifest, manifest_file)

def download_file(url: str, dest: str, hash: str = None) -> None:
    with requests.get(url) as r:
        r.raise_for_status()
        if hash is not None and hashlib.sha256(r.content).hexdigest() != hash:
            raise Exception("Hash mismatch")
        with open(dest, 'wb') as f:
            f.write(r.content)

def download_chart(name: str, version: str, repository: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdirname:
        env = {
            "HELM_CACHE_HOME": f'{tmpdirname}',
            "HELM_CONFIG_HOME": f'{tmpdirname}'
        }

        p = subprocess.run(["helm", "repo", "add", "tmp", repository], capture_output=True, env=env)
        if p.returncode != 0:
            raise Exception(f'Error adding Helm repository: {repository}, error: {p.stderr}')

        p = subprocess.run(["helm", "search", "repo", f'tmp/{name}', "--version", version, "--output", "json"], capture_output=True, text=True, env=env)
        if p.returncode != 0:
            raise Exception(f'Error searching Helm repository: {repository}, error: {p.stderr}')

        charts = json.loads(p.stdout)
        chart = next((chart for chart in charts if chart.get('name') == f'tmp/{name}'), None)
        if len(charts) == 0 or chart == None:
            raise Exception(f'Chart: {name}:{version} not found in {repository}')

        debug(f"Resolved chart {name} version {version} to {chart.get('version')}")

        version = chart.get('version')
        with open(f'{tmpdirname}/repository/tmp-index.yaml') as f:
            document = yaml.load(f, Loader=yaml.SafeLoader)
            for chart in document["entries"][name]:
                if chart["version"] == version:
                    url = chart["urls"][0]
                    debug(f"Downloading chart: {name}:{version}")
                    if not (url.startswith("http://") or url.startswith("https://")):
                        url = f'{repository}/{url}'
                    download_file(url, f"work/helm-chart-repo.tmp/{os.path.basename(url)}", chart["digest"])
                    return {"chart": chart['name'], "version": chart['version'], "digest": chart["digest"]}

    raise Exception(f'Chart: {name}:{version} not found in {repository}')

def template_flux(root_dir: str, git_repo: str, dir: str, git_repos: dict = collections.defaultdict(dict)) -> str:
    p = subprocess.run(["kubectl", "kustomize", dir], capture_output=True, text=True)
    if p.returncode != 0:
        raise Exception(f'Error templating flux, dir: {dir}, error: {p.stderr}')

    # Return early if no manifest was outputted
    if p.stdout == '':
        return p.stdout

    kustomizations = []

    # https://regex101.com/r/AHQNR4/1
    pattern = re.compile('^(https?|ssh)://(.*@)?([^/:]*)[^/]*(.*)$')
    git_repo = pattern.sub(r'\3\4', git_repo)

    documents = yaml.load_all(p.stdout, Loader=yaml.SafeLoader)
    for document in documents:
        if document["kind"] == "GitRepository":
            repo = document.get('spec').get('url')
            repo = pattern.sub(r'\3\4', repo)
            if repo == git_repo:
                metadata = document.get('metadata')
                git_repos[metadata.get('namespace')][metadata.get('name')] = repo
        elif document["kind"] == "Kustomization":
            kustomizations.append(document)

    manifests = p.stdout
    for kustomization in kustomizations:
        spec = kustomization.get('spec')
        source_ref = spec.get('sourceRef')
        namespace = kustomization.get('metadata').get('namespace')
        git_name = source_ref.get('name')
        git_namespace = source_ref.get('namespace', namespace)
        if git_namespace in git_repos and git_name in git_repos.get(git_namespace):
            new_dir = os.path.normpath(f"{root_dir}/{spec.get('path')}")
            # Prevents looping
            if new_dir == dir:
                continue
            new_manifests = template_flux(root_dir, git_repo, new_dir, git_repos)
            if new_manifests != '':
                manifests += '---\n'
                manifests += new_manifests

    return manifests

def mirror_charts(config: dict, manifest: dict, manifest_file: str) -> None:
    if not Path("work/flux").is_dir():
        raise Exception('Please run mirror-flux before mirror-charts')
    for dir in ["work/helm-chart-repo.tmp", "work/flux/flux/charts"]:
        if Path(dir).is_dir():
            shutil.rmtree(dir)
        os.makedirs(dir)

    manifests = template_flux("work/flux", config["flux_repository"]["repository"], "work/flux/" + config["flux_repository"]["entrypoint"])

    helm_repos = collections.defaultdict(dict)
    helm_charts = []

    documents = yaml.load_all(manifests, Loader=yaml.SafeLoader)
    for document in documents:
        if document["kind"] == "HelmRepository":
            metadata = document.get('metadata')
            helm_repos[metadata.get('namespace')][metadata.get('name')] = document.get('spec').get('url')
        elif document["kind"] == "HelmRelease":
            chart_spec = document.get('spec').get('chart').get('spec')
            values = document.get('spec').get('values')
            helm_charts.append({
                "chart": chart_spec.get('chart'),
                "version": chart_spec.get('version'),
                "helm_repo": {
                    "namespace": chart_spec.get('sourceRef').get('namespace'),
                    "name": chart_spec.get('sourceRef').get('name'),
                },
                "values": values
            })

    charts = []
    for chart in helm_charts:
        helm_repo_namespace = chart.get('helm_repo').get('namespace')
        helm_repo_name = chart.get('helm_repo').get('name')
        helm_repo = helm_repos.get(helm_repo_namespace).get(helm_repo_name)
        chart = download_chart(chart.get('chart'), str(chart.get('version') or ''), helm_repo)
        charts.append(chart)

    p = subprocess.run(["helm", "repo", "index", "work/helm-chart-repo.tmp"], capture_output=True)
    if p.returncode != 0:
        raise Exception(f'Error generating chart repository index, error: {p.stderr}')

    if Path('work/helm-chart-repo').is_dir():
        shutil.rmtree("work/helm-chart-repo")
    os.rename("work/helm-chart-repo.tmp", "work/helm-chart-repo")

    if 'charts' in manifest:
        for new_chart in charts:
            # FIXME: This isn't scalable
            for chart in manifest["charts"]:
                if new_chart["chart"] == chart["chart"] and new_chart["version"] == chart["version"]:
                    if new_chart["digest"] != chart["digest"]:
                        raise Exception(f"Digest mismatch for chart: {new_chart['chart']}:{new_chart['version']}, got: {new_chart['digest']}, expected: {chart['digest']}")
                    break
    manifest["charts"] = charts
    save_manifest(manifest, manifest_file)

def mirror_image(image: str) -> None:
    if Path(f'work/images.tmp/{image}').is_dir():
        return
    if Path(f'work/images/{image}').is_dir():
        debug(f"Reusing existing image: {image}")
        Path(f"work/images.tmp/{image[0:image.rindex('/')]}").mkdir(parents=True, exist_ok=True)
        shutil.copytree(f'work/images/{image}', f'work/images.tmp/{image}')
        return
    debug(f"Pulling image: {image}")
    p = subprocess.run(["skopeo", "sync", "--all", "--scoped", "--src", "docker", "--dest", "dir", image, "work/images.tmp"], capture_output=True)
    if p.returncode != 0:
        raise Exception(f'Error syncing image: {image}, error: {p.stderr}')

def mirror_images(manifest: dict, manifest_file: str, incremental: bool) -> None:
    os.makedirs("work/images", exist_ok=True)
    if Path('work/images.tmp').is_dir():
        shutil.rmtree("work/images.tmp")
    os.makedirs("work/images.tmp")

    digest_tag_mapping = {}
    if 'images' not in manifest:
        raise Exception('Please run run resolve-images before mirror-images')
    images = manifest["images"]
    for i, image in enumerate(images):
        if incremental and "skip" in image and image["skip"]:
            continue
        elif incremental:
            images[i]["skip"] = True
        image_name_digest = f'{image["registry"]}/{image["image"]}@{image["digest"]}'
        if "tag" in image:
            image_name_tag = f'{image["registry"]}/{image["image"]}:{image["tag"]}'
            digest_tag_mapping[image_name_digest] = image_name_tag
        else:
            digest_tag_mapping[image_name_digest] = None
        mirror_image(image_name_digest)
    with open("work/images.tmp/mappings.json", "w") as f:
        json.dump(digest_tag_mapping, f, indent=4, sort_keys=True)

    if Path('work/images').is_dir():
        shutil.rmtree("work/images")
    os.rename("work/images.tmp", "work/images")
    save_manifest(manifest, manifest_file)

def template_charts(api_versions: list[str], values: dict[str, str]) -> Generator[int, None, None]:
    manifests = []
    base_args = ["helm", "template"]
    for api_version in api_versions:
        base_args += ["--api-versions", api_version]
    for k, v in values.items():
        base_args += [f'--set={k}={v}']
    for chart in list(Path('.').glob("work/helm-chart-repo/*.tgz")):
        debug(f"Templating chart: {chart.name}")
        p = subprocess.run(base_args + [chart], capture_output=True)
        if p.returncode != 0:
            if not "library charts are not installable" in str(p.stderr):
                raise Exception(f'Error templating chart: {chart}, error: {p.stderr}')
        yield p.stdout

def extract_images(k8s_manifests: str) -> list[str]:
    images = []
    for d in yaml.load_all(k8s_manifests, Loader=yaml.SafeLoader):
        # TODO: Handle kind: list + cephVersion
        if d is None:
            continue
        if d["kind"] == "CronJob":
            d["spec"] = d["spec"]["jobTemplate"]["spec"]
        if d["kind"] in ["Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicationController"]:
            containers = d["spec"]["template"]["spec"]["containers"]
            if "initContainers" in d["spec"]["template"]["spec"]:
                containers += d["spec"]["template"]["spec"]["initContainers"]
            images += [container["image"] for container in containers]

    # Special case Docker Hub
    # https://github.com/containers/image/blob/1895e312af410ccdee5efa44e5223ec93ae76001/docs/containers-transports.5.md#dockerdocker-reference
    for i, v in enumerate(images):
        if "/" not in v:
            images[i] = "docker.io/library/" + v
        else:
            s = v.split("/")
            if not ("." in s[0] or ":" in s[0] or "localhost" == s[0]):
                images[i] = "docker.io/" + v
    return images

def resolve_image(image: str) -> str:
    p = subprocess.run(["skopeo", "inspect", "--raw", f'docker://{image}'], capture_output=True)
    if p.returncode != 0:
        raise Exception(f'Error inspecting image: {image}, error: {p.stderr}')

    manifest = json.loads(p.stdout)
    # https://github.com/opencontainers/image-spec/blob/43a7dee1ec31e0ad091d2dc93f6ada1392fba587/image-index.md
    if (manifest["mediaType"] == "application/vnd.docker.distribution.manifest.list.v2+json" or
        manifest["mediaType"] == "application/vnd.oci.image.index.v1+json"):
        debug(f"Found images index for: {image}")
        for m in manifest["manifests"]:
            if (m["platform"]["architecture"] == "amd64" and
                m["platform"]["os"] == "linux"):
                return m["digest"]
        raise Exception(f'Error finding amd64 image: {image}')
    else:
        return f"sha256:{hashlib.sha256(p.stdout).hexdigest()}"

def process_image(image_reference: str) -> dict:
    debug(f"Processing image: {image_reference}")
    if "@" in image_reference:
        digest = image_reference[image_reference.index("@")+1:]
        # "Docker references with both a tag and digest are currently not supported"
        if image_reference.index(":") < image_reference.index("@"):
            image_reference = image_reference[:image_reference.index("@")]
    else:
        digest = resolve_image(image_reference)

    debug(f"Resolved image {image_reference} to {digest}")
    image = {
        "registry": image_reference.split("/")[0],
        "digest": digest
    }
    if "@" in image_reference:
        image["image"] = image_reference[image_reference.index("/")+1:image_reference.index("@")]
    elif ":" not in image_reference:
        image["image"] = image_reference[image_reference.index("/")]
        image["tag"] = "latest"
    else:
        image["image"] = image_reference[image_reference.index("/")+1:image_reference.index(":")]
        image["tag"] = image_reference[image_reference.index(":")+1:]
    return image

def resolve_images(config: dict, manifest: dict, manifest_file: str) -> None:
    if not Path("work/helm-chart-repo").is_dir():
        raise Exception('Please run run mirror-charts before resolve-images')

    helm_config = config.get("helm", {})
    images = {}
    if "extra_images" in helm_config:
        for image in helm_config["extra_images"]:
            if image not in images:
                images[image] = process_image(image)

    manifests = template_flux("work/flux", config["flux_repository"]["repository"], "work/flux/" + config["flux_repository"]["entrypoint"])
    for m in itertools.chain(iter([manifests]), template_charts(helm_config.get("api_versions", []), helm_config.get("values", {}))):
        for image in extract_images(m):
            if image not in images:
                images[image] = process_image(image)

    images = list(images.values())
    for image in manifest.get("images", []):
        # FIXME: This isn't scalable
        if "skip" in image and image["skip"]:
            image.pop('skip')
            for new_image in images:
                if image == new_image:
                    new_image["skip"] = True
                    break
    manifest["images"] = images
    save_manifest(manifest, manifest_file)

def tar(manifest_file: str) -> None:
    name = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    with tarfile.open(f"{name}.tar", "w") as tar:
        tar.add(manifest_file, arcname=f"{name}/manifest.json")
        tar.add("sync.sh", arcname=f"{name}/sync.sh")
        tar.add("work", arcname=name)
    print(f"Created {name}.tar")

def save_manifest(manifest: dict, manifest_file: str) -> dict:
    with open(f"{manifest_file}.tmp", "w") as f:
        json.dump(manifest, f, indent=4, sort_keys=True)
        f.write('\n')
    os.rename(f"{manifest_file}.tmp", manifest_file)

def load_manifest(manifest_file: str) -> dict:
    with open(manifest_file) as f:
        return json.load(f)

def load_config(config_file: str) -> dict:
    with open(config_file) as f:
        return json.load(f)

def main() -> None:
    args = parser.parse_args()

    config_file = args.config
    manifest_file = args.manifest

    try:
        config = load_config(config_file)
    except FileNotFoundError:
        print('config.json not found, please use the -c option to specify the config file')
        sys.exit(1)
    try:
        manifest = load_manifest(manifest_file)
    except FileNotFoundError:
        manifest = {}

    os.makedirs("work", exist_ok=True)

    if args.verbose:
        basicConfig(level=DEBUG)

    if "mirror-flux" == args.subcommand:
        mirror_flux(manifest, manifest_file, config["flux_repository"])
    elif "mirror-charts" == args.subcommand:
        mirror_charts(config, manifest, manifest_file)
    elif "mirror-images" == args.subcommand:
        mirror_images(manifest, manifest_file, args.incremental)
    elif "resolve-images" == args.subcommand:
        resolve_images(config, manifest, manifest_file)
    elif "tar" == args.subcommand:
        tar(manifest_file)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
