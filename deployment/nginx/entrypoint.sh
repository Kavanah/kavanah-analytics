#!/bin/sh
set -e

# Substitute SERVER_NAME, FB_SERVER_NAME, and ES_HOST into nginx config.
# Only these three variables are substituted — nginx's own $variable syntax is preserved.
mkdir -p /etc/nginx/conf.d
envsubst '${SERVER_NAME} ${FB_SERVER_NAME} ${ES_HOST}' \
  < /usr/local/openresty/nginx/conf/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf

exec /usr/local/openresty/bin/openresty -g 'daemon off;'
