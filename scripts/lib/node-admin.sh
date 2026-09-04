#!/usr/bin/env bash

# Authenticated access to the PoC Node administration API. Production callers
# should provide a 0600 token file; the literal default is only for the
# loopback-only docker-compose.poc.yml smoke environment.
node_admin_curl() {
  local token
  if [[ -n "${NODE_INTERNAL_ADMIN_TOKEN_FILE:-}" ]]; then
    if [[ ! -r "$NODE_INTERNAL_ADMIN_TOKEN_FILE" ]]; then
      echo "Node admin token file is not readable" >&2
      return 1
    fi
    IFS= read -r token <"$NODE_INTERNAL_ADMIN_TOKEN_FILE"
  else
    token="${NODE_INTERNAL_ADMIN_TOKEN:-poc-admin-token}"
  fi
  if [[ -z "$token" ]]; then
    echo "Node admin token is empty" >&2
    return 1
  fi

  # curl reads the header from stdin, keeping the bearer token out of argv.
  printf 'Authorization: Bearer %s\n' "$token" | curl -H @- "$@"
}
