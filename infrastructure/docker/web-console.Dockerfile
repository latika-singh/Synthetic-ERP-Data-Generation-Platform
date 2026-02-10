# =============================================================================
# Synthetic ERP Data Generation Platform — Web Console Dockerfile
# =============================================================================
# Production-optimized multi-stage Dockerfile for the React 19.x Web Console.
#
# Stage 1 (builder): Installs npm dependencies and compiles the Vite/React
#   production bundle with TypeScript 5.x and TailwindCSS 4.x processing.
# Stage 2 (production): Serves the static build output via nginx 1.25 with
#   SPA routing, gzip compression, security headers, and cache control.
#
# Build context: Project root (.)
# Source path:   src/web/
#
# Usage:
#   docker build -f infrastructure/docker/web-console.Dockerfile \
#     --build-arg VITE_API_BASE_URL=https://api.example.com \
#     --build-arg VITE_AUTH0_DOMAIN=example.auth0.com \
#     --build-arg VITE_AUTH0_CLIENT_ID=your_client_id \
#     --build-arg VITE_AUTH0_AUDIENCE=https://api.example.com \
#     -t synthetic-erp-web-console:latest .
#
# Air-gapped deployment (C-003):
#   Replace node:20-alpine / nginx:1.25-alpine with your private registry:
#     --build-arg NODE_IMAGE=registry.internal/node:20-alpine
#     --build-arg NGINX_IMAGE=registry.internal/nginx:1.25-alpine
# =============================================================================

# ---------------------------------------------------------------------------
# Global build arguments for private-registry overrides (air-gapped C-003)
# ---------------------------------------------------------------------------
ARG NODE_IMAGE=node:20-alpine
ARG NGINX_IMAGE=nginx:1.25-alpine

# =============================================================================
# Stage 1 — Build
# =============================================================================
# Uses node:20-alpine to install npm dependencies, compile TypeScript, process
# TailwindCSS, and produce an optimised Vite production bundle.
# =============================================================================
FROM ${NODE_IMAGE} AS builder

# -- Build-time arguments for Vite environment variable injection -------------
# These ARGs are baked into the static bundle at build time via import.meta.env.
ARG VITE_API_BASE_URL=http://localhost:5000
ARG VITE_AUTH0_DOMAIN=localhost
ARG VITE_AUTH0_CLIENT_ID=default_client_id
ARG VITE_AUTH0_AUDIENCE=https://api.synthetic-erp.local
ARG NODE_ENV=production

# -- Metadata labels for the builder stage ------------------------------------
LABEL stage=builder

# -- Working directory ---------------------------------------------------------
WORKDIR /app

# -- Install system-level build dependencies -----------------------------------
# Python and build tools are occasionally required by node-gyp native modules
# (e.g. some transitive dependencies of TailwindCSS or PostCSS plugins).
RUN apk add --no-cache python3 make g++

# -- Copy package manifests first for Docker layer caching --------------------
# This ensures that the expensive `npm ci` step is only re-run when dependency
# declarations change, not on every source-code edit.
COPY src/web/package.json src/web/package-lock.json* ./

# -- Install dependencies (deterministic, reproducible) -----------------------
# --production=false ensures devDependencies (Vite, TypeScript, TailwindCSS,
# ESLint, Prettier, testing libraries) are installed — they are required for
# the production build step.
RUN npm ci --production=false --ignore-scripts=false && \
    npm cache clean --force

# -- Copy all source and configuration files required for the build -----------
COPY src/web/tsconfig.json ./tsconfig.json
COPY src/web/tailwind.config.ts ./tailwind.config.ts
COPY src/web/vite.config.ts ./vite.config.ts
COPY src/web/.eslintrc.json ./.eslintrc.json
COPY src/web/.prettierrc ./.prettierrc
COPY src/web/index.html ./index.html
COPY src/web/src/ ./src/

# -- Propagate build-time ARGs into environment for Vite ----------------------
# Vite reads VITE_* variables from the process environment during build and
# inlines them into the client bundle as import.meta.env.VITE_*.
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_AUTH0_DOMAIN=${VITE_AUTH0_DOMAIN} \
    VITE_AUTH0_CLIENT_ID=${VITE_AUTH0_CLIENT_ID} \
    VITE_AUTH0_AUDIENCE=${VITE_AUTH0_AUDIENCE} \
    NODE_ENV=${NODE_ENV}

# -- Build the production bundle -----------------------------------------------
# `tsc && vite build` is the expected "build" script in package.json:
#   1. TypeScript compiler type-checks the entire project
#   2. Vite produces a tree-shaken, minified, code-split production bundle
#      with hashed filenames in /app/dist
RUN npm run build

# -- Remove source maps from the production bundle (optional security) --------
# Source maps can expose application logic; remove them for production images.
RUN find /app/dist -name '*.map' -type f -delete 2>/dev/null || true


# =============================================================================
# Stage 2 — Production
# =============================================================================
# Uses nginx:1.25-alpine to serve the static build output with optimised
# configuration for SPA routing, compression, security, and caching.
# =============================================================================
FROM ${NGINX_IMAGE} AS production

# -- Container metadata labels (OCI Image Spec) --------------------------------
LABEL org.opencontainers.image.title="synthetic-erp-web-console" \
      org.opencontainers.image.description="Web Console for Synthetic ERP Data Generation Platform" \
      org.opencontainers.image.source="infrastructure/docker/web-console.Dockerfile" \
      org.opencontainers.image.vendor="Synthetic ERP Platform" \
      org.opencontainers.image.licenses="MIT"

# -- Remove the default nginx configuration ------------------------------------
RUN rm -rf /etc/nginx/conf.d/default.conf /etc/nginx/nginx.conf

# -- Write the custom nginx configuration inline --------------------------------
# This avoids the need for a separate nginx.conf file in the build context and
# keeps the Dockerfile fully self-contained.
RUN cat > /etc/nginx/nginx.conf <<'NGINX_CONF'
# =============================================================================
# nginx.conf — Synthetic ERP Web Console
# =============================================================================
# Production-optimised configuration for serving the React SPA with:
#   • SPA routing (try_files fallback to index.html)
#   • Gzip compression for text-based assets
#   • Security headers (X-Frame-Options, CSP, HSTS-ready, etc.)
#   • Aggressive caching for hashed static assets
#   • No-cache for index.html (always serve the latest version)
# =============================================================================

# Worker configuration — auto-detect CPU cores
worker_processes auto;

# Limit file descriptors per worker to a safe default
worker_rlimit_nofile 2048;

# Error log destination (stderr for Docker log collection)
error_log /var/log/nginx/error.log warn;

# PID file location (writable by nginx user)
pid /tmp/nginx.pid;

events {
    worker_connections 1024;
    multi_accept on;
    use epoll;
}

http {
    # ---- Core settings -------------------------------------------------------
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # Structured access log format for observability
    log_format main escape=json
        '{'
            '"time":"$time_iso8601",'
            '"remote_addr":"$remote_addr",'
            '"request":"$request",'
            '"status":$status,'
            '"body_bytes_sent":$body_bytes_sent,'
            '"request_time":$request_time,'
            '"http_referer":"$http_referer",'
            '"http_user_agent":"$http_user_agent"'
        '}';

    access_log /var/log/nginx/access.log main;

    # Performance optimisations
    sendfile        on;
    tcp_nopush      on;
    tcp_nodelay     on;
    keepalive_timeout 65;
    types_hash_max_size 2048;

    # Hide nginx version in server header (security hardening)
    server_tokens off;

    # ---- Gzip compression ----------------------------------------------------
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 6;
    gzip_buffers 16 8k;
    gzip_http_version 1.1;
    gzip_min_length 256;
    gzip_types
        text/plain
        text/css
        text/xml
        text/javascript
        application/javascript
        application/x-javascript
        application/json
        application/xml
        application/xml+rss
        application/vnd.ms-fontobject
        application/x-font-ttf
        font/opentype
        image/svg+xml
        image/x-icon;

    # ---- Temporary file paths (writable by nginx user) -----------------------
    client_body_temp_path /tmp/client_temp;
    proxy_temp_path       /tmp/proxy_temp;
    fastcgi_temp_path     /tmp/fastcgi_temp;
    uwsgi_temp_path       /tmp/uwsgi_temp;
    scgi_temp_path        /tmp/scgi_temp;

    # ---- Server block --------------------------------------------------------
    server {
        listen 80;
        listen [::]:80;
        server_name _;

        root /usr/share/nginx/html;
        index index.html;

        # -- Security headers --------------------------------------------------
        # Prevent the page from being embedded in an iframe (clickjacking)
        add_header X-Frame-Options "DENY" always;

        # Prevent MIME-type sniffing
        add_header X-Content-Type-Options "nosniff" always;

        # Enable XSS filter in legacy browsers
        add_header X-XSS-Protection "1; mode=block" always;

        # Control referrer information sent with requests
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;

        # Permissions policy — disable unnecessary browser features
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;

        # Content Security Policy — allow Auth0 and API domains
        # The $VITE_* variables are baked into the JS bundle at build time, so
        # CSP must allow connections to the Auth0 domain and API Gateway.
        # Use 'self' as the baseline; operators should override via env as needed.
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https://*.auth0.com https://*.amazonaws.com http://localhost:* https://localhost:*; frame-src 'self' https://*.auth0.com; object-src 'none'; base-uri 'self'; form-action 'self';" always;

        # -- SPA fallback routing -----------------------------------------------
        # React Router handles client-side routing; nginx must serve index.html
        # for all paths that do not match a physical file on disk.
        location / {
            try_files $uri $uri/ /index.html;
        }

        # -- index.html: always re-validate (no-cache) -------------------------
        # The HTML entry point references hashed JS/CSS bundles; it must always
        # be fetched fresh so users receive the latest deployment.
        location = /index.html {
            add_header Cache-Control "no-cache, no-store, must-revalidate" always;
            add_header Pragma "no-cache" always;
            add_header Expires "0" always;
            # Re-apply security headers (add_header in a nested location block
            # overrides the parent block in nginx)
            add_header X-Frame-Options "DENY" always;
            add_header X-Content-Type-Options "nosniff" always;
            add_header X-XSS-Protection "1; mode=block" always;
            add_header Referrer-Policy "strict-origin-when-cross-origin" always;
        }

        # -- Static assets: aggressive caching ----------------------------------
        # Vite produces hashed filenames (e.g. index-abc123.js); these are
        # immutable and safe to cache for one year.
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot|webp|avif)$ {
            expires 1y;
            add_header Cache-Control "public, max-age=31536000, immutable" always;
            # Re-apply security headers
            add_header X-Frame-Options "DENY" always;
            add_header X-Content-Type-Options "nosniff" always;
            access_log off;
        }

        # -- Health check endpoint for Kubernetes probes ------------------------
        location = /healthz {
            access_log off;
            return 200 'ok';
            add_header Content-Type text/plain;
        }

        # -- Deny access to hidden files (.env, .git, etc.) --------------------
        location ~ /\. {
            deny all;
            access_log off;
            log_not_found off;
        }
    }
}
NGINX_CONF

# -- Copy the built static assets from Stage 1 ---------------------------------
COPY --from=builder /app/dist /usr/share/nginx/html

# -- Ensure log and temp directories are writable by nginx user ----------------
RUN mkdir -p /var/log/nginx /tmp/client_temp /tmp/proxy_temp \
             /tmp/fastcgi_temp /tmp/uwsgi_temp /tmp/scgi_temp && \
    chown -R nginx:nginx /usr/share/nginx/html \
                         /var/log/nginx \
                         /var/cache/nginx \
                         /tmp/client_temp \
                         /tmp/proxy_temp \
                         /tmp/fastcgi_temp \
                         /tmp/uwsgi_temp \
                         /tmp/scgi_temp \
                         /etc/nginx/nginx.conf && \
    chmod -R 755 /usr/share/nginx/html

# -- Expose port 80 (HTTP) -----------------------------------------------------
EXPOSE 80

# -- Health check (Kubernetes liveness/readiness probe compatible) -------------
# Uses wget (available in alpine) instead of curl for smaller image footprint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:80/ || exit 1

# -- Run as non-root nginx user (security hardening) ---------------------------
USER nginx

# -- Start nginx in foreground mode (required for Docker) ----------------------
CMD ["nginx", "-g", "daemon off;"]
