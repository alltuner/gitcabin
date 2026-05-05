// ABOUTME: cab — invokes gh against a local gitcabin via an unprivileged-port HTTP proxy.
// ABOUTME: Stdlib + go-git only. gh stays out of the way except for actual passthrough.

// cab is a thin shim around gh. It exists because gh hardcodes port 80 for
// `github.localhost`, which forces a privileged-port binding on the host. cab
// sets HTTP_PROXY to gitcabin's unprivileged port (18080 by default) so gh's
// http://api.github.localhost/ requests round-trip through loopback to
// gitcabin without anyone touching port 80.
//
// Subcommands:
//
//   cab login                          register the host with gh (idempotent)
//   cab status                         is gitcabin reachable + gh registered?
//   cab logout                         forget the host
//   cab repo init <owner>/<name>       host-side bare-repo init (uses go-git)
//   cab <anything else>                ensure registered, then exec gh
//
// Env knobs:
//
//   GITCABIN_PROXY    full proxy URL (default http://127.0.0.1:${GITCABIN_PORT:-18080}).
//                     Override to `http://gitcabin:8000` when running inside
//                     the gitcabin compose network.
//   GITCABIN_PORT     shorthand: builds the default GITCABIN_PROXY when GITCABIN_PROXY
//                     isn't set. Default 18080.
//   GITCABIN_HOST     hostname gh registers gitcabin under (default github.localhost).
//                     Almost never needs to change — github.localhost is the one
//                     hostname gh special-cases for plain HTTP.
//   GITCABIN_DATA_DIR where `cab repo init` writes bare repos (default ./data).
//
// Why no gh subprocess for auth ops: gh just reads + writes
// ~/.config/gh/hosts.yml. We do the same directly. That makes login / status /
// logout instant and removes the cold-start paid by every gh process boot.
// gh is invoked only for the actual passthrough (`cab issue create` etc),
// where gh's UI is the whole point.

package main

import (
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/go-git/go-git/v5"
)

const (
	defaultPort = "18080"
	defaultHost = "github.localhost"
	defaultUser = "cab"
	// gitcabin doesn't verify tokens — anyone reaching the port is the owner —
	// so any non-empty value gets gh to consider the host registered.
	// "gitcabin-no-auth" is a hint to anyone inspecting their hosts.yml later.
	placeholderToken = "gitcabin-no-auth"
)

func main() {
	proxy := proxyURL()
	host := envDefault("GITCABIN_HOST", defaultHost)

	if len(os.Args) < 2 {
		help()
		return
	}

	switch os.Args[1] {
	case "login":
		ensureRegistered(host)
		printStatus(host)
	case "status", "doctor":
		statusCheck(proxy, host)
	case "logout":
		removeHostFromFile(host)
		fmt.Printf("forgot %s\n", host)
	case "repo":
		repoSubcommand(proxy, host, os.Args[2:])
	case "-h", "--help", "help":
		help()
	default:
		// Hot path. ensureRegistered is a single file stat in the common case.
		ensureRegistered(host)
		execGh(proxy, host, os.Args[1:])
	}
}

// ---- env / config -------------------------------------------------------- //

// proxyURL resolves the URL gh's HTTP traffic is forwarded through. Two
// shapes: a full override via GITCABIN_PROXY (used in the docker image to
// point at the in-network service hostname), or the default
// http://127.0.0.1:${GITCABIN_PORT:-18080} for host-side use.
func proxyURL() string {
	if v := os.Getenv("GITCABIN_PROXY"); v != "" {
		return v
	}
	return "http://127.0.0.1:" + envDefault("GITCABIN_PORT", defaultPort)
}

func envDefault(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ---- subcommands -------------------------------------------------------- //

// statusCheck verifies (1) the gitcabin proxy responds at the configured URL
// and (2) gh has the host registered. Both have to be true for a fresh
// invocation to succeed; printing them separately makes diagnosis easier.
func statusCheck(proxy, host string) {
	if err := pingProxy(proxy, host); err != nil {
		die("proxy at %s is not responding (%v)\nis gitcabin running?", proxy, err)
	}
	fmt.Printf("  proxy %s -> %s    OK\n", proxy, host)
	printStatus(host)
}

// printStatus mimics `gh auth status --hostname <host>` for our one host. We
// reproduce the format gh users expect rather than shelling out.
func printStatus(host string) {
	if !hostInHostsFile(host) {
		fmt.Printf("  %s\n    × not registered with gh — run `cab login`\n", host)
		os.Exit(1)
	}
	fmt.Printf("  %s\n    ✓ registered with gh (placeholder token)\n", host)
}

// pingProxy issues a minimal request to gitcabin via the proxy. Any HTTP
// response (including error codes) means the proxy is up; only a transport
// failure (connection refused, timeout) is a hard fail.
func pingProxy(proxyStr, host string) error {
	proxy, err := url.Parse(proxyStr)
	if err != nil {
		return err
	}
	client := &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(proxy)},
		Timeout:   2 * time.Second,
	}
	resp, err := client.Get(fmt.Sprintf("http://api.%s/", host))
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

// repoSubcommand handles the `cab repo …` namespace. `cab repo init <slug>`
// is cab-specific (go-git PlainInit, no docker exec); everything else passes
// through to gh.
func repoSubcommand(proxy, host string, args []string) {
	if len(args) >= 1 && args[0] == "init" {
		if len(args) != 2 {
			die("usage: cab repo init <owner>/<name>")
		}
		repoInit(args[1])
		return
	}
	ensureRegistered(host)
	execGh(proxy, host, append([]string{"repo"}, args...))
}

// repoInit creates a fresh bare git repository under the gitcabin data dir
// using go-git's PlainInit — no shell-out, no docker exec, no git binary
// dependency on the host.
//
// Path resolution: ${GITCABIN_DATA_DIR:-./data}/repos/<owner>/<name>.git.
// The default assumes the user is in the gitcabin project directory (where
// docker compose's bind mount lives at ./data); GITCABIN_DATA_DIR overrides.
//
// Limitation: when cab itself runs inside a container, the data volume isn't
// usually mounted — that case fails with a clear error. A future
// `createRepository` GraphQL mutation in gitcabin would let dockerized cab
// init repos via API; out of scope for now.
func repoInit(slug string) {
	if strings.Count(slug, "/") != 1 || strings.HasPrefix(slug, "/") || strings.HasSuffix(slug, "/") {
		die("expected <owner>/<name>, got %q", slug)
	}
	dataDir := envDefault("GITCABIN_DATA_DIR", "./data")
	target := filepath.Join(dataDir, "repos", slug+".git")
	if _, err := os.Stat(target); err == nil {
		fmt.Printf("%s already initialized\n", slug)
		return
	}
	if _, err := git.PlainInit(target, true); err != nil {
		die("could not init %s at %s (%v)\nis the gitcabin data dir reachable? (set GITCABIN_DATA_DIR if not in the project root)",
			slug, target, err)
	}
	// go-git's PlainInit writes a minimal `[core]` block (just `bare = true`),
	// which `git rev-parse --is-bare-repository` doesn't recognize. Overwrite
	// with what `git init --bare` produces so command-line git tooling agrees
	// the repo is bare. Indented with tabs because that's what git itself
	// writes — keeps repos byte-identical between cab-init'd and git-init'd.
	canonical := "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n"
	if err := os.WriteFile(filepath.Join(target, "config"), []byte(canonical), 0o644); err != nil {
		die("init succeeded but config rewrite failed (%v)", err)
	}
	fmt.Printf("initialized %s at %s\n", slug, target)
}

// ---- hosts.yml manipulation --------------------------------------------- //
//
// We read + write ~/.config/gh/hosts.yml directly instead of shelling out to
// `gh auth login --with-token` / `gh auth status` / `gh auth logout`. gh's
// auth subsystem isn't doing anything magic here — it stores host records as
// YAML blocks of the shape below. Manipulating the file as text-level YAML is
// safe because gh always writes well-formed output, and we only touch entries
// we own (top-level <host>: blocks).
//
//   github.localhost:
//       user: cab
//       oauth_token: gitcabin-no-auth
//       git_protocol: https
//       users:
//           cab:
//               oauth_token: gitcabin-no-auth
//               git_protocol: https
//
// Skipping the gh subprocess saves ~30ms per auth-shaped call and means cab's
// hot path (ensureRegistered before passthrough) is a single file stat.

// ensureRegistered is the hot path: called before every passthrough. The
// common case is "host already in the file" — that returns instantly. The
// uncommon case (first invocation per machine) writes the registration block.
func ensureRegistered(host string) {
	if hostInHostsFile(host) {
		return
	}
	writeHostBlock(host)
	fmt.Fprintf(os.Stderr, "registered %s with gh (one-time)\n", host)
}

// hostInHostsFile checks whether the host appears as a top-level block in
// hosts.yml. Reads the whole file once and uses findHostBlock; missing or
// unreadable files are treated as "not registered."
func hostInHostsFile(host string) bool {
	data, err := os.ReadFile(hostsYAMLPath())
	if err != nil {
		return false
	}
	start, _ := findHostBlock(string(data), host)
	return start >= 0
}

// writeHostBlock unconditionally appends a registration block to hosts.yml.
// Caller is responsible for checking that the host isn't already there
// (ensureRegistered does this); calling unconditionally would duplicate the
// block, which gh tolerates but is a smell.
func writeHostBlock(host string) {
	path := hostsYAMLPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		die("could not create gh config dir (%v)", err)
	}
	existing, _ := os.ReadFile(path) // fine if missing
	block := fmt.Sprintf(
		"%s:\n    user: %s\n    oauth_token: %s\n    git_protocol: https\n    users:\n        %s:\n            oauth_token: %s\n            git_protocol: https\n",
		host, defaultUser, placeholderToken, defaultUser, placeholderToken,
	)
	out := []byte(block)
	if len(existing) > 0 {
		// Make sure the existing content ends with a newline before appending
		// the new block, so the YAML stays well-formed.
		if existing[len(existing)-1] != '\n' {
			existing = append(existing, '\n')
		}
		out = append(existing, out...)
	}
	if err := os.WriteFile(path, out, 0o600); err != nil {
		die("could not write %s (%v)", path, err)
	}
}

// removeHostFromFile deletes the host's top-level block from hosts.yml.
// No-op if the file is missing or the host isn't in it.
func removeHostFromFile(host string) {
	path := hostsYAMLPath()
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	start, end := findHostBlock(string(data), host)
	if start < 0 {
		return
	}
	lines := strings.Split(string(data), "\n")
	out := append([]string{}, lines[:start]...)
	out = append(out, lines[end:]...)
	if err := os.WriteFile(path, []byte(strings.Join(out, "\n")), 0o600); err != nil {
		die("could not write %s (%v)", path, err)
	}
}

// findHostBlock locates a `<host>:` block in a hosts.yml string and returns
// the [start, end) line range it occupies. A block begins at the line whose
// content is exactly `<host>:` (or `<host>: …` followed by a space) at column
// 0, and ends at the next column-0 line or EOF — gh's nested fields are all
// indented. Returns (-1, -1) if the host isn't present.
//
// This is the single source of truth for host-block bounds; both
// hostInHostsFile and removeHostFromFile use it.
func findHostBlock(content, host string) (start, end int) {
	lines := strings.Split(content, "\n")
	startMarker := host + ":"

	start = -1
	for i, line := range lines {
		if line == startMarker || strings.HasPrefix(line, startMarker+" ") {
			start = i
			break
		}
	}
	if start == -1 {
		return -1, -1
	}
	end = len(lines)
	for j := start + 1; j < len(lines); j++ {
		if lines[j] == "" {
			continue
		}
		// Indented = still inside this host's block. Column-0 = next block.
		if !strings.HasPrefix(lines[j], " ") && !strings.HasPrefix(lines[j], "\t") {
			end = j
			break
		}
	}
	return start, end
}

// hostsYAMLPath returns the conventional location gh reads + writes:
// $GH_CONFIG_DIR or $XDG_CONFIG_HOME/gh or $HOME/.config/gh, then hosts.yml.
// Following gh's own resolution order keeps us in lockstep without poking at
// gh internals.
func hostsYAMLPath() string {
	if d := os.Getenv("GH_CONFIG_DIR"); d != "" {
		return filepath.Join(d, "hosts.yml")
	}
	if d := os.Getenv("XDG_CONFIG_HOME"); d != "" {
		return filepath.Join(d, "gh", "hosts.yml")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".config", "gh", "hosts.yml")
}

// ---- gh exec (passthrough only) ----------------------------------------- //

// execGh replaces our process with gh — the kernel's exec, not a child fork.
// This is what lets `cab issue create` behave indistinguishably from the gh
// command for signal handling, exit codes, and process tracking.
func execGh(proxy, host string, args []string) {
	path, err := exec.LookPath("gh")
	if err != nil {
		die("gh not found on PATH")
	}
	if err := syscall.Exec(path, append([]string{"gh"}, args...), proxyEnv(proxy, host)); err != nil {
		die("exec gh: %v", err)
	}
}

// proxyEnv builds the env for gh: copies the parent minus NO_PROXY (which
// would override our HTTP_PROXY for loopback hosts in many dev setups), then
// sets HTTP_PROXY + GH_HOST.
func proxyEnv(proxy, host string) []string {
	parent := os.Environ()
	out := make([]string, 0, len(parent)+2)
	for _, kv := range parent {
		if strings.HasPrefix(kv, "no_proxy=") || strings.HasPrefix(kv, "NO_PROXY=") {
			continue
		}
		out = append(out, kv)
	}
	out = append(out,
		"HTTP_PROXY="+proxy,
		"GH_HOST="+host,
	)
	return out
}

// ---- misc ---------------------------------------------------------------- //

// die formats a message, prints it to stderr with a "cab: " prefix, and exits
// non-zero. Single source of truth for fatal errors so the format stays
// consistent across the CLI.
func die(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "cab: "+format+"\n", args...)
	os.Exit(1)
}

func help() {
	fmt.Println(`cab — invoke gh against a local gitcabin via an unprivileged-port HTTP proxy.

Usage:
  cab login                            Register the host with gh (idempotent)
  cab status                           Is gitcabin up and reachable?
  cab logout                           Forget the host
  cab repo init <owner>/<name>         Host-side bare-repo init (uses go-git)
  cab issue create -R me/cabin ...     Any other gh subcommand passes through

Env:
  GITCABIN_PROXY    full proxy URL (overrides GITCABIN_PORT)
  GITCABIN_PORT     gitcabin's host port (default 18080)
  GITCABIN_HOST     hostname gh registers gitcabin under (default github.localhost)
  GITCABIN_DATA_DIR data dir for cab repo init (default ./data)

See docs/cab.md for the design.`)
}
