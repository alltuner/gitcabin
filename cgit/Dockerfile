# Tiny cgit + lighttpd image. lighttpd is a few hundred KB plus the cgit
# binary itself; the whole image fits in ~80 MB. Apache (the canonical
# Debian setup for cgit) was overkill — we don't use any of its modules.

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends cgit lighttpd \
    && rm -rf /var/lib/apt/lists/*

# Custom cgitrc: scan-path lets cgit auto-discover all bare repos under
# /repos (which compose mounts from the host's data/repos/), so adding
# a new repo is just creating a new bare dir on disk — no cgit reload.
COPY cgitrc /etc/cgitrc

# lighttpd config replaces Debian's default entirely — we only need cgi,
# alias, and rewrite. No conf-enabled snippets, no fastcgi, no compress.
COPY lighttpd.conf /etc/lighttpd/lighttpd.conf

EXPOSE 80

# `-D` keeps lighttpd in the foreground so docker logs see its output.
CMD ["lighttpd", "-D", "-f", "/etc/lighttpd/lighttpd.conf"]
