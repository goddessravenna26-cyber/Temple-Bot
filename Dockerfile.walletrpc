# monero-wallet-rpc as a standalone Railway service.
#
# This container holds a VIEW-ONLY wallet. It cannot spend funds even if the
# host is fully compromised. It syncs nothing — it queries a remote daemon.
#
# Deploy as a SECOND Railway service in the same project, then set the bot's
# MONERO_RPC_URL to http://<service-name>.railway.internal:18083/json_rpc

FROM ubuntu:24.04

ARG MONERO_VERSION=v0.18.3.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fSL "https://downloads.getmonero.org/cli/monero-linux-x64-${MONERO_VERSION}.tar.bz2" \
        -o /tmp/monero.tar.bz2; \
    mkdir -p /opt/monero; \
    tar -xjf /tmp/monero.tar.bz2 -C /opt/monero --strip-components=1; \
    mv /opt/monero/monero-wallet-rpc /usr/local/bin/; \
    mv /opt/monero/monero-wallet-cli /usr/local/bin/; \
    rm -rf /tmp/monero.tar.bz2 /opt/monero

RUN useradd -m -u 10001 monero
WORKDIR /wallet
RUN chown monero:monero /wallet
USER monero

COPY --chown=monero:monero entrypoint.sh /usr/local/bin/entrypoint.sh

EXPOSE 18083
ENTRYPOINT ["/bin/bash", "/usr/local/bin/entrypoint.sh"]
