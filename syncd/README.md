# syncd

`syncd` is a daemon for mirroring artifacts described in a [manifest file](../README.md#manifest-file) created by the [`syncctl` tool](../README.md), between two sets of artifacts services (registry, chart repository, yggdrasil git repository).

The manifest file must be stored in a Git repository, which the tool will check every `-sync-interval`.

**Note:** Support for pushing to a chart repository is not supported at the moment.

## Usage

```sh
$ ./syncd --help
Usage of ./syncd:
  -armored-keyring string
        Armored keyring for verifying the manifest's Git repository commits
  -destination-chart-repository string
        Destination chart repository to push to
  -destination-registry string
        Destination registry to push to
  -destination-yggdrasil-git-repository string
        Destination yggdrasil git repository to push to
  -manifest-git-repository string
        Git repository to pull the manifest file from
  -source-chart-repository string
        Source chart repository to pull from
  -source-registry string
        Source registry to pull from
  -source-yggdrasil-git-repository string
        Source yggdrasil git repository to pull from
  -sync-interval uint
        Synchronizing interval (default 60)
```
