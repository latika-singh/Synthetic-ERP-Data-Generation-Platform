# =============================================================================
# Synthetic ERP Data Generation Platform — Web Console Dockerfile
# =============================================================================
# Multi-stage Docker build for the React 19.x + TypeScript 5.x + TailwindCSS 4.x
# frontend application. Stage 1 compiles the Vite/React production bundle using
# node:20-alpine. Stage 2 serves the static output via nginx:1.25-alpine with
# optimized configuration for SPA routing, gzip compression, security headers,
# and static asset caching.
#
# Usage:
#   docker build \
#     --build-arg VITE_API_BASE_URL=https://api.example.com \
#     --build-arg VITE_AUTH0_DOMAIN=example.auth0.com \
#     --build-arg VITE_AUTH0_CLIENT_ID=your-client-id \
#     -t synthetic-erp-web-console:latest .
#
# Air-gapped build (C-003):
#   docker build \
#     --build-arg NPM_REGISTRY=https://private-registry.internal/npm \
#     --build-arg VITE_API_BASE_URL=https://api.internal \
#     -t synthetic-erp-web-console:latest .
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Build — Compile the Vite/React production bundle
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build

# Container metadata labels
LABEL maintainer="Synthetic ERP Platform Team <platform-team@synthetic-erp.io>"
LABEL version="1.0.0"
LABEL description="Build stage for Synthetic ERP Data Generation Platform Web Console"
LABEL org.opencontainers.image.source="https://github.com/synthetic-erp/platform"

# Build arguments for environment-specific configuration injected at build time.
# Vite embeds these as static values in the compiled JavaScript bundle.
ARG NODE_ENV=production
ARG VITE_API_BASE_URL=http://localhost:5000
ARG VITE_AUTH0_DOMAIN=
ARG VITE_AUTH0_CLIENT_ID=
ARG NPM_REGISTRY=

# Propagate build args into environment variables so Vite can access them
# during the build step via import.meta.env
ENV NODE_ENV=${NODE_ENV}
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_AUTH0_DOMAIN=${VITE_AUTH0_DOMAIN}
ENV VITE_AUTH0_CLIENT_ID=${VITE_AUTH0_CLIENT_ID}

# Set working directory for the build context
WORKDIR /app

# (C-003) Support air-gapped environments by allowing a private NPM registry.
# When NPM_REGISTRY is provided, configure npm to use it instead of the public
# registry. This enables builds in isolated networks with mirrored packages.
RUN if [ -n "$NPM_REGISTRY" ]; then \
      npm config set registry "$NPM_REGISTRY"; \
    fi

# Copy dependency manifests first for optimal Docker layer caching.
# Only re-runs npm ci when package.json or package-lock.json change,
# avoiding full reinstall on every source code change.
COPY package.json package-lock.json* ./

# Install all dependencies (including devDependencies needed for building).
# --production=false ensures devDependencies are installed for TypeScript
# compilation, Vite bundling, TailwindCSS processing, and ESLint checks.
# --ignore-scripts prevents arbitrary post-install scripts for security.
RUN npm ci --production=false --ignore-scripts && \
    npm cache clean --force

# Copy configuration files required for the build process
COPY tsconfig.json ./
COPY tailwind.config.ts ./
COPY vite.config.ts ./
COPY postcss.config.* ./
COPY index.html ./

# Copy the application source code
COPY src/ ./src/

# Copy public assets directory if it exists
COPY public/ ./public/ 2>/dev/null || true

# Build the optimized production bundle using Vite.
# This runs TypeScript compilation, TailwindCSS processing,
# code splitting, tree-shaking, and minification.
# Output directory: /app/dist
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: Serve — Production nginx server for static assets
# ---------------------------------------------------------------------------
FROM nginx:1.25-alpine AS production

# Container metadata labels
LABEL maintainer="Synthetic ERP Platform Team <platform-team@synthetic-erp.io>"
LABEL version="1.0.0"
LABEL description="Synthetic ERP Data Generation Platform — Web Console"
LABEL org.opencontainers.image.title="synthetic-erp-web-console"
LABEL org.opencontainers.image.description="Self-service Web Console for the Synthetic ERP Data Generation Platform"
LABEL org.opencontainers.image.vendor="Synthetic ERP Platform"
LABEL org.opencontainers.image.licenses="MIT"

# Remove the default nginx configuration and static files
RUN rm -rf /etc/nginx/conf.d/default.conf /usr/share/nginx/html/*

# Write the custom nginx configuration inline.
# This configuration is optimized for serving a React single-page application
# with security hardening, gzip compression, and efficient static asset caching.
RUN cat > /etc/nginx/nginx.conf << 'NGINX_CONF'
# =========================================================================
# nginx configuration for Synthetic ERP Web Console
# Optimized for React SPA serving with security headers and compression
# =========================================================================

# Run as the unprivileged nginx user
user nginx;

# Auto-detect available CPU cores for worker processes
worker_processes auto;

# Error log configuration — warn level for production
error_log /var/log/nginx/error.log warn;

# PID file location
pid /var/run/nginx.pid;

# Event processing configuration
events {
    # Maximum simultaneous connections per worker
    worker_connections 1024;

    # Use epoll for efficient event handling on Linux
    use epoll;

    # Accept multiple connections at once
    multi_accept on;
}

http {
    # =====================================================================
    # Core Settings
    # =====================================================================

    # Include standard MIME types for proper Content-Type headers
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    # Optimize file serving with sendfile and TCP optimizations
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;

    # Connection timeouts
    keepalive_timeout 65;
    client_body_timeout 12;
    client_header_timeout 12;
    send_timeout 10;

    # Limit request body size (no file uploads through nginx)
    client_max_body_size 1m;

    # Hide nginx version from response headers for security
    server_tokens off;

    # =====================================================================
    # Logging Format
    # =====================================================================

    # JSON-structured access log format for log aggregation compatibility
    log_format json_combined escape=json
        '{'
            '"time_local":"$time_local",'
            '"remote_addr":"$remote_addr",'
            '"request_method":"$request_method",'
            '"request_uri":"$request_uri",'
            '"status":$status,'
            '"body_bytes_sent":$body_bytes_sent,'
            '"request_time":$request_time,'
            '"http_referrer":"$http_referer",'
            '"http_user_agent":"$http_user_agent",'
            '"http_x_forwarded_for":"$http_x_forwarded_for"'
        '}';

    access_log /var/log/nginx/access.log json_combined;

    # =====================================================================
    # Gzip Compression
    # =====================================================================

    # Enable gzip compression for text-based responses
    gzip on;

    # Minimum response size to trigger compression (bytes)
    gzip_min_length 256;

    # Compression level (1-9, 6 is a good balance of speed vs ratio)
    gzip_comp_level 6;

    # Enable compression for proxied requests
    gzip_proxied any;

    # Append Vary: Accept-Encoding header for proper caching
    gzip_vary on;

    # MIME types eligible for gzip compression
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
        application/xhtml+xml
        application/vnd.ms-fontobject
        application/x-font-ttf
        application/x-font-opentype
        font/opentype
        font/eot
        font/otf
        image/svg+xml;

    # Disable gzip for older browsers that don't handle it well
    gzip_disable "msie6";

    # =====================================================================
    # Server Block — Web Console
    # =====================================================================

    server {
        # Listen on port 80 (TLS termination handled by Kubernetes Ingress
        # or an upstream load balancer providing TLS 1.3)
        listen 80;
        listen [::]:80;

        # Server name (wildcard for flexibility across environments)
        server_name _;

        # Document root — static build output from Vite
        root /usr/share/nginx/html;
        index index.html;

        # =================================================================
        # Security Headers
        # =================================================================

        # Prevent the page from being embedded in iframes (clickjacking protection)
        add_header X-Frame-Options "DENY" always;

        # Prevent MIME type sniffing attacks
        add_header X-Content-Type-Options "nosniff" always;

        # Enable XSS filtering in older browsers
        add_header X-XSS-Protection "1; mode=block" always;

        # Referrer policy — send origin only for cross-origin requests
        add_header Referrer-Policy "strict-origin-when-cross-origin" always;

        # Content Security Policy — restrict resource loading sources
        # Allows Auth0 domains for authentication and API Gateway for data
        add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' https://*.auth0.com; frame-src 'self' https://*.auth0.com; object-src 'none'; base-uri 'self'; form-action 'self';" always;

        # Permissions Policy — disable unnecessary browser features
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;

        # =================================================================
        # SPA Routing — React Router Support
        # =================================================================

        # Main location block: try to serve the exact file, then the
        # directory, and finally fall back to index.html for client-side
        # routing. This is essential for React Router to handle routes
        # like /dashboard, /generation/wizard, /admin, etc.
        location / {
            try_files $uri $uri/ /index.html;

            # Disable caching for the HTML entry point so users always
            # get the latest application version after deployments
            add_header Cache-Control "no-store, no-cache, must-revalidate, proxy-revalidate" always;
            add_header Pragma "no-cache" always;
            add_header Expires "0" always;
        }

        # =================================================================
        # Static Asset Caching — Immutable Hashed Assets
        # =================================================================

        # JavaScript bundles — Vite produces content-hashed filenames
        # (e.g., index-abc123.js) making them safe for aggressive caching
        location ~* \.(?:js|mjs)$ {
            expires 1y;
            add_header Cache-Control "public, max-age=31536000, immutable" always;
            add_header X-Content-Type-Options "nosniff" always;
            access_log off;
        }

        # CSS stylesheets — content-hashed by Vite/TailwindCSS
        location ~* \.css$ {
            expires 1y;
            add_header Cache-Control "public, max-age=31536000, immutable" always;
            add_header X-Content-Type-Options "nosniff" always;
            access_log off;
        }

        # Images, icons, and fonts — long-term cache with immutable flag
        location ~* \.(?:png|jpg|jpeg|gif|ico|svg|webp|avif|woff|woff2|ttf|eot|otf)$ {
            expires 1y;
            add_header Cache-Control "public, max-age=31536000, immutable" always;
            add_header X-Content-Type-Options "nosniff" always;
            access_log off;
        }

        # Source maps — only accessible in non-production or for debugging
        location ~* \.map$ {
            return 404;
        }

        # =================================================================
        # Health Check Endpoint
        # =================================================================

        # Lightweight health check for Kubernetes liveness/readiness probes
        # and Docker health check commands. Returns 200 if nginx is serving.
        location /health {
            access_log off;
            return 200 '{"status":"healthy","service":"web-console"}';
            add_header Content-Type application/json;
        }

        # =================================================================
        # Error Pages
        # =================================================================

        # Custom error handling — return to SPA for client-side error pages
        error_page 404 /index.html;

        # Suppress error logging for favicon requests
        location = /favicon.ico {
            log_not_found off;
            access_log off;
        }

        # Suppress error logging for robots.txt
        location = /robots.txt {
            log_not_found off;
            access_log off;
        }

        # =================================================================
        # Security — Block Sensitive Paths
        # =================================================================

        # Deny access to hidden files (e.g., .env, .git)
        location ~ /\. {
            deny all;
            access_log off;
            log_not_found off;
        }
    }
}
NGINX_CONF

# Copy the built static assets from the build stage into the nginx
# document root. This is the only output carried from Stage 1.
COPY --from=build /app/dist /usr/share/nginx/html

# Ensure correct ownership of static files for the nginx user
RUN chown -R nginx:nginx /usr/share/nginx/html && \
    chmod -R 755 /usr/share/nginx/html

# Create required directories with proper permissions for non-root operation
RUN mkdir -p /var/cache/nginx /var/log/nginx /var/run && \
    chown -R nginx:nginx /var/cache/nginx /var/log/nginx /var/run && \
    chmod -R 755 /var/cache/nginx /var/log/nginx /var/run

# Expose HTTP port (TLS termination at Ingress/LB layer)
EXPOSE 80

# Health check — verifies nginx is serving the application.
# Used by Docker health monitoring and can supplement Kubernetes probes.
# Interval: 30s, Timeout: 5s, Start period: 10s, Retries: 3
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:80/health || exit 1

# Run as non-root nginx user for security hardening.
# The nginx master process handles privilege dropping internally,
# but setting USER ensures the container process itself is non-root.
USER nginx

# Start nginx in the foreground (required for Docker containers)
CMD ["nginx", "-g", "daemon off;"]
