# Local Development Setup Guide

This guide walks you through setting up the **Synthetic-ERP-Data-Generation-Platform** on your local machine for development. By the end, you will have all eight services (six backend microservices, one frontend application, plus MongoDB and Redis data stores) running locally.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Quick Start](#2-quick-start)
3. [Environment Variables](#3-environment-variables)
4. [Docker Compose Services](#4-docker-compose-services)
5. [Backend Development Without Docker](#5-backend-development-without-docker)
6. [Frontend Development Without Docker](#6-frontend-development-without-docker)
7. [Makefile Targets](#7-makefile-targets)
8. [Database Setup](#8-database-setup)
9. [Running Tests Locally](#9-running-tests-locally)
10. [Common Issues and Troubleshooting](#10-common-issues-and-troubleshooting)

---

## 1. Prerequisites

Ensure the following tools are installed on your development machine before proceeding.

| Tool | Required Version | Verification Command | Notes |
|------|-----------------|---------------------|-------|
| **Docker** | 25.x+ | `docker --version` | Docker Desktop (macOS/Windows) or Docker Engine (Linux) |
| **Docker Compose** | v2.x+ (Compose V2) | `docker compose version` | Included in Docker Desktop; standalone install on Linux |
| **Python** | 3.12+ | `python3 --version` | Used for all six backend microservices |
| **Node.js** | 20.x LTS | `node --version` | Used for the Web Console frontend |
| **npm** | 10.x+ | `npm --version` | Ships with Node.js 20.x |
| **Git** | 2.40+ | `git --version` | Source control |
| **Make** | GNU Make 4.x+ | `make --version` | Build automation via Makefile |

### Installing Prerequisites

<details>
<summary><strong>macOS (Homebrew)</strong></summary>

```bash
# Docker Desktop
brew install --cask docker

# Python 3.12
brew install python@3.12

# Node.js 20 LTS
brew install node@20

# Make (pre-installed on macOS, but update via Homebrew if needed)
brew install make
```

</details>

<details>
<summary><strong>Ubuntu / Debian</strong></summary>

```bash
# Docker Engine
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Python 3.12
sudo apt-get install -y python3.12 python3.12-venv python3.12-dev

# Node.js 20 LTS (via NodeSource)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Make
sudo apt-get install -y build-essential
```

</details>

<details>
<summary><strong>Windows</strong></summary>

1. Install [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/).
2. Install Python 3.12+ from [python.org](https://www.python.org/downloads/) — ensure "Add to PATH" is checked.
3. Install Node.js 20 LTS from [nodejs.org](https://nodejs.org/).
4. Install Make via [Chocolatey](https://chocolatey.org/): `choco install make`.
5. Install Git from [git-scm.com](https://git-scm.com/).

</details>

---

## 2. Quick Start

Get the entire platform running in under five minutes.

### Step 1 — Clone the Repository

```bash
git clone https://github.com/your-org/Synthetic-ERP-Data-Generation-Platform.git
cd Synthetic-ERP-Data-Generation-Platform
```

### Step 2 — Configure Environment Variables

```bash
cp .env.example .env
```

Open `.env` in your editor and review the default values. For local development, the defaults in `.env.example` work out of the box with Docker Compose — no changes are strictly required for a basic run.

### Step 3 — Start All Services

```bash
docker compose up -d
```

This command builds all Docker images (on first run) and starts every service in detached mode. The initial build may take 5–15 minutes depending on your internet speed and machine.

### Step 4 — Verify All Services Are Running

```bash
docker compose ps
```

You should see all services in a **healthy** or **running** state:

```
NAME                    STATUS       PORTS
api-gateway             Up (healthy) 0.0.0.0:5000->5000/tcp
generation-engine       Up (healthy) 0.0.0.0:5001->5001/tcp
profiling-service       Up (healthy) 0.0.0.0:5002->5002/tcp
quality-service         Up (healthy) 0.0.0.0:5003->5003/tcp
compliance-service      Up (healthy) 0.0.0.0:5004->5004/tcp
provisioning-service    Up (healthy) 0.0.0.0:5005->5005/tcp
web-console             Up           0.0.0.0:3000->80/tcp
mongodb                 Up (healthy) 0.0.0.0:27017->27017/tcp
redis                   Up (healthy) 0.0.0.0:6379->6379/tcp
```

### Step 5 — Access the Platform

| Interface | URL | Description |
|-----------|-----|-------------|
| **Web Console** | [http://localhost:3000](http://localhost:3000) | Self-service web application (React) |
| **API Gateway** | [http://localhost:5000](http://localhost:5000) | REST API entry point |
| **API Health Check** | [http://localhost:5000/health](http://localhost:5000/health) | API Gateway health status |
| **API Readiness** | [http://localhost:5000/ready](http://localhost:5000/ready) | API Gateway readiness probe |

### Step 6 — Verify Health Endpoints

```bash
# API Gateway health check
curl -s http://localhost:5000/health | python3 -m json.tool

# Generation Engine health check
curl -s http://localhost:5001/health | python3 -m json.tool

# Profiling Service health check
curl -s http://localhost:5002/health | python3 -m json.tool

# Quality Service health check
curl -s http://localhost:5003/health | python3 -m json.tool

# Compliance Service health check
curl -s http://localhost:5004/health | python3 -m json.tool

# Provisioning Service health check
curl -s http://localhost:5005/health | python3 -m json.tool
```

Each endpoint returns JSON with service status, version, and upstream dependency checks:

```json
{
  "status": "healthy",
  "service": "api-gateway",
  "version": "1.0.0",
  "dependencies": {
    "mongodb": "connected",
    "redis": "connected"
  }
}
```

### Stopping the Platform

```bash
# Stop all services (preserves volumes)
docker compose down

# Stop all services and remove volumes (fresh start)
docker compose down -v
```

---

## 3. Environment Variables

All services follow the [Twelve-Factor App](https://12factor.net/) methodology and are configured exclusively through environment variables. The `.env.example` file serves as the canonical template.

### Generating the Local `.env` File

```bash
cp .env.example .env
```

> **Security Note:** Never commit the `.env` file to version control. It is listed in `.gitignore`.

### Variable Reference

Below is a detailed walkthrough of every variable group in `.env.example`.

#### General Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ENVIRONMENT` | `development` | Runtime environment (`development`, `staging`, `production`) |
| `DEBUG` | `true` | Enable debug mode (disable in production) |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `SECRET_KEY` | `change-me` | Application-wide secret key for session signing |

#### Flask / API Gateway

| Variable | Default | Description |
|----------|---------|-------------|
| `FLASK_APP` | `app` | Flask application module |
| `FLASK_ENV` | `development` | Flask environment mode |
| `FLASK_SECRET_KEY` | `change-me-secret` | Flask-specific secret key for CSRF and session security |
| `API_PORT` | `5000` | Port for the API Gateway to listen on |
| `API_HOST` | `0.0.0.0` | Bind address for the API Gateway |
| `CORS_ORIGINS` | `http://localhost:3000` | Allowed CORS origins (comma-separated) |

#### Auth0 Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH0_DOMAIN` | `your-tenant.auth0.com` | Auth0 tenant domain |
| `AUTH0_CLIENT_ID` | `your-client-id` | Auth0 application Client ID |
| `AUTH0_CLIENT_SECRET` | `your-client-secret` | Auth0 application Client Secret |
| `AUTH0_AUDIENCE` | `https://api.synthetic-erp.com` | Auth0 API audience identifier |
| `AUTH0_CALLBACK_URL` | `http://localhost:3000/callback` | OAuth callback URL after login |
| `JWT_SECRET_KEY` | `your-jwt-secret` | JWT signing secret (RS256 key for production) |
| `JWT_ALGORITHM` | `RS256` | JWT algorithm (`RS256` recommended for production) |

#### MongoDB

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGODB_DATABASE` | `synthetic_erp` | Default database name |
| `MONGODB_USERNAME` | `admin` | MongoDB authentication username |
| `MONGODB_PASSWORD` | `change-me` | MongoDB authentication password |

#### Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `REDIS_PASSWORD` | `change-me` | Redis authentication password |

#### Service URLs (Inter-Service Communication)

| Variable | Default | Description |
|----------|---------|-------------|
| `GENERATION_ENGINE_URL` | `http://localhost:5001` | Generation Engine base URL |
| `PROFILING_SERVICE_URL` | `http://localhost:5002` | Profiling Service base URL |
| `QUALITY_SERVICE_URL` | `http://localhost:5003` | Quality Service base URL |
| `COMPLIANCE_SERVICE_URL` | `http://localhost:5004` | Compliance Service base URL |
| `PROVISIONING_SERVICE_URL` | `http://localhost:5005` | Provisioning Service base URL |

> **Note:** When running inside Docker Compose, use service names instead of `localhost` (e.g., `http://generation-engine:5001`). The Docker Compose file overrides these automatically.

#### Generation Engine Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `BATCH_SIZE` | `10000` | Records per generation batch |
| `MAX_WORKERS` | `4` | Parallel worker count for batch processing |
| `GPU_ENABLED` | `false` | Enable GPU acceleration for AI/ML generation |
| `MODEL_PATH` | `/models` | Directory for trained GAN/VAE model files |

#### Quality Service Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `QUALITY_THRESHOLD` | `0.95` | Minimum quality score (weighted composite, 0.0–1.0) |

#### Compliance Service Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SPACY_MODEL` | `en_core_web_lg` | spaCy NLP model for PII entity recognition |

#### Profiling Service Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCOVERY_TIMEOUT` | `300` | Schema discovery timeout in seconds |

#### AWS Credentials (for S3 Export)

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | `your-key` | AWS IAM access key |
| `AWS_SECRET_ACCESS_KEY` | `your-secret` | AWS IAM secret access key |
| `AWS_REGION` | `us-east-1` | AWS region |
| `S3_BUCKET` | `synthetic-erp-exports` | S3 bucket for data exports |

#### Azure Credentials (for Blob Storage Export)

| Variable | Default | Description |
|----------|---------|-------------|
| `AZURE_STORAGE_CONNECTION_STRING` | `your-connection-string` | Azure Storage account connection string |
| `AZURE_CONTAINER_NAME` | `synthetic-erp-exports` | Azure Blob container name |

#### GCP Credentials (for Cloud Storage Export)

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_PROJECT_ID` | `your-project` | GCP project identifier |
| `GCP_BUCKET_NAME` | `synthetic-erp-exports` | GCS bucket name |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/path/to/credentials.json` | Path to GCP service account key JSON |

#### Encryption

| Variable | Default | Description |
|----------|---------|-------------|
| `ENCRYPTION_KEY` | `your-aes-256-key` | AES-256 encryption key for data at rest |
| `VAULT_ADDR` | `http://localhost:8200` | HashiCorp Vault address |
| `VAULT_TOKEN` | `your-vault-token` | HashiCorp Vault access token |

#### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OpenTelemetry Collector gRPC endpoint |
| `PROMETHEUS_PORT` | `9090` | Prometheus metrics scrape port |

#### Multi-Tenant

| Variable | Default | Description |
|----------|---------|-------------|
| `DEFAULT_TENANT_ID` | `default` | Default tenant identifier for local development |
| `TENANT_ISOLATION` | `namespace` | Tenant isolation strategy (`namespace` or `database`) |

---

## 4. Docker Compose Services

The `docker-compose.yml` file orchestrates the entire platform for local development. Below is a breakdown of every service.

### Service Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            Docker Compose                                │
│                                                                          │
│  ┌───────────────┐   ┌──────────────────┐   ┌───────────────────┐       │
│  │  web-console   │   │   api-gateway     │   │ generation-engine │       │
│  │  :3000 → :80   │──▶│   :5000           │──▶│   :5001            │       │
│  └───────────────┘   └──────┬───────────┘   └───────────────────┘       │
│                              │                                           │
│              ┌───────────────┼───────────────┐                           │
│              ▼               ▼               ▼                           │
│  ┌──────────────────┐ ┌─────────────┐ ┌──────────────────┐              │
│  │profiling-service  │ │quality-     │ │compliance-       │              │
│  │  :5002            │ │service:5003 │ │service  :5004    │              │
│  └──────────────────┘ └─────────────┘ └──────────────────┘              │
│              │               │               │                           │
│              ▼               ▼               ▼                           │
│  ┌──────────────────┐                                                    │
│  │provisioning-     │                                                    │
│  │service  :5005    │                                                    │
│  └──────────────────┘                                                    │
│                                                                          │
│  ┌──────────────────┐   ┌──────────────────┐                            │
│  │   mongodb :27017  │   │   redis  :6379    │                            │
│  │   (mongo:7.0)     │   │   (redis:7-alpine)│                            │
│  └──────────────────┘   └──────────────────┘                            │
│                                                                          │
│  Network: synthetic-erp-network (bridge)                                 │
│  Volumes: mongodb_data, redis_data                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

### Service Definitions

#### Backend Services

| Service | Image Build Context | Port Mapping | Depends On | Health Check |
|---------|-------------------|--------------|------------|--------------|
| **api-gateway** | `src/backend/api_gateway/Dockerfile` | `5000:5000` | mongodb, redis | `GET /health` every 30s |
| **generation-engine** | `src/backend/generation_engine/Dockerfile` | `5001:5001` | mongodb, redis | `GET /health` every 30s |
| **profiling-service** | `src/backend/profiling_service/Dockerfile` | `5002:5002` | mongodb | `GET /health` every 30s |
| **quality-service** | `src/backend/quality_service/Dockerfile` | `5003:5003` | mongodb | `GET /health` every 30s |
| **compliance-service** | `src/backend/compliance_service/Dockerfile` | `5004:5004` | mongodb | `GET /health` every 30s |
| **provisioning-service** | `src/backend/provisioning_service/Dockerfile` | `5005:5005` | mongodb | `GET /health` every 30s |

#### Frontend

| Service | Image Build Context | Port Mapping | Depends On | Notes |
|---------|-------------------|--------------|------------|-------|
| **web-console** | `src/web/Dockerfile` | `3000:80` | api-gateway | Nginx serves static React build; proxies `/api` to api-gateway |

#### Data Stores

| Service | Docker Image | Port Mapping | Persistent Volume | Health Check |
|---------|-------------|--------------|-------------------|--------------|
| **mongodb** | `mongo:7.0` | `27017:27017` | `mongodb_data:/data/db` | `mongosh --eval "db.runCommand('ping')"` every 10s |
| **redis** | `redis:7-alpine` | `6379:6379` | `redis_data:/data` | `redis-cli ping` every 10s |

### Useful Docker Compose Commands

```bash
# Build images without starting services
docker compose build

# Build a specific service
docker compose build api-gateway

# Start all services in detached mode
docker compose up -d

# Start a specific service (and its dependencies)
docker compose up -d api-gateway

# View real-time logs for all services
docker compose logs -f

# View logs for a specific service
docker compose logs -f generation-engine

# Restart a single service
docker compose restart api-gateway

# Scale a service (for local load testing)
docker compose up -d --scale generation-engine=3

# Execute a command inside a running container
docker compose exec api-gateway bash

# Stop all services
docker compose down

# Stop and remove all volumes (fresh database)
docker compose down -v
```

### Verifying Health After Startup

Run the following script to confirm all services are healthy:

```bash
#!/bin/bash
# health-check.sh — Verify all platform services are healthy

SERVICES=(
  "api-gateway:5000"
  "generation-engine:5001"
  "profiling-service:5002"
  "quality-service:5003"
  "compliance-service:5004"
  "provisioning-service:5005"
)

echo "Checking platform service health..."
echo "-----------------------------------"

for SVC in "${SERVICES[@]}"; do
  NAME="${SVC%%:*}"
  PORT="${SVC##*:}"
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/health" 2>/dev/null)
  if [ "$STATUS" = "200" ]; then
    echo "✅  ${NAME} (port ${PORT}): healthy"
  else
    echo "❌  ${NAME} (port ${PORT}): unhealthy (HTTP ${STATUS})"
  fi
done

# Check MongoDB
if mongosh --quiet --eval "db.runCommand('ping').ok" mongodb://localhost:27017 2>/dev/null | grep -q 1; then
  echo "✅  mongodb (port 27017): healthy"
else
  echo "❌  mongodb (port 27017): unhealthy"
fi

# Check Redis
if redis-cli -h localhost -p 6379 ping 2>/dev/null | grep -q PONG; then
  echo "✅  redis (port 6379): healthy"
else
  echo "❌  redis (port 6379): unhealthy"
fi

echo "-----------------------------------"
echo "Health check complete."
```

---

## 5. Backend Development Without Docker

When actively developing a backend service, running it directly on your host (outside Docker) provides faster iteration with hot-reload support.

### 5.1 Create a Python Virtual Environment

Create a shared virtual environment at the repository root, or per-service virtual environments — either approach works:

#### Option A: Single Shared Virtual Environment (Recommended for Development)

```bash
# From the repository root
python3.12 -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install shared utilities first
pip install -r src/backend/shared/requirements.txt 2>/dev/null || true

# Install dependencies for the service you are working on
pip install -r src/backend/api_gateway/requirements.txt
pip install -r src/backend/generation_engine/requirements.txt
pip install -r src/backend/profiling_service/requirements.txt
pip install -r src/backend/quality_service/requirements.txt
pip install -r src/backend/compliance_service/requirements.txt
pip install -r src/backend/provisioning_service/requirements.txt
```

#### Option B: Per-Service Virtual Environments (Isolated)

```bash
cd src/backend/api_gateway
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 5.2 Configure PYTHONPATH

All backend services import shared utilities from `src/backend/shared/`. Set the `PYTHONPATH` so Python can resolve these imports:

```bash
# From the repository root
export PYTHONPATH="${PWD}/src/backend:${PYTHONPATH}"
```

> **Tip:** Add this to your shell profile (`.bashrc`, `.zshrc`) or your IDE's run configuration to avoid repeating it.

This allows imports like:

```python
from shared.database.mongodb import get_mongo_client
from shared.auth.jwt_handler import validate_token
from shared.logging.structured_logger import get_logger
```

### 5.3 Run an Individual Service

Ensure MongoDB and Redis are running (either via Docker Compose or natively):

```bash
# Start only data stores via Docker Compose
docker compose up -d mongodb redis
```

Then run any backend service with Flask's development server:

```bash
# Source environment variables
source .env

# API Gateway (port 5000)
cd src/backend/api_gateway
FLASK_APP=app FLASK_ENV=development flask run --host 0.0.0.0 --port 5000 --reload

# Generation Engine (port 5001)
cd src/backend/generation_engine
FLASK_APP=app FLASK_ENV=development flask run --host 0.0.0.0 --port 5001 --reload

# Profiling Service (port 5002)
cd src/backend/profiling_service
FLASK_APP=app FLASK_ENV=development flask run --host 0.0.0.0 --port 5002 --reload

# Quality Service (port 5003)
cd src/backend/quality_service
FLASK_APP=app FLASK_ENV=development flask run --host 0.0.0.0 --port 5003 --reload

# Compliance Service (port 5004)
cd src/backend/compliance_service
FLASK_APP=app FLASK_ENV=development flask run --host 0.0.0.0 --port 5004 --reload

# Provisioning Service (port 5005)
cd src/backend/provisioning_service
FLASK_APP=app FLASK_ENV=development flask run --host 0.0.0.0 --port 5005 --reload
```

Flask's `--reload` flag enables automatic restarts when source files change.

### 5.4 Running with Gunicorn (Production-Like)

For testing production-like behavior locally:

```bash
cd src/backend/api_gateway
gunicorn --bind 0.0.0.0:5000 --workers 4 --threads 2 --timeout 120 wsgi:app
```

---

## 6. Frontend Development Without Docker

The Web Console uses React 19.x with TypeScript 5.x, built by Vite for fast Hot Module Replacement (HMR).

### 6.1 Install Dependencies

```bash
cd src/web
npm install
```

### 6.2 Start the Vite Development Server

```bash
npm run dev
```

The development server starts at **http://localhost:3000** with:

- **Hot Module Replacement (HMR):** Changes to `.tsx`, `.ts`, and `.css` files reflect instantly.
- **TypeScript type checking:** Errors surface in the terminal.
- **TailwindCSS JIT:** Utility classes are compiled on demand.

### 6.3 Proxy Configuration to Backend API

During local development, the Vite dev server proxies API requests to the API Gateway. This is configured in `vite.config.ts`:

```typescript
// src/web/vite.config.ts (proxy section)
export default defineConfig({
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:5000',
        changeOrigin: true,
      },
    },
  },
});
```

This means frontend requests to `/api/v1/generation/jobs` are automatically forwarded to `http://localhost:5000/api/v1/generation/jobs`.

> **Important:** Ensure the API Gateway is running (either via Docker Compose or Flask's dev server) before starting frontend development.

### 6.4 Other Frontend Commands

```bash
# Type-check without emitting output
npx tsc --noEmit

# Run ESLint
npx eslint src/ --ext .ts,.tsx

# Run Prettier format check
npx prettier --check src/

# Fix formatting
npx prettier --write src/

# Build for production
npm run build

# Preview production build locally
npm run preview
```

---

## 7. Makefile Targets

The root `Makefile` provides a unified interface for all common development tasks across both backend and frontend.

### Available Targets

| Target | Command | Description |
|--------|---------|-------------|
| `make install` | Install all dependencies | Installs Python packages for all six services and runs `npm install` for the frontend |
| `make lint` | Run all linters | Executes Ruff on Python code and ESLint on TypeScript code |
| `make format` | Auto-format all code | Runs `ruff format` on Python and `prettier --write` on TypeScript |
| `make test` | Run all tests | Executes `pytest` (backend) and `vitest` (frontend) |
| `make test-unit` | Run unit tests only | `pytest tests/unit/ -v --cov` |
| `make test-integration` | Run integration tests | `pytest tests/integration/ -v` |
| `make test-e2e` | Run E2E tests | Playwright tests via `npx playwright test` |
| `make build` | Build all artifacts | Builds Docker images for all services |
| `make docker-build` | Build Docker images | Builds and tags all service images for the registry |
| `make docker-push` | Push Docker images | Pushes all tagged images to the container registry |
| `make docker-up` | Start Docker Compose | `docker compose up -d` |
| `make docker-down` | Stop Docker Compose | `docker compose down -v` |
| `make clean` | Remove build artifacts | Deletes `__pycache__`, `.pytest_cache`, `node_modules`, `dist`, and build output |
| `make db-init` | Initialize database | Creates MongoDB collections, indexes, and seed data |
| `make security-scan` | Run security scans | Executes Trivy (container scanning) and Bandit (Python SAST) |
| `make help` | Show all targets | Prints this table of targets with descriptions |

### Usage Examples

```bash
# Full development setup from scratch
make install

# Quick validation before committing
make lint && make test-unit

# Format all code (Python + TypeScript)
make format

# Complete CI-equivalent check
make lint && make test && make build

# Rebuild and restart only the API Gateway
make docker-build SERVICE=api-gateway && docker compose up -d api-gateway

# Clean everything and start fresh
make clean && make install
```

### Running Individual Backend Linters

```bash
# Ruff — Python linter
ruff check src/backend/

# Ruff — auto-fix issues
ruff check src/backend/ --fix

# Ruff — format Python code
ruff format src/backend/

# mypy — static type checking
mypy src/backend/ --config-file pyproject.toml
```

### Running Individual Frontend Linters

```bash
cd src/web

# ESLint
npx eslint src/ --ext .ts,.tsx

# Prettier — check formatting
npx prettier --check src/

# Prettier — fix formatting
npx prettier --write src/
```

---

## 8. Database Setup

### 8.1 MongoDB 7.0

MongoDB serves as the **Metadata Repository** for the platform, storing five core collections.

#### Automatic Setup via Docker Compose

When you run `docker compose up -d`, MongoDB starts with the credentials defined in your `.env` file. The service is accessible at `mongodb://localhost:27017`.

#### Connecting with `mongosh`

```bash
# Connect using Docker
docker compose exec mongodb mongosh -u admin -p "${MONGODB_PASSWORD}" --authenticationDatabase admin

# Or connect from host (if mongosh is installed locally)
mongosh "mongodb://admin:${MONGODB_PASSWORD}@localhost:27017/synthetic_erp?authSource=admin"
```

#### Core Collections

Initialize the five core collections and their indexes:

```javascript
// Switch to the application database
use synthetic_erp;

// 1. Generation Profiles — Stores generation job configurations and results
db.createCollection("generation_profiles");
db.generation_profiles.createIndex({ "tenant_id": 1, "created_at": -1 });
db.generation_profiles.createIndex({ "status": 1 });
db.generation_profiles.createIndex({ "job_id": 1 }, { unique: true });

// 2. Statistical Profiles — Stores column-level statistical distributions
db.createCollection("statistical_profiles");
db.statistical_profiles.createIndex({ "tenant_id": 1, "schema_id": 1 });
db.statistical_profiles.createIndex({ "table_name": 1, "column_name": 1 });

// 3. Schema Definitions — Stores discovered ERP schema metadata
db.createCollection("schema_definitions");
db.schema_definitions.createIndex({ "tenant_id": 1, "erp_system": 1 });
db.schema_definitions.createIndex({ "schema_id": 1 }, { unique: true });

// 4. Audit Logs — Tamper-evident action audit trail (7-year retention)
db.createCollection("audit_logs", {
  capped: false
});
db.audit_logs.createIndex({ "tenant_id": 1, "timestamp": -1 });
db.audit_logs.createIndex({ "action": 1, "user_id": 1 });
db.audit_logs.createIndex({ "checksum": 1 });

// 5. Tenant Configurations — Per-tenant settings and quotas
db.createCollection("tenant_configurations");
db.tenant_configurations.createIndex({ "tenant_id": 1 }, { unique: true });
```

> **Tip:** Run `make db-init` to execute this initialization automatically.

#### Seed Data for Development

The `make db-init` target also inserts sample seed data for the `default` tenant, including a sample schema definition and generation template so you can test the generation wizard immediately.

### 8.2 Redis 7.x

Redis serves as the **caching and session layer** for the platform.

#### Automatic Setup via Docker Compose

Redis starts automatically with password authentication as configured in `.env`.

#### Connecting with `redis-cli`

```bash
# Connect using Docker
docker compose exec redis redis-cli -a "${REDIS_PASSWORD}"

# Or connect from host
redis-cli -h localhost -p 6379 -a "${REDIS_PASSWORD}"
```

#### Verifying Redis

```bash
docker compose exec redis redis-cli -a "${REDIS_PASSWORD}" ping
# Expected: PONG

docker compose exec redis redis-cli -a "${REDIS_PASSWORD}" info server | head -5
```

Redis is used for:

- **API response caching** — Frequently accessed profiles and schemas
- **Session management** — JWT session tracking
- **Rate limiting** — Per-tenant request counters
- **Job progress tracking** — Real-time generation progress via pub/sub
- **Distributed locking** — Concurrent job coordination

---

## 9. Running Tests Locally

The project follows the **test pyramid** strategy: extensive unit tests, focused integration tests, and targeted end-to-end tests.

### 9.1 Unit Tests (Backend — pytest)

```bash
# Run all backend unit tests
pytest tests/unit/ -v --cov=src/backend --cov-report=term-missing

# Run tests for a specific service
pytest tests/unit/backend/test_api_gateway.py -v
pytest tests/unit/backend/test_generation_engine.py -v
pytest tests/unit/backend/test_profiling_service.py -v
pytest tests/unit/backend/test_quality_service.py -v
pytest tests/unit/backend/test_compliance_service.py -v
pytest tests/unit/backend/test_provisioning_service.py -v

# Run tests matching a pattern
pytest tests/unit/ -k "test_pii_detection" -v

# Generate HTML coverage report
pytest tests/unit/ --cov=src/backend --cov-report=html
# Open htmlcov/index.html in your browser
```

### 9.2 Unit Tests (Frontend — Vitest)

```bash
cd src/web

# Run all frontend tests
npm test

# Run tests in watch mode (for development)
npx vitest

# Run tests with coverage
npx vitest run --coverage

# Run a specific test file
npx vitest run src/components/common/DataTable.test.tsx
```

### 9.3 Integration Tests

Integration tests verify cross-service workflows against real MongoDB and Redis instances:

```bash
# Ensure data stores are running
docker compose up -d mongodb redis

# Run integration tests
pytest tests/integration/ -v --timeout=120

# Run a specific integration test
pytest tests/integration/test_generation_flow.py -v
pytest tests/integration/test_api_endpoints.py -v
pytest tests/integration/test_database_operations.py -v
```

### 9.4 End-to-End Tests (Playwright)

E2E tests exercise the full platform through the Web Console:

```bash
# Install Playwright browsers (first time only)
cd tests/e2e
npx playwright install --with-deps

# Ensure the full platform is running
docker compose up -d

# Run all E2E tests
npx playwright test

# Run a specific E2E test
npx playwright test tests/generation-wizard.spec.ts
npx playwright test tests/dashboard.spec.ts
npx playwright test tests/authentication.spec.ts

# Run with visual browser (headed mode)
npx playwright test --headed

# View the last test report
npx playwright show-report
```

### 9.5 Performance Tests

```bash
# Locust — load testing
cd tests/performance
pip install locust
locust -f locustfile.py --host=http://localhost:5000 --headless -u 100 -r 10 -t 60s

# k6 — sustained load testing
k6 run k6_load_test.js
```

### 9.6 Security Tests

```bash
# Bandit — Python SAST
bandit -r src/backend/ -c tests/security/bandit.yaml

# Trivy — container vulnerability scanning
trivy image api-gateway:latest --config tests/security/trivy-config.yaml
```

### Test Configuration

Test settings are defined in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_functions = ["test_*"]
addopts = "-v --strict-markers --cov=src/backend --cov-report=term-missing --cov-report=html"
markers = [
    "unit: Unit tests",
    "integration: Integration tests",
    "e2e: End-to-end tests",
    "performance: Performance tests",
    "security: Security tests",
]

[tool.coverage.run]
source = ["src/backend"]
omit = ["*/tests/*", "*/__pycache__/*"]

[tool.coverage.report]
precision = 2
show_missing = true
fail_under = 80
```

---

## 10. Common Issues and Troubleshooting

### Port Conflicts

**Symptom:** `docker compose up` fails with `Bind for 0.0.0.0:5000 failed: port is already allocated`.

**Cause:** Another process is using the port.

**Fix:**

```bash
# Find which process is using the port
lsof -i :5000   # macOS/Linux
netstat -ano | findstr :5000  # Windows

# Kill the conflicting process
kill -9 <PID>

# Or change the port mapping in docker-compose.yml
# e.g., "5050:5000" instead of "5000:5000"
```

Common port conflicts:

| Port | Default User | Potential Conflict |
|------|-------------|-------------------|
| 3000 | Web Console | React dev servers, Grafana |
| 5000 | API Gateway | macOS AirPlay Receiver (disable in System Preferences → General → AirDrop & Handoff) |
| 5432 | — | Local PostgreSQL |
| 27017 | MongoDB | Local MongoDB installation |
| 6379 | Redis | Local Redis installation |

### MongoDB Connection Issues

**Symptom:** Services fail to start with `ServerSelectionTimeoutError` or `Authentication failed`.

**Fix:**

```bash
# 1. Verify MongoDB is running
docker compose ps mongodb

# 2. Check MongoDB logs
docker compose logs mongodb

# 3. Verify credentials match .env
docker compose exec mongodb mongosh -u admin -p "your-password" --authenticationDatabase admin --eval "db.runCommand('ping')"

# 4. If authentication database is wrong, recreate the container
docker compose down -v
docker compose up -d mongodb
```

### Redis Authentication Errors

**Symptom:** `NOAUTH Authentication required` or `ERR invalid password`.

**Fix:**

```bash
# 1. Verify Redis is running
docker compose ps redis

# 2. Test connection with password
docker compose exec redis redis-cli -a "your-password" ping

# 3. Ensure REDIS_PASSWORD in .env matches the --requirepass in docker-compose.yml
grep REDIS_PASSWORD .env
```

### Python Version Mismatch

**Symptom:** `SyntaxError` or `ModuleNotFoundError` when running backend services.

**Fix:**

```bash
# Check your Python version
python3 --version

# If below 3.12, install Python 3.12
# macOS:
brew install python@3.12

# Ubuntu:
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt-get update
sudo apt-get install python3.12 python3.12-venv

# Recreate virtual environment with the correct Python
rm -rf .venv
python3.12 -m venv .venv
source .venv/bin/activate
make install
```

### Node.js Version Mismatch

**Symptom:** Frontend build fails with compatibility errors or `unsupported engine` warnings.

**Fix:**

```bash
# Check your Node.js version
node --version

# If not 20.x, install the correct version
# Using nvm (recommended):
nvm install 20
nvm use 20

# Verify
node --version  # Should show v20.x.x
npm --version   # Should show 10.x.x

# Reinstall frontend dependencies
cd src/web
rm -rf node_modules package-lock.json
npm install
```

### Shared Utilities Import Errors

**Symptom:** `ModuleNotFoundError: No module named 'shared'` when running a backend service outside Docker.

**Fix:**

```bash
# Set PYTHONPATH to include the backend root
export PYTHONPATH="${PWD}/src/backend:${PYTHONPATH}"

# Verify the path is set correctly
python3 -c "import shared; print(shared.__file__)"
```

### Docker Build Failures

**Symptom:** `docker compose build` fails during dependency installation.

**Fix:**

```bash
# 1. Clear Docker build cache
docker builder prune -f

# 2. Rebuild without cache
docker compose build --no-cache

# 3. Check available disk space
docker system df

# 4. Free up Docker resources
docker system prune -a --volumes
```

### Generation Engine GPU Issues

**Symptom:** GPU not detected by PyTorch in the Generation Engine.

**Fix:**

```bash
# 1. For local development, set GPU_ENABLED=false in .env
# CPU mode is sufficient for development and testing

# 2. If GPU is needed, ensure NVIDIA Container Toolkit is installed
nvidia-smi  # Check GPU is accessible
docker run --gpus all nvidia/cuda:12.0-base nvidia-smi  # Check Docker GPU access

# 3. Add GPU configuration to docker-compose.yml for generation-engine:
#    deploy:
#      resources:
#        reservations:
#          devices:
#            - driver: nvidia
#              count: 1
#              capabilities: [gpu]
```

### Services Start but Health Checks Fail

**Symptom:** Services show as "starting" or "unhealthy" in `docker compose ps`.

**Fix:**

```bash
# 1. Check service logs for errors
docker compose logs api-gateway

# 2. Verify dependencies started first
docker compose ps mongodb redis

# 3. Increase health check start period in docker-compose.yml if services
#    need more time to initialize (e.g., compliance-service loading spaCy models)

# 4. Restart unhealthy services
docker compose restart compliance-service
```

### Memory Issues with Docker

**Symptom:** Services crash or Docker Desktop becomes unresponsive.

**Fix:**

Increase Docker Desktop memory allocation:
- **macOS/Windows:** Docker Desktop → Settings → Resources → Memory → Set to at least **8 GB** (16 GB recommended for running all services).
- **Linux:** Adjust Docker daemon configuration at `/etc/docker/daemon.json`:

```json
{
  "default-shm-size": "256m",
  "storage-driver": "overlay2"
}
```

### Resetting to a Clean State

If all else fails, perform a complete reset:

```bash
# Stop everything and remove all volumes
docker compose down -v --remove-orphans

# Remove all project Docker images
docker images | grep synthetic-erp | awk '{print $3}' | xargs docker rmi -f

# Remove Python virtual environment
rm -rf .venv

# Remove Node.js dependencies
rm -rf src/web/node_modules

# Start fresh
cp .env.example .env
make install
docker compose up -d
```

---

## Additional Resources

- **Architecture Overview:** [docs/architecture/overview.md](../architecture/overview.md)
- **API Documentation:** [docs/api/openapi.yaml](../api/openapi.yaml)
- **Kubernetes Deployment:** [docs/deployment/kubernetes.md](./kubernetes.md)
- **Air-Gapped Deployment:** [docs/deployment/air-gapped.md](./air-gapped.md)
- **Contributing Guidelines:** [CONTRIBUTING.md](../../CONTRIBUTING.md)
- **Generation Wizard User Guide:** [docs/user-guide/generation-wizard.md](../user-guide/generation-wizard.md)
