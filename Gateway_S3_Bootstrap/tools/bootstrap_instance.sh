#!/bin/bash
# Runs on the EC2 instance after UserData downloads this script + app.tgz from S3.
# Args: <CloudFormationStackName> <LogicalResourceId> <Region>
set -Eeuo pipefail
STACK=$1
RESOURCE=$2
REGION=$3
exec > >(tee -a /var/log/gateway-userdata.log) 2>&1
phase() { printf '%s\n' "$1" | tee /var/log/gateway-setup-stage; }
fail() { printf '%s\n' "$1" | tee /var/log/gateway-setup-error >&2; exit 1; }
signal() {
  rc=$1
  stage=$(cat /var/log/gateway-setup-stage 2>/dev/null || echo bootstrap)
  err=$(tr '\n' ' ' </var/log/gateway-setup-error 2>/dev/null | cut -c1-100 || true)
  if test -n "$err"; then
    reason="Setup exit $rc; stage: $stage; error: $err"
  else
    reason="Setup exit $rc; stage: $stage"
  fi
  /opt/aws/bin/cfn-signal -e "$rc" --reason "$reason" --stack "$STACK" --resource "$RESOURCE" --region "$REGION" || true
}
trap 'signal $?' EXIT

phase extract-app
test -s /tmp/gateway-app.tgz || fail 'app tarball missing'
mkdir -p /opt/gateway/src
tar -xzf /tmp/gateway-app.tgz -C /opt/gateway/src
test -s /opt/gateway/src/install.sh || fail 'install.sh missing from tarball'
test -s /etc/gateway/runtime.json || fail 'runtime.json missing'

phase run-install
export GATEWAY_CONFIG=/etc/gateway/runtime.json
bash /opt/gateway/src/install.sh
phase READY
