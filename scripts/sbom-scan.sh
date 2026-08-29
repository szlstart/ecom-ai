#!/bin/sh
set -eu
IMAGE="${IMAGE:?IMAGE is required}"
case "$IMAGE" in *@sha256:????????????????????????????????????????????????????????????????) ;; *) echo "IMAGE must be digest pinned" >&2; exit 2 ;; esac
command -v syft >/dev/null
command -v trivy >/dev/null
command -v cosign >/dev/null
artifact_directory="${ARTIFACT_DIRECTORY:-artifacts/supply-chain}"
case "$artifact_directory" in /|"$HOME") echo "unsafe ARTIFACT_DIRECTORY" >&2; exit 2 ;; esac
mkdir -p "$artifact_directory"
image_digest=${IMAGE##*@sha256:}
sbom_file="$artifact_directory/sbom-${image_digest}.spdx.json"
syft "$IMAGE" -o "spdx-json=$sbom_file"
trivy image --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed "$IMAGE"
if [ -n "${COSIGN_KEY:-}" ]; then
  : "${COSIGN_PUBLIC_KEY:?required when COSIGN_KEY is provided}"
  if [ "${COSIGN_SIGN:-false}" = "true" ]; then
    cosign sign --yes --key "$COSIGN_KEY" "$IMAGE"
  fi
  cosign verify --key "$COSIGN_PUBLIC_KEY" "$IMAGE"
else
  : "${COSIGN_CERTIFICATE_IDENTITY_REGEXP:?required for keyless verification}"
  : "${COSIGN_CERTIFICATE_OIDC_ISSUER:?required for keyless verification}"
  if [ "${COSIGN_SIGN:-false}" = "true" ]; then
    cosign sign --yes "$IMAGE"
  fi
  cosign verify \
    --certificate-identity-regexp "$COSIGN_CERTIFICATE_IDENTITY_REGEXP" \
    --certificate-oidc-issuer "$COSIGN_CERTIFICATE_OIDC_ISSUER" "$IMAGE"
fi
test -s "$sbom_file"
echo "SBOM, vulnerability scan, and signature verification passed for $IMAGE"
