#!/usr/bin/env bash
# Optional testing-only ACM certificate helper for an AWS Academy Learner Lab.
# Preferred: a validated ACM certificate for a domain your group controls.
set -euo pipefail
umask 077

LAB_CERT_HOSTNAME="${1:-lab-gateway.invalid}"
REGION="us-east-1"
if [[ "$LAB_CERT_HOSTNAME" == "--help" || "$LAB_CERT_HOSTNAME" == "-h" ]]; then
  printf '%s\n' 'Usage: bash tools/make_lab_certificate.sh [test-hostname]' \
    'Requires openssl and AWS CLI with the current temporary Learner Lab credentials.' \
    'Imports a self-signed, 7-day testing certificate into ACM us-east-1.' \
    'It is not browser-trusted. Its name will not match the default ALB DNS name.'
  exit 0
fi
if [[ ${#LAB_CERT_HOSTNAME} -gt 253 || ! "$LAB_CERT_HOSTNAME" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ || "$LAB_CERT_HOSTNAME" == *..* ]]; then
  printf '%s\n' 'Invalid test hostname. Use a DNS hostname, without a scheme, path, port or wildcard.' >&2
  exit 2
fi
for TOOL in aws openssl mktemp; do
  command -v "$TOOL" >/dev/null || { printf 'Missing dependency: %s\n' "$TOOL" >&2; exit 1; }
done

printf '%s\n' 'TESTING ONLY: the resulting certificate is self-signed, not browser-trusted.' >&2
printf '%s\n' 'Use only dummy files. Do not describe this as a trusted production HTTPS deployment.' >&2
printf '%s\n' 'Checking the active AWS identity (no credentials are printed)...' >&2
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json >&2
printf 'ACM import region: %s\nCertificate hostname: %s\n' "$REGION" "$LAB_CERT_HOSTNAME" >&2

CERT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gateway-cert.XXXXXXXX")"
chmod 700 "$CERT_DIR"
trap 'rm -rf -- "$CERT_DIR"' EXIT HUP INT TERM

openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 7 \
  -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
  -subj "/CN=$LAB_CERT_HOSTNAME" -addext "subjectAltName=DNS:$LAB_CERT_HOSTNAME" \
  >/dev/null 2>&1

CERT_ARN="$(aws acm import-certificate --region "$REGION" \
  --certificate "fileb://$CERT_DIR/cert.pem" \
  --private-key "fileb://$CERT_DIR/key.pem" \
  --tags Key=Project,Value=INFS3701-Corporate-Gateway Key=Purpose,Value=LearnerLabTesting \
  --query CertificateArn --output text)"
printf '%s\n' "$CERT_ARN"
printf '%s\n' 'Copy the ARN above into the CloudFormation CertificateArn parameter.' >&2
printf '%s\n' 'The local private key has been scheduled for deletion when this script exits.' >&2
printf '%s\n' 'Delete this imported certificate from ACM after deleting the load balancer.' >&2
