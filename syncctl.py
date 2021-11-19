#!/usr/bin/env python3
import yaml
import json
import requests
import git
import os
import hashlib
import subprocess
import shutil
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
subcommands = parser.add_subparsers(dest="subcommand")
ci_parser = subcommands.add_parser(
    "mirror-yggdrasil",
    help="mirror the yggdrasil git repository to the local fs",
)
ci_parser = subcommands.add_parser(
    "mirror-helm",
    help="mirror the helm git repository to the local fs and download all charts",
)
ci_parser = subcommands.add_parser(
    "mirror-images",
    help="mirror the container images to the local fs",
)
ci_parser = subcommands.add_parser(
    "resolve-images",
    help="resolve container images to digest and update the manifest file",
)

def mirror_yggdrasil(c: dict) -> None:
    if not Path("work/yggdrasil").is_dir():
        repo = git.Repo.clone_from(c["repository"], "work/yggdrasil", multi_options=["--bare"])
    else:
        repo = git.Repo('work/yggdrasil')
    if repo.head.object.hexsha != c["commit"]:
        ref = git.SymbolicReference.from_path(repo, "HEAD").ref.path
        repo.git.fetch()
        repo.git.update_ref(ref, c["commit"])

def download_file(url: str, dest: str, hash: str) -> None:
    with requests.get(url) as r:
        r.raise_for_status()
        if hashlib.sha256(r.content).hexdigest() != hash:
            raise Exception("Hash mismatch")
        with open(dest, 'wb') as f:
            f.write(r.content)

def mirror_helm(c: dict) -> None:
    if not Path("work/helm-git-repo").is_dir():
        debug(f"Cloning Helm git repository: {c['repository']}")
        repo = git.Repo.clone_from(c["repository"], "work/helm-git-repo")
    else:
        repo = git.Repo('work/helm-git-repo')
    if repo.head.object.hexsha != c["commit"]:
        debug(f"Helm git repository out-of-date. Got {repo.head.object.hexsha}, expected {c['commit']}")
        if Path('work/helm-chart-repo').is_dir():
            shutil.rmtree("work/helm-chart-repo")
        repo.remotes.origin.fetch()
        repo.head.reference = c["commit"]
        repo.head.reset(index=True, working_tree=True)
    else:
        debug(f"Helm git repository is up-to-date")

    if Path("work/helm-chart-repo.tmp").is_dir():
        shutil.rmtree("work/helm-chart-repo.tmp")

    if Path("work/helm-chart-repo").is_dir():
        return

    os.makedirs("work/helm-chart-repo.tmp")

    f = open("work/helm-git-repo/index.yaml")
    index = yaml.load(f.read(), Loader=yaml.SafeLoader)
    f.close()
    for k, v in index["entries"].items():
        for c in v:
            debug(f"Mirroring chart: {c}")
            url = c["urls"][0]
            # edit the index in place to set a relative url
            c["urls"][0] = os.path.basename(url)
            file = "work/helm-chart-repo.tmp/" + os.path.basename(url)
            hash = c["digest"]
            download_file(url, file, hash)
    with open("work/helm-chart-repo.tmp/index.yaml", "w") as f:
        yaml.dump(index, f)
    os.rename("work/helm-chart-repo.tmp", "work/helm-chart-repo")

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

def mirror_images(c: dict) -> None:
    os.makedirs("work/images", exist_ok=True)
    if Path('work/images.tmp').is_dir():
        shutil.rmtree("work/images.tmp")
    os.makedirs("work/images.tmp")

    digest_tag_mapping = {}
    for image in c:
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

def template_charts(api_versions: list[str], skip_charts: list[str], values: dict[str, str]) -> Generator[int, None, None]:
    manifests = []
    base_args = ["helm", "template"]
    for api_version in api_versions:
        base_args += ["--api-versions", api_version]
    for k, v in values.items():
        base_args += [f'--set={k}={v}']
    for chart in list(Path('.').glob("work/helm-chart-repo/*.tgz")):
        if chart.name in skip_charts:
            debug(f"Skipping chart: {chart.name}")
            continue
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
    p = subprocess.run(["skopeo", "inspect", f'docker://{image}'], capture_output=True)
    if p.returncode != 0:
        raise Exception(f'Error inspecting image: {image}, error: {p.stderr}')

    return json.loads(p.stdout)["Digest"]

def resolve_images(c: dict, manifest: dict) -> None:
    images = {}
    for m in template_charts(c["api_versions"], c["skip_charts"], c["values"]):
        for image in extract_images(m):
            if image not in images:
                digest = resolve_image(image)
                images[image] = {
                    "registry": image.split("/")[0],
                    "digest": digest
                }
                if "@" in image:
                     images[image]["image"] = image[image.index("/")+1:image.index("@")]
                else:
                     images[image]["image"] = image[image.index("/")+1:image.index(":")]
                     images[image]["tag"] = image[image.index(":")+1:]
    manifest["images"] = list(images.values())
    save_manifest(manifest)

def save_manifest(manifest: dict) -> dict:
    with open("manifest.json.tmp", "w") as f:
        json.dump(manifest, f, indent=4, sort_keys=True)
        f.write('\n')
    os.rename("manifest.json.tmp", "manifest.json")

def load_manifest() -> dict:
    with open("manifest.json") as f:
        return json.load(f)

def main() -> None:
    args = parser.parse_args()

    manifest = load_manifest()
    os.makedirs("work", exist_ok=True)

    if args.verbose:
        basicConfig(level=DEBUG)

    if "mirror-yggdrasil" == args.subcommand:
        mirror_yggdrasil(manifest["yggdrasil_repository"])
    elif "mirror-helm" == args.subcommand:
        mirror_helm(manifest["helm_repository"])
    elif "mirror-images" == args.subcommand:
        mirror_images(manifest["images"])
    elif "resolve-images" == args.subcommand:
        resolve_images(manifest["helm_repository"], manifest)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
