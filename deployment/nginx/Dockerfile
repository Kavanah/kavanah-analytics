FROM openresty/openresty:alpine

RUN apk add --no-cache certbot certbot-nginx apache2-utils gettext

COPY nginx.conf /usr/local/openresty/nginx/conf/nginx.conf
COPY conf.d/default.conf.template /usr/local/openresty/nginx/conf/conf.d/default.conf.template
COPY .htpasswd /etc/nginx/.htpasswd
COPY km-pw-reset.js /usr/local/openresty/nginx/html/km-pw-reset.js
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
