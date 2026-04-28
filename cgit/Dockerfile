# Tiny cgit + Apache image. cgit is a CGI binary; Debian's package ships
# an Apache config snippet at /etc/apache2/conf-available/cgit.conf that
# exposes it under /cgit/. We enable that and let Apache do the heavy lifting.

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends cgit apache2 \
    && a2enmod cgid \
    && a2enconf cgit \
    && rm -rf /var/lib/apt/lists/*

# Custom cgitrc: scan-path lets cgit auto-discover all bare repos under
# /repos (which compose mounts from the host's data/repos/), so adding
# a new repo is just creating a new bare dir on disk — no cgit reload.
COPY cgitrc /etc/cgitrc

# Apache is configured by Debian to log to /var/log/apache2/. We redirect
# both logs to /dev/stdout/stderr so `docker compose logs cgit` shows them.
RUN ln -sf /dev/stdout /var/log/apache2/access.log \
    && ln -sf /dev/stderr /var/log/apache2/error.log

EXPOSE 80

# `apachectl -D FOREGROUND` is the canonical way to keep apache attached to
# stdout in a container.
CMD ["apachectl", "-D", "FOREGROUND"]
