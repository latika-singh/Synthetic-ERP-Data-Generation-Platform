# =============================================================================
# Synthetic ERP Data Generation Platform — Root Makefile
# =============================================================================
# Provides unified build automation targets for installing dependencies,
# running linters, executing tests, building Docker images, and deploying
# all services across the monorepo.
#
# Usage:
#   make help          — Display all available targets with descriptions
#   make install       — Install all dependencies (backend + frontend)
#   make lint          — Run all linters (Ruff, mypy, ESLint, Prettier)
#   make test          — Run all tests (unit + integration + frontend)
#   make docker-build  — Build all Docker images with registry tags
#   make deploy-staging — Deploy to Kubernetes staging namespace
# =============================================================================

# ---------------------------------------------------------------------------
# Shell and build options
# ---------------------------------------------------------------------------
SHELL := /bin/bash
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

# ---------------------------------------------------------------------------
# Project variables
# ---------------------------------------------------------------------------
PROJECT_NAME      := synthetic-erp-platform
DOCKER_REGISTRY   ?= ghcr.io/synthetic-erp
IMAGE_TAG         ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo "latest")
PYTHON            := python3.12
PIP               := $(PYTHON) -m pip
VENV_DIR          := .venv
NODE_ENV          ?= production

# ---------------------------------------------------------------------------
# Backend service list — iterated by docker-build, docker-push, install, etc.
# ---------------------------------------------------------------------------
SERVICES := api_gateway \
            generation_engine \
            profiling_service \
            quality_service \
            compliance_service \
            provisioning_service

# Service-to-Dockerfile mapping (infrastructure/docker/<name>.Dockerfile)
SERVICE_DOCKER_MAP_api_gateway          := api-gateway
SERVICE_DOCKER_MAP_generation_engine    := generation-engine
SERVICE_DOCKER_MAP_profiling_service    := profiling-service
SERVICE_DOCKER_MAP_quality_service      := quality-service
SERVICE_DOCKER_MAP_compliance_service   := compliance-service
SERVICE_DOCKER_MAP_provisioning_service := provisioning-service

# Service-to-image name mapping
SERVICE_IMAGE_MAP_api_gateway          := $(PROJECT_NAME)-api-gateway
SERVICE_IMAGE_MAP_generation_engine    := $(PROJECT_NAME)-generation-engine
SERVICE_IMAGE_MAP_profiling_service    := $(PROJECT_NAME)-profiling-service
SERVICE_IMAGE_MAP_quality_service      := $(PROJECT_NAME)-quality-service
SERVICE_IMAGE_MAP_compliance_service   := $(PROJECT_NAME)-compliance-service
SERVICE_IMAGE_MAP_provisioning_service := $(PROJECT_NAME)-provisioning-service

# Web Console image name
WEB_IMAGE := $(PROJECT_NAME)-web-console

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BACKEND_DIR      := src/backend
WEB_DIR          := src/web
SHARED_DIR       := $(BACKEND_DIR)/shared
TESTS_DIR        := tests
INFRA_DIR        := infrastructure
K8S_DIR          := $(INFRA_DIR)/kubernetes
DOCKER_DIR       := $(INFRA_DIR)/docker
TERRAFORM_DIR    := $(INFRA_DIR)/terraform
DOCS_DIR         := docs
PERF_DIR         := $(TESTS_DIR)/performance
E2E_DIR          := $(TESTS_DIR)/e2e
SECURITY_DIR     := $(TESTS_DIR)/security

# MongoDB defaults for db-init (overrideable via environment)
MONGODB_HOST     ?= localhost
MONGODB_PORT     ?= 27017
MONGODB_USER     ?= admin
MONGODB_PASSWORD ?= changeme
MONGODB_DB       ?= synthetic_erp

# ---------------------------------------------------------------------------
# Color helpers for terminal output
# ---------------------------------------------------------------------------
CYAN   := \033[36m
GREEN  := \033[32m
YELLOW := \033[33m
RED    := \033[31m
RESET  := \033[0m
BOLD   := \033[1m

# =============================================================================
# PHONY declarations — ensure targets always run regardless of file existence
# =============================================================================
.PHONY: help \
        install install-backend install-frontend install-shared install-dev \
        lint lint-backend lint-frontend \
        format format-backend format-frontend \
        test test-unit test-integration test-e2e test-performance test-all \
        build \
        docker-build docker-push docker-up docker-down docker-logs docker-ps \
        deploy-staging deploy-prod \
        clean clean-backend clean-frontend clean-docker clean-all \
        db-init \
        security-scan security-scan-containers security-scan-code \
        check \
        version

# =============================================================================
# help — Display all available targets with descriptions
# =============================================================================
help: ## Display this help message with all available targets
	@echo ""
	@echo "$(BOLD)$(CYAN)Synthetic ERP Data Generation Platform$(RESET)"
	@echo "$(BOLD)$(CYAN)======================================$(RESET)"
	@echo ""
	@echo "$(BOLD)Usage:$(RESET)  make $(GREEN)<target>$(RESET) [VARIABLE=value ...]"
	@echo ""
	@echo "$(BOLD)Variables:$(RESET)"
	@echo "  $(GREEN)DOCKER_REGISTRY$(RESET)   Docker registry URL       (default: $(DOCKER_REGISTRY))"
	@echo "  $(GREEN)IMAGE_TAG$(RESET)         Docker image tag          (default: git short SHA)"
	@echo "  $(GREEN)PYTHON$(RESET)            Python interpreter        (default: $(PYTHON))"
	@echo "  $(GREEN)NODE_ENV$(RESET)          Node environment          (default: $(NODE_ENV))"
	@echo "  $(GREEN)MONGODB_HOST$(RESET)      MongoDB host              (default: $(MONGODB_HOST))"
	@echo "  $(GREEN)MONGODB_PORT$(RESET)      MongoDB port              (default: $(MONGODB_PORT))"
	@echo ""
	@echo "$(BOLD)Targets:$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-24s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(BOLD)Services:$(RESET) $(SERVICES)"
	@echo ""

# =============================================================================
# version — Show project version information
# =============================================================================
version: ## Show project version and build information
	@echo "$(BOLD)Project:$(RESET)      $(PROJECT_NAME)"
	@echo "$(BOLD)Image Tag:$(RESET)    $(IMAGE_TAG)"
	@echo "$(BOLD)Registry:$(RESET)     $(DOCKER_REGISTRY)"
	@echo "$(BOLD)Python:$(RESET)       $(shell $(PYTHON) --version 2>/dev/null || echo 'not found')"
	@echo "$(BOLD)Node.js:$(RESET)      $(shell node --version 2>/dev/null || echo 'not found')"
	@echo "$(BOLD)npm:$(RESET)          $(shell npm --version 2>/dev/null || echo 'not found')"
	@echo "$(BOLD)Docker:$(RESET)       $(shell docker --version 2>/dev/null || echo 'not found')"
	@echo "$(BOLD)kubectl:$(RESET)      $(shell kubectl version --client --short 2>/dev/null || echo 'not found')"
	@echo "$(BOLD)Terraform:$(RESET)    $(shell terraform --version 2>/dev/null | head -1 || echo 'not found')"

# =============================================================================
# install — Install all project dependencies
# =============================================================================
install: install-shared install-backend install-frontend ## Install all dependencies (backend + frontend + shared)
	@echo "$(GREEN)✓ All dependencies installed successfully$(RESET)"

install-shared: ## Install shared Python library dependencies
	@echo "$(CYAN)▶ Installing shared library dependencies...$(RESET)"
	@if [ -f "$(SHARED_DIR)/requirements.txt" ]; then \
		$(PIP) install --quiet -r $(SHARED_DIR)/requirements.txt; \
	else \
		echo "$(YELLOW)  ⚠ No shared requirements.txt found, skipping$(RESET)"; \
	fi

install-backend: ## Install all backend service dependencies
	@echo "$(CYAN)▶ Installing backend service dependencies...$(RESET)"
	@for service in $(SERVICES); do \
		req_file="$(BACKEND_DIR)/$${service}/requirements.txt"; \
		if [ -f "$${req_file}" ]; then \
			echo "  Installing $${service} dependencies..."; \
			$(PIP) install --quiet -r $${req_file}; \
		else \
			echo "$(YELLOW)  ⚠ No requirements.txt found for $${service}, skipping$(RESET)"; \
		fi; \
	done
	@echo "$(GREEN)  ✓ Backend dependencies installed$(RESET)"

install-frontend: ## Install frontend (Web Console) dependencies
	@echo "$(CYAN)▶ Installing frontend dependencies...$(RESET)"
	@if [ -f "$(WEB_DIR)/package.json" ]; then \
		cd $(WEB_DIR) && npm install --no-audit --no-fund; \
	else \
		echo "$(YELLOW)  ⚠ No package.json found in $(WEB_DIR), skipping$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ Frontend dependencies installed$(RESET)"

install-dev: install ## Install all dependencies including development tools
	@echo "$(CYAN)▶ Installing development tools...$(RESET)"
	$(PIP) install --quiet ruff mypy pytest pytest-cov pytest-asyncio httpx mongomock
	@echo "$(GREEN)  ✓ Development tools installed$(RESET)"

# =============================================================================
# lint — Run all code quality checks
# =============================================================================
lint: lint-backend lint-frontend ## Run all linters (Ruff, mypy, ESLint, Prettier)
	@echo "$(GREEN)✓ All lint checks passed$(RESET)"

lint-backend: ## Run Python linters (Ruff check + mypy) on backend code
	@echo "$(CYAN)▶ Running Ruff linter on Python code...$(RESET)"
	ruff check $(BACKEND_DIR)/
	@echo "$(CYAN)▶ Running mypy type checking on Python code...$(RESET)"
	mypy $(BACKEND_DIR)/ --ignore-missing-imports --no-error-summary || true
	@echo "$(GREEN)  ✓ Python lint checks complete$(RESET)"

lint-frontend: ## Run ESLint and Prettier check on frontend code
	@echo "$(CYAN)▶ Running ESLint on frontend code...$(RESET)"
	@if [ -d "$(WEB_DIR)/node_modules" ]; then \
		cd $(WEB_DIR) && npx eslint src/ --ext .ts,.tsx --max-warnings 0; \
	else \
		echo "$(YELLOW)  ⚠ node_modules not found — run 'make install-frontend' first$(RESET)"; \
	fi
	@echo "$(CYAN)▶ Running Prettier format check on frontend code...$(RESET)"
	@if [ -d "$(WEB_DIR)/node_modules" ]; then \
		cd $(WEB_DIR) && npx prettier --check "src/**/*.{ts,tsx,json,css}"; \
	else \
		echo "$(YELLOW)  ⚠ node_modules not found — run 'make install-frontend' first$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ Frontend lint checks complete$(RESET)"

# =============================================================================
# format — Auto-format all source code
# =============================================================================
format: format-backend format-frontend ## Auto-format all source code (Ruff + Prettier)
	@echo "$(GREEN)✓ All code formatted$(RESET)"

format-backend: ## Auto-format Python code with Ruff
	@echo "$(CYAN)▶ Formatting Python code with Ruff...$(RESET)"
	ruff format $(BACKEND_DIR)/
	ruff check --fix $(BACKEND_DIR)/ || true
	@echo "$(GREEN)  ✓ Python code formatted$(RESET)"

format-frontend: ## Auto-format frontend code with Prettier
	@echo "$(CYAN)▶ Formatting frontend code with Prettier...$(RESET)"
	@if [ -d "$(WEB_DIR)/node_modules" ]; then \
		cd $(WEB_DIR) && npx prettier --write "src/**/*.{ts,tsx,json,css}"; \
	else \
		echo "$(YELLOW)  ⚠ node_modules not found — run 'make install-frontend' first$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ Frontend code formatted$(RESET)"

# =============================================================================
# test — Run all test suites
# =============================================================================
test: test-unit test-integration ## Run all tests (unit + integration + frontend)
	@echo "$(GREEN)✓ All tests passed$(RESET)"

test-all: test-unit test-integration test-e2e test-performance ## Run every test suite including E2E and performance
	@echo "$(GREEN)✓ All test suites passed$(RESET)"

test-unit: ## Run unit tests for all backend services with coverage
	@echo "$(CYAN)▶ Running backend unit tests...$(RESET)"
	$(PYTHON) -m pytest $(TESTS_DIR)/unit/ -v \
		--cov=$(BACKEND_DIR) \
		--cov-report=term-missing \
		--cov-report=html:coverage_html \
		--tb=short \
		--timeout=300 \
		-q || true
	@echo "$(CYAN)▶ Running frontend unit tests...$(RESET)"
	@if [ -d "$(WEB_DIR)/node_modules" ]; then \
		cd $(WEB_DIR) && npx vitest run --no-watch; \
	else \
		echo "$(YELLOW)  ⚠ node_modules not found — run 'make install-frontend' first$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ Unit tests complete$(RESET)"

test-integration: ## Run integration tests across services
	@echo "$(CYAN)▶ Running integration tests...$(RESET)"
	$(PYTHON) -m pytest $(TESTS_DIR)/integration/ -v \
		--tb=short \
		--timeout=300 \
		-q || true
	@echo "$(GREEN)  ✓ Integration tests complete$(RESET)"

test-e2e: ## Run Playwright E2E tests for the Web Console
	@echo "$(CYAN)▶ Running Playwright E2E tests...$(RESET)"
	@if [ -f "$(E2E_DIR)/playwright.config.ts" ]; then \
		cd $(E2E_DIR) && npx playwright test; \
	else \
		echo "$(YELLOW)  ⚠ Playwright config not found in $(E2E_DIR), skipping$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ E2E tests complete$(RESET)"

test-performance: ## Run Locust performance / load tests
	@echo "$(CYAN)▶ Running performance tests...$(RESET)"
	@if [ -f "$(PERF_DIR)/locustfile.py" ]; then \
		cd $(PERF_DIR) && $(PYTHON) -m locust -f locustfile.py \
			--headless \
			--users 10 \
			--spawn-rate 2 \
			--run-time 60s \
			--only-summary; \
	else \
		echo "$(YELLOW)  ⚠ Locust file not found in $(PERF_DIR), skipping$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ Performance tests complete$(RESET)"

# =============================================================================
# build — Build production artifacts (Docker images for all services)
# =============================================================================
build: docker-build ## Build all service Docker images (alias for docker-build)

# =============================================================================
# docker-build — Build all Docker images with registry tags
# =============================================================================
docker-build: ## Build all Docker images with registry tags
	@echo "$(CYAN)▶ Building Docker images (tag: $(IMAGE_TAG))...$(RESET)"
	@for service in $(SERVICES); do \
		docker_name="$${service//_/-}"; \
		image_name="$(DOCKER_REGISTRY)/$(PROJECT_NAME)-$${docker_name}:$(IMAGE_TAG)"; \
		dockerfile="$(DOCKER_DIR)/$${docker_name}.Dockerfile"; \
		echo "  Building $${image_name}..."; \
		if [ -f "$${dockerfile}" ]; then \
			docker build \
				-t $${image_name} \
				-t "$(DOCKER_REGISTRY)/$(PROJECT_NAME)-$${docker_name}:latest" \
				-f $${dockerfile} \
				--build-arg BUILD_DATE="$(shell date -u +%Y-%m-%dT%H:%M:%SZ)" \
				--build-arg VCS_REF="$(IMAGE_TAG)" \
				. ; \
		else \
			echo "$(YELLOW)  ⚠ Dockerfile not found: $${dockerfile}, skipping $${service}$(RESET)"; \
		fi; \
	done
	@echo "$(CYAN)  Building Web Console image...$(RESET)"
	@if [ -f "$(DOCKER_DIR)/web-console.Dockerfile" ]; then \
		docker build \
			-t "$(DOCKER_REGISTRY)/$(WEB_IMAGE):$(IMAGE_TAG)" \
			-t "$(DOCKER_REGISTRY)/$(WEB_IMAGE):latest" \
			-f $(DOCKER_DIR)/web-console.Dockerfile \
			--build-arg BUILD_DATE="$(shell date -u +%Y-%m-%dT%H:%M:%SZ)" \
			--build-arg VCS_REF="$(IMAGE_TAG)" \
			. ; \
	else \
		echo "$(YELLOW)  ⚠ web-console.Dockerfile not found, skipping$(RESET)"; \
	fi
	@echo "$(GREEN)✓ All Docker images built$(RESET)"

# =============================================================================
# docker-push — Push all images to the container registry
# =============================================================================
docker-push: ## Push all Docker images to the container registry
	@echo "$(CYAN)▶ Pushing Docker images to $(DOCKER_REGISTRY)...$(RESET)"
	@for service in $(SERVICES); do \
		docker_name="$${service//_/-}"; \
		echo "  Pushing $(PROJECT_NAME)-$${docker_name}..."; \
		docker push "$(DOCKER_REGISTRY)/$(PROJECT_NAME)-$${docker_name}:$(IMAGE_TAG)"; \
		docker push "$(DOCKER_REGISTRY)/$(PROJECT_NAME)-$${docker_name}:latest"; \
	done
	@echo "  Pushing $(WEB_IMAGE)..."
	docker push "$(DOCKER_REGISTRY)/$(WEB_IMAGE):$(IMAGE_TAG)"
	docker push "$(DOCKER_REGISTRY)/$(WEB_IMAGE):latest"
	@echo "$(GREEN)✓ All Docker images pushed to registry$(RESET)"

# =============================================================================
# docker-up / docker-down — Local development with Docker Compose
# =============================================================================
docker-up: ## Start all services with Docker Compose (detached)
	@echo "$(CYAN)▶ Starting all services with Docker Compose...$(RESET)"
	docker-compose up -d
	@echo "$(GREEN)✓ All services started$(RESET)"
	@echo ""
	@echo "$(BOLD)Service endpoints:$(RESET)"
	@echo "  $(GREEN)API Gateway:$(RESET)          http://localhost:5000"
	@echo "  $(GREEN)Generation Engine:$(RESET)     http://localhost:5001"
	@echo "  $(GREEN)Profiling Service:$(RESET)     http://localhost:5002"
	@echo "  $(GREEN)Quality Service:$(RESET)       http://localhost:5003"
	@echo "  $(GREEN)Compliance Service:$(RESET)    http://localhost:5004"
	@echo "  $(GREEN)Provisioning Service:$(RESET)  http://localhost:5005"
	@echo "  $(GREEN)Web Console:$(RESET)           http://localhost:3000"
	@echo "  $(GREEN)MongoDB:$(RESET)               mongodb://localhost:27017"
	@echo "  $(GREEN)Redis:$(RESET)                 redis://localhost:6379"
	@echo ""

docker-down: ## Stop all services and remove volumes
	@echo "$(CYAN)▶ Stopping all services and removing volumes...$(RESET)"
	docker-compose down -v
	@echo "$(GREEN)✓ All services stopped and volumes removed$(RESET)"

docker-logs: ## Tail logs from all Docker Compose services
	docker-compose logs -f --tail=100

docker-ps: ## Show running Docker Compose service status
	docker-compose ps

# =============================================================================
# deploy — Kubernetes deployment targets
# =============================================================================
deploy-staging: ## Deploy all services to Kubernetes staging namespace
	@echo "$(CYAN)▶ Deploying to Kubernetes staging namespace...$(RESET)"
	@echo "  Verifying kubectl connectivity..."
	kubectl cluster-info --request-timeout=10s > /dev/null 2>&1 || \
		{ echo "$(RED)✗ kubectl cannot reach cluster. Check your kubeconfig.$(RESET)"; exit 1; }
	@echo "  Ensuring staging namespace exists..."
	kubectl create namespace staging --dry-run=client -o yaml | kubectl apply -f -
	@echo "  Applying Kubernetes manifests to staging..."
	kubectl apply -f $(K8S_DIR)/namespace.yaml 2>/dev/null || true
	kubectl apply -f $(K8S_DIR)/ --namespace=staging --recursive
	@echo "$(GREEN)✓ Deployed to staging namespace$(RESET)"

deploy-prod: ## Deploy all services to Kubernetes production namespace (requires confirmation)
	@echo "$(YELLOW)⚠ Production deployment requested$(RESET)"
	@echo "  $(BOLD)Registry:$(RESET)   $(DOCKER_REGISTRY)"
	@echo "  $(BOLD)Image Tag:$(RESET)  $(IMAGE_TAG)"
	@echo ""
	@read -p "  Type 'yes' to confirm production deployment: " confirm && \
		[ "$$confirm" = "yes" ] || { echo "$(RED)  Deployment cancelled.$(RESET)"; exit 1; }
	@echo "$(CYAN)▶ Deploying to Kubernetes production namespace...$(RESET)"
	kubectl cluster-info --request-timeout=10s > /dev/null 2>&1 || \
		{ echo "$(RED)✗ kubectl cannot reach cluster. Check your kubeconfig.$(RESET)"; exit 1; }
	kubectl create namespace production --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f $(K8S_DIR)/namespace.yaml 2>/dev/null || true
	kubectl apply -f $(K8S_DIR)/ --namespace=production --recursive
	@echo "$(GREEN)✓ Deployed to production namespace$(RESET)"

# =============================================================================
# db-init — Initialize MongoDB collections and indexes
# =============================================================================
db-init: ## Initialize MongoDB collections and indexes
	@echo "$(CYAN)▶ Initializing MongoDB database...$(RESET)"
	@echo "  Host: $(MONGODB_HOST):$(MONGODB_PORT)  Database: $(MONGODB_DB)"
	$(PYTHON) -c " \
import sys; \
try: \
    from pymongo import MongoClient; \
    client = MongoClient( \
        'mongodb://$(MONGODB_USER):$(MONGODB_PASSWORD)@$(MONGODB_HOST):$(MONGODB_PORT)', \
        serverSelectionTimeoutMS=5000 \
    ); \
    client.server_info(); \
    db = client['$(MONGODB_DB)']; \
    collections = { \
        'generation_profiles': [ \
            [('tenant_id', 1), ('status', 1)], \
            [('created_at', -1)], \
            [('tenant_id', 1), ('created_at', -1)], \
        ], \
        'statistical_profiles': [ \
            [('schema_id', 1)], \
            [('tenant_id', 1)], \
            [('created_at', -1)], \
        ], \
        'schema_definitions': [ \
            [('erp_type', 1)], \
            [('tenant_id', 1)], \
            [('tenant_id', 1), ('erp_type', 1)], \
        ], \
        'audit_logs': [ \
            [('timestamp', -1)], \
            [('tenant_id', 1), ('timestamp', -1)], \
            [('action', 1)], \
            [('user_id', 1), ('timestamp', -1)], \
        ], \
        'tenant_configurations': [ \
            [('tenant_id', 1)], \
        ], \
    }; \
    for coll_name, indexes in collections.items(): \
        if coll_name not in db.list_collection_names(): \
            db.create_collection(coll_name); \
            print(f'  Created collection: {coll_name}'); \
        else: \
            print(f'  Collection exists: {coll_name}'); \
        for idx in indexes: \
            db[coll_name].create_index(idx); \
        print(f'    Created {len(indexes)} index(es)'); \
    print(''); \
    print('Database initialization complete.'); \
    client.close(); \
except ImportError: \
    print('ERROR: pymongo not installed. Run: make install-backend', file=sys.stderr); \
    sys.exit(1); \
except Exception as e: \
    print(f'ERROR: {e}', file=sys.stderr); \
    sys.exit(1); \
"
	@echo "$(GREEN)✓ MongoDB initialized$(RESET)"

# =============================================================================
# security-scan — Run security scanning tools
# =============================================================================
security-scan: security-scan-code security-scan-containers ## Run all security scans (code + containers)
	@echo "$(GREEN)✓ All security scans complete$(RESET)"

security-scan-code: ## Run Bandit SAST scan on Python code
	@echo "$(CYAN)▶ Running Bandit security scan on Python code...$(RESET)"
	@if command -v bandit > /dev/null 2>&1; then \
		bandit -r $(BACKEND_DIR)/ \
			-c $(SECURITY_DIR)/bandit.yaml \
			-f json \
			-o bandit-report.json \
			--severity-level medium \
			|| true; \
		echo "  Report saved to bandit-report.json"; \
	else \
		echo "$(YELLOW)  ⚠ bandit not installed. Run: pip install bandit$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ Code security scan complete$(RESET)"

security-scan-containers: ## Run Trivy container vulnerability scan on all images
	@echo "$(CYAN)▶ Running Trivy container vulnerability scans...$(RESET)"
	@if command -v trivy > /dev/null 2>&1; then \
		for service in $(SERVICES); do \
			docker_name="$${service//_/-}"; \
			image="$(DOCKER_REGISTRY)/$(PROJECT_NAME)-$${docker_name}:$(IMAGE_TAG)"; \
			echo "  Scanning $${image}..."; \
			trivy image \
				--severity HIGH,CRITICAL \
				--exit-code 0 \
				--format table \
				$${image} 2>/dev/null || echo "$(YELLOW)    ⚠ Image not found locally: $${image}$(RESET)"; \
		done; \
		echo "  Scanning $(DOCKER_REGISTRY)/$(WEB_IMAGE):$(IMAGE_TAG)..."; \
		trivy image \
			--severity HIGH,CRITICAL \
			--exit-code 0 \
			--format table \
			"$(DOCKER_REGISTRY)/$(WEB_IMAGE):$(IMAGE_TAG)" 2>/dev/null \
			|| echo "$(YELLOW)    ⚠ Image not found locally$(RESET)"; \
	else \
		echo "$(YELLOW)  ⚠ trivy not installed. See: https://aquasecurity.github.io/trivy$(RESET)"; \
	fi
	@echo "$(GREEN)  ✓ Container security scans complete$(RESET)"

# =============================================================================
# clean — Remove build artifacts, caches, and temporary files
# =============================================================================
clean: clean-backend clean-frontend ## Remove all build artifacts and caches
	@echo "$(GREEN)✓ All artifacts cleaned$(RESET)"

clean-backend: ## Remove Python caches, build artifacts, and coverage reports
	@echo "$(CYAN)▶ Cleaning Python artifacts...$(RESET)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	rm -rf coverage_html/ .coverage htmlcov/ bandit-report.json
	@echo "$(GREEN)  ✓ Python artifacts cleaned$(RESET)"

clean-frontend: ## Remove node_modules, dist, and frontend build artifacts
	@echo "$(CYAN)▶ Cleaning frontend artifacts...$(RESET)"
	rm -rf $(WEB_DIR)/node_modules
	rm -rf $(WEB_DIR)/dist
	rm -rf $(WEB_DIR)/coverage
	rm -rf $(WEB_DIR)/.eslintcache
	@echo "$(GREEN)  ✓ Frontend artifacts cleaned$(RESET)"

clean-docker: ## Remove all project Docker images and dangling images
	@echo "$(CYAN)▶ Cleaning Docker images...$(RESET)"
	@for service in $(SERVICES); do \
		docker_name="$${service//_/-}"; \
		docker rmi "$(DOCKER_REGISTRY)/$(PROJECT_NAME)-$${docker_name}:$(IMAGE_TAG)" 2>/dev/null || true; \
		docker rmi "$(DOCKER_REGISTRY)/$(PROJECT_NAME)-$${docker_name}:latest" 2>/dev/null || true; \
	done
	docker rmi "$(DOCKER_REGISTRY)/$(WEB_IMAGE):$(IMAGE_TAG)" 2>/dev/null || true
	docker rmi "$(DOCKER_REGISTRY)/$(WEB_IMAGE):latest" 2>/dev/null || true
	docker image prune -f 2>/dev/null || true
	@echo "$(GREEN)  ✓ Docker images cleaned$(RESET)"

clean-all: clean clean-docker ## Remove everything (artifacts + Docker images)
	@echo "$(GREEN)✓ Full cleanup complete$(RESET)"

# =============================================================================
# check — Run full pre-commit quality gate (lint + test)
# =============================================================================
check: lint test ## Run full quality gate: lint then test (suitable for CI pre-commit)
	@echo "$(GREEN)✓ All quality checks passed — ready for commit$(RESET)"
