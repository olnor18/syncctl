# syncctl

`syncctl` is a tool for packaging all the artifacts for running [Yggdrasil](https://github.com/distributed-technologies/yggdrasil) in a air-gapped environment.

## Usage

`syncctl` needs a `manifest.json` file (see [`manifest.json.sample`](manifest.json.sample) for a example)

```sh
$ ./syncctl
usage: syncctl [-h] [-v] {mirror-yggdrasil,mirror-helm,mirror-images,resolve-images,tar} ...

positional arguments:
  {mirror-yggdrasil,mirror-helm,mirror-images,resolve-images,tar}
    mirror-yggdrasil    mirror the yggdrasil git repository to the local fs
    mirror-helm         mirror the helm git repository to the local fs and download all charts
    mirror-images       mirror the container images to the local fs
    resolve-images      resolve container images to digest and update the manifest file
    tar                 create a tarball

optional arguments:
  -h, --help            show this help message and exit
  -v, --verbose         Causes to print debugging messages about the progress
```
