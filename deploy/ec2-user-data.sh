#!/bin/bash
# EC2 "User data" for Ubuntu 24.04: paste this whole file into
# Launch instance -> Advanced details -> User data.
# REPO_URL is this repo; ADMIN_KEY is optional.
REPO_URL="https://github.com/sakshhh/paytm-seat-reservation.git"
ADMIN_KEY=""   # optional: set your own admin key; leave empty to auto-generate one

set -euxo pipefail
exec > >(tee /var/log/seat-reservation-setup.log) 2>&1

# 1. Docker Engine + compose plugin (official install script)
curl -fsSL https://get.docker.com | sh

# 2. Kernel limits for a connection stampede
cat > /etc/sysctl.d/99-burst.conf <<'EOF'
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
fs.file-max = 2097152
EOF
sysctl --system

# 3. Code
git clone "$REPO_URL" /opt/seat-reservation
chmod +x /opt/seat-reservation/deploy/start.sh

# 4. Start now and on every boot
cat > /etc/systemd/system/seat-reservation.service <<EOF
[Unit]
Description=Seat reservation stack
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
$( [ -n "$ADMIN_KEY" ] && echo "Environment=ADMIN_KEY=$ADMIN_KEY" )
ExecStart=/opt/seat-reservation/deploy/start.sh
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now seat-reservation.service
