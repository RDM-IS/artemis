#!/bin/bash
set -e

echo "Installing ACOS systemd service..."

sudo cp /home/ec2-user/artemis/infrastructure/acos.service \
  /etc/systemd/system/acos.service

sudo systemctl daemon-reload
sudo systemctl enable acos
sudo systemctl start acos
sudo systemctl status acos

echo "ACOS service installed and running."
