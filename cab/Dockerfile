# ABOUTME: Builds a thin docker image for cab — usable via `docker run`/alias.
# ABOUTME: Multi-stage: Go builder produces a static binary, alpine runner adds gh.

# syntax=docker/dockerfile:1

# ---- builder ------------------------------------------------------------- #
# Static Go build so the final image doesn't need libc gymnastics.
FROM golang:1.26-alpine AS builder
WORKDIR /src

# Cache module downloads in a separate layer. go-git pulls in a few
# transitive crypto/net deps; go.sum pins them.
COPY go.mod go.sum ./
RUN go mod download

# Build the binary. CGO_ENABLED=0 + -trimpath gives us a small reproducible
# binary that runs anywhere we put it.
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/cab .

# ---- runtime ------------------------------------------------------------- #
# alpine plus a pinned gh release. We grab the upstream binary directly
# rather than apk's `github-cli` package because Alpine ships an older
# release that queries fields gitcabin's partial schema doesn't expose;
# pinning gh to the same version the host typically runs avoids that drift.
FROM alpine:3.20
ARG GH_VERSION=2.92.0
RUN apk add --no-cache ca-certificates wget tar && \
    arch=$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/') && \
    wget -qO /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" && \
    tar -xz -C /tmp -f /tmp/gh.tgz && \
    mv /tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh /usr/local/bin/gh && \
    rm -rf /tmp/gh*

# Run as a non-root user. gh writes its config under $HOME, which we set to
# /home/cab — that's where the seed hosts.yml below lives so the very first
# `docker run` doesn't have to pay the gh-auth-login cost.
RUN adduser -D -u 1000 cab
USER cab
WORKDIR /home/cab
ENV HOME=/home/cab
ENV XDG_CONFIG_HOME=/home/cab/.config

# Defaults that match the gitcabin compose stack: when this container joins
# the gitcabin network, the API service is reachable as `gitcabin-api:8000`.
# We point the proxy there (HTTP_PROXY) but keep GH_HOST=github.localhost so
# gh stays on its plain-HTTP fast path.
ENV GITCABIN_PROXY=http://gitcabin-api:8000
ENV GITCABIN_HOST=github.localhost

# Pre-seed gh's hosts.yml so cab's hot path (host already registered? skip
# the gh subprocess) takes the fast path on every cold container start.
# The token value is a placeholder — gitcabin doesn't verify tokens, so any
# non-empty string works.
RUN mkdir -p /home/cab/.config/gh \
 && printf 'github.localhost:\n    user: cab\n    oauth_token: gitcabin-no-auth\n    git_protocol: https\n    users:\n        cab:\n            oauth_token: gitcabin-no-auth\n            git_protocol: https\n' \
    > /home/cab/.config/gh/hosts.yml

COPY --from=builder /out/cab /usr/local/bin/cab

ENTRYPOINT ["cab"]
CMD ["help"]
