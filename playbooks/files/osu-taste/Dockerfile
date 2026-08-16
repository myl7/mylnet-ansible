# Static site. The build already ran locally (vite build -> dist/); this image
# only serves the artifacts, so the container has no node toolchain.
FROM nginx:alpine

# nginx.conf adds gzip (data.json is ~14 MB and shrinks to ~3 MB over the wire)
# and cache headers; it replaces the stock default server.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY dist/ /usr/share/nginx/html/
