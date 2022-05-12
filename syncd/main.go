package main

import (
	"crypto/sha256"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/ioutil"
	"log"
	"net/http"
	"os/exec"
	"time"

	"github.com/go-git/go-billy/v5/memfs"
	"github.com/go-git/go-git/v5"
	"github.com/go-git/go-git/v5/config"
	"github.com/go-git/go-git/v5/storage/memory"
)

var (
	syncInterval                      uint
	manifestGitRepository             string
	armoredKeyRing                    string
	sourceRegistry                    string
	destinationRegistry               string
	sourceChartRepository             string
	destinationChartRepository        string
	sourceYggdrasilGitRepository      string
	destinationYggdrasilGitRepository string
)

func init() {
	flag.UintVar(&syncInterval, "sync-interval", 60, "Synchronizing interval")
	flag.StringVar(&manifestGitRepository, "manifest-git-repository", "", "Git repository to pull the manifest file from")
	flag.StringVar(&armoredKeyRing, "armored-keyring", "", "Armored keyring for verifying the manifest's Git repository commits")
	flag.StringVar(&sourceRegistry, "source-registry", "", "Source registry to pull from")
	flag.StringVar(&destinationRegistry, "destination-registry", "", "Destination registry to push to")
	flag.StringVar(&sourceChartRepository, "source-chart-repository", "", "Source chart repository to pull from")
	flag.StringVar(&destinationChartRepository, "destination-chart-repository", "", "Destination chart repository to push to")
	flag.StringVar(&sourceYggdrasilGitRepository, "source-yggdrasil-git-repository", "", "Source yggdrasil git repository to pull from")
	flag.StringVar(&destinationYggdrasilGitRepository, "destination-yggdrasil-git-repository", "", "Destination yggdrasil git repository to push to")
}

type chart struct {
	Chart   string `json:"chart"`
	Digest  string `json:"digest"`
	Version string `json:"version"`
}

type image struct {
	Digest   string `json:"digest"`
	Image    string `json:"image"`
	Registry string `json:"registry"`
	Tag      string `json:"tag"`
}

type yggdrasilRepository struct {
	Commit     string `json:"commit"`
	Repository string `json:"repository"`
}

type manifest struct {
	Charts              []chart             `json:"charts"`
	Images              []image             `json:"images"`
	YggdrasilRepository yggdrasilRepository `json:"yggdrasil_repository"`
}

func mirrorImage(src, dst string, image image) error {
	srcImage := fmt.Sprintf("%s/%s/%s@%s", src, image.Registry, image.Image, image.Digest)
	var dstImage string
	if image.Tag == "" {
		dstImage = srcImage
	} else {
		dstImage = fmt.Sprintf("%s/%s/%s:%s", dst, image.Registry, image.Image, image.Tag)
	}

	cmd := exec.Command("skopeo", "copy", "--all", "--preserve-digests", "--src-tls-verify=false", "--dest-tls-verify=false", fmt.Sprintf("docker://%s", srcImage), fmt.Sprintf("docker://%s", dstImage))
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("failure mirroring image: %v, output: %v, error: %w", image, string(out), err)
	}
	return nil
}

func mirrorImages(src, dst string, images []image) error {
	for _, image := range images {
		if err := mirrorImage(src, dst, image); err != nil {
			return err
		}
	}
	return nil
}

func mirrorChart(src, dst string, chart chart) error {
	resp, err := http.Get(fmt.Sprintf("%s/%s-%s.tgz", src, chart.Chart, chart.Version))
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("chart %v not found on %v", chart, src)
	}

	h := sha256.New()

	if _, err := io.Copy(h, resp.Body); err != nil {
		return err
	}

	digest := fmt.Sprintf("%x", h.Sum(nil))
	if chart.Digest != digest {
		return fmt.Errorf("digest for chart %v not matching, expected: %s, got: %s", chart, chart.Digest, digest)
	}
	// TODO: Push to dst chart repository
	return nil
}

func mirrorCharts(src, dst string, charts []chart) error {
	for _, chart := range charts {
		if err := mirrorChart(src, dst, chart); err != nil {
			return err
		}
	}
	return nil
}

func mirrorYggdrasil(src, dst string, yggdrasilRepository yggdrasilRepository) error {
	r, err := git.Init(memory.NewStorage(), nil)
	if err != nil {
		return err
	}

	_, err = r.CreateRemote(&config.RemoteConfig{
		Name: "src",
		URLs: []string{src},
	})
	if err != nil {
		return nil
	}

	_, err = r.CreateRemote(&config.RemoteConfig{
		Name: "dst",
		URLs: []string{dst},
	})
	if err != nil {
		return nil
	}

	err = r.Fetch(&git.FetchOptions{
		RemoteName: "src",
		RefSpecs: []config.RefSpec{
			config.RefSpec(fmt.Sprintf("%s:FETCH_HEAD", yggdrasilRepository.Commit)),
		},
	})
	if err != nil {
		return err
	}

	err = r.Push(&git.PushOptions{
		RemoteName: "dst",
		RefSpecs:   []config.RefSpec{config.RefSpec("FETCH_HEAD:refs/heads/master")},
		Force:      true,
	})
	return err
}

func synchronize(lastSuccessfulSynchronization string, keyRing *string) (string, error) {
	// TODO: check without a full clone
	fs := memfs.New()
	r, err := git.Clone(memory.NewStorage(), fs, &git.CloneOptions{
		URL:   manifestGitRepository,
		Depth: 1,
	})

	head, err := r.Head()
	if err != nil {
		return lastSuccessfulSynchronization, err
	}
	hash := head.Hash()
	if hash.String() == lastSuccessfulSynchronization {
		return lastSuccessfulSynchronization, nil
	}

	if keyRing != nil {
		commit, err := r.CommitObject(hash)
		if err != nil {
			return lastSuccessfulSynchronization, err
		}

		if commit.PGPSignature == "" {
			return lastSuccessfulSynchronization, fmt.Errorf("commit isn't signed: %v", commit.ID())
		}
		if _, err := commit.Verify(*keyRing); err != nil {
			return lastSuccessfulSynchronization, err
		}
	}

	file, err := fs.Open("manifest.json")
	if err != nil {
		return lastSuccessfulSynchronization, err
	}
	defer file.Close()

	data, err := ioutil.ReadAll(file)
	if err != nil {
		return lastSuccessfulSynchronization, err
	}
	var m manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return lastSuccessfulSynchronization, err
	}

	if err := mirrorImages(sourceRegistry, destinationRegistry, m.Images); err != nil {
		return lastSuccessfulSynchronization, err
	}

	if err := mirrorCharts(sourceChartRepository, destinationChartRepository, m.Charts); err != nil {
		return lastSuccessfulSynchronization, err
	}

	if err := mirrorYggdrasil(sourceYggdrasilGitRepository, destinationYggdrasilGitRepository, m.YggdrasilRepository); err != nil {
		return lastSuccessfulSynchronization, err
	}
	return hash.String(), nil
}

func main() {
	flag.Parse()

	var lastSuccessfulSynchronization string

	var keyRing *string
	if armoredKeyRing != "" {
		buf, err := ioutil.ReadFile(armoredKeyRing)
		if err != nil {
			log.Fatal(err)
		}
		s := string(buf)
		keyRing = &s
	}

	log.Printf("Synchronizing every %d second", syncInterval)
	for range time.Tick(time.Second * time.Duration(syncInterval)) {
		hash, err := synchronize(lastSuccessfulSynchronization, keyRing)
		if err != nil {
			log.Printf("Error synchronizing: %v", err)
		} else {
			lastSuccessfulSynchronization = hash
		}
	}
}
