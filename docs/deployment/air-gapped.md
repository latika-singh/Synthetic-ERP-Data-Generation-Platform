# Air-Gapped Deployment Guide

## Synthetic-ERP-Data-Generation-Platform — Offline / Disconnected Environment Deployment

> **Constraint C-003**: The platform must operate in air-gapped environments with offline package support and private registry mirroring.
>
> **Rule R-008**: All Docker images must be buildable from a private registry, all Python/Node.js packages must be installable from offline mirrors, and all Terraform providers must be cacheable locally.

This guide provides step-by-step procedures for deploying the Synthetic-ERP-Data-Generation-Platform in environments that have **no internet connectivity**. These procedures are essential for secure government, financial, defense, and other regulated environments where external network access is strictly prohibited.

---

## Table of Contents

1. [Prerequisites and Overview](#1-prerequisites-and-overview)
2. [Private Container Registry Mirroring](#2-private-container-registry-mirroring)
3. [Offline Python (pip) Package Repository](#3-offline-python-pip-package-repository)
4. [Offline Node.js (npm) Package Repository](#4-offline-nodejs-npm-package-repository)
5. [Terraform Provider Caching](#5-terraform-provider-caching)
6. [Private Helm Chart Repository](#6-private-helm-chart-repository)
7. [spaCy NLP Model Bundling](#7-spacy-nlp-model-bundling)
8. [JDBC Driver Bundling](#8-jdbc-driver-bundling)
9. [Verification Checklist](#9-verification-checklist)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites and Overview

### 1.1 Environment Architecture

An air-gapped deployment requires a **two-zone architecture**:

| Zone | Network Access | Purpose |
|------|---------------|---------|
| **Internet-Connected Staging Host** | Full internet | Downloads all artifacts (images, packages, providers, models) |
| **Air-Gapped Target Environment** | No internet | Hosts the production platform using only pre-staged artifacts |

Artifacts are transferred between zones via approved physical media (USB drives, DVDs) or a one-way data diode, depending on your organization's security policies.

### 1.2 Staging Host Requirements

The internet-connected staging host must have:

- **Docker Engine** 25.x or later
- **Python** 3.12 or later
- **Node.js** 20.x LTS (npm 10.x)
- **Terraform** 1.7 or later
- **Helm** 3.x
- **Git** 2.x
- **Sufficient disk space**: at least 100 GB free (GPU-enabled images such as PyTorch and TensorFlow are large)
- Access to:
  - Docker Hub (`docker.io`)
  - PyPI (`pypi.org`)
  - npm registry (`registry.npmjs.org`)
  - Terraform Registry (`registry.terraform.io`)
  - GitHub Releases (for spaCy models)

### 1.3 Air-Gapped Target Environment Requirements

The air-gapped target environment must have:

- **Docker Engine** 25.x or later
- **Kubernetes** 1.29 or later (managed or self-hosted)
- **kubectl** 1.29 or later
- **Helm** 3.x
- A **private container registry** (Harbor, Docker Registry, or cloud-native equivalent)
- A **private package repository** (devpi, Artifactory, or Nexus) for Python packages
- A **private npm registry** (Verdaccio, Artifactory, or Nexus) for Node.js packages
- A **file server or NFS share** for Terraform providers and Helm charts
- Sufficient compute and storage resources per the [Kubernetes Deployment Guide](./kubernetes.md)

### 1.4 Artifact Manifest

The following artifact categories must be transferred to the air-gapped environment:

| Category | Approximate Size | Transfer Format |
|----------|-----------------|-----------------|
| Docker images (base + custom) | ~25 GB | `.tar.gz` archives |
| Python packages (all services) | ~8 GB | Wheel/sdist files |
| Node.js packages (frontend) | ~500 MB | Tarball archive |
| Terraform providers | ~1 GB | Provider binary archives |
| Helm charts | ~50 MB | `.tgz` chart archives |
| spaCy NLP models | ~800 MB | Model packages |
| JDBC drivers | ~200 MB | JAR files |
| **Total** | **~35 GB** | — |

---

## 2. Private Container Registry Mirroring

### 2.1 Required Docker Images

#### Base Images (from Docker Hub)

| Image | Tag | Used By |
|-------|-----|---------|
| `python` | `3.12-slim` | All six backend services |
| `node` | `20-alpine` | Web Console build stage |
| `nginx` | `1.25-alpine` | Web Console production serving |
| `mongo` | `7.0` | MongoDB metadata repository |
| `redis` | `7-alpine` | Redis cache and session store |

#### Custom Service Images

| Image Name | Source Dockerfile | Description |
|------------|-------------------|-------------|
| `synthetic-erp/api-gateway` | `infrastructure/docker/api-gateway.Dockerfile` | Flask API Gateway service |
| `synthetic-erp/generation-engine` | `infrastructure/docker/generation-engine.Dockerfile` | Multi-method data generation engine |
| `synthetic-erp/profiling-service` | `infrastructure/docker/profiling-service.Dockerfile` | ERP schema discovery and statistical profiling |
| `synthetic-erp/quality-service` | `infrastructure/docker/quality-service.Dockerfile` | Data quality validation service |
| `synthetic-erp/compliance-service` | `infrastructure/docker/compliance-service.Dockerfile` | PII detection and regulatory compliance |
| `synthetic-erp/provisioning-service` | `infrastructure/docker/provisioning-service.Dockerfile` | Database connectors and cloud export |
| `synthetic-erp/web-console` | `src/web/Dockerfile` | React web console (served via nginx) |

### 2.2 Pull and Save Images on Staging Host

Run the following commands on the **internet-connected staging host** to pull all required images:

```bash
#!/usr/bin/env bash
# pull-images.sh — Pull all required Docker images on the staging host

set -euo pipefail

# Base images
docker pull python:3.12-slim
docker pull node:20-alpine
docker pull nginx:1.25-alpine
docker pull mongo:7.0
docker pull redis:7-alpine

echo "All base images pulled successfully."
```

Build the custom service images from the project source:

```bash
#!/usr/bin/env bash
# build-custom-images.sh — Build all custom service images

set -euo pipefail

REGISTRY_PREFIX="synthetic-erp"
TAG="latest"

# Build each service image
docker build -t ${REGISTRY_PREFIX}/api-gateway:${TAG} \
  -f infrastructure/docker/api-gateway.Dockerfile .

docker build -t ${REGISTRY_PREFIX}/generation-engine:${TAG} \
  -f infrastructure/docker/generation-engine.Dockerfile .

docker build -t ${REGISTRY_PREFIX}/profiling-service:${TAG} \
  -f infrastructure/docker/profiling-service.Dockerfile .

docker build -t ${REGISTRY_PREFIX}/quality-service:${TAG} \
  -f infrastructure/docker/quality-service.Dockerfile .

docker build -t ${REGISTRY_PREFIX}/compliance-service:${TAG} \
  -f infrastructure/docker/compliance-service.Dockerfile .

docker build -t ${REGISTRY_PREFIX}/provisioning-service:${TAG} \
  -f infrastructure/docker/provisioning-service.Dockerfile .

docker build -t ${REGISTRY_PREFIX}/web-console:${TAG} \
  -f src/web/Dockerfile .

echo "All custom service images built successfully."
```

Export all images into compressed tar archives for physical transfer:

```bash
#!/usr/bin/env bash
# save-images.sh — Save all Docker images to tar archives

set -euo pipefail

EXPORT_DIR="./airgap-artifacts/docker-images"
mkdir -p "${EXPORT_DIR}"

# Base images
docker save python:3.12-slim | gzip > "${EXPORT_DIR}/python-3.12-slim.tar.gz"
docker save node:20-alpine | gzip > "${EXPORT_DIR}/node-20-alpine.tar.gz"
docker save nginx:1.25-alpine | gzip > "${EXPORT_DIR}/nginx-1.25-alpine.tar.gz"
docker save mongo:7.0 | gzip > "${EXPORT_DIR}/mongo-7.0.tar.gz"
docker save redis:7-alpine | gzip > "${EXPORT_DIR}/redis-7-alpine.tar.gz"

# Custom service images
SERVICES=(
  "api-gateway"
  "generation-engine"
  "profiling-service"
  "quality-service"
  "compliance-service"
  "provisioning-service"
  "web-console"
)

for svc in "${SERVICES[@]}"; do
  docker save "synthetic-erp/${svc}:latest" | gzip > "${EXPORT_DIR}/${svc}.tar.gz"
done

echo "All images saved to ${EXPORT_DIR}/"
ls -lh "${EXPORT_DIR}/"
```

### 2.3 Set Up Private Registry in Air-Gapped Environment

#### Option A: Harbor (Recommended for Enterprise)

Harbor provides vulnerability scanning, RBAC, and image replication. Install Harbor on a dedicated host within the air-gapped network:

```bash
# Transfer the Harbor offline installer to the air-gapped environment
# Download from https://github.com/goharbor/harbor/releases on the staging host
# File: harbor-offline-installer-v2.10.0.tgz

# On the air-gapped host:
tar xzf harbor-offline-installer-v2.10.0.tgz
cd harbor

# Edit harbor.yml to configure:
#   hostname: registry.internal.example.com
#   https.certificate / https.private_key (TLS is mandatory for air-gapped security)
#   harbor_admin_password: <strong-password>
#   database.password: <strong-password>

./install.sh --with-trivy
```

#### Option B: Docker Registry (Lightweight)

For simpler deployments, a self-hosted Docker Registry is sufficient:

```bash
# Load the registry image from a pre-saved archive
docker load < registry-2.tar.gz

# Create a directory for persistent storage and TLS certificates
mkdir -p /opt/registry/data /opt/registry/certs

# Copy your TLS certificate and key
cp domain.crt /opt/registry/certs/
cp domain.key /opt/registry/certs/

# Start the registry
docker run -d \
  --restart=always \
  --name registry \
  -v /opt/registry/data:/var/lib/registry \
  -v /opt/registry/certs:/certs \
  -e REGISTRY_HTTP_ADDR=0.0.0.0:443 \
  -e REGISTRY_HTTP_TLS_CERTIFICATE=/certs/domain.crt \
  -e REGISTRY_HTTP_TLS_KEY=/certs/domain.key \
  -p 443:443 \
  registry:2
```

### 2.4 Load and Push Images to Private Registry

On the air-gapped host, load and re-tag images for the private registry:

```bash
#!/usr/bin/env bash
# load-and-push-images.sh — Load images and push to private registry

set -euo pipefail

PRIVATE_REGISTRY="registry.internal.example.com"
IMAGE_DIR="./airgap-artifacts/docker-images"

# Load base images
echo "Loading base images..."
docker load < "${IMAGE_DIR}/python-3.12-slim.tar.gz"
docker load < "${IMAGE_DIR}/node-20-alpine.tar.gz"
docker load < "${IMAGE_DIR}/nginx-1.25-alpine.tar.gz"
docker load < "${IMAGE_DIR}/mongo-7.0.tar.gz"
docker load < "${IMAGE_DIR}/redis-7-alpine.tar.gz"

# Re-tag and push base images
docker tag python:3.12-slim "${PRIVATE_REGISTRY}/library/python:3.12-slim"
docker push "${PRIVATE_REGISTRY}/library/python:3.12-slim"

docker tag node:20-alpine "${PRIVATE_REGISTRY}/library/node:20-alpine"
docker push "${PRIVATE_REGISTRY}/library/node:20-alpine"

docker tag nginx:1.25-alpine "${PRIVATE_REGISTRY}/library/nginx:1.25-alpine"
docker push "${PRIVATE_REGISTRY}/library/nginx:1.25-alpine"

docker tag mongo:7.0 "${PRIVATE_REGISTRY}/library/mongo:7.0"
docker push "${PRIVATE_REGISTRY}/library/mongo:7.0"

docker tag redis:7-alpine "${PRIVATE_REGISTRY}/library/redis:7-alpine"
docker push "${PRIVATE_REGISTRY}/library/redis:7-alpine"

# Load and push custom service images
SERVICES=(
  "api-gateway"
  "generation-engine"
  "profiling-service"
  "quality-service"
  "compliance-service"
  "provisioning-service"
  "web-console"
)

for svc in "${SERVICES[@]}"; do
  echo "Loading and pushing ${svc}..."
  docker load < "${IMAGE_DIR}/${svc}.tar.gz"
  docker tag "synthetic-erp/${svc}:latest" "${PRIVATE_REGISTRY}/synthetic-erp/${svc}:latest"
  docker push "${PRIVATE_REGISTRY}/synthetic-erp/${svc}:latest"
done

echo "All images loaded and pushed to ${PRIVATE_REGISTRY}"
```

### 2.5 Update Dockerfiles for Private Registry

All Dockerfiles must reference the private registry. Update the `FROM` directives:

```dockerfile
# Before (internet-connected):
FROM python:3.12-slim AS base

# After (air-gapped):
ARG REGISTRY=registry.internal.example.com/library
FROM ${REGISTRY}/python:3.12-slim AS base
```

Pass the `--build-arg REGISTRY=registry.internal.example.com/library` flag when building in the air-gapped environment. Alternatively, configure Docker daemon mirrors:

```json
{
  "registry-mirrors": ["https://registry.internal.example.com"],
  "insecure-registries": []
}
```

Save this as `/etc/docker/daemon.json` and restart the Docker daemon.

### 2.6 Update Kubernetes Manifests

All Kubernetes Deployment and StatefulSet manifests must reference the private registry. Update the `image` fields:

```yaml
# Before:
image: synthetic-erp/api-gateway:latest

# After:
image: registry.internal.example.com/synthetic-erp/api-gateway:latest
```

If your private registry requires authentication, create an image pull secret:

```bash
kubectl create secret docker-registry regcred \
  --docker-server=registry.internal.example.com \
  --docker-username=<username> \
  --docker-password=<password> \
  --namespace=synthetic-erp-platform

# Reference in Deployments:
# spec:
#   imagePullSecrets:
#     - name: regcred
```

---

## 3. Offline Python (pip) Package Repository

### 3.1 Download Packages on Staging Host

Each backend service has its own `requirements.txt`. Download all packages (including dependencies) as wheel files on the staging host.

```bash
#!/usr/bin/env bash
# download-pip-packages.sh — Download all Python packages for offline installation

set -euo pipefail

EXPORT_DIR="./airgap-artifacts/pip-packages"
mkdir -p "${EXPORT_DIR}"

# Target platform specification
# Adjust --platform and --python-version to match your air-gapped environment
PLATFORM_ARGS="--platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all:"

SERVICES=(
  "src/backend/api_gateway"
  "src/backend/generation_engine"
  "src/backend/profiling_service"
  "src/backend/quality_service"
  "src/backend/compliance_service"
  "src/backend/provisioning_service"
)

for svc_path in "${SERVICES[@]}"; do
  svc_name=$(basename "${svc_path}")
  echo "Downloading packages for ${svc_name}..."
  
  pip download \
    -r "${svc_path}/requirements.txt" \
    -d "${EXPORT_DIR}/${svc_name}" \
    ${PLATFORM_ARGS} || {
      # Fallback: some packages may not have pre-built wheels; download source distributions
      echo "Retrying ${svc_name} without --only-binary..."
      pip download \
        -r "${svc_path}/requirements.txt" \
        -d "${EXPORT_DIR}/${svc_name}"
    }
done

# Download shared utilities dependencies (if any)
if [ -f "src/backend/shared/requirements.txt" ]; then
  echo "Downloading shared utility packages..."
  pip download \
    -r "src/backend/shared/requirements.txt" \
    -d "${EXPORT_DIR}/shared" \
    ${PLATFORM_ARGS} || pip download \
      -r "src/backend/shared/requirements.txt" \
      -d "${EXPORT_DIR}/shared"
fi

echo "All pip packages downloaded to ${EXPORT_DIR}/"
du -sh "${EXPORT_DIR}"/*
```

### 3.2 Handle Platform-Specific Binary Wheels

Certain packages require platform-specific compiled wheels. These must be downloaded for the **exact target platform** (typically `manylinux2014_x86_64` for Linux):

| Package | Notes |
|---------|-------|
| `numpy` 1.26+ | Pre-built wheels available for `manylinux2014_x86_64` |
| `scipy` 1.12+ | Requires BLAS/LAPACK; use pre-built `manylinux` wheels |
| `torch` 2.x | Very large (~2 GB); download CPU-only variant with `--extra-index-url https://download.pytorch.org/whl/cpu` if GPU is not available |
| `tensorflow` 2.x | ~600 MB; CPU-only variant available |
| `pyarrow` 15.x | Pre-built wheels available |
| `cryptography` 42.x | Requires OpenSSL headers; use pre-built wheels |
| `spacy` 3.7.x | Pre-built wheels available; NLP models handled separately (Section 7) |

For PyTorch CPU-only builds (if the air-gapped environment does not have GPUs):

```bash
pip download torch==2.2.0+cpu \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  -d "${EXPORT_DIR}/generation_engine"
```

For PyTorch with CUDA support (if GPUs are available):

```bash
pip download torch==2.2.0+cu121 \
  --extra-index-url https://download.pytorch.org/whl/cu121 \
  -d "${EXPORT_DIR}/generation_engine"
```

### 3.3 Set Up Private PyPI Mirror

#### Option A: devpi (Recommended for Simplicity)

On the air-gapped environment, set up devpi as a local PyPI mirror:

```bash
# Install devpi from the offline package cache (devpi must be pre-downloaded)
pip install --no-index --find-links=./airgap-artifacts/pip-packages/shared devpi-server devpi-web

# Initialize and start devpi
devpi-server --init
devpi-server --start --host 0.0.0.0 --port 3141

# Create an index and upload packages
devpi login root --password=""
devpi index -c root/local type=local
devpi use root/local

# Upload all downloaded packages
for svc_dir in ./airgap-artifacts/pip-packages/*/; do
  devpi upload --from-dir "${svc_dir}"
done
```

#### Option B: JFrog Artifactory or Sonatype Nexus

For enterprise deployments, use Artifactory or Nexus as a private PyPI repository:

1. Install Artifactory/Nexus on a server within the air-gapped network.
2. Create a local PyPI repository (e.g., `pypi-local`).
3. Upload all wheel/sdist files from `./airgap-artifacts/pip-packages/` to the repository.
4. Configure the repository URL in pip configuration (see Section 3.4).

#### Option C: Simple Local Directory (No Server Required)

For the simplest approach, serve packages directly from a shared filesystem:

```bash
# Merge all packages into a single directory
mkdir -p /opt/pip-packages
for svc_dir in ./airgap-artifacts/pip-packages/*/; do
  cp "${svc_dir}"/*.whl "${svc_dir}"/*.tar.gz /opt/pip-packages/ 2>/dev/null || true
done
```

### 3.4 Configure pip for Offline Installation

Create or update `pip.conf` on every host (or bake it into Docker images):

```ini
# /etc/pip.conf (system-wide) or ~/.config/pip/pip.conf (per-user)

# Option A: devpi mirror
[global]
index-url = http://devpi.internal.example.com:3141/root/local/+simple/
trusted-host = devpi.internal.example.com

# Option B: Artifactory
# [global]
# index-url = https://artifactory.internal.example.com/api/pypi/pypi-local/simple
# trusted-host = artifactory.internal.example.com

# Option C: Local directory (for Docker builds)
# [global]
# no-index = true
# find-links = /opt/pip-packages
```

For Docker builds, update the Dockerfiles to use the local package source:

```dockerfile
# Copy packages into the build context
COPY ./airgap-artifacts/pip-packages/api_gateway /tmp/pip-packages

# Install from local directory
RUN pip install --no-index --find-links=/tmp/pip-packages -r requirements.txt

# Clean up
RUN rm -rf /tmp/pip-packages
```

### 3.5 Per-Service Package Reference

The following table summarizes the key packages required by each service:

| Service | Key Packages |
|---------|-------------|
| **api_gateway** | flask 3.1.x, flask-restful 0.3.x, flask-jwt-extended 4.6.x, flask-cors 4.0.x, gunicorn 21.x, pymongo 4.x, redis 5.x, pydantic 2.x, structlog 24.x, opentelemetry-api 1.x, opentelemetry-sdk 1.x, opentelemetry-instrumentation-flask 0.x, prometheus-client 0.20.x, python-jose 3.3.x, circuitbreaker 2.0.x |
| **generation_engine** | langchain 0.3.x, langchain-community 0.3.x, torch 2.x, tensorflow 2.x, scipy 1.12+, numpy 1.26+, pandas 2.x, pyarrow 15.x, pymongo 4.x, redis 5.x, pydantic 2.x, structlog 24.x |
| **profiling_service** | scipy 1.12+, numpy 1.26+, pandas 2.x, jaydebeapi 1.2.x, pymongo 4.x, pydantic 2.x, structlog 24.x |
| **quality_service** | great-expectations 0.18.x, pydantic 2.x, scipy 1.12+, pymongo 4.x, structlog 24.x |
| **compliance_service** | spacy 3.7.x, pydantic 2.x, pymongo 4.x, python-jose 3.3.x, cryptography 42.x, structlog 24.x |
| **provisioning_service** | boto3 1.34.x, azure-storage-blob 12.x, google-cloud-storage 2.x, jaydebeapi 1.2.x, pymongo 4.x, pydantic 2.x, structlog 24.x |

---

## 4. Offline Node.js (npm) Package Repository

### 4.1 Download Frontend Packages on Staging Host

On the internet-connected staging host, install and pack all frontend dependencies:

```bash
#!/usr/bin/env bash
# download-npm-packages.sh — Create offline npm package cache

set -euo pipefail

EXPORT_DIR="./airgap-artifacts/npm-packages"
mkdir -p "${EXPORT_DIR}"

cd src/web

# Clean install to ensure lock file consistency
npm ci

# Method 1: Pack all dependencies into tarballs
# This creates a directory of .tgz files for each package
npm pack --pack-destination="${EXPORT_DIR}" $(npm ls --all --parseable | tail -n +2 | xargs -I{} basename {})

# Method 2 (Recommended): Create a tarball of the entire node_modules
tar czf "${EXPORT_DIR}/node_modules.tar.gz" node_modules/

# Also preserve the lock file for reproducible installs
cp package-lock.json "${EXPORT_DIR}/"
cp package.json "${EXPORT_DIR}/"

cd ../..

echo "npm packages archived to ${EXPORT_DIR}/"
du -sh "${EXPORT_DIR}"
```

### 4.2 Set Up Verdaccio (Private npm Registry)

Verdaccio is a lightweight private npm registry suitable for air-gapped environments:

```bash
# On the staging host, download Verdaccio and its dependencies
mkdir -p ./airgap-artifacts/verdaccio
cd ./airgap-artifacts/verdaccio
npm pack verdaccio
npm install --global verdaccio --prefix ./verdaccio-install

# Transfer the verdaccio-install directory to the air-gapped environment
```

On the air-gapped environment:

```bash
# Install Verdaccio from offline cache
npm install --global --offline verdaccio

# Configure Verdaccio
cat > /opt/verdaccio/config.yaml <<'EOF'
storage: /opt/verdaccio/storage
auth:
  htpasswd:
    file: /opt/verdaccio/htpasswd
    max_users: 100
uplinks: {}
packages:
  '**':
    access: $all
    publish: $authenticated
    proxy: ""
listen: 0.0.0.0:4873
max_body_size: 200mb
EOF

# Start Verdaccio
verdaccio --config /opt/verdaccio/config.yaml &

# Publish all packages from tarballs
for pkg_tarball in ./airgap-artifacts/npm-packages/*.tgz; do
  npm publish "${pkg_tarball}" --registry http://localhost:4873
done
```

### 4.3 Alternative: Direct node_modules Transfer

For the simplest approach (without running a private registry), transfer `node_modules` directly:

```bash
# On the air-gapped build host:
cd src/web

# Extract the pre-built node_modules
tar xzf /path/to/airgap-artifacts/npm-packages/node_modules.tar.gz

# Copy the preserved lock file
cp /path/to/airgap-artifacts/npm-packages/package-lock.json .

# Verify integrity
npm ls --all

# Build the frontend application
npm run build
```

### 4.4 Configure npm for Offline Usage

Configure npm on air-gapped hosts to use the private registry:

```bash
# Point npm to the private Verdaccio registry
npm config set registry http://verdaccio.internal.example.com:4873

# Disable strict SSL if using self-signed certificates
npm config set strict-ssl false

# For Docker builds, set the registry via build arguments
docker build \
  --build-arg NPM_REGISTRY=http://verdaccio.internal.example.com:4873 \
  -t synthetic-erp/web-console:latest \
  -f src/web/Dockerfile .
```

Update the Web Console Dockerfile to accept the registry argument:

```dockerfile
ARG NPM_REGISTRY=https://registry.npmjs.org
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm config set registry ${NPM_REGISTRY} && npm ci
COPY . .
RUN npm run build

FROM nginx:1.25-alpine
COPY --from=build /app/dist /usr/share/nginx/html
```

### 4.5 Key Frontend Packages Reference

| Package | Version | Purpose |
|---------|---------|---------|
| `react` | 19.x | UI component framework |
| `react-dom` | 19.x | React DOM rendering |
| `react-router-dom` | 6.x | Client-side routing |
| `typescript` | 5.x | TypeScript language |
| `tailwindcss` | 4.x | Utility-first CSS framework |
| `zustand` | 4.x | Lightweight state management |
| `axios` | 1.7.x | HTTP client for API calls |
| `recharts` | 2.x | React charting library |
| `@auth0/auth0-react` | 2.x | Auth0 React SDK |
| `vite` | 5.x | Build tool and dev server |
| `@vitejs/plugin-react` | 4.x | Vite React plugin |

---

## 5. Terraform Provider Caching

### 5.1 Mirror Providers on Staging Host

Terraform supports creating a local filesystem mirror of all required providers:

```bash
#!/usr/bin/env bash
# mirror-terraform-providers.sh — Cache all Terraform providers for offline use

set -euo pipefail

EXPORT_DIR="./airgap-artifacts/terraform-providers"
mkdir -p "${EXPORT_DIR}"

cd infrastructure/terraform

# Create a provider mirror from the current configuration
terraform providers mirror -platform=linux_amd64 "${EXPORT_DIR}"

cd ../..

echo "Terraform providers mirrored to ${EXPORT_DIR}/"
find "${EXPORT_DIR}" -type f | head -20
du -sh "${EXPORT_DIR}"
```

This mirrors all providers declared in `infrastructure/terraform/versions.tf`:

| Provider | Registry Path | Version |
|----------|--------------|---------|
| AWS | `hashicorp/aws` | 5.x |
| Azure | `hashicorp/azurerm` | 3.x |
| GCP | `hashicorp/google` | 5.x |
| Kubernetes | `hashicorp/kubernetes` | 2.x |
| Helm | `hashicorp/helm` | 2.x |

### 5.2 Transfer and Configure Filesystem Mirror

Transfer the `terraform-providers/` directory to the air-gapped environment and configure Terraform to use it.

Create or update the Terraform CLI configuration file on every host that runs Terraform:

```hcl
# ~/.terraformrc (Linux/macOS) or %APPDATA%\terraform.rc (Windows)

provider_installation {
  filesystem_mirror {
    path    = "/opt/terraform-providers"
    include = ["registry.terraform.io/*/*"]
  }
  direct {
    exclude = ["registry.terraform.io/*/*"]
  }
}
```

Copy the mirrored providers to the configured path:

```bash
# Transfer and extract
cp -r ./airgap-artifacts/terraform-providers/* /opt/terraform-providers/

# Verify the mirror structure
ls -R /opt/terraform-providers/registry.terraform.io/
# Expected structure:
# registry.terraform.io/
# ├── hashicorp/
# │   ├── aws/
# │   │   └── 5.x.x/
# │   │       └── linux_amd64/
# │   │           └── terraform-provider-aws_v5.x.x_x5
# │   ├── azurerm/
# │   ├── google/
# │   ├── kubernetes/
# │   └── helm/
```

### 5.3 Verify Terraform Initialization

```bash
cd infrastructure/terraform

# Initialize Terraform using the local mirror (no internet access required)
terraform init

# Verify providers are loaded from the filesystem mirror
terraform providers

# Expected output should list all five providers with local source paths
```

### 5.4 Terraform Module Caching

If using external Terraform modules (from the Terraform Registry), cache them as well:

```bash
# On the staging host, download modules
terraform get -update

# The modules are cached in .terraform/modules/
# Archive the modules directory
tar czf ./airgap-artifacts/terraform-modules.tar.gz .terraform/modules/

# On the air-gapped environment, extract before running terraform init
tar xzf terraform-modules.tar.gz -C infrastructure/terraform/
```

---

## 6. Private Helm Chart Repository

### 6.1 Required Helm Charts

The platform may use the following Helm charts for infrastructure components:

| Chart | Repository | Purpose |
|-------|-----------|---------|
| `ingress-nginx` | `https://kubernetes.github.io/ingress-nginx` | NGINX Ingress Controller |
| `prometheus` | `https://prometheus-community.github.io/helm-charts` | Monitoring stack |
| `grafana` | `https://grafana.github.io/helm-charts` | Dashboards and visualization |
| `sealed-secrets` | `https://bitnami-labs.github.io/sealed-secrets` | Encrypted secrets management |
| `cert-manager` | `https://charts.jetstack.io` | TLS certificate automation |

### 6.2 Download Charts on Staging Host

```bash
#!/usr/bin/env bash
# download-helm-charts.sh — Download all required Helm charts

set -euo pipefail

EXPORT_DIR="./airgap-artifacts/helm-charts"
mkdir -p "${EXPORT_DIR}"

# Add chart repositories
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm repo add jetstack https://charts.jetstack.io
helm repo update

# Download charts as .tgz archives
helm pull ingress-nginx/ingress-nginx --destination "${EXPORT_DIR}"
helm pull prometheus-community/prometheus --destination "${EXPORT_DIR}"
helm pull grafana/grafana --destination "${EXPORT_DIR}"
helm pull sealed-secrets/sealed-secrets --destination "${EXPORT_DIR}"
helm pull jetstack/cert-manager --destination "${EXPORT_DIR}"

echo "Helm charts downloaded to ${EXPORT_DIR}/"
ls -lh "${EXPORT_DIR}"
```

### 6.3 Set Up ChartMuseum in Air-Gapped Environment

ChartMuseum serves as a private Helm chart repository:

```bash
# Transfer the ChartMuseum binary and chart archives to the air-gapped environment

# Start ChartMuseum (pre-downloaded binary)
./chartmuseum --debug \
  --port=8080 \
  --storage="local" \
  --storage-local-rootdir="/opt/helm-charts" &

# Upload charts
for chart in ./airgap-artifacts/helm-charts/*.tgz; do
  curl --data-binary "@${chart}" http://localhost:8080/api/charts
done

# Add ChartMuseum as a Helm repository
helm repo add internal http://chartmuseum.internal.example.com:8080
helm repo update
```

### 6.4 Alternative: OCI-Based Helm Registry

If you are already running Harbor as your private container registry, you can push Helm charts as OCI artifacts:

```bash
# Enable OCI support (default in Helm 3.8+)
export HELM_EXPERIMENTAL_OCI=1

# Login to Harbor
helm registry login registry.internal.example.com

# Push charts to Harbor
for chart in ./airgap-artifacts/helm-charts/*.tgz; do
  helm push "${chart}" oci://registry.internal.example.com/charts
done

# Install from OCI registry
helm install ingress-nginx oci://registry.internal.example.com/charts/ingress-nginx
```

---

## 7. spaCy NLP Model Bundling

The Compliance Service uses spaCy for NLP-based PII detection. spaCy models must be pre-downloaded and bundled into the Docker image since they are fetched from GitHub Releases at install time.

### 7.1 Download spaCy Models on Staging Host

```bash
#!/usr/bin/env bash
# download-spacy-models.sh — Download spaCy NLP models for offline use

set -euo pipefail

EXPORT_DIR="./airgap-artifacts/spacy-models"
mkdir -p "${EXPORT_DIR}"

# Ensure spaCy is installed
pip install spacy==3.7.*

# Download the model packages as wheel files
pip download en_core_web_sm==3.7.* -d "${EXPORT_DIR}" --no-deps
pip download en_core_web_lg==3.7.* -d "${EXPORT_DIR}" --no-deps

echo "spaCy models downloaded to ${EXPORT_DIR}/"
ls -lh "${EXPORT_DIR}"
```

### 7.2 Bundle Models into the Compliance Service Dockerfile

Update the Compliance Service Dockerfile to install spaCy models from local files:

```dockerfile
# infrastructure/docker/compliance-service.Dockerfile (air-gapped variant)

ARG REGISTRY=registry.internal.example.com/library
FROM ${REGISTRY}/python:3.12-slim AS base

WORKDIR /app

# Copy requirements and install from offline cache
COPY ./airgap-artifacts/pip-packages/compliance_service /tmp/pip-packages
COPY src/backend/compliance_service/requirements.txt .
RUN pip install --no-index --find-links=/tmp/pip-packages -r requirements.txt

# Install spaCy models from pre-downloaded wheels
COPY ./airgap-artifacts/spacy-models /tmp/spacy-models
RUN pip install --no-index --find-links=/tmp/spacy-models \
  en_core_web_sm==3.7.* \
  en_core_web_lg==3.7.*

# Verify models are installed
RUN python -c "import spacy; spacy.load('en_core_web_sm'); spacy.load('en_core_web_lg'); print('spaCy models OK')"

# Copy application code
COPY src/backend/compliance_service/ .
COPY src/backend/shared/ ./shared/

# Clean up
RUN rm -rf /tmp/pip-packages /tmp/spacy-models

EXPOSE 5004
CMD ["gunicorn", "--bind", "0.0.0.0:5004", "--workers", "4", "wsgi:app"]
```

### 7.3 Verify Model Availability

After building the image, verify that spaCy models are available:

```bash
docker run --rm synthetic-erp/compliance-service:latest \
  python -c "
import spacy
nlp_sm = spacy.load('en_core_web_sm')
nlp_lg = spacy.load('en_core_web_lg')
doc = nlp_lg('John Smith lives at 123 Main Street, New York.')
print('Entities found:', [(ent.text, ent.label_) for ent in doc.ents])
print('spaCy models verified successfully.')
"
```

---

## 8. JDBC Driver Bundling

The **Profiling Service** and **Provisioning Service** use JDBC drivers (via JayDeBeApi) to connect to target ERP databases. These drivers must be included in the Docker images.

### 8.1 Required JDBC Drivers

| Database | Driver JAR | Download Source | License |
|----------|-----------|-----------------|---------|
| PostgreSQL | `postgresql-42.7.x.jar` | [jdbc.postgresql.org](https://jdbc.postgresql.org/) | BSD-2-Clause |
| Oracle | `ojdbc11.jar` | [Oracle Technology Network](https://www.oracle.com/database/technologies/appdev/jdbc-downloads.html) | Oracle Free Use Terms |
| SQL Server | `mssql-jdbc-12.x.jre11.jar` | [Microsoft Download Center](https://learn.microsoft.com/en-us/sql/connect/jdbc/download-microsoft-jdbc-driver-for-sql-server) | MIT |
| SAP HANA | `ngdbc-2.x.jar` | [SAP Development Tools](https://tools.hana.ondemand.com/#hanatools) | SAP Developer License |

### 8.2 Download Drivers on Staging Host

```bash
#!/usr/bin/env bash
# download-jdbc-drivers.sh — Download all required JDBC drivers

set -euo pipefail

EXPORT_DIR="./airgap-artifacts/jdbc-drivers"
mkdir -p "${EXPORT_DIR}"

# PostgreSQL JDBC driver
curl -Lo "${EXPORT_DIR}/postgresql-42.7.3.jar" \
  "https://jdbc.postgresql.org/download/postgresql-42.7.3.jar"

# Oracle JDBC driver (requires acceptance of Oracle license)
# Download ojdbc11.jar from Oracle Technology Network and place in EXPORT_DIR
echo "NOTE: Download ojdbc11.jar manually from Oracle and place in ${EXPORT_DIR}/"

# SQL Server JDBC driver
curl -Lo "${EXPORT_DIR}/mssql-jdbc-12.6.1.jre11.jar" \
  "https://repo1.maven.org/maven2/com/microsoft/sqlserver/mssql-jdbc/12.6.1.jre11/mssql-jdbc-12.6.1.jre11.jar"

# SAP HANA JDBC driver (requires SAP developer account)
# Download ngdbc-2.x.jar from SAP Development Tools and place in EXPORT_DIR
echo "NOTE: Download ngdbc-2.x.jar manually from SAP and place in ${EXPORT_DIR}/"

echo "JDBC drivers prepared in ${EXPORT_DIR}/"
ls -lh "${EXPORT_DIR}"
```

### 8.3 Bundle Drivers into Service Dockerfiles

Both the Profiling Service and Provisioning Service Dockerfiles must include a JRE and the JDBC drivers:

```dockerfile
# Example: infrastructure/docker/profiling-service.Dockerfile (air-gapped variant)

ARG REGISTRY=registry.internal.example.com/library
FROM ${REGISTRY}/python:3.12-slim AS base

# Install JRE for JDBC connectivity (required by JayDeBeApi)
RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set JAVA_HOME
ENV JAVA_HOME=/usr/lib/jvm/default-java

WORKDIR /app

# Copy and install Python packages from offline cache
COPY ./airgap-artifacts/pip-packages/profiling_service /tmp/pip-packages
COPY src/backend/profiling_service/requirements.txt .
RUN pip install --no-index --find-links=/tmp/pip-packages -r requirements.txt

# Copy JDBC drivers
COPY ./airgap-artifacts/jdbc-drivers/ /opt/jdbc-drivers/
ENV CLASSPATH="/opt/jdbc-drivers/*"

# Copy application code
COPY src/backend/profiling_service/ .
COPY src/backend/shared/ ./shared/

# Clean up
RUN rm -rf /tmp/pip-packages

EXPOSE 5002
CMD ["gunicorn", "--bind", "0.0.0.0:5002", "--workers", "4", "wsgi:app"]
```

Apply the same pattern to the **Provisioning Service** Dockerfile (port 5005).

### 8.4 Handle JRE for Air-Gapped Environments

If `apt-get install default-jre-headless` is not available (no APT mirror), pre-install the JRE in the base image:

```bash
# On the staging host, create a custom base image with JRE
docker build -t custom-python-jre:3.12-slim -f - . <<'EOF'
FROM python:3.12-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/default-java
EOF

# Save and transfer this image
docker save custom-python-jre:3.12-slim | gzip > \
  ./airgap-artifacts/docker-images/custom-python-jre-3.12-slim.tar.gz
```

### 8.5 APT Package Mirroring (Optional)

If your Dockerfiles require system packages (`apt-get install`), set up a local APT mirror or pre-bake packages:

```bash
# On the staging host, download .deb packages for the target distribution
mkdir -p ./airgap-artifacts/apt-packages
cd ./airgap-artifacts/apt-packages

# Use apt-get download to fetch .deb files (run in a container matching the base image)
docker run --rm -v "$(pwd):/export" python:3.12-slim bash -c "
  apt-get update
  apt-get download -y default-jre-headless $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances default-jre-headless | grep '^\w' | sort -u)
  cp /var/cache/apt/archives/*.deb /export/ 2>/dev/null || true
  mv *.deb /export/ 2>/dev/null || true
"
```

On the air-gapped environment, install from local `.deb` files:

```dockerfile
COPY ./airgap-artifacts/apt-packages /tmp/apt-packages
RUN dpkg -i /tmp/apt-packages/*.deb || apt-get install -f -y && \
    rm -rf /tmp/apt-packages
```

---

## 9. Verification Checklist

Use the following checklist to verify that the air-gapped environment is fully prepared before deploying the platform.

### 9.1 Container Registry Verification

- [ ] Private registry (Harbor/Docker Registry) is running and accessible via HTTPS
- [ ] TLS certificates are valid and trusted by all nodes
- [ ] All **5 base images** are present in the registry:
  - [ ] `python:3.12-slim`
  - [ ] `node:20-alpine`
  - [ ] `nginx:1.25-alpine`
  - [ ] `mongo:7.0`
  - [ ] `redis:7-alpine`
- [ ] All **7 custom service images** are present in the registry:
  - [ ] `synthetic-erp/api-gateway:latest`
  - [ ] `synthetic-erp/generation-engine:latest`
  - [ ] `synthetic-erp/profiling-service:latest`
  - [ ] `synthetic-erp/quality-service:latest`
  - [ ] `synthetic-erp/compliance-service:latest`
  - [ ] `synthetic-erp/provisioning-service:latest`
  - [ ] `synthetic-erp/web-console:latest`
- [ ] Kubernetes `imagePullSecrets` configured for registry authentication
- [ ] Image pull test passes: `docker pull registry.internal.example.com/synthetic-erp/api-gateway:latest`

### 9.2 Python Package Repository Verification

- [ ] Private PyPI mirror (devpi/Artifactory/Nexus) is running
- [ ] All packages for each service install successfully:
  ```bash
  pip install --index-url http://devpi.internal.example.com:3141/root/local/+simple/ \
    -r src/backend/api_gateway/requirements.txt
  ```
- [ ] Platform-specific binary wheels are available (numpy, scipy, torch, tensorflow, cryptography, pyarrow)
- [ ] `pip.conf` configured on all build hosts and in Docker build contexts

### 9.3 npm Package Repository Verification

- [ ] Private npm registry (Verdaccio) is running, or node_modules archive is available
- [ ] Frontend build completes without internet access:
  ```bash
  cd src/web && npm ci && npm run build
  ```
- [ ] `.npmrc` configured to point to private registry

### 9.4 Terraform Provider Verification

- [ ] Provider filesystem mirror is populated at `/opt/terraform-providers/`
- [ ] `.terraformrc` configured with `filesystem_mirror` block
- [ ] `terraform init` completes without internet access:
  ```bash
  cd infrastructure/terraform && terraform init
  ```
- [ ] All 5 providers resolve locally: aws, azurerm, google, kubernetes, helm

### 9.5 Helm Chart Verification

- [ ] Private Helm chart repository (ChartMuseum/Harbor OCI) is running
- [ ] All required charts are available:
  - [ ] `ingress-nginx`
  - [ ] `prometheus`
  - [ ] `grafana`
  - [ ] `sealed-secrets`
  - [ ] `cert-manager`
- [ ] `helm search repo internal/` returns all charts

### 9.6 NLP Model and JDBC Verification

- [ ] spaCy models bundled in Compliance Service image:
  ```bash
  docker run --rm synthetic-erp/compliance-service:latest \
    python -c "import spacy; spacy.load('en_core_web_sm'); spacy.load('en_core_web_lg')"
  ```
- [ ] JDBC drivers present in Profiling Service and Provisioning Service images:
  ```bash
  docker run --rm synthetic-erp/profiling-service:latest \
    ls /opt/jdbc-drivers/
  ```
- [ ] JRE is available in JDBC-dependent service images:
  ```bash
  docker run --rm synthetic-erp/profiling-service:latest java -version
  ```

### 9.7 End-to-End Deployment Smoke Test

- [ ] `kubectl apply -f infrastructure/kubernetes/namespace.yaml` succeeds
- [ ] MongoDB StatefulSet starts and becomes ready
- [ ] Redis StatefulSet starts and becomes ready
- [ ] All six backend service Deployments roll out successfully
- [ ] Web Console Deployment starts and serves the frontend
- [ ] Ingress routes traffic correctly (API and Web Console)
- [ ] Health check endpoints respond:
  ```bash
  curl -k https://platform.internal.example.com/api/v1/health
  curl -k https://platform.internal.example.com/
  ```

---

## 10. Troubleshooting

### 10.1 Docker Image Pull Failures

**Symptom**: `ImagePullBackOff` or `ErrImagePull` in Kubernetes pod events.

**Causes and Solutions**:

| Cause | Solution |
|-------|----------|
| Image not in private registry | Re-run the load-and-push script; verify with `docker pull` |
| Registry TLS certificate not trusted | Add the CA certificate to `/etc/docker/certs.d/<registry>/ca.crt` on all nodes |
| Missing `imagePullSecrets` | Create the `regcred` secret and add `imagePullSecrets` to Deployment specs |
| Wrong image tag | Verify the tag in `docker images` matches the Kubernetes manifest |

```bash
# Debug image pull issues
kubectl describe pod <pod-name> -n synthetic-erp-platform
kubectl get events -n synthetic-erp-platform --sort-by='.lastTimestamp'
```

### 10.2 pip Install Failures

**Symptom**: `pip install` fails with "Could not find a version that satisfies the requirement".

**Causes and Solutions**:

| Cause | Solution |
|-------|----------|
| Package not in offline cache | Re-run `pip download` for the missing package on the staging host |
| Wrong platform wheels | Download wheels for the correct platform (`manylinux2014_x86_64`, `manylinux_2_17_aarch64`, etc.) |
| Missing source-only package | Download without `--only-binary=:all:` to include sdist tarballs |
| Dependency resolution conflict | Pin exact versions in requirements.txt and re-download |
| `pip.conf` not configured | Verify `pip config list` shows the correct `index-url` |

```bash
# Debug pip issues
pip install --verbose --no-index --find-links=/opt/pip-packages <package-name>
pip debug --verbose  # Shows supported platforms and tags
```

### 10.3 npm Install Failures

**Symptom**: `npm ci` fails with "ERESOLVE" or "404 Not Found".

**Causes and Solutions**:

| Cause | Solution |
|-------|----------|
| Package not in Verdaccio | Publish the missing package tarball to Verdaccio |
| Version mismatch | Ensure `package-lock.json` matches the published versions |
| Scoped package issue | Ensure scoped packages (e.g., `@auth0/auth0-react`) are published under the correct scope |
| Registry URL misconfigured | Verify `npm config get registry` returns the private registry URL |

```bash
# Debug npm issues
npm cache clean --force
npm ci --verbose --registry http://verdaccio.internal.example.com:4873
```

### 10.4 Terraform Init Failures

**Symptom**: `terraform init` fails with "Failed to query available provider packages".

**Causes and Solutions**:

| Cause | Solution |
|-------|----------|
| Provider not in filesystem mirror | Re-run `terraform providers mirror` on the staging host |
| Wrong platform binaries | Mirror for the correct platform: `terraform providers mirror -platform=linux_amd64` |
| `.terraformrc` not configured | Verify the file exists at `~/.terraformrc` and contains the `filesystem_mirror` block |
| Provider version constraint mismatch | Update `versions.tf` constraints to match the mirrored provider versions |

```bash
# Debug Terraform issues
TF_LOG=DEBUG terraform init
terraform providers  # List required providers
ls -R /opt/terraform-providers/  # Verify mirror contents
```

### 10.5 spaCy Model Loading Failures

**Symptom**: `OSError: [E050] Can't find model 'en_core_web_sm'`.

**Causes and Solutions**:

| Cause | Solution |
|-------|----------|
| Model not installed in Docker image | Add `pip install` for the model wheel in the Dockerfile |
| Model version/spaCy version mismatch | Ensure spaCy and model versions are compatible (e.g., spaCy 3.7.x with model 3.7.x) |
| Model installed in wrong Python environment | Verify with `python -c "import spacy; print(spacy.util.get_installed_models())"` |

### 10.6 JDBC Connection Failures

**Symptom**: `jaydebeapi.DatabaseError: java.sql.SQLException`.

**Causes and Solutions**:

| Cause | Solution |
|-------|----------|
| JDBC driver JAR not found | Verify `CLASSPATH` environment variable includes `/opt/jdbc-drivers/*` |
| JRE not installed | Verify with `java -version` inside the container |
| `JAVA_HOME` not set | Export `JAVA_HOME=/usr/lib/jvm/default-java` in the Dockerfile |
| Database server unreachable | Verify network connectivity from the pod to the database host |

```bash
# Debug JDBC issues inside a running container
kubectl exec -it <pod-name> -n synthetic-erp-platform -- bash
java -version
echo $CLASSPATH
ls /opt/jdbc-drivers/
python -c "import jaydebeapi; print('JayDeBeApi OK')"
```

### 10.7 DNS and Network Resolution

In air-gapped environments, DNS may not resolve external hostnames. Ensure:

- All internal services use internal DNS names (e.g., `registry.internal.example.com`)
- CoreDNS or equivalent is configured in the Kubernetes cluster
- `/etc/hosts` entries are added on non-Kubernetes hosts for internal services
- NetworkPolicies allow traffic between platform services and the private registries

```bash
# Verify DNS resolution within the cluster
kubectl run -it --rm dns-test --image=busybox --restart=Never -- nslookup registry.internal.example.com
```

### 10.8 Certificate Trust Issues

Self-signed certificates are common in air-gapped environments. Ensure:

- CA certificates are distributed to all nodes at `/etc/ssl/certs/`
- Docker daemon trusts the registry CA: `/etc/docker/certs.d/<registry>/ca.crt`
- Python `requests` library trusts the CA: set `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`
- Node.js trusts the CA: set `NODE_EXTRA_CA_CERTS=/etc/ssl/certs/internal-ca.crt`
- Terraform trusts the CA: set `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`
- Helm trusts the CA: `--ca-file /etc/ssl/certs/internal-ca.crt` flag or `HELM_CA_FILE` environment variable

---

## Appendix A: Complete Artifact Transfer Script

The following script automates the creation of a complete air-gapped artifact bundle on the staging host:

```bash
#!/usr/bin/env bash
# create-airgap-bundle.sh — Create complete artifact bundle for air-gapped deployment
#
# Run this script on the internet-connected staging host.
# The output is a single compressed archive ready for physical transfer.

set -euo pipefail

BUNDLE_DIR="./airgap-bundle"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BUNDLE_ARCHIVE="synthetic-erp-airgap-${TIMESTAMP}.tar.gz"

echo "=== Creating air-gapped artifact bundle ==="
echo "Bundle directory: ${BUNDLE_DIR}"
echo ""

rm -rf "${BUNDLE_DIR}"
mkdir -p "${BUNDLE_DIR}"/{docker-images,pip-packages,npm-packages,terraform-providers,helm-charts,spacy-models,jdbc-drivers}

# Step 1: Docker images
echo "[1/7] Pulling and saving Docker images..."
for img in python:3.12-slim node:20-alpine nginx:1.25-alpine mongo:7.0 redis:7-alpine; do
  docker pull "${img}"
  fname=$(echo "${img}" | tr ':/' '-')
  docker save "${img}" | gzip > "${BUNDLE_DIR}/docker-images/${fname}.tar.gz"
done

# Build and save custom images
SERVICES=(api-gateway generation-engine profiling-service quality-service compliance-service provisioning-service web-console)
for svc in "${SERVICES[@]}"; do
  docker build -t "synthetic-erp/${svc}:latest" -f "infrastructure/docker/${svc}.Dockerfile" . 2>/dev/null || \
  docker build -t "synthetic-erp/${svc}:latest" -f "src/web/Dockerfile" . 2>/dev/null || true
  docker save "synthetic-erp/${svc}:latest" | gzip > "${BUNDLE_DIR}/docker-images/${svc}.tar.gz"
done

# Step 2: Python packages
echo "[2/7] Downloading Python packages..."
for svc_path in src/backend/api_gateway src/backend/generation_engine src/backend/profiling_service \
                src/backend/quality_service src/backend/compliance_service src/backend/provisioning_service; do
  svc_name=$(basename "${svc_path}")
  pip download -r "${svc_path}/requirements.txt" -d "${BUNDLE_DIR}/pip-packages/${svc_name}" 2>/dev/null || true
done

# Step 3: npm packages
echo "[3/7] Archiving npm packages..."
cd src/web
npm ci 2>/dev/null || true
tar czf "../../${BUNDLE_DIR}/npm-packages/node_modules.tar.gz" node_modules/ 2>/dev/null || true
cp package.json package-lock.json "../../${BUNDLE_DIR}/npm-packages/" 2>/dev/null || true
cd ../..

# Step 4: Terraform providers
echo "[4/7] Mirroring Terraform providers..."
cd infrastructure/terraform
terraform providers mirror -platform=linux_amd64 "../../${BUNDLE_DIR}/terraform-providers" 2>/dev/null || true
cd ../..

# Step 5: Helm charts
echo "[5/7] Downloading Helm charts..."
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets 2>/dev/null || true
helm repo add jetstack https://charts.jetstack.io 2>/dev/null || true
helm repo update 2>/dev/null || true
helm pull ingress-nginx/ingress-nginx --destination "${BUNDLE_DIR}/helm-charts" 2>/dev/null || true
helm pull prometheus-community/prometheus --destination "${BUNDLE_DIR}/helm-charts" 2>/dev/null || true
helm pull grafana/grafana --destination "${BUNDLE_DIR}/helm-charts" 2>/dev/null || true
helm pull sealed-secrets/sealed-secrets --destination "${BUNDLE_DIR}/helm-charts" 2>/dev/null || true
helm pull jetstack/cert-manager --destination "${BUNDLE_DIR}/helm-charts" 2>/dev/null || true

# Step 6: spaCy models
echo "[6/7] Downloading spaCy models..."
pip download en_core_web_sm==3.7.* -d "${BUNDLE_DIR}/spacy-models" --no-deps 2>/dev/null || true
pip download en_core_web_lg==3.7.* -d "${BUNDLE_DIR}/spacy-models" --no-deps 2>/dev/null || true

# Step 7: JDBC drivers
echo "[7/7] Downloading JDBC drivers..."
curl -sLo "${BUNDLE_DIR}/jdbc-drivers/postgresql-42.7.3.jar" \
  "https://jdbc.postgresql.org/download/postgresql-42.7.3.jar" 2>/dev/null || true
curl -sLo "${BUNDLE_DIR}/jdbc-drivers/mssql-jdbc-12.6.1.jre11.jar" \
  "https://repo1.maven.org/maven2/com/microsoft/sqlserver/mssql-jdbc/12.6.1.jre11/mssql-jdbc-12.6.1.jre11.jar" 2>/dev/null || true

# Create the final archive
echo ""
echo "=== Creating final bundle archive ==="
tar czf "${BUNDLE_ARCHIVE}" -C "${BUNDLE_DIR}" .

echo ""
echo "=== Bundle complete ==="
echo "Archive: ${BUNDLE_ARCHIVE}"
echo "Size: $(du -sh ${BUNDLE_ARCHIVE} | cut -f1)"
echo ""
echo "Transfer this archive to the air-gapped environment via approved media."
```

---

## Appendix B: Quick Reference — Environment Variables for Air-Gapped Configuration

Set the following environment variables on air-gapped hosts to redirect all package managers to internal mirrors:

```bash
# Docker — Private registry mirror
# /etc/docker/daemon.json:
# { "registry-mirrors": ["https://registry.internal.example.com"] }

# pip — Private PyPI mirror
export PIP_INDEX_URL="http://devpi.internal.example.com:3141/root/local/+simple/"
export PIP_TRUSTED_HOST="devpi.internal.example.com"

# npm — Private npm registry
export NPM_CONFIG_REGISTRY="http://verdaccio.internal.example.com:4873"

# Terraform — Provider filesystem mirror (configured via .terraformrc)
export TF_CLI_CONFIG_FILE="/etc/terraform/terraformrc"

# Node.js — Trust internal CA
export NODE_EXTRA_CA_CERTS="/etc/ssl/certs/internal-ca.crt"

# Python requests — Trust internal CA
export REQUESTS_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"

# SSL — System-wide CA trust
export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"

# Java — Trust internal CA for JDBC connections
export JAVA_TOOL_OPTIONS="-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts"
```

---

## Appendix C: Maintenance — Updating Artifacts

When new versions of the platform or its dependencies are released, repeat the artifact preparation process:

1. **Pull the latest source code** on the staging host.
2. **Re-run all download scripts** (Sections 2–8) to capture updated packages.
3. **Build new Docker images** with updated code and dependencies.
4. **Create a new artifact bundle** using the script in Appendix A.
5. **Transfer and deploy** the new bundle to the air-gapped environment.
6. **Perform a rolling update** in Kubernetes to pick up new image versions.

Maintain a version log of all artifact bundles transferred to the air-gapped environment for audit and rollback purposes. This supports SOC 2 Type II compliance requirements (Constraint C-004) by providing a traceable record of all software changes deployed to the restricted environment.
