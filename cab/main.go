// ABOUTME: cab — invokes gh against a local gitcabin via an unprivileged-port HTTP proxy.
// ABOUTME: Native Go rewrite of scripts/cab. Stdlib only; no external deps.

// cab is a thin shim around gh. It exists because gh hardcodes port 80 for
// `github.localhost`, which forces a privileged-port binding on the host. cab
// sets HTTP_PROXY to gitcabin's unprivileged port (8080 by default) so gh's
// http://api.github.localhost/ requests round-trip through loopback to
// gitcabin without anyone touching port 80.
//
// Subcommands:
//
//   cab login                          register the host with gh (idempotent)
//   cab status                         is gitcabin reachable + gh registered?
//   cab logout                         forget the host
//   cab repo init <owner>/<name>       host-side bare-repo init (requires docker)
//   cab <anything else>                ensure registered, then exec gh
//
// Env knobs:
//
//   GITCABIN_PROXY    full proxy URL (default http://127.0.0.1:${GITCABIN_PORT:-8080}).
//                     Override to `http://gitcabin-api:8000` when running inside
//                     the gitcabin compose network.
//   GITCABIN_PORT     shorthand: builds the default GITCABIN_PROXY when GITCABIN_PROXY
//                     isn't set. Default 8080.
//   GITCABIN_HOST     hostname gh registers gitcabin under (default github.localhost).
//                     Almost never needs to change — github.localhost is the one
//                     hostname gh special-cases for plain HTTP.
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
	defaultPort = "8080"
	defaultHost = "github.localhost"
	defaultUser = "cab"
	// Placeholder gitcabin doesn't verify tokens — anyone reaching the port
	// is the owner — so any non-empty value gets gh to consider the host
	// registered. Calling it gitcabin-no-auth is a hint to anyone
	// inspecting their hosts.yml later.
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
		printStatus(proxy, host)
	case "status", "doctor":
		statusCheck(proxy, host)
	case "logout":
		removeHost(host)
		fmt.Printf("forgot %s\n", host)
	case "repo":
		repoSubcommand(proxy, host, os.Args[2:])
	case "-h", "--help", "help":
		help()
	default:
		// Hot path: every passthrough invocation. ensureRegistered is a
		// single file stat in the common case, so we pay essentially zero
		// for it on subsequent calls.
		ensureRegistered(host)
		execGh(proxy, host, os.Args[1:])
	}
}

// proxyURL resolves the URL gh's HTTP traffic is forwarded through. Two
// shapes: direct override via GITCABIN_PROXY (used in the docker image to
// point at the in-network service hostname), or the default
// http://127.0.0.1:${GITCABIN_PORT:-8080} for host-side use.
func proxyURL() string {
	if v := os.Getenv("GITCABIN_PROXY"); v != "" {
		return v
	}
	return "http://127.0.0.1:" + envDefault("GITCABIN_PORT", defaultPort)
}

// ---- subcommands -------------------------------------------------------- //

// statusCheck verifies (1) the gitcabin proxy responds at the configured URL
// and (2) gh has the host registered. Both have to be true for a fresh
// invocation to succeed; printing them separately makes diagnosis easier.
func statusCheck(proxy, host string) {
	if err := pingProxy(proxy, host); err != nil {
		fmt.Fprintf(os.Stderr, "cab: proxy at %s is not responding (%v)\n", proxy, err)
		fmt.Fprintln(os.Stderr, "is gitcabin running?")
		os.Exit(1)
	}
	fmt.Printf("  proxy %s -> %s    OK\n", proxy, host)
	printStatus(proxy, host)
}

// printStatus mimics `gh auth status --hostname <host>` for our one host.
// We reproduce the format gh users expect rather than shelling out.
func printStatus(proxy, host string) {
	if !hostInHostsFile(host) {
		fmt.Printf("  %s\n    × not registered with gh — run `cab login`\n", host)
		os.Exit(1)
	}
	fmt.Printf("  %s\n    ✓ registered with gh (placeholder token)\n", host)
}

// pingProxy issues a minimal request to gitcabin via the proxy. Anything that
// returns successfully (or even an HTTP error code) means the proxy is up;
// only a transport failure (connection refused, timeout) is a hard fail.
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

// repoSubcommand handles the `cab repo …` namespace. The only cab-specific
// command here is `cab repo init <owner>/<name>`, which initializes a fresh
// bare repo by docker-execing into the gitcabin-api container. Everything
// else passes through to gh.
func repoSubcommand(proxy, host string, args []string) {
	if len(args) >= 1 && args[0] == "init" {
		if len(args) != 2 {
			fmt.Fprintln(os.Stderr, "usage: cab repo init <owner>/<name>")
			os.Exit(2)
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
	if !strings.Contains(slug, "/") || strings.Count(slug, "/") != 1 {
		fmt.Fprintf(os.Stderr, "cab: expected <owner>/<name>, got %q\n", slug)
		os.Exit(2)
	}
	dataDir := envDefault("GITCABIN_DATA_DIR", "./data")
	target := filepath.Join(dataDir, "repos", slug+".git")
	if _, err := os.Stat(target); err == nil {
		fmt.Printf("%s already initialized\n", slug)
		return
	}
	if _, err := git.PlainInit(target, true); err != nil {
		fmt.Fprintf(os.Stderr, "cab: could not init %s at %s (%v)\n", slug, target, err)
		fmt.Fprintln(os.Stderr, "is the gitcabin data dir reachable? (set GITCABIN_DATA_DIR if not in the project root)")
		os.Exit(1)
	}

	// go-git's PlainInit writes a minimal `[core]` block (just `bare = true`),
	// which `git rev-parse --is-bare-repository` doesn't recognize. Overwrite
	// with what `git init --bare` produces so command-line git tooling agrees
	// the repo is bare. Indented with tabs because that's what git itself
	// writes — keeps repos byte-identical between cab-init'd and git-init'd.
	canonical := "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n"
	if err := os.WriteFile(filepath.Join(target, "config"), []byte(canonical), 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "cab: init succeeded but config rewrite failed (%v)\n", err)
		os.Exit(1)
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
	writeHost(host)
	fmt.Fprintf(os.Stderr, "registered %s with gh (one-time)\n", host)
}

// removeHost deletes the host's top-level block from hosts.yml. No-op if the
// host isn't there.
func removeHost(host string) {
	path := hostsYAMLPath()
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	updated, removed := stripHostBlock(string(data), host)
	if !removed {
		return
	}
	if err := os.WriteFile(path, []byte(updated), 0o600); err != nil {
		fmt.Fprintf(os.Stderr, "cab: could not write %s (%v)\n", path, err)
		os.Exit(1)
	}
}

// writeHost appends a fresh block to hosts.yml registering the host with the
// placeholder token. Idempotent: if the block already exists we don't touch
// the file.
func writeHost(host string) {
	if hostInHostsFile(host) {
		return
	}
	path := hostsYAMLPath()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		fmt.Fprintf(os.Stderr, "cab: could not create gh config dir (%v)\n", err)
		os.Exit(1)
	}

	existing, _ := os.ReadFile(path) // ok if missing
	block := fmt.Sprintf(
		"%s:\n    user: %s\n    oauth_token: %s\n    git_protocol: https\n    users:\n        %s:\n            oauth_token: %s\n            git_protocol: https\n",
		host, defaultUser, placeholderToken, defaultUser, placeholderToken,
	)
	out := []byte(block)
	if len(existing) > 0 {
		// Make sure existing content ends with a newline before appending.
		if existing[len(existing)-1] != '\n' {
			existing = append(existing, '\n')
		}
		out = append(existing, out...)
	}
	if err := os.WriteFile(path, out, 0o600); err != nil {
		fmt.Fprintf(os.Stderr, "cab: could not write %s (%v)\n", path, err)
		os.Exit(1)
	}
}

// hostInHostsFile checks whether the host appears as a top-level block in
// hosts.yml. Looks for `<host>:` either at the very start of the file or
// immediately after a newline. gh writes well-formed YAML, so this string-
// level check is reliable without pulling in a YAML parser.
func hostInHostsFile(host string) bool {
	data, err := os.ReadFile(hostsYAMLPath())
	if err != nil {
		return false
	}
	prefix := host + ":"
	if strings.HasPrefix(string(data), prefix) {
		return true
	}
	return strings.Contains(string(data), "\n"+prefix)
}

// stripHostBlock removes the named host's top-level block from a hosts.yml
// string. Returns the updated content and a flag indicating whether anything
// changed. A "block" begins at `<host>:` (column 0) and ends at the next
// column-0 line or EOF — gh's nested fields are all indented.
func stripHostBlock(content, host string) (string, bool) {
	lines := strings.Split(content, "\n")
	startMarker := host + ":"

	start := -1
	for i, line := range lines {
		if line == startMarker || strings.HasPrefix(line, startMarker+" ") {
			start = i
			break
		}
	}
	if start == -1 {
		return content, false
	}
	// Find the end: first line at column 0 that isn't blank.
	end := len(lines)
	for j := start + 1; j < len(lines); j++ {
		if lines[j] == "" {
			continue
		}
		if !strings.HasPrefix(lines[j], " ") && !strings.HasPrefix(lines[j], "\t") {
			end = j
			break
		}
	}
	out := append([]string{}, lines[:start]...)
	out = append(out, lines[end:]...)
	return strings.Join(out, "\n"), true
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
		fmt.Fprintln(os.Stderr, "cab: gh not found on PATH")
		os.Exit(1)
	}
	if err := syscall.Exec(path, append([]string{"gh"}, args...), proxyEnv(proxy, host)); err != nil {
		fmt.Fprintf(os.Stderr, "cab: exec gh: %v\n", err)
		os.Exit(1)
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

func envDefault(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func help() {
	fmt.Println(`cab — invoke gh against a local gitcabin via an unprivileged-port HTTP proxy.

Usage:
  cab login                            Register the host with gh (idempotent)
  cab status                           Is gitcabin up and reachable?
  cab logout                           Forget the host
  cab repo init <owner>/<name>         Host-side bare-repo init (requires docker)
  cab issue create -R me/cabin ...     Any other gh subcommand passes through

Env:
  GITCABIN_PROXY    full proxy URL (overrides GITCABIN_PORT)
  GITCABIN_PORT     gitcabin's host port (default 8080)
  GITCABIN_HOST     hostname gh registers gitcabin under (default github.localhost)

See docs/cab.md for the design.`)
}
