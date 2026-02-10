# Synthetic-ERP-Data-Generation-Platform

[![CI](https://github.com/your-org/Synthetic-ERP-Data-Generation-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/your-org/Synthetic-ERP-Data-Generation-Platform/actions/workflows/ci.yml)
[![Security Scan](https://github.com/your-org/Synthetic-ERP-Data-Generation-Platform/actions/workflows/security-scan.yml/badge.svg)](https://github.com/your-org/Synthetic-ERP-Data-Generation-Platform/actions/workflows/security-scan.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![React 19](https://img.shields.io/badge/react-19.x-61dafb.svg)](https://react.dev/)
[![TypeScript 5](https://img.shields.io/badge/typescript-5.x-3178c6.svg)](https://www.typescriptlang.org/)

---

## Table of Contents

- [Project Overview](#project-overview)
- [Architecture Overview](#architecture-overview)
- [Features](#features)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Development Setup](#development-setup)
- [Testing](#testing)
- [API Documentation](#api-documentation)
- [Deployment](#deployment)
- [Technology Stack](#technology-stack)
- [Contributing](#contributing)
- [License](#license)
- [Security](#security)

---

## Project Overview

The **Synthetic-ERP-Data-Generation-Platform** is an enterprise-grade solution for generating high-fidelity synthetic data that mirrors the structure, relationships, and statistical properties of production ERP systems. It addresses the fundamental **Privacy vs. Data Scarcity** paradox by enabling organizations to create realistic test and development datasets without exposing sensitive production information.

### Supported ERP Systems

| ERP System | Connectivity | Supported Modules |
|---|---|---|
| **SAP ERP** | RFC/BAPI, IDoc | Financial Accounting, HR, Sales & Distribution, Material Management |
| **Oracle E-Business Suite** | Database Link, OData | Financial Accounting, HR, Sales & Distribution, Material Management |
| **Microsoft Dynamics 365** | Web API, OData | Financial Accounting, HR, Sales & Distribution, Material Management |
| **Legacy Systems** | Generic JDBC | Financial Accounting, HR, Sales & Distribution, Material Management |

### ERP Modules (Initial Release)

- **Financial Accounting** — General ledger entries, invoices, payments, account reconciliation
- **Human Resources** — Employee records, payroll, benefits administration, organizational structure
- **Sales & Distribution** — Sales orders, customer master data, pricing, shipping
- **Material Management** — Inventory records, purchase orders, vendor management, warehouse data

### Key Capabilities

- **Multi-method data generation** — AI/ML (GANs, VAEs), rules-based, statistical synthesis, and intelligent masking
- **Zero PII leakage** — Automated PII detection and regulatory compliance verification
- **≥95% statistical fidelity** — Weighted quality scoring ensuring generated data is statistically indistinguishable from source distributions
- **Referential integrity** — Automatic foreign key relationship maintenance across tables and ERP modules
- **Multi-format export** — SQL, CSV, JSON, and Apache Parquet output formats
- **Horizontal scalability** — Kubernetes-native with 1M+ records/minute throughput target

---

## Architecture Overview

The platform follows a **polyglot microservices architecture** organized into five distinct layers:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          UI LAYER                                       │
│                Web Console (React 19 + TypeScript 5 + TailwindCSS 4)    │
├─────────────────────────────────────────────────────────────────────────┤
│                          API LAYER                                      │
│              API Gateway (Flask 3.1 + JWT/Auth0 + Rate Limiting)        │
├──────────┬──────────┬──────────┬──────────┬─────────────────────────────┤
│          │          │ SERVICE  │          │                             │
│Generation│Profiling │ LAYER    │Compliance│  Provisioning              │
│ Engine   │ Service  │ Quality  │ Service  │  Service                   │
│(LangChain│(SciPy/   │ Service  │(spaCy/   │  (JDBC +                   │
│+PyTorch/ │ NumPy/   │(Great    │ regex)   │   Cloud SDKs)              │
│TensorFlow│ Pandas)  │ Expect.) │          │                             │
├──────────┴──────────┴──────────┴──────────┴─────────────────────────────┤
│                          DATA LAYER                                     │
│         MongoDB 7.0 (Metadata Repository)  +  Redis 7.x (Cache)        │
├─────────────────────────────────────────────────────────────────────────┤
│                     INFRASTRUCTURE LAYER                                │
│       Docker 25.x  |  Kubernetes 1.29+  |  Terraform 1.7+              │
└─────────────────────────────────────────────────────────────────────────┘
```

### Service Components

| Service | Technology | Purpose |
|---|---|---|
| **Web Console** | React 19.x, TypeScript 5.x, TailwindCSS 4.x, Zustand, React Router 6.x | Self-service UI with multi-step generation wizard, dashboards, schema browser |
| **API Gateway** | Flask 3.1.x, Flask-JWT-Extended, Flask-CORS, Gunicorn 21.x | RESTful API entry point with authentication, rate limiting, request routing |
| **Generation Engine** | LangChain 0.3.x, PyTorch 2.x, TensorFlow 2.x, SciPy, NumPy | Multi-method synthetic data generation orchestration |
| **Profiling Service** | SciPy 1.12+, NumPy 1.26+, Pandas 2.x, JayDeBeApi | ERP schema discovery and statistical profiling (metadata only) |
| **Quality Service** | Great Expectations 0.18.x, Pydantic 2.x | Data quality validation with weighted scoring model |
| **Compliance Service** | spaCy 3.7.x, regex, python-jose | PII detection, GDPR/HIPAA/CCPA verification, compliance certification |
| **Provisioning Service** | JayDeBeApi, boto3, azure-storage-blob, google-cloud-storage | Database provisioning and cloud storage export |

### Data Flow

```
User → Web Console → API Gateway → Generation Engine → Quality Service
                                                      → Compliance Service
                                                      → Provisioning Service → Target DB / Cloud Storage
```

1. User configures a generation job via the Web Console wizard
2. API Gateway authenticates, validates, and routes the request
3. Generation Engine selects the optimal method and produces synthetic records in batches
4. Quality Service validates statistical fidelity, business rules, and referential integrity
5. Compliance Service scans for PII and verifies regulatory compliance
6. Provisioning Service exports data to the target database or cloud storage

### Inter-Service Communication

All backend services communicate via **HTTP/REST** over the internal service mesh. MongoDB serves as the persistent metadata repository, and Redis provides caching, session management, and real-time job progress tracking via pub/sub.

---

## Features

The platform implements 20 core features organized across data generation, integration, security, and operations:

### Data Generation

| ID | Feature | Description |
|---|---|---|
| **F-001** | Multi-Method Data Generation | AI/ML generation (GANs, VAEs), rules-based generation, statistical synthesis, and intelligent masking |
| **F-002** | ERP Schema Support | Native schema discovery for SAP, Oracle EBS, Microsoft Dynamics, and legacy JDBC systems |
| **F-003** | Referential Integrity Enforcement | Automatic foreign key relationship maintenance across tables and ERP modules |
| **F-004** | Data Quality Validation | Weighted quality scoring: 40% statistical fidelity + 30% business rules + 30% referential integrity (≥95% target) |
| **F-005** | Zero PII Leakage | Dual-layer PII detection (spaCy NLP + regex) with compliance certification |

### Integration & Export

| ID | Feature | Description |
|---|---|---|
| **F-006** | Multi-Format Export | SQL, CSV, JSON, and Apache Parquet output formats |
| **F-007** | RESTful API Gateway | Versioned REST API (/api/v1/) with OpenAPI 3.0 documentation |
| **F-008** | Database Connectors | JDBC provisioning to PostgreSQL, Oracle, SQL Server, and SAP HANA |
| **F-009** | Cloud Storage Integration | AWS S3, Azure Blob Storage, and GCP Cloud Storage with AES-256 encryption |
| **F-010** | CI/CD Pipeline Integration | Jenkins, GitLab CI, Azure DevOps, and GitHub Actions webhook support |

### Security & Compliance

| ID | Feature | Description |
|---|---|---|
| **F-011** | Role-Based Access Control | Five roles: Platform Admin, Data Engineer, Developer, QA Engineer, Data Analyst |
| **F-012** | Audit Logging | Tamper-evident audit trails with SHA-256 checksums and 7-year retention |
| **F-013** | Data Encryption | AES-256 at rest (per-tenant keys) and TLS 1.3 in transit |
| **F-014** | GDPR Compliance | Right to erasure, data minimization, processing records |
| **F-015** | HIPAA Compliance | PHI detection and safeguards for healthcare ERP data |

### Operations & Scalability

| ID | Feature | Description |
|---|---|---|
| **F-016** | Multi-Tenant Architecture | Namespace isolation, resource quotas, and tenant-specific configurations |
| **F-017** | Horizontal Scalability | Kubernetes HPA with 1M+ records/minute throughput target |
| **F-018** | Self-Service Web Console | React-based UI with generation wizard, dashboards, and monitoring (Screens S-001 through S-010) |
| **F-019** | CCPA Compliance | California Consumer Privacy Act verification for generated datasets |
| **F-020** | SOC 2 Type II Compliance | Comprehensive audit controls, encryption, and access management |

---

## Quick Start

Launch the entire platform locally using Docker Compose.

### Prerequisites

| Requirement | Minimum Version |
|---|---|
| Docker | 25.x |
| Docker Compose | 2.24+ |
| Node.js | 20.x (LTS) |
| Python | 3.12+ |
| Git | 2.40+ |

### 1. Clone the Repository

```bash
git clone https://github.com/your-org/Synthetic-ERP-Data-Generation-Platform.git
cd Synthetic-ERP-Data-Generation-Platform
```

### 2. Configure Environment Variables

```bash
cp .env.example .env
# Edit .env with your Auth0 credentials, database passwords, and cloud provider keys
```

### 3. Start All Services

```bash
docker-compose up -d
```

This launches all services, MongoDB, and Redis. The first build may take several minutes while Docker images are constructed.

### 4. Verify the Platform

| Service | URL | Health Check |
|---|---|---|
| Web Console | [http://localhost:3000](http://localhost:3000) | — |
| API Gateway | [http://localhost:5000](http://localhost:5000) | [http://localhost:5000/health](http://localhost:5000/health) |
| Generation Engine | [http://localhost:5001](http://localhost:5001) | [http://localhost:5001/health](http://localhost:5001/health) |
| Profiling Service | [http://localhost:5002](http://localhost:5002) | [http://localhost:5002/health](http://localhost:5002/health) |
| Quality Service | [http://localhost:5003](http://localhost:5003) | [http://localhost:5003/health](http://localhost:5003/health) |
| Compliance Service | [http://localhost:5004](http://localhost:5004) | [http://localhost:5004/health](http://localhost:5004/health) |
| Provisioning Service | [http://localhost:5005](http://localhost:5005) | [http://localhost:5005/health](http://localhost:5005/health) |
| MongoDB | `localhost:27017` | — |
| Redis | `localhost:6379` | — |

### 5. Stop All Services

```bash
docker-compose down
# To also remove volumes (database data):
docker-compose down -v
```

---

## Project Structure

The repository follows a monorepo layout with clearly separated concerns:

```
Synthetic-ERP-Data-Generation-Platform/
├── .github/
│   └── workflows/               # GitHub Actions CI/CD pipelines
│       ├── ci.yml               # Lint, type-check, unit & integration tests
│       ├── build.yml            # Docker image build and push
│       ├── deploy-staging.yml   # Staging deployment
│       ├── deploy-prod.yml      # Production deployment with approval gates
│       └── security-scan.yml    # Trivy, dependency audit, SAST
├── docs/
│   ├── api/
│   │   └── openapi.yaml         # OpenAPI 3.0 specification
│   ├── architecture/
│   │   └── overview.md          # Architecture overview and diagrams
│   ├── deployment/
│   │   ├── local-setup.md       # Local development setup guide
│   │   ├── kubernetes.md        # Kubernetes deployment guide
│   │   └── air-gapped.md       # Air-gapped deployment procedures
│   └── user-guide/
│       └── generation-wizard.md # Generation wizard user guide
├── infrastructure/
│   ├── docker/                  # Dockerfiles for all services
│   ├── kubernetes/              # Kubernetes manifests
│   │   ├── namespace.yaml
│   │   ├── ingress.yaml
│   │   ├── network-policies.yaml
│   │   ├── hpa.yaml
│   │   ├── secrets.yaml
│   │   ├── resource-quotas.yaml
│   │   ├── api-gateway/         # Deployment, Service, ConfigMap
│   │   ├── generation-engine/
│   │   ├── profiling-service/
│   │   ├── quality-service/
│   │   ├── compliance-service/
│   │   ├── provisioning-service/
│   │   ├── mongodb/             # StatefulSet, Service
│   │   └── redis/               # StatefulSet, Service
│   └── terraform/               # Infrastructure as Code
│       ├── main.tf
│       ├── variables.tf
│       ├── outputs.tf
│       ├── versions.tf
│       ├── modules/
│       │   ├── networking/      # VPC, subnets, security groups
│       │   ├── kubernetes/      # Managed K8s (EKS/AKS/GKE)
│       │   ├── database/        # MongoDB Atlas, Redis
│       │   ├── storage/         # S3/Blob/GCS buckets
│       │   └── security/        # IAM, KMS, secrets
│       └── environments/
│           ├── dev.tfvars
│           ├── staging.tfvars
│           └── prod.tfvars
├── src/
│   ├── backend/
│   │   ├── shared/              # Shared utilities (DB, auth, logging, observability)
│   │   │   ├── database/        # MongoDB & Redis connection managers
│   │   │   ├── auth/            # JWT handler, RBAC
│   │   │   ├── logging/         # Structured JSON logging
│   │   │   ├── observability/   # Prometheus metrics, OpenTelemetry tracing
│   │   │   ├── config/          # Base configuration
│   │   │   └── middleware/      # Circuit breaker, health checks
│   │   ├── api_gateway/         # Flask REST API Gateway
│   │   │   ├── routes/          # Blueprint route modules
│   │   │   ├── middleware/      # Auth, rate limiter, error handler, tenant
│   │   │   ├── schemas/         # Pydantic request/response models
│   │   │   ├── services/        # Business logic layer
│   │   │   ├── models/          # MongoDB document models
│   │   │   └── utils/           # Validators, pagination
│   │   ├── generation_engine/   # Multi-method data generation
│   │   │   ├── generators/      # AI/ML, rules, statistical, masking
│   │   │   ├── orchestrator/    # LangChain job orchestration
│   │   │   ├── models/          # GAN, VAE model definitions
│   │   │   ├── integrity/       # Referential integrity enforcement
│   │   │   ├── formatters/      # SQL, CSV, JSON, Parquet output
│   │   │   └── utils/           # Progress tracker, checkpointing
│   │   ├── profiling_service/   # ERP schema discovery & statistical profiling
│   │   │   ├── connectors/      # SAP, Oracle, Dynamics, JDBC
│   │   │   ├── discovery/       # Schema extraction, relationship mapping
│   │   │   ├── profilers/       # Statistical profiling, pattern analysis
│   │   │   └── models/          # Schema & profile MongoDB models
│   │   ├── quality_service/     # Data quality validation
│   │   │   ├── validators/      # Statistical, business rules, referential integrity
│   │   │   └── scoring/         # Weighted quality scoring, report generation
│   │   ├── compliance_service/  # PII detection & regulatory compliance
│   │   │   ├── detectors/       # PII, NLP, pattern detectors
│   │   │   ├── regulations/     # GDPR, HIPAA, CCPA rules
│   │   │   └── certification/   # Compliance certification, audit logging
│   │   └── provisioning_service/# Database & cloud export
│   │       ├── connectors/      # PostgreSQL, Oracle, SQL Server, SAP HANA
│   │       ├── cloud/           # AWS S3, Azure Blob, GCP Cloud Storage
│   │       └── exporters/       # File & database export orchestration
│   └── web/                     # React Web Console
│       ├── src/
│       │   ├── components/      # Reusable UI components
│       │   │   ├── common/      # Sidebar, Header, DataTable, Modal, etc.
│       │   │   ├── charts/      # Distribution, Quality, Throughput charts
│       │   │   └── wizard/      # Generation wizard step components
│       │   ├── pages/           # Page components (S-001 through S-010)
│       │   ├── store/           # Zustand state stores
│       │   ├── services/        # Axios API service clients
│       │   ├── hooks/           # Custom React hooks
│       │   └── types/           # TypeScript type definitions
│       ├── package.json
│       ├── tsconfig.json
│       ├── tailwind.config.ts
│       └── vite.config.ts
├── tests/
│   ├── unit/
│   │   ├── backend/             # pytest unit tests for all services
│   │   └── web/                 # Jest/Vitest component tests
│   ├── integration/             # Cross-service integration tests
│   ├── e2e/                     # Playwright E2E tests
│   ├── performance/             # Locust/k6 load tests
│   └── security/                # Trivy, Bandit security configs
├── .env.example                 # Environment variable template
├── .gitignore                   # Git ignore rules
├── CONTRIBUTING.md              # Contribution guidelines
├── docker-compose.yml           # Local development orchestration
├── docker-compose.prod.yml      # Production compose overlay
├── LICENSE                      # Project license
├── Makefile                     # Build automation targets
├── pyproject.toml               # Root Python tooling config (Ruff, mypy, pytest)
└── README.md                    # This file
```

---

## Development Setup

### Backend Services (Python 3.12+)

Each backend service follows the **Flask Application Factory** pattern and can be developed independently.

#### 1. Set Up the Python Virtual Environment

```bash
# From the repository root
python3.12 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows
```

#### 2. Install Dependencies

```bash
# Install shared utilities
pip install -r src/backend/shared/requirements.txt

# Install a specific service (e.g., API Gateway)
pip install -r src/backend/api_gateway/requirements.txt
```

#### 3. Run a Service Locally

```bash
# Ensure MongoDB and Redis are running (via Docker Compose or locally)
docker-compose up -d mongodb redis

# Set environment variables
export FLASK_APP=src/backend/api_gateway/app.py
export FLASK_ENV=development
export MONGODB_URI=mongodb://localhost:27017
export REDIS_URL=redis://localhost:6379/0

# Start the API Gateway
flask run --host=0.0.0.0 --port=5000
```

#### 4. Lint and Type Check

```bash
# Lint all Python code with Ruff
ruff check src/backend/

# Format all Python code
ruff format src/backend/

# Run mypy static type checking
mypy src/backend/
```

### Frontend — Web Console (Node.js 20.x)

#### 1. Install Dependencies

```bash
cd src/web
npm install
```

#### 2. Start the Development Server

```bash
npm run dev
# The Web Console is available at http://localhost:3000
```

#### 3. Lint and Format

```bash
# Run ESLint
npx eslint src/

# Run Prettier
npx prettier --check src/
npx prettier --write src/   # Auto-fix formatting
```

#### 4. Build for Production

```bash
npm run build
# Output is generated in dist/
```

### Using the Makefile

The root `Makefile` provides convenient targets for common operations:

```bash
make install          # Install all backend and frontend dependencies
make lint             # Run all linters (Ruff, mypy, ESLint, Prettier)
make format           # Auto-format all code
make test             # Run all tests (unit + integration)
make test-unit        # Run unit tests only
make test-integration # Run integration tests only
make test-e2e         # Run Playwright E2E tests
make docker-build     # Build all Docker images
make docker-up        # Start all services via Docker Compose
make docker-down      # Stop all services
make clean            # Remove build artifacts and caches
make help             # Display all available targets
```

---

## Testing

The project follows the **test pyramid** strategy with extensive unit tests, focused integration tests, and targeted end-to-end tests.

### Unit Tests (Backend)

```bash
# Run all backend unit tests with coverage
pytest tests/unit/backend/ -v --cov=src/backend --cov-report=term-missing

# Run tests for a specific service
pytest tests/unit/backend/test_api_gateway.py -v
pytest tests/unit/backend/test_generation_engine.py -v
pytest tests/unit/backend/test_profiling_service.py -v
pytest tests/unit/backend/test_quality_service.py -v
pytest tests/unit/backend/test_compliance_service.py -v
pytest tests/unit/backend/test_provisioning_service.py -v
```

### Unit Tests (Frontend)

```bash
cd src/web
npm test                     # Run all component tests via Vitest
npm test -- --coverage       # With coverage reporting
```

### Integration Tests

```bash
# Requires running MongoDB and Redis (docker-compose up -d mongodb redis)
pytest tests/integration/ -v
```

### End-to-End Tests (Playwright)

```bash
# Requires the full platform running (docker-compose up -d)
cd tests/e2e
npx playwright install       # Install browsers (first time only)
npx playwright test          # Run all E2E tests
npx playwright test --ui     # Run with interactive UI
```

### Performance Tests

```bash
# Locust load test
cd tests/performance
locust -f locustfile.py --host=http://localhost:5000

# k6 load test
k6 run k6_load_test.js
```

### Security Tests

```bash
# Container vulnerability scanning
trivy image synthetic-erp/api-gateway:latest

# Python security lint
bandit -r src/backend/ -c tests/security/bandit.yaml
```

### Coverage Requirements

| Test Category | Target Coverage | Framework |
|---|---|---|
| Backend Unit Tests | ≥80% | pytest + pytest-cov |
| Frontend Unit Tests | ≥80% | Vitest + @testing-library/react |
| Integration Tests | Critical paths | pytest |
| E2E Tests | Wizard, Dashboard, Auth flows | Playwright |
| Performance Tests | 1M+ records/min throughput | Locust, k6 |

---

## API Documentation

The platform exposes a versioned RESTful API documented with the **OpenAPI 3.0** specification.

### OpenAPI Specification

The complete API specification is available at:

```
docs/api/openapi.yaml
```

### Swagger UI

When running the API Gateway locally or via Docker Compose, the interactive Swagger UI is accessible at:

```
http://localhost:5000/api/docs
```

### Key API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/generation/jobs` | Create a new generation job |
| `GET` | `/api/v1/generation/jobs/{id}` | Retrieve generation job status and results |
| `GET` | `/api/v1/generation/jobs` | List all generation jobs (paginated) |
| `POST` | `/api/v1/profiles` | Create a statistical profile |
| `GET` | `/api/v1/profiles/{id}` | Retrieve a statistical profile |
| `POST` | `/api/v1/schemas/discover` | Trigger ERP schema discovery |
| `GET` | `/api/v1/schemas/{id}` | Retrieve a schema definition |
| `GET` | `/api/v1/templates` | List generation templates |
| `POST` | `/api/v1/templates` | Create a generation template |
| `POST` | `/api/v1/export` | Export generated data |
| `GET` | `/health` | Service health check |
| `GET` | `/ready` | Service readiness probe |

### Authentication

All API endpoints (except `/health` and `/ready`) require a valid JWT bearer token issued by Auth0. Include the token in the `Authorization` header:

```
Authorization: Bearer <your-jwt-token>
```

---

## Deployment

### Kubernetes Deployment

The platform is designed for production deployment on **Kubernetes 1.29+** with horizontal pod autoscaling.

```bash
# Apply all Kubernetes manifests
kubectl apply -f infrastructure/kubernetes/namespace.yaml
kubectl apply -f infrastructure/kubernetes/ --recursive

# Verify deployments
kubectl get pods -n synthetic-erp-platform
```

For detailed Kubernetes deployment instructions, see: [docs/deployment/kubernetes.md](docs/deployment/kubernetes.md)

### Terraform Infrastructure Provisioning

Provision cloud infrastructure across AWS, Azure, or GCP using **Terraform 1.7+**:

```bash
cd infrastructure/terraform

# Initialize Terraform
terraform init

# Plan for a specific environment
terraform plan -var-file=environments/dev.tfvars

# Apply infrastructure changes
terraform apply -var-file=environments/dev.tfvars
```

For complete infrastructure provisioning guides, see: [docs/deployment/local-setup.md](docs/deployment/local-setup.md)

### Air-Gapped Deployment

The platform supports deployment in air-gapped (disconnected) environments:

- All Docker images can be built from a private container registry
- Python and Node.js packages are installable from offline mirrors
- Terraform providers can be cached locally for offline use
- spaCy NLP models are pre-downloaded into Docker images

For air-gapped deployment procedures, see: [docs/deployment/air-gapped.md](docs/deployment/air-gapped.md)

### Production Docker Compose

For production-like deployments outside of Kubernetes:

```bash
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

This applies production overlays including resource limits, replicas, TLS configuration, and log rotation.

---

## Technology Stack

### Backend

| Technology | Version | Purpose |
|---|---|---|
| Python | 3.12+ | Backend services programming language |
| Flask | 3.1.x | Lightweight REST API framework |
| Flask-RESTful | 0.3.x | Resource-based REST API structure |
| Flask-JWT-Extended | 4.6.x | JWT authentication with Auth0 |
| Flask-CORS | 4.0.x | Cross-origin request handling |
| Gunicorn | 21.x | Production WSGI HTTP server |
| Pydantic | 2.x | Data validation and schema models |
| LangChain | 0.3.x | AI/ML generation orchestration |
| PyTorch | 2.x | GAN-based synthetic data generation |
| TensorFlow | 2.x | VAE-based synthetic data generation |
| SciPy | 1.12+ | Statistical distribution modeling |
| NumPy | 1.26+ | Numerical computing |
| Pandas | 2.x | Data manipulation and transformation |
| Great Expectations | 0.18.x | Data quality validation framework |
| spaCy | 3.7.x | NLP entity recognition for PII detection |
| PyArrow | 15.x | Apache Parquet file format support |
| structlog | 24.x | Structured JSON logging |
| OpenTelemetry | 1.x | Distributed tracing |
| Prometheus Client | 0.20.x | Metrics exporter |

### Frontend

| Technology | Version | Purpose |
|---|---|---|
| React | 19.x | UI component framework |
| TypeScript | 5.x | Typed JavaScript |
| TailwindCSS | 4.x | Utility-first CSS framework |
| Vite | 5.x | Build tool and development server |
| Zustand | 4.x | Lightweight state management |
| React Router | 6.x | Client-side routing |
| Axios | 1.7.x | HTTP client for API calls |
| Recharts | 2.x | Data visualization charts |
| Auth0 React SDK | 2.x | Auth0 authentication integration |

### Data Layer

| Technology | Version | Purpose |
|---|---|---|
| MongoDB | 7.0 | Metadata repository (profiles, schemas, audit logs) |
| Redis | 7.x | Session caching, API response caching, job progress |
| PyMongo | 4.x | MongoDB Python driver |

### Infrastructure & DevOps

| Technology | Version | Purpose |
|---|---|---|
| Docker | 25.x | Container runtime |
| Kubernetes | 1.29+ | Container orchestration with HPA |
| Terraform | 1.7+ | Infrastructure as code (AWS, Azure, GCP) |
| GitHub Actions | — | CI/CD pipelines |
| Nginx | 1.25 | Frontend static serving and reverse proxy |
| HashiCorp Vault | — | Secrets management and encryption key storage |

### Database Connectors

| Target System | Versions Supported | Connectivity |
|---|---|---|
| PostgreSQL | 12.x – 16.x | JDBC |
| Oracle Database | 19c – 23ai | JDBC |
| SQL Server | 2019 – 2022 | JDBC |
| SAP HANA | 2.0 SPS 07+ | JDBC |

### Cloud Storage

| Provider | Service | SDK |
|---|---|---|
| AWS | S3 | boto3 1.34.x |
| Azure | Blob Storage | azure-storage-blob 12.x |
| GCP | Cloud Storage | google-cloud-storage 2.x |

### Code Quality

| Tool | Version | Purpose |
|---|---|---|
| Ruff | 0.4.x | Python linter and formatter |
| mypy | 1.10.x | Python static type checking |
| ESLint | 8.x | TypeScript/JavaScript linting |
| Prettier | 3.x | Code formatting |
| pytest | 8.x | Python testing framework |
| Vitest | 1.x | Frontend unit testing |
| Playwright | 1.x | End-to-end testing |
| Locust | — | Python-based load testing |
| k6 | — | JavaScript-based load testing |
| Trivy | — | Container vulnerability scanning |
| Bandit | — | Python security linting |

---

## Contributing

We welcome contributions to the Synthetic-ERP-Data-Generation-Platform. Please read our [Contributing Guidelines](CONTRIBUTING.md) before submitting pull requests.

### Quick Reference

- **Branching Strategy:** Trunk-based development
- **Commit Format:** [Conventional Commits](https://www.conventionalcommits.org/)
- **Python Style:** PEP 8 via Ruff, Google-style docstrings, type hints on all functions
- **TypeScript Style:** ESLint + Prettier, strict mode, explicit return types
- **Terraform Style:** HashiCorp style guide
- **Testing Requirement:** All PRs must include relevant tests and maintain ≥80% coverage

---

## License

This project is licensed under the terms of the [MIT License](LICENSE).

---

## Security

The Synthetic-ERP-Data-Generation-Platform is designed with security as a foundational principle, ensuring compliance with major regulatory frameworks.

### Regulatory Compliance

| Framework | Scope | Key Controls |
|---|---|---|
| **GDPR** | EU data protection | Right to erasure, data minimization, processing records, consent management |
| **HIPAA** | US healthcare data | PHI detection, access controls, audit trails, encryption requirements |
| **CCPA** | California consumer privacy | Consumer data rights verification, opt-out mechanisms |
| **SOC 2 Type II** | Service organization controls | Comprehensive audit trails, access management, encryption, availability |

### Security Controls

- **Authentication:** Auth0-based OAuth 2.0 / OpenID Connect with JWT RS256 tokens
- **Authorization:** Role-Based Access Control (RBAC) with five graduated permission levels, enforced via Open Policy Agent (OPA)
- **Encryption at Rest:** AES-256-GCM with per-tenant keys managed by HashiCorp Vault
- **Encryption in Transit:** TLS 1.3 minimum for all inter-service and external communication
- **PII Detection:** Dual-layer approach combining spaCy NLP entity recognition and regex pattern matching for SSN, email, phone, financial account, and address patterns
- **Audit Logging:** Tamper-evident audit trails with SHA-256 checksums stored in MongoDB with 7-year retention
- **Network Isolation:** Kubernetes NetworkPolicies enforcing multi-tenant traffic isolation
- **Air-Gapped Support:** Full offline deployment capability with private registries and local package mirrors

### Privacy-First Architecture

The platform enforces a strict privacy boundary:

- **No production data access** — The Profiling Service captures schema metadata only; raw production data never enters the system
- **Legally distinct output** — Generation models create synthetic data that is statistically similar but legally distinct from source data
- **Zero PII guarantee** — Every generated dataset passes through the Compliance Service PII scanner before release

### Reporting Security Issues

If you discover a security vulnerability, please report it responsibly by emailing the project maintainers. Do not open a public GitHub issue for security vulnerabilities.

---

<p align="center">
  <strong>Synthetic-ERP-Data-Generation-Platform</strong> — Enterprise-grade synthetic ERP data generation with privacy compliance.
</p>
