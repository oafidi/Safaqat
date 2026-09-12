FROM node:22-alpine AS styles

WORKDIR /build
COPY tailwind.config.js styles.css *.html *.js ./
RUN npx --yes tailwindcss@3.4.17 -c tailwind.config.js -i styles.css -o compiled.css --minify

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html dashboard.html offer.html auth.js dashboard.js offer.js /usr/share/nginx/html/
COPY --from=styles /build/compiled.css /usr/share/nginx/html/styles.css

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1
