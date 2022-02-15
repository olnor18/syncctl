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
    "-m", "--manifest", help="Specify manifest file to use", default="manifest.json"
)
subcommands = parser.add_subparsers(dest="subcommand")
ci_parser = subcommands.add_parser(
    "mirror-yggdrasil",
    help="mirror the yggdrasil git repository to the local fs",
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

def mirror_yggdrasil(yggdrasil_config: dict) -> None:
    if not Path("work/yggdrasil").is_dir():
        repo = git.Repo.clone_from(yggdrasil_config["repository"], "work/yggdrasil")
    else:
        repo = git.Repo('work/yggdrasil')
    if repo.head.object.hexsha != yggdrasil_config["commit"]:
        repo.git.fetch()
        repo.git.reset('--hard', yggdrasil_config["commit"])

def download_file(url: str, dest: str, hash: str = None) -> None:
    with requests.get(url) as r:
        r.raise_for_status()
        if hash is not None and hashlib.sha256(r.content).hexdigest() != hash:
            raise Exception("Hash mismatch")
        with open(dest, 'wb') as f:
            f.write(r.content)

def download_chart(name: str, version: str, repository: str) -> dict:
    with requests.get(f'{repository}/index.yaml') as r:
        r.raise_for_status()
        document = yaml.load(r.content, Loader=yaml.SafeLoader)
        for chart in document["entries"][name]:
            if chart["version"] == version:
                url = chart["urls"][0]
                debug(f"Downloading chart: {name}:{version}")
                if not (url.startswith("http://") or url.startswith("https://")):
                    url = f'{repository}/{url}'
                download_file(url, f"work/helm-chart-repo.tmp/{os.path.basename(url)}", chart["digest"])
                return {"chart": chart['name'], "version": chart['version'], "digest": chart["digest"]}
    raise Exception(f'Chart: {name}:{version} not found in {repository}')

def download_dependencies(chart: str) -> list[str]:
    dependencies = []
    f = open(f"work/yggdrasil/{chart}/Chart.yaml")
    index = yaml.load(f.read(), Loader=yaml.SafeLoader)
    for chart in index["dependencies"]:
        dependencies.append(download_chart(chart["name"], chart["version"], chart["repository"]))
    return dependencies

def mirror_charts(manifest: dict, manifest_file: str) -> None:
    if not Path("work/yggdrasil").is_dir():
        raise Exception('Please run mirror-yggdrasil before mirror-charts')
    for dir in ["work/helm-chart-repo.tmp", "work/yggdrasil/yggdrasil/charts"]:
        if Path(dir).is_dir():
            shutil.rmtree(dir)
        os.makedirs(dir)

    charts = []
    charts.extend(download_dependencies("nidhogg"))
    for dependency in download_dependencies("yggdrasil"):
        charts.append(dependency)
        shutil.copyfile(f"work/helm-chart-repo.tmp/{dependency['chart']}-{dependency['version']}.tgz", f"work/yggdrasil/yggdrasil/charts/{dependency['chart']}-{dependency['version']}.tgz")

    p = subprocess.run(["helm", "template", "--set=loadbalancer.ipRangeStart=127.0.0.1", "work/yggdrasil/yggdrasil"], capture_output=True)
    if p.returncode != 0:
        raise Exception(f'Error templating yggdrasil, error: {p.stderr}')
    documents = yaml.load_all(p.stdout, Loader=yaml.SafeLoader)
    for document in documents:
        if document["apiVersion"] == "argoproj.io/v1alpha1" and document["kind"] == "Application" and 'chart' in document["spec"]["source"]:
            source = document["spec"]["source"]
            chart = download_chart(source["chart"], source["targetRevision"], source["repoURL"])
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
    else:
        image["image"] = image_reference[image_reference.index("/")+1:image_reference.index(":")]
        image["tag"] = image_reference[image_reference.index(":")+1:]
    return image

def resolve_images(manifest: dict, manifest_file: str) -> None:
    if not Path("work/helm-chart-repo").is_dir():
        raise Exception('Please run run mirror-charts before resolve-images')

    helm_config = manifest.get("helm", {})
    images = {}
    if "extra_images" in helm_config:
        for image in helm_config["extra_images"]:
            if image not in images:
                images[image] = process_image(image)
    for m in template_charts(helm_config.get("api_versions", []), helm_config.get("values", {})):
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

def tar() -> None:
    name = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    with tarfile.open(f"{name}.tar", "w") as tar:
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

def main() -> None:
    args = parser.parse_args()

    manifest_file = args.manifest
    manifest = load_manifest(manifest_file)
    os.makedirs("work", exist_ok=True)

    if args.verbose:
        basicConfig(level=DEBUG)

    if "mirror-yggdrasil" == args.subcommand:
        mirror_yggdrasil(manifest["yggdrasil_repository"])
    elif "mirror-charts" == args.subcommand:
        mirror_charts(manifest, manifest_file)
    elif "mirror-images" == args.subcommand:
        mirror_images(manifest, manifest_file, args.incremental)
    elif "resolve-images" == args.subcommand:
        resolve_images(manifest, manifest_file)
    elif "tar" == args.subcommand:
        tar()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
