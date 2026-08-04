#!/bin/bash
# monero-wallet-rpc in --wallet-dir mode.
#
# Boots with NO wallet loaded. bot.py calls open_wallet / generate_from_keys
# over JSON-RPC at startup, so this container holds no keys.
#
# Environment:
#   MONERO_RPC_USER / MONERO_RPC_PASSWORD  digest auth. Set BOTH here AND on
#                                          the bot service, identically.
#   MONERO_DAEMON_ADDRESS                  remote node; no local sync needed
#   WALLET_DIR                             volume mount (default /wallet/data)
#   MONERO_RPC_PORT                        default 18083

set -euo pipefail

WALLET_DIR="${WALLET_DIR:-/wallet/data}"
RPC_PORT="${MONERO_RPC_PORT:-18083}"
DAEMON="${MONERO_DAEMON_ADDRESS:-https://xmr-node.cakewallet.com:18081}"
RUN_UID=10001
RUN_GID=10001

echo "[entrypoint] wallet dir : ${WALLET_DIR}"
echo "[entrypoint] daemon     : ${DAEMON}"
echo "[entrypoint] rpc port   : ${RPC_PORT}"

# Railway mounts volumes owned by root. Without this the daemon cannot write
# its .keys file, and generate_from_keys fails with a permission error that
# reaches the bot as a generic RPC failure.
mkdir -p "${WALLET_DIR}"
if [ "$(id -u)" = "0" ]; then
    echo "[entrypoint] fixing ownership of ${WALLET_DIR} -> ${RUN_UID}:${RUN_GID}"
    chown -R "${RUN_UID}:${RUN_GID}" "$(dirname "${WALLET_DIR}")"
    DROP_PRIV=(setpriv --reuid="${RUN_UID}" --regid="${RUN_GID}" --init-groups --)
else
    echo "[entrypoint] already unprivileged (uid $(id -u)); skipping chown"
    DROP_PRIV=()
fi

AUTH_ARGS=()
if [ -n "${MONERO_RPC_USER:-}" ] && [ -n "${MONERO_RPC_PASSWORD:-}" ]; then
    echo "[entrypoint] digest auth ENABLED for user '${MONERO_RPC_USER}'"
    AUTH_ARGS+=(--rpc-login "${MONERO_RPC_USER}:${MONERO_RPC_PASSWORD}")
else
    echo "[entrypoint] WARNING: MONERO_RPC_USER/MONERO_RPC_PASSWORD not set."
    echo "[entrypoint] WARNING: RPC login DISABLED. Anything that can reach this"
    echo "[entrypoint] WARNING: port can read your payment history. Acceptable"
    echo "[entrypoint] WARNING: only while no public domain is attached."
    AUTH_ARGS+=(--disable-rpc-login)
fi

# IPv6 is not optional here. Railway's private network is IPv6-only on
# environments created before 2025-10-16, and <service>.railway.internal
# publishes an AAAA record. Binding 0.0.0.0 alone listens on IPv4 only, so the
# bot dials IPv6, packets are dropped, and you get a connect TIMEOUT rather
# than a refusal. These flags produce a dual-stack listener.
exec "${DROP_PRIV[@]}" monero-wallet-rpc \
    --wallet-dir "${WALLET_DIR}" \
    --rpc-bind-ip 0.0.0.0 \
    --rpc-use-ipv6 \
    --rpc-bind-ipv6-address :: \
    --rpc-bind-port "${RPC_PORT}" \
    --confirm-external-bind \
    "${AUTH_ARGS[@]}" \
    --daemon-address "${DAEMON}" \
    --trusted-daemon \
    --non-interactive \
    --log-level 1
