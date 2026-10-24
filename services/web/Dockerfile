FROM nginx:1.27-alpine

COPY --chmod=0755 services/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --chmod=0755 services/web/static /usr/share/nginx/html
