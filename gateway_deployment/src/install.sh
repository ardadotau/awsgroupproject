#!/bin/bash
# Installs the project into an Amazon Linux 2023 EC2 instance.
# Called by the CloudFormation User Data as root. No passwords are embedded here.
set -Eeuo pipefail
export GATEWAY_CONFIG=${GATEWAY_CONFIG:-/etc/gateway/runtime.json}
SRC=$(cd "$(dirname "$0")" && pwd)
ERROR_LOG=/var/log/gateway-setup-error
DETAIL_LOG=/var/log/gateway-setup-detail
test -s "$GATEWAY_CONFIG"
# AL2023's system python3 remains 3.9. Keep it intact for OS/cfn tools;
# install the app into a separate 3.11 environment for current dependencies.
phase() { printf '%s\n' "$1" | tee /var/log/gateway-setup-stage; }
fail() {
  printf '%s\n' "$1" | tee "$ERROR_LOG" >&2
  exit 1
}
note_error() {
  # Keep a short, signal-safe summary for CloudFormation Status reason.
  if test -s "$DETAIL_LOG"; then
    tail -n 25 "$DETAIL_LOG" | tr '\n' ' ' | cut -c1-180 > "$ERROR_LOG" || true
  fi
}

: > "$DETAIL_LOG"
phase 'Installing Python and system packages'
ok=0
for attempt in 1 2 3; do
  if dnf install -y python3.11 python3.11-pip openssl curl >>"$DETAIL_LOG" 2>&1; then ok=1; break; fi
  sleep 5
done
if test "$ok" -ne 1; then
  note_error
  fail 'dnf could not install python3.11/openssl/curl'
fi

id gateway >/dev/null 2>&1 || useradd --system --home-dir /var/lib/gateway --shell /sbin/nologin gateway
install -d -m 750 -o gateway -g gateway /var/lib/gateway /var/log/gateway
install -d -m 750 -o root -g gateway /etc/gateway /etc/gateway/tls
install -d -m 755 /opt/gateway
chown -R root:gateway /opt/gateway/src
chmod -R u=rwX,g=rX,o= /opt/gateway/src
chown root:gateway "$GATEWAY_CONFIG"
chmod 640 "$GATEWAY_CONFIG"

phase 'Installing application dependencies'
python3.11 -m venv /opt/gateway/venv
ok=0
for attempt in 1 2 3; do
  if /opt/gateway/venv/bin/pip install --disable-pip-version-check -r "$SRC/requirements.txt" >>"$DETAIL_LOG" 2>&1; then ok=1; break; fi
  sleep 10
done
if test "$ok" -ne 1; then
  note_error
  fail 'pip install of requirements.txt failed (check NAT/DNS to PyPI)'
fi

phase 'Downloading RDS CA bundle'
if ! curl --fail --silent --show-error --retry 5 --retry-delay 3 --max-time 40 \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  -o /opt/gateway/rds-ca.pem >>"$DETAIL_LOG" 2>&1; then
  note_error
  fail 'could not download RDS CA bundle'
fi
chmod 644 /opt/gateway/rds-ca.pem
test -s /opt/gateway/rds-ca.pem || fail 'RDS CA bundle is empty'

phase 'Initializing shared PostgreSQL database'
# Both servers can initialize simultaneously. app.py init-db must serialize DDL.
ok=0
for attempt in $(seq 1 18); do
  if /opt/gateway/venv/bin/python "$SRC/app.py" init-db >>"$DETAIL_LOG" 2>&1; then
    ok=1
    break
  fi
  sleep 10
done
if test "$ok" -ne 1; then
  note_error
  fail 'init-db failed after retries (secrets, TLS, SG, or RDS readiness)'
fi

# Private backend TLS. ALB encrypts target connections and does not validate
# target certificates. The public ALB certificate is supplied separately in ACM.
phase 'Creating private backend TLS certificate'
openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
  -subj '/CN=gateway-private-backend' \
  -keyout /etc/gateway/tls/backend.key -out /etc/gateway/tls/backend.crt >>"$DETAIL_LOG" 2>&1
chown root:gateway /etc/gateway/tls/backend.key
chmod 640 /etc/gateway/tls/backend.key
chmod 644 /etc/gateway/tls/backend.crt
cat > /etc/systemd/system/gateway.service <<'UNIT'
[Unit]
Description=Corporate Cloud Storage Gateway Flask application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gateway
Group=gateway
RuntimeDirectory=gateway
RuntimeDirectoryMode=0750
WorkingDirectory=/opt/gateway/src
Environment=GATEWAY_CONFIG=/etc/gateway/runtime.json
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/gateway/venv/bin/gunicorn --bind 0.0.0.0:8443 --certfile /etc/gateway/tls/backend.crt --keyfile /etc/gateway/tls/backend.key --workers 2 --threads 4 --timeout 120 --access-logfile /var/log/gateway/access.log --error-logfile /var/log/gateway/error.log app:app
Restart=always
RestartSec=5
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
# One serving process instead of Nginx plus an application server.
# ALB and RDS metrics remain in CloudWatch without an installed log agent.
phase 'Starting HTTPS application service'
systemctl daemon-reload
systemctl enable --now gateway
phase 'Checking application and database readiness'
for attempt in $(seq 1 60); do
  if curl --fail --silent --show-error --max-time 8 --insecure https://127.0.0.1:8443/health; then
    phase 'READY'
    printf '\nGATEWAY_READY\n'
    exit 0
  fi
  sleep 5
done
journalctl -u gateway -n 40 --no-pager 2>/dev/null | tr '\n' ' ' | cut -c1-180 > "$ERROR_LOG" || true
fail 'Gateway did not become healthy; inspect journalctl -u gateway and /var/log/gateway/error.log'
