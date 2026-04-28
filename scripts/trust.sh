#!/usr/bin/env bash
# ABOUTME: Extract Caddy's per-machine local CA cert and install it into the OS
# ABOUTME: trust store so gh / curl / browsers trust *.gitcabin.localhost cleanly.
#
# Run once after `docker compose -f compose.yml -f compose.tls.yml up -d`.
# Subsequent runs are idempotent — re-installing an already-trusted CA is a no-op
# on every supported platform.
#
# Supported: macOS, Debian/Ubuntu, Fedora/RHEL/CentOS, Arch, openSUSE.
# Windows users: see docs/installation.md for the certutil one-liner.

set -euo pipefail

CADDY_CONTAINER="${GITCABIN_CADDY_CONTAINER:-gitcabin-caddy}"
CA_PATH_IN_CONTAINER="/data/caddy/pki/authorities/local/root.crt"
CA_LOCAL_PATH="${TMPDIR:-/tmp}/gitcabin-ca-$$.pem"
CA_FRIENDLY_NAME="gitcabin local CA"

cleanup() { rm -f "$CA_LOCAL_PATH"; }
trap cleanup EXIT

# ---- 1. Confirm Caddy is up and the CA exists ---------------------------------

if ! command -v docker >/dev/null 2>&1; then
	echo "error: docker is not on PATH. Install Docker before running this script." >&2
	exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q "^${CADDY_CONTAINER}$"; then
	cat >&2 <<-EOF
		error: container "${CADDY_CONTAINER}" is not running.
		Start the TLS stack first:
		    docker compose -f compose.yml -f compose.tls.yml up -d
		Then re-run this script.
	EOF
	exit 1
fi

if ! docker exec "$CADDY_CONTAINER" test -f "$CA_PATH_IN_CONTAINER"; then
	cat >&2 <<-EOF
		error: Caddy hasn't generated its local CA yet (path $CA_PATH_IN_CONTAINER missing).
		This usually means the TLS stack started seconds ago. Wait ~10 seconds and re-run.
		If the file still doesn't appear, check Caddy's logs: docker compose logs caddy
	EOF
	exit 1
fi

# Extract the CA cert to a tempfile on the host. Cleanup runs on exit.
docker exec "$CADDY_CONTAINER" cat "$CA_PATH_IN_CONTAINER" > "$CA_LOCAL_PATH"

if ! [ -s "$CA_LOCAL_PATH" ]; then
	echo "error: extracted CA cert is empty. Aborting." >&2
	exit 1
fi

# ---- 2. Install per-platform --------------------------------------------------

OS="$(uname -s)"

install_macos() {
	echo "==> macOS: adding CA to System keychain (you'll be prompted for sudo)"
	sudo security add-trusted-cert -d -r trustRoot \
		-k /Library/Keychains/System.keychain "$CA_LOCAL_PATH"
	echo "==> done. Verify: openssl s_client -connect mycabin.gitcabin.localhost:443 -servername mycabin.gitcabin.localhost </dev/null 2>/dev/null | openssl x509 -noout -issuer"
}

install_debian() {
	echo "==> Debian/Ubuntu: copying CA to /usr/local/share/ca-certificates/ (sudo)"
	sudo install -m 0644 "$CA_LOCAL_PATH" /usr/local/share/ca-certificates/gitcabin-local.crt
	sudo update-ca-certificates
}

install_fedora() {
	echo "==> Fedora/RHEL: copying CA to /etc/pki/ca-trust/source/anchors/ (sudo)"
	sudo install -m 0644 "$CA_LOCAL_PATH" /etc/pki/ca-trust/source/anchors/gitcabin-local.crt
	sudo update-ca-trust
}

install_arch() {
	echo "==> Arch: copying CA to /etc/ca-certificates/trust-source/anchors/ (sudo)"
	sudo install -m 0644 "$CA_LOCAL_PATH" /etc/ca-certificates/trust-source/anchors/gitcabin-local.crt
	sudo trust extract-compat
}

install_suse() {
	echo "==> openSUSE: copying CA to /etc/pki/trust/anchors/ (sudo)"
	sudo install -m 0644 "$CA_LOCAL_PATH" /etc/pki/trust/anchors/gitcabin-local.pem
	sudo update-ca-certificates
}

case "$OS" in
	Darwin)
		install_macos
		;;
	Linux)
		# Detect distribution family.
		if [ -r /etc/os-release ]; then
			# shellcheck disable=SC1091
			. /etc/os-release
			case "${ID:-}${ID_LIKE:-}" in
				*debian*|*ubuntu*) install_debian ;;
				*fedora*|*rhel*|*centos*) install_fedora ;;
				*arch*) install_arch ;;
				*suse*|*opensuse*) install_suse ;;
				*)
					echo "error: unrecognized Linux distribution (ID=$ID, ID_LIKE=${ID_LIKE:-}) " >&2
					echo "       The CA is at $CA_LOCAL_PATH if you want to install it manually." >&2
					trap - EXIT  # keep the file around so the user can use it
					exit 2
					;;
			esac
		else
			echo "error: /etc/os-release missing; cannot detect Linux distribution" >&2
			exit 2
		fi
		;;
	*)
		cat >&2 <<-EOF
			error: $OS is not supported by this script.
			On Windows: certutil.exe -addstore -user Root <path-to-cert>
			            (extract the cert with: docker exec $CADDY_CONTAINER cat $CA_PATH_IN_CONTAINER > gitcabin-ca.pem)
			On other systems: install $CA_LOCAL_PATH into your trust store manually.
		EOF
		exit 2
		;;
esac

echo
echo "==> CA installed. You can now:"
echo "      gh auth login --hostname mycabin.gitcabin.localhost --with-token <<< any-token"
echo "      GH_HOST=mycabin.gitcabin.localhost gh auth status"
echo
echo "    (substitute any subdomain you like for 'mycabin' — the cert covers *.gitcabin.localhost)"
