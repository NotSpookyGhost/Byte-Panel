#!/bin/bash
mkdir -p /data/plugins /data/logs
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf