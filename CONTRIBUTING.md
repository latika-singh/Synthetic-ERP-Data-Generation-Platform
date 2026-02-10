# Contributing to Synthetic-ERP-Data-Generation-Platform

Thank you for your interest in contributing to the Synthetic-ERP-Data-Generation-Platform! This document provides comprehensive guidelines for contributing to the project, including development environment setup, coding standards, testing requirements, and the pull request process.

All contributors must adhere to these guidelines to maintain code quality, consistency, and reliability across the platform's six backend microservices, frontend application, and infrastructure components.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Development Environment Setup](#development-environment-setup)
- [Project Structure](#project-structure)
- [Git Workflow](#git-workflow)
- [Commit Message Format](#commit-message-format)
- [Coding Standards](#coding-standards)
  - [Python Code Style](#python-code-style)
  - [TypeScript Code Style](#typescript-code-style)
  - [Terraform Code Style](#terraform-code-style)
- [Testing Requirements](#testing-requirements)
- [Code Quality Gates](#code-quality-gates)
- [Docker Development Workflow](#docker-development-workflow)
- [Pull Request Process](#pull-request-process)
- [Architecture Decision Records](#architecture-decision-records)
- [Issue Reporting Guidelines](#issue-reporting-guidelines)
- [Security Considerations](#security-considerations)

---

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment. All interactions—whether in issues, pull requests, code reviews, or discussions—must be professional, constructive, and welcoming to contributors of all experience levels.

Key principles:

- Be respectful and considerate in all communications
- Provide constructive feedback focused on code quality and technical merit
- Welcome newcomers and help them understand project conventions
- Report unacceptable behavior to the project maintainers

---

## Development Environment Setup

### Prerequisites

Ensure the following tools are installed on your development machine before contributing:

| Tool | Required Version | Purpose |
|------|------------------|---------|
| **Python** | 3.12+ | Backend microservices runtime |
| **Node.js** | 20.x (LTS) | Frontend build toolchain |
| **npm** | 10.x+ | Node.js package manager |
| **Docker** | 25.x | Container runtime |
| **Docker Compose** | 2.24+ | Multi-service orchestration |
| **Git** | 2.40+ | Version control |
| **Make** | 4.0+ | Build automation |

### Optional Tools

| Tool | Version | Purpose |
|------|---------|---------|
| **Terraform** | 1.7+ | Infrastructure as code (for infra contributions) |
| **kubectl** | 1.29+ | Kubernetes management (for deployment contributions) |
| **Helm** | 3.14+ | Kubernetes package management |

### Initial Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/your-org/Synthetic-ERP-Data-Generation-Platform.git
   cd Synthetic-ERP-Data-Generation-Platform
   ```

2. **Copy the environment template:**

   ```bash
   cp .env.example .env
   ```

   Update `.env` with your local configuration values. Never commit the `.env` file.

3. **Set up the Python virtual environment:**

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate  # Linux/macOS
   # or
   .venv\Scripts\activate     # Windows
   ```

4. **Install backend dependencies:**

   ```bash
   # Install shared utilities
   pip install -r src/backend/shared/requirements.txt

   # Install service-specific dependencies (example: API Gateway)
   pip install -r src/backend/api_gateway/requirements.txt

   # Or install all services at once via Makefile
   make install
   ```

5. **Install frontend dependencies:**

   ```bash
   cd src/web
   npm install
   cd ../..
   ```

6. **Install development tools:**

   ```bash
   pip install ruff mypy pytest pytest-cov httpx mongomock
   ```

7. **Start local services via Docker Compose:**

   ```bash
   docker-compose up -d mongodb redis
   ```

   This launches MongoDB 7.0 and Redis 7.x for local development. Individual backend services can be run directly via Flask's development server or through Docker Compose.

8. **Verify the setup:**

   ```bash
   make lint       # Run all linters
   make test-unit  # Run unit tests
   ```

---

## Project Structure

Understanding the monorepo layout is essential for contributing to the correct areas:

```
Synthetic-ERP-Data-Generation-Platform/
├── src/
│   ├── backend/
│   │   ├── api_gateway/          # Flask REST API Gateway (Port 5000)
│   │   ├── generation_engine/    # Multi-method data generation (Port 5001)
│   │   ├── profiling_service/    # ERP schema discovery & profiling (Port 5002)
│   │   ├── quality_service/      # Data quality validation (Port 5003)
│   │   ├── compliance_service/   # PII detection & compliance (Port 5004)
│   │   ├── provisioning_service/ # DB connectors & cloud export (Port 5005)
│   │   └── shared/               # Shared utilities (DB, auth, logging)
│   └── web/                      # React 19.x Web Console (Port 3000)
├── infrastructure/
│   ├── docker/                   # Dockerfiles for all services
│   ├── kubernetes/               # Kubernetes manifests
│   └── terraform/                # Terraform IaC modules
├── tests/
│   ├── unit/                     # pytest (backend) & Vitest (frontend)
│   ├── integration/              # Cross-service integration tests
│   ├── e2e/                      # Playwright E2E tests
│   ├── performance/              # Locust/k6 load tests
│   └── security/                 # Security scanning configs
├── docs/                         # API docs, architecture, guides
├── .github/workflows/            # CI/CD pipelines
├── docker-compose.yml            # Local development orchestration
├── docker-compose.prod.yml       # Production overlay
├── pyproject.toml                # Root Python tooling config
├── Makefile                      # Build automation
├── .env.example                  # Environment variable template
└── CONTRIBUTING.md               # This file
```

### Service Ownership

Each backend service follows the same internal structure:

```
service_name/
├── __init__.py                   # Package marker
├── app.py                        # Flask Application Factory (create_app)
├── config.py                     # Environment-based configuration
├── routes/                       # Flask Blueprints for HTTP endpoints
├── services/                     # Business logic layer
├── models/                       # MongoDB document models
├── schemas/                      # Pydantic validation schemas
├── utils/                        # Service-specific utilities
├── requirements.txt              # Service dependencies
└── Dockerfile                    # Container image definition
```

---

## Git Workflow

This project follows **trunk-based development** with short-lived feature branches.

### Branching Strategy

| Branch Type | Naming Convention | Lifetime | Merges Into |
|-------------|-------------------|----------|-------------|
| **Main** | `main` | Permanent | — |
| **Feature** | `feat/<ticket-id>-<short-description>` | 1–3 days | `main` |
| **Bugfix** | `fix/<ticket-id>-<short-description>` | 1–2 days | `main` |
| **Hotfix** | `hotfix/<ticket-id>-<short-description>` | Hours | `main` |
| **Chore** | `chore/<short-description>` | 1–2 days | `main` |
| **Docs** | `docs/<short-description>` | 1–2 days | `main` |
| **Infra** | `infra/<short-description>` | 1–3 days | `main` |

### Branch Workflow

1. **Create a feature branch from `main`:**

   ```bash
   git checkout main
   git pull origin main
   git checkout -b feat/TICKET-123-add-parquet-export
   ```

2. **Make focused, incremental commits** following the [Commit Message Format](#commit-message-format).

3. **Rebase onto `main` before opening a PR:**

   ```bash
   git fetch origin
   git rebase origin/main
   ```

4. **Push and open a Pull Request** targeting `main`.

5. **After approval and CI passes**, squash-merge into `main`.

### Branch Rules

- Feature branches must be up-to-date with `main` before merging
- Branch lifetime should not exceed 3 days; break large changes into smaller PRs
- Delete feature branches after merging
- Never push directly to `main`; all changes require a PR

---

## Commit Message Format

This project uses **[Conventional Commits](https://www.conventionalcommits.org/)** for clear, structured commit history.

### Format

```
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

### Types

| Type | Description | Example |
|------|-------------|---------|
| `feat` | New feature | `feat(generation): add Parquet output formatter` |
| `fix` | Bug fix | `fix(api-gateway): resolve JWT refresh token rotation` |
| `docs` | Documentation changes | `docs(contributing): add Terraform style guidelines` |
| `style` | Code style changes (no logic change) | `style(backend): apply Ruff formatting rules` |
| `refactor` | Code restructuring (no behavior change) | `refactor(profiling): extract JDBC connection pooling` |
| `test` | Adding or updating tests | `test(quality): add weighted scoring unit tests` |
| `chore` | Maintenance tasks | `chore(deps): update Flask to 3.1.1` |
| `ci` | CI/CD changes | `ci(github): add security scan workflow` |
| `perf` | Performance improvement | `perf(generation): optimize batch processing buffer` |
| `build` | Build system changes | `build(docker): optimize multi-stage Dockerfile` |
| `infra` | Infrastructure changes | `infra(terraform): add GCP storage module` |

### Scope

The scope should identify the affected service or module:

| Scope | Area |
|-------|------|
| `api-gateway` | API Gateway service |
| `generation` | Generation Engine service |
| `profiling` | Profiling Service |
| `quality` | Quality Service |
| `compliance` | Compliance Service |
| `provisioning` | Provisioning Service |
| `shared` | Shared backend utilities |
| `web` | Frontend Web Console |
| `infra` | Infrastructure (Terraform, Kubernetes) |
| `docker` | Docker and Compose configurations |
| `ci` | CI/CD workflows |
| `deps` | Dependency updates |

### Rules

- Subject line must be lowercase, imperative mood, and no period at the end
- Subject line must not exceed 72 characters
- Body should wrap at 80 characters and explain *what* and *why* (not *how*)
- Breaking changes must include `BREAKING CHANGE:` in the footer
- Reference related issues with `Refs: #<issue-number>` or `Closes: #<issue-number>`

### Examples

```
feat(generation): add VAE-based synthetic data generator

Implement Variational Autoencoder architecture for continuous latent
space representation of tabular data. Supports both training and
inference modes with configurable latent dimensions.

Refs: #42
```

```
fix(compliance): correct HIPAA PHI detection false positives

Refine regex patterns for medical record numbers to reduce false
positive rate from 12% to under 2%. Adds negative lookahead for
common numeric patterns that are not PHI.

Closes: #87
```

---

## Coding Standards

### Python Code Style

All Python code must adhere to **PEP 8** standards, enforced via the **Ruff** linter and formatter.

#### Configuration

Python tooling is configured in the root `pyproject.toml`:

- **Ruff**: Linting and formatting with `target-version = "py312"` and `line-length = 120`
- **mypy**: Static type checking with `python_version = "3.12"` and `disallow_untyped_defs = true`
- **pytest**: Test runner with coverage reporting targeting ≥80% coverage

#### General Rules

| Rule | Standard |
|------|----------|
| Line length | 120 characters maximum |
| Indentation | 4 spaces (no tabs) |
| String quotes | Double quotes (`"`) |
| Import ordering | `isort` via Ruff (stdlib → third-party → first-party) |
| Trailing commas | Required in multi-line structures |
| Type hints | **Required** on all function parameters and return types |
| Docstrings | **Required** on all public classes, methods, and functions |

#### Type Hints

Type hints are mandatory on all function signatures:

```python
# Correct — fully typed
def calculate_quality_score(
    statistical_score: float,
    business_rules_score: float,
    referential_integrity_score: float,
) -> float:
    """Calculate weighted composite quality score.

    Args:
        statistical_score: Statistical fidelity score (0.0 to 1.0).
        business_rules_score: Business rules compliance score (0.0 to 1.0).
        referential_integrity_score: Referential integrity score (0.0 to 1.0).

    Returns:
        Weighted composite quality score (0.0 to 1.0).

    Raises:
        ValueError: If any score is outside the valid range.
    """
    if not all(0.0 <= s <= 1.0 for s in [
        statistical_score, business_rules_score, referential_integrity_score
    ]):
        raise ValueError("All scores must be between 0.0 and 1.0")

    return (
        0.4 * statistical_score
        + 0.3 * business_rules_score
        + 0.3 * referential_integrity_score
    )


# Incorrect — missing types
def calculate_quality_score(stat, business, referential):
    return 0.4 * stat + 0.3 * business + 0.3 * referential
```

#### Docstrings (Google Style)

All public classes, methods, and functions must include **Google-style** docstrings:

```python
class GenerationJob:
    """Represents a synthetic data generation job.

    Manages the lifecycle of a generation request from submission
    through completion, including method selection, batch processing,
    and quality validation.

    Attributes:
        job_id: Unique identifier for the generation job.
        tenant_id: Tenant namespace for multi-tenant isolation.
        status: Current job status (submitted, generating, validating, completed, failed).
        config: Generation configuration parameters.
    """

    def __init__(self, tenant_id: str, config: JobConfig) -> None:
        """Initialize a new generation job.

        Args:
            tenant_id: Tenant namespace identifier.
            config: Generation configuration with method, schema, and parameters.
        """
        self.job_id: str = generate_uuid()
        self.tenant_id: str = tenant_id
        self.status: str = "submitted"
        self.config: JobConfig = config
```

#### Flask Patterns

All backend services must use:

- **Application Factory pattern** — `create_app()` function in `app.py`
- **Blueprint-based routing** — one Blueprint per domain in the `routes/` directory
- **Service layer separation** — business logic in `services/`, HTTP handling in `routes/`
- **Pydantic 2.x** — for all request/response validation schemas
- **structlog** — for structured JSON logging

```python
# app.py — Application Factory pattern
from flask import Flask

def create_app(config_name: str = "development") -> Flask:
    """Create and configure the Flask application.

    Args:
        config_name: Configuration environment name.

    Returns:
        Configured Flask application instance.
    """
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    # Initialize extensions
    register_extensions(app)

    # Register blueprints
    register_blueprints(app)

    # Register error handlers
    register_error_handlers(app)

    return app
```

#### Linting and Formatting Commands

```bash
# Lint all Python code
ruff check src/backend/

# Auto-fix linting issues
ruff check --fix src/backend/

# Format all Python code
ruff format src/backend/

# Run mypy type checking
mypy src/backend/

# Run all checks via Makefile
make lint
```

### TypeScript Code Style

All frontend TypeScript code must pass **ESLint** and **Prettier** validation with strict TypeScript settings.

#### Configuration

- **TypeScript**: `strict: true` in `tsconfig.json`
- **ESLint**: Configured in `src/web/.eslintrc.json` with React and TypeScript rules
- **Prettier**: Configured in `src/web/.prettierrc` for consistent formatting

#### General Rules

| Rule | Standard |
|------|----------|
| Line length | 100 characters (Prettier printWidth) |
| Indentation | 2 spaces |
| Semicolons | Required |
| String quotes | Single quotes (`'`) |
| Trailing commas | Required (`"trailingComma": "all"`) |
| Return types | **Explicit** on all functions and methods |
| Interface naming | PascalCase, prefixed with `I` only for disambiguation |
| Type vs Interface | Prefer `interface` for object shapes, `type` for unions/intersections |

#### Component Structure

React components must follow this structure:

```typescript
// ComponentName.tsx
import React from 'react';

// Type/interface definitions
interface ComponentNameProps {
  title: string;
  onSubmit: (data: FormData) => void;
  isLoading?: boolean;
}

// Component implementation
export const ComponentName: React.FC<ComponentNameProps> = ({
  title,
  onSubmit,
  isLoading = false,
}): React.ReactElement => {
  // Hooks at the top
  const [state, setState] = React.useState<string>('');

  // Event handlers
  const handleSubmit = (event: React.FormEvent): void => {
    event.preventDefault();
    onSubmit(new FormData(event.target as HTMLFormElement));
  };

  // Render
  return (
    <div className="p-4">
      <h2 className="text-xl font-semibold">{title}</h2>
      {isLoading ? (
        <LoadingSpinner />
      ) : (
        <form onSubmit={handleSubmit}>
          {/* Form content */}
        </form>
      )}
    </div>
  );
};
```

#### State Management (Zustand)

Zustand stores must be typed and follow this pattern:

```typescript
// store/exampleStore.ts
import { create } from 'zustand';

interface ExampleState {
  items: Item[];
  isLoading: boolean;
  fetchItems: () => Promise<void>;
  addItem: (item: Item) => void;
}

export const useExampleStore = create<ExampleState>((set) => ({
  items: [],
  isLoading: false,
  fetchItems: async (): Promise<void> => {
    set({ isLoading: true });
    try {
      const items = await api.getItems();
      set({ items, isLoading: false });
    } catch (error) {
      set({ isLoading: false });
      throw error;
    }
  },
  addItem: (item: Item): void => {
    set((state) => ({ items: [...state.items, item] }));
  },
}));
```

#### Linting and Formatting Commands

```bash
cd src/web

# Lint TypeScript/React code
npx eslint src/ --ext .ts,.tsx

# Auto-fix ESLint issues
npx eslint src/ --ext .ts,.tsx --fix

# Format with Prettier
npx prettier --write src/

# Check formatting without writing
npx prettier --check src/

# Type check
npx tsc --noEmit
```

### Terraform Code Style

All Terraform configurations must follow the **HashiCorp style guide**.

#### General Rules

| Rule | Standard |
|------|----------|
| Indentation | 2 spaces |
| File naming | Lowercase with hyphens (`main.tf`, `variables.tf`, `outputs.tf`) |
| Resource naming | Lowercase with underscores (`aws_instance.web_server`) |
| Variable descriptions | **Required** on all variables |
| Output descriptions | **Required** on all outputs |
| Module structure | `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf` per module |

#### Module Structure

Every Terraform module must contain:

```
module_name/
├── main.tf           # Resource definitions
├── variables.tf      # Input variables with descriptions and types
├── outputs.tf        # Output values with descriptions
└── versions.tf       # Required provider versions (if root module)
```

#### Variable Definitions

```hcl
variable "cluster_name" {
  description = "Name of the Kubernetes cluster"
  type        = string
  validation {
    condition     = length(var.cluster_name) > 0
    error_message = "Cluster name must not be empty."
  }
}

variable "node_count" {
  description = "Number of worker nodes in the cluster"
  type        = number
  default     = 3
  validation {
    condition     = var.node_count >= 1 && var.node_count <= 100
    error_message = "Node count must be between 1 and 100."
  }
}
```

#### Formatting Commands

```bash
# Format all Terraform files
terraform fmt -recursive infrastructure/terraform/

# Validate configuration
terraform validate
```

---

## Testing Requirements

All contributions must include appropriate tests. The project follows the **test pyramid** strategy with extensive unit tests, focused integration tests, and targeted end-to-end tests.

### Test Coverage Thresholds

| Test Type | Coverage Target | Tool |
|-----------|----------------|------|
| Backend unit tests | ≥80% line coverage | pytest + pytest-cov |
| Frontend unit tests | ≥80% line coverage | Vitest |
| Integration tests | Critical paths covered | pytest |
| E2E tests | Core user flows covered | Playwright |

### Backend Testing (Python — pytest)

All backend services are tested with **pytest 8.x**.

#### Running Tests

```bash
# Run all unit tests with coverage
pytest tests/unit/ -v --cov=src/backend --cov-report=term-missing

# Run tests for a specific service
pytest tests/unit/backend/test_api_gateway.py -v

# Run integration tests
pytest tests/integration/ -v

# Run with markers
pytest -m "unit" -v
pytest -m "integration" -v

# Run via Makefile
make test-unit
make test-integration
```

#### Writing Backend Tests

```python
import pytest
from unittest.mock import MagicMock, patch

from api_gateway.app import create_app


@pytest.fixture
def app():
    """Create application instance for testing."""
    app = create_app("testing")
    yield app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


class TestGenerationEndpoint:
    """Tests for the generation job API endpoint."""

    def test_create_generation_job_returns_201(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Verify POST /api/v1/generation/jobs creates a job successfully."""
        payload = {
            "method": "statistical",
            "schema_id": "schema-001",
            "record_count": 10000,
        }
        response = client.post(
            "/api/v1/generation/jobs",
            json=payload,
            headers=auth_headers,
        )
        assert response.status_code == 201
        assert "job_id" in response.json

    def test_create_generation_job_requires_auth(
        self, client: FlaskClient
    ) -> None:
        """Verify POST /api/v1/generation/jobs returns 401 without JWT."""
        response = client.post("/api/v1/generation/jobs", json={})
        assert response.status_code == 401
```

#### Test File Naming

| Pattern | Description |
|---------|-------------|
| `test_<module>.py` | Unit test file for a specific module |
| `conftest.py` | Shared fixtures per test directory |
| `test_<feature>_integration.py` | Integration test file |

### Frontend Testing (TypeScript — Vitest)

All React components and hooks are tested with **Vitest** and **React Testing Library**.

#### Running Tests

```bash
cd src/web

# Run all tests
npx vitest run

# Run tests in watch mode (development only)
npx vitest

# Run with coverage
npx vitest run --coverage

# Run via Makefile
make test-frontend
```

#### Writing Frontend Tests

```typescript
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { GenerationWizard } from '@/pages/GenerationWizard';

describe('GenerationWizard', () => {
  it('renders the method selection step initially', (): void => {
    render(<GenerationWizard />);
    expect(screen.getByText('Select Generation Method')).toBeInTheDocument();
  });

  it('advances to schema config after method selection', async (): Promise<void> => {
    render(<GenerationWizard />);
    fireEvent.click(screen.getByText('Statistical'));
    fireEvent.click(screen.getByText('Next'));
    expect(await screen.findByText('Configure Schema')).toBeInTheDocument();
  });
});
```

### End-to-End Testing (Playwright)

Critical user flows are tested with **Playwright**.

#### Running E2E Tests

```bash
# Run all E2E tests
cd tests/e2e
npx playwright test

# Run specific test file
npx playwright test tests/generation-wizard.spec.ts

# Run with browser visible (debugging)
npx playwright test --headed

# Generate HTML report
npx playwright test --reporter=html
```

#### E2E Test Scope

| Flow | Test File | Description |
|------|-----------|-------------|
| Generation Wizard | `generation-wizard.spec.ts` | Complete wizard flow: method → schema → params → review → submit |
| Dashboard | `dashboard.spec.ts` | Dashboard data rendering, navigation, and interaction |
| Authentication | `authentication.spec.ts` | Login, logout, token refresh, role-based access |

### Performance Testing

Performance tests verify the platform meets the 1M+ records/minute throughput target.

```bash
# Run Locust load tests
cd tests/performance
locust -f locustfile.py --headless -u 100 -r 10 --run-time 5m

# Run k6 load tests
k6 run k6_load_test.js
```

---

## Code Quality Gates

All pull requests must pass the following automated quality gates before merging:

### Required CI Checks

| Gate | Tool | Threshold | Command |
|------|------|-----------|---------|
| Python linting | Ruff | Zero errors | `ruff check src/backend/` |
| Python type checking | mypy | Zero errors | `mypy src/backend/` |
| Python formatting | Ruff | Zero diffs | `ruff format --check src/backend/` |
| TypeScript linting | ESLint | Zero errors | `npx eslint src/ --ext .ts,.tsx` |
| TypeScript formatting | Prettier | Zero diffs | `npx prettier --check src/` |
| TypeScript type checking | tsc | Zero errors | `npx tsc --noEmit` |
| Backend test coverage | pytest-cov | ≥80% | `pytest --cov --cov-fail-under=80` |
| Frontend test coverage | Vitest | ≥80% | `npx vitest run --coverage` |
| Security scan (Python) | Bandit | Zero high-severity | `bandit -r src/backend/` |
| Security scan (containers) | Trivy | Zero critical CVEs | `trivy image <image>` |
| Terraform formatting | terraform fmt | Zero diffs | `terraform fmt -check -recursive` |
| Terraform validation | terraform validate | Zero errors | `terraform validate` |

### Pre-Commit Checks

Before committing, run the full quality check locally:

```bash
# Run all quality checks
make lint

# Alternatively, run individual checks
ruff check src/backend/
ruff format --check src/backend/
mypy src/backend/
cd src/web && npx eslint src/ --ext .ts,.tsx && npx prettier --check src/
```

### Quality Standards Summary

- **Zero tolerance** for linting errors, type errors, and formatting violations
- **≥80% test coverage** for all backend and frontend code
- **Zero high-severity** security vulnerabilities in dependencies and container images
- **All existing tests must pass** — no regressions allowed
- **No `TODO`, `FIXME`, or `HACK` comments** in production code (use issue tracker instead)

---

## Docker Development Workflow

### Running the Full Platform

```bash
# Start all services, MongoDB, and Redis
docker-compose up -d

# View service logs
docker-compose logs -f api-gateway
docker-compose logs -f generation-engine

# Check service health
curl http://localhost:5000/health     # API Gateway
curl http://localhost:5001/health     # Generation Engine
curl http://localhost:5002/health     # Profiling Service
curl http://localhost:5003/health     # Quality Service
curl http://localhost:5004/health     # Compliance Service
curl http://localhost:5005/health     # Provisioning Service

# Stop all services
docker-compose down

# Stop and remove volumes (full reset)
docker-compose down -v
```

### Running Individual Services

For faster development iteration on a single service:

```bash
# Start only infrastructure dependencies
docker-compose up -d mongodb redis

# Run the service locally with Flask dev server
cd src/backend/api_gateway
flask run --port 5000 --debug
```

### Rebuilding After Changes

```bash
# Rebuild a specific service
docker-compose build api-gateway

# Rebuild and restart
docker-compose up -d --build api-gateway

# Rebuild all services
docker-compose build
```

### Docker Image Best Practices

When modifying Dockerfiles, follow these standards:

- Use **multi-stage builds** to minimize final image size
- Base images: `python:3.12-slim` for backend, `node:20-alpine` for frontend build, `nginx:1.25-alpine` for frontend serving
- Pin dependency versions in `requirements.txt` (e.g., `flask==3.1.0`, not `flask>=3.0`)
- Run processes as non-root users
- Include `HEALTHCHECK` instructions
- Order `COPY` commands from least to most frequently changed (leverage layer caching)
- Use `.dockerignore` to exclude unnecessary files from build context

---

## Pull Request Process

### Before Opening a PR

1. **Ensure your branch is up-to-date** with `main`:

   ```bash
   git fetch origin
   git rebase origin/main
   ```

2. **Run all quality checks locally:**

   ```bash
   make lint
   make test-unit
   ```

3. **Verify Docker builds (if modifying Dockerfiles or dependencies):**

   ```bash
   docker-compose build <service-name>
   ```

4. **Self-review your changes** — read through the diff before requesting review.

### PR Requirements

| Requirement | Description |
|-------------|-------------|
| **Title** | Follows Conventional Commits format (e.g., `feat(generation): add Parquet formatter`) |
| **Description** | Clearly explains what, why, and how; references related issues |
| **Tests** | All new and modified code has corresponding tests |
| **Documentation** | Public APIs are documented; READMEs updated if needed |
| **No regressions** | All existing tests pass |
| **Quality gates pass** | All CI checks green |
| **Scope** | Focused on a single concern; no unrelated changes bundled |

### PR Description Template

```markdown
## Summary
Brief description of the change and its purpose.

## Type of Change
- [ ] New feature
- [ ] Bug fix
- [ ] Refactor
- [ ] Documentation
- [ ] Infrastructure
- [ ] CI/CD

## Related Issues
Closes #<issue-number>

## Changes Made
- Detailed list of changes
- Organized by affected component

## Testing
- Description of tests added or modified
- How to verify the change manually (if applicable)

## Checklist
- [ ] Code follows project coding standards
- [ ] Type hints added for all new Python functions
- [ ] Google-style docstrings added for all public APIs
- [ ] Tests added with ≥80% coverage for new code
- [ ] No linting errors (`make lint` passes)
- [ ] No type errors (`mypy` passes)
- [ ] Documentation updated (if applicable)
- [ ] Docker builds succeed (if applicable)
- [ ] No secrets or credentials committed
```

### Review Process

1. **Minimum one approval** required from a code owner or maintainer
2. **Two approvals required** for:
   - Changes to `src/backend/shared/` (affects all services)
   - Changes to infrastructure (Terraform, Kubernetes)
   - Changes to CI/CD workflows
   - Changes to authentication or authorization logic
   - Changes to compliance or PII detection logic
3. **Reviewers should check:**
   - Code quality, readability, and adherence to standards
   - Test coverage and quality of test cases
   - Security implications (no hardcoded secrets, proper input validation)
   - Performance implications (no N+1 queries, proper indexing)
   - Multi-tenant isolation (no cross-tenant data leakage)
   - SOC 2 compliance (audit logging for sensitive operations)
4. **Review turnaround**: Aim to review within 1 business day
5. **Merge strategy**: **Squash and merge** for feature/fix branches to maintain clean history

### After Merging

- Delete the feature branch
- Verify CI/CD pipeline succeeds on `main`
- If deploying, follow the deployment runbook for the affected service(s)

---

## Architecture Decision Records

Significant architectural decisions are documented as **Architecture Decision Records (ADRs)** in the `docs/architecture/` directory.

### When to Write an ADR

Create an ADR for decisions that:

- Introduce a new dependency or framework
- Change an existing architectural pattern
- Affect multiple services or components
- Have significant trade-offs or alternatives considered
- Impact security, performance, or scalability

### ADR Format

```markdown
# ADR-NNN: Title of Decision

## Status
Proposed | Accepted | Deprecated | Superseded by ADR-XXX

## Context
What is the issue that we're seeing that motivates this decision or change?

## Decision
What is the change that we're proposing and/or doing?

## Consequences
What becomes easier or more difficult to do because of this change?

### Positive
- Benefit 1
- Benefit 2

### Negative
- Trade-off 1
- Trade-off 2

### Neutral
- Side effect 1

## Alternatives Considered
- Alternative A: Description and reason for rejection
- Alternative B: Description and reason for rejection
```

### ADR Numbering

ADRs are numbered sequentially: `ADR-001`, `ADR-002`, etc. Never reuse numbers, even for deprecated decisions.

---

## Issue Reporting Guidelines

### Bug Reports

When reporting bugs, include:

| Field | Description |
|-------|-------------|
| **Title** | Concise summary (e.g., "Quality score calculation ignores referential integrity weight") |
| **Environment** | OS, Docker version, browser (if frontend), service version |
| **Steps to Reproduce** | Numbered, specific steps to trigger the bug |
| **Expected Behavior** | What should have happened |
| **Actual Behavior** | What actually happened |
| **Error Logs** | Relevant log output (redact any sensitive data) |
| **Screenshots** | For frontend issues, include screenshots of the UI |

### Bug Report Template

```markdown
## Bug Description
A clear, concise description of the bug.

## Environment
- OS: [e.g., Ubuntu 22.04]
- Docker: [e.g., 25.0.3]
- Service: [e.g., API Gateway v1.0.0]
- Browser: [e.g., Chrome 120] (if frontend)

## Steps to Reproduce
1. Navigate to '...'
2. Click on '...'
3. Observe '...'

## Expected Behavior
Description of what should happen.

## Actual Behavior
Description of what actually happens.

## Error Logs
```
Paste relevant log output here (redact sensitive data).
```

## Additional Context
Any other relevant information.
```

### Feature Requests

Feature requests must include:

| Field | Description |
|-------|-------------|
| **Title** | Descriptive summary of the feature |
| **Problem Statement** | What problem does this solve? |
| **Proposed Solution** | How should the feature work? |
| **Alternatives** | Other approaches considered |
| **Scope** | Which services and components are affected? |
| **Acceptance Criteria** | How do we know the feature is complete? |

### Issue Labels

| Label | Description |
|-------|-------------|
| `bug` | Something isn't working |
| `feature` | New feature request |
| `enhancement` | Improvement to existing functionality |
| `documentation` | Documentation improvements |
| `security` | Security-related issue |
| `performance` | Performance-related issue |
| `infrastructure` | Infrastructure/DevOps related |
| `good-first-issue` | Good for newcomers |
| `help-wanted` | Extra attention needed |
| `priority:critical` | Must fix immediately |
| `priority:high` | Fix in current sprint |
| `priority:medium` | Fix in next sprint |
| `priority:low` | Fix when convenient |

---

## Security Considerations

The Synthetic-ERP-Data-Generation-Platform handles enterprise data generation with strict security and compliance requirements. All contributors must adhere to these security practices:

### Mandatory Security Practices

- **Never commit secrets**: API keys, passwords, tokens, and credentials must never appear in code. Use environment variables and `.env` files (which are `.gitignore`d)
- **Never log sensitive data**: Ensure structured logging does not include PII, credentials, or tokens. Use field redaction in log formatters
- **Validate all inputs**: Use Pydantic models for backend request validation and TypeScript interfaces for frontend data contracts
- **Enforce multi-tenant isolation**: Every database query and API response must be scoped to the authenticated tenant. Cross-tenant access must be impossible by design
- **Audit sensitive operations**: All data generation, export, compliance certification, and administrative actions must produce audit log entries in the `audit_logs` MongoDB collection
- **Use encryption standards**: AES-256 for data at rest, TLS 1.3 for data in transit. Do not implement custom cryptographic algorithms
- **Follow the principle of least privilege**: JWT tokens should carry minimal claims; RBAC permissions should grant the minimum access required for each role

### RBAC Roles Reference

| Role | Scope | Key Permissions |
|------|-------|-----------------|
| Platform Admin | System-wide | Full access, user management, tenant configuration |
| Data Engineer | Tenant-scoped | Schema management, profile configuration, template creation |
| Developer | Tenant-scoped | Generation job execution, export, API access |
| QA Engineer | Tenant-scoped | Quality report access, test data generation |
| Data Analyst | Tenant-scoped | Read-only access to profiles, reports, and schemas |

### Reporting Security Vulnerabilities

If you discover a security vulnerability, **do NOT open a public issue**. Instead:

1. Email the security team at the address listed in the project's `SECURITY.md` (if available) or contact the maintainers directly
2. Include a detailed description of the vulnerability, steps to reproduce, and potential impact
3. Allow reasonable time for the team to address the issue before any public disclosure

---

## Questions and Support

- **General questions**: Open a GitHub Discussion
- **Bug reports**: Open a GitHub Issue using the bug report template
- **Feature requests**: Open a GitHub Issue using the feature request template
- **Security issues**: Follow the [security reporting process](#reporting-security-vulnerabilities)
- **Architecture discussions**: Propose an ADR and open a PR for review

Thank you for contributing to the Synthetic-ERP-Data-Generation-Platform! Your contributions help build a robust, enterprise-grade synthetic data generation solution.
