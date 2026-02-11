# Kubernetes Production Deployment Guide

> **Platform:** Synthetic-ERP-Data-Generation-Platform  
> **Kubernetes Version:** 1.29+  
> **Last Updated:** 2025

This guide provides step-by-step instructions for deploying the Synthetic-ERP-Data-Generation-Platform to a production Kubernetes cluster. It covers namespace creation, secrets management, data-store provisioning, service deployments, ingress configuration, auto-scaling, network policies, resource quotas, and monitoring.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Architecture Overview](#2-architecture-overview)
3. [Namespace Setup](#3-namespace-setup)
4. [Sealed Secrets — Credential Management](#4-sealed-secrets--credential-management)
5. [ConfigMaps — Service Configuration](#5-configmaps--service-configuration)
6. [StatefulSets — Data Stores](#6-statefulsets--data-stores)
7. [Deployments — Application Services](#7-deployments--application-services)
8. [Services — Networking](#8-services--networking)
9. [Ingress — TLS 1.3 and Routing](#9-ingress--tls-13-and-routing)
10. [HPA — Horizontal Pod Autoscaler](#10-hpa--horizontal-pod-autoscaler)
11. [NetworkPolicies — Zero-Trust Segmentation](#11-networkpolicies--zero-trust-segmentation)
12. [ResourceQuotas and LimitRanges](#12-resourcequotas-and-limitranges)
13. [Monitoring Stack](#13-monitoring-stack)
14. [Deployment Order and Verification](#14-deployment-order-and-verification)
15. [Scaling and Operations](#15-scaling-and-operations)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Prerequisites

### 1.1 Required Tools

| Tool | Minimum Version | Purpose |
|------|-----------------|---------|
| `kubectl` | 1.29+ | Kubernetes CLI for cluster management |
| `helm` | 3.14+ | Kubernetes package manager for chart-based installs |
| `kubeseal` | 0.25+ | Bitnami Sealed Secrets CLI for encrypting secrets |
| `docker` | 25.x | Container image build and push (if building locally) |
| `openssl` | 3.x | TLS certificate generation (if not using cert-manager) |

### 1.2 Cluster Requirements

| Resource | Minimum (Development) | Recommended (Production) |
|----------|-----------------------|--------------------------|
| Nodes | 3 | 5–10 |
| Total CPU | 16 cores | 48+ cores |
| Total RAM | 64 GB | 192+ GB |
| Storage Class | `standard` (or equivalent) | `gp3` (AWS) / `managed-premium` (Azure) / `pd-ssd` (GCP) |
| Kubernetes Version | 1.29 | 1.29+ |
| Container Runtime | containerd 1.7+ | containerd 1.7+ |
| CNI Plugin | Calico / Cilium / VPC-CNI | Calico / Cilium (for NetworkPolicy) |
| Ingress Controller | NGINX Ingress 1.9+ | NGINX Ingress 1.9+ |

### 1.3 Managed Kubernetes Services

This guide supports deployment to the following managed Kubernetes providers:

- **AWS EKS** — Elastic Kubernetes Service (recommended with `gp3` storage class and ALB Ingress)
- **Azure AKS** — Azure Kubernetes Service (recommended with `managed-premium` storage and Azure Application Gateway)
- **GCP GKE** — Google Kubernetes Engine (recommended with `pd-ssd` storage and GCE Ingress)

### 1.4 Pre-Deployment Checklist

Before proceeding, ensure the following are available:

- [ ] Kubernetes cluster is provisioned and `kubectl` is configured (`kubectl cluster-info` succeeds)
- [ ] Container images for all services are pushed to a container registry accessible from the cluster
- [ ] TLS certificate and private key for the platform domain (e.g., `synthetic-erp.example.com`)
- [ ] MongoDB root credentials are prepared
- [ ] Redis password is generated (minimum 32 characters, random)
- [ ] Auth0 tenant is configured with client ID, client secret, domain, and API audience
- [ ] JWT signing keys are generated (RSA 2048-bit keypair for RS256)
- [ ] AES-256 encryption key is generated (32-byte random key, base64-encoded)
- [ ] Cloud provider credentials (AWS/Azure/GCP) are prepared if cloud storage export is used
- [ ] NGINX Ingress Controller is installed (or will be installed via Helm in this guide)
- [ ] A CNI plugin with NetworkPolicy support is installed (Calico, Cilium, or equivalent)
- [ ] Helm repositories are added for sealed-secrets, prometheus, and grafana

### 1.5 Container Images

The following container images must be available in your container registry:

| Image | Default Tag | Service |
|-------|-------------|---------|
| `synthetic-erp-platform/api-gateway` | `latest` | API Gateway (Flask 3.1.x) |
| `synthetic-erp-platform/generation-engine` | `latest` | Generation Engine (LangChain + PyTorch/TensorFlow) |
| `synthetic-erp-platform/profiling-service` | `latest` | Profiling Service (SciPy/JDBC) |
| `synthetic-erp-platform/quality-service` | `latest` | Quality Service (Great Expectations) |
| `synthetic-erp-platform/compliance-service` | `latest` | Compliance Service (spaCy) |
| `synthetic-erp-platform/provisioning-service` | `latest` | Provisioning Service (JDBC/Cloud SDKs) |
| `synthetic-erp-platform/web-console` | `latest` | Web Console (React 19.x / nginx) |
| `mongo` | `7.0` | MongoDB (from Docker Hub) |
| `redis` | `7-alpine` | Redis (from Docker Hub) |

> **Air-Gapped Environments:** If deploying to an air-gapped cluster, see [Air-Gapped Deployment Guide](./air-gapped.md) for instructions on mirroring images to a private registry.

---

## 2. Architecture Overview

The platform runs as a set of microservices inside a Kubernetes cluster, organized into two namespaces:

```
┌─────────────────────────────────────────────────────────────────┐
│  Namespace: synthetic-erp-platform                              │
│                                                                 │
│  ┌──────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │  Web Console │  │   API Gateway    │  │ Generation Engine│  │
│  │  (React/Nginx)│  │  (Flask 3.1.x)  │  │ (LangChain/ML)  │  │
│  │  Port: 80    │  │  Port: 5000      │  │  Port: 5001      │  │
│  └──────────────┘  └──────────────────┘  └──────────────────┘  │
│                                                                 │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │Profiling Service │  │ Quality Service  │  │  Compliance  │  │
│  │  (SciPy/JDBC)   │  │(Great Expectations)│ │  (spaCy/NLP) │  │
│  │  Port: 5002      │  │  Port: 5003      │  │  Port: 5004  │  │
│  └──────────────────┘  └──────────────────┘  └──────────────┘  │
│                                                                 │
│  ┌──────────────────┐  ┌───────────┐  ┌───────────┐           │
│  │   Provisioning   │  │ MongoDB   │  │   Redis   │           │
│  │  (JDBC/Cloud)    │  │  7.0 RS   │  │  7-alpine │           │
│  │  Port: 5005      │  │ Port:27017│  │ Port:6379 │           │
│  └──────────────────┘  └───────────┘  └───────────┘           │
│                                                                 │
│  NGINX Ingress → TLS 1.3 termination → path-based routing     │
│  NetworkPolicies → Zero-trust per-service isolation            │
│  HPA → Auto-scaling for 1M+ records/minute throughput          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  Namespace: monitoring                                          │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │  Prometheus  │  │   Grafana    │  │  OpenTelemetry       │  │
│  │  Metrics     │  │  Dashboards  │  │  Collector           │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Service Communication Map

| Source | Target | Protocol | Port | Purpose |
|--------|--------|----------|------|---------|
| External (Users) | NGINX Ingress | HTTPS | 443 | TLS 1.3 termination |
| NGINX Ingress | API Gateway | HTTP | 5000 | REST API routing (`/api/v1/*`) |
| NGINX Ingress | Web Console | HTTP | 80 | Frontend SPA (`/`) |
| API Gateway | Generation Engine | HTTP | 5001 | Job dispatch |
| API Gateway | Profiling Service | HTTP | 5002 | Schema discovery |
| API Gateway | Quality Service | HTTP | 5003 | Quality queries |
| API Gateway | Compliance Service | HTTP | 5004 | Compliance queries |
| API Gateway | Provisioning Service | HTTP | 5005 | Export dispatch |
| Generation Engine | Quality Service | HTTP | 5003 | Post-generation validation |
| Generation Engine | Compliance Service | HTTP | 5004 | PII scanning |
| Generation Engine | Provisioning Service | HTTP | 5005 | Output delivery |
| All Backend Services | MongoDB | TCP | 27017 | Metadata persistence |
| API Gateway, Gen Engine | Redis | TCP | 6379 | Caching, rate limiting, progress |

---

## 3. Namespace Setup

Create the two namespaces with Pod Security Standards labels for admission control.

### 3.1 Apply Namespace Manifest

```bash
kubectl apply -f infrastructure/kubernetes/namespace.yaml
```

The `namespace.yaml` creates:

- **`synthetic-erp-platform`** — Main application namespace for all workloads, with `restricted` Pod Security Standard enforcement:
  - `pod-security.kubernetes.io/enforce: restricted`
  - `pod-security.kubernetes.io/audit: restricted`
  - `pod-security.kubernetes.io/warn: restricted`

- **`monitoring`** — Observability namespace for Prometheus, Grafana, and OpenTelemetry Collector, with `baseline` enforcement:
  - `pod-security.kubernetes.io/enforce: baseline`
  - `pod-security.kubernetes.io/audit: restricted`

### 3.2 Verify Namespace Creation

```bash
kubectl get namespaces -l app.kubernetes.io/part-of=synthetic-erp-platform
```

Expected output:

```
NAME                      STATUS   AGE
synthetic-erp-platform    Active   10s
monitoring                Active   10s
```

### 3.3 Set Default Namespace

For convenience during the rest of this deployment, set the default namespace:

```bash
kubectl config set-context --current --namespace=synthetic-erp-platform
```

---

## 4. Sealed Secrets — Credential Management

The platform uses [Bitnami Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) to encrypt Kubernetes Secrets at rest in version control. Only the Sealed Secrets controller running in the cluster can decrypt them.

### 4.1 Install the Sealed Secrets Controller

```bash
helm repo add sealed-secrets https://bitnami-labs.github.io/sealed-secrets
helm repo update

helm install sealed-secrets sealed-secrets/sealed-secrets \
  --namespace kube-system \
  --set resources.requests.cpu=50m \
  --set resources.requests.memory=64Mi \
  --set resources.limits.cpu=100m \
  --set resources.limits.memory=128Mi \
  --wait
```

Verify the controller is running:

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=sealed-secrets
```

### 4.2 Fetch the Sealed Secrets Public Key

```bash
kubeseal --fetch-cert \
  --controller-name=sealed-secrets \
  --controller-namespace=kube-system \
  > sealed-secrets-pub-cert.pem
```

### 4.3 Create and Encrypt Secrets

The platform requires the following secret groups. For each, create a raw Kubernetes Secret, then encrypt it with `kubeseal`.

#### 4.3.1 MongoDB Credentials

```bash
kubectl create secret generic mongodb-credentials \
  --namespace synthetic-erp-platform \
  --from-literal=MONGODB_URI="mongodb://admin:<password>@mongodb-0.mongodb,mongodb-1.mongodb,mongodb-2.mongodb:27017/synthetic_erp_platform?replicaSet=rs0&authSource=admin" \
  --from-literal=MONGODB_USERNAME="admin" \
  --from-literal=MONGODB_PASSWORD="<your-mongodb-password>" \
  --from-literal=MONGODB_DATABASE="synthetic_erp_platform" \
  --dry-run=client -o yaml | \
  kubeseal --cert sealed-secrets-pub-cert.pem \
    --format yaml > infrastructure/kubernetes/sealed-mongodb-credentials.yaml
```

#### 4.3.2 Redis Credentials

```bash
kubectl create secret generic redis-credentials \
  --namespace synthetic-erp-platform \
  --from-literal=REDIS_URL="redis://:@redis-0.redis:6379/0" \
  --from-literal=REDIS_PASSWORD="<your-redis-password-min-32-chars>" \
  --dry-run=client -o yaml | \
  kubeseal --cert sealed-secrets-pub-cert.pem \
    --format yaml > infrastructure/kubernetes/sealed-redis-credentials.yaml
```

#### 4.3.3 Auth0 Credentials

```bash
kubectl create secret generic auth0-credentials \
  --namespace synthetic-erp-platform \
  --from-literal=AUTH0_DOMAIN="<your-tenant>.auth0.com" \
  --from-literal=AUTH0_CLIENT_ID="<your-auth0-client-id>" \
  --from-literal=AUTH0_CLIENT_SECRET="<your-auth0-client-secret>" \
  --from-literal=AUTH0_AUDIENCE="https://api.synthetic-erp-platform.com" \
  --dry-run=client -o yaml | \
  kubeseal --cert sealed-secrets-pub-cert.pem \
    --format yaml > infrastructure/kubernetes/sealed-auth0-credentials.yaml
```

#### 4.3.4 JWT Signing Keys

Generate an RSA 2048-bit keypair for RS256 JWT signing:

```bash
openssl genrsa -out jwt-private.pem 2048
openssl rsa -in jwt-private.pem -pubout -out jwt-public.pem

kubectl create secret generic jwt-keys \
  --namespace synthetic-erp-platform \
  --from-literal=JWT_SECRET_KEY="$(openssl rand -hex 64)" \
  --from-file=JWT_PRIVATE_KEY=jwt-private.pem \
  --from-file=JWT_PUBLIC_KEY=jwt-public.pem \
  --dry-run=client -o yaml | \
  kubeseal --cert sealed-secrets-pub-cert.pem \
    --format yaml > infrastructure/kubernetes/sealed-jwt-keys.yaml

# Clean up local key files
rm jwt-private.pem jwt-public.pem
```

#### 4.3.5 Cloud Provider Credentials

```bash
kubectl create secret generic cloud-credentials \
  --namespace synthetic-erp-platform \
  --from-literal=AWS_ACCESS_KEY_ID="<your-aws-access-key>" \
  --from-literal=AWS_SECRET_ACCESS_KEY="<your-aws-secret-key>" \
  --from-literal=AZURE_STORAGE_CONNECTION_STRING="<your-azure-connection-string>" \
  --from-literal=GCP_SERVICE_ACCOUNT_KEY="$(cat gcp-service-account.json | base64 -w0)" \
  --dry-run=client -o yaml | \
  kubeseal --cert sealed-secrets-pub-cert.pem \
    --format yaml > infrastructure/kubernetes/sealed-cloud-credentials.yaml
```

#### 4.3.6 Encryption Keys

```bash
kubectl create secret generic encryption-keys \
  --namespace synthetic-erp-platform \
  --from-literal=AES_256_KEY="$(openssl rand -base64 32)" \
  --from-literal=FLASK_SECRET_KEY="$(openssl rand -hex 64)" \
  --dry-run=client -o yaml | \
  kubeseal --cert sealed-secrets-pub-cert.pem \
    --format yaml > infrastructure/kubernetes/sealed-encryption-keys.yaml
```

#### 4.3.7 TLS Certificate Secret

```bash
kubectl create secret tls synthetic-erp-tls-secret \
  --namespace synthetic-erp-platform \
  --cert=path/to/tls.crt \
  --key=path/to/tls.key
```

> **Note:** Alternatively, use [cert-manager](https://cert-manager.io/) with Let's Encrypt for automated TLS certificate provisioning and renewal.

### 4.4 Apply All Sealed Secrets

```bash
# Apply the template with placeholder encrypted values
kubectl apply -f infrastructure/kubernetes/secrets.yaml

# Or apply individually sealed secrets
kubectl apply -f infrastructure/kubernetes/sealed-mongodb-credentials.yaml
kubectl apply -f infrastructure/kubernetes/sealed-redis-credentials.yaml
kubectl apply -f infrastructure/kubernetes/sealed-auth0-credentials.yaml
kubectl apply -f infrastructure/kubernetes/sealed-jwt-keys.yaml
kubectl apply -f infrastructure/kubernetes/sealed-cloud-credentials.yaml
kubectl apply -f infrastructure/kubernetes/sealed-encryption-keys.yaml
```

### 4.5 Verify Secrets

```bash
kubectl get secrets -n synthetic-erp-platform
```

Expected secrets:

```
NAME                       TYPE                                  DATA   AGE
mongodb-credentials        Opaque                                4      30s
redis-credentials          Opaque                                2      30s
auth0-credentials          Opaque                                4      30s
jwt-keys                   Opaque                                3      30s
cloud-credentials          Opaque                                4      30s
encryption-keys            Opaque                                2      30s
synthetic-erp-tls-secret   kubernetes.io/tls                     2      30s
```

> **Security Note:** Never commit raw (unencrypted) Kubernetes Secrets to version control. Always use `kubeseal` to encrypt before committing. Rotate secrets periodically per SOC 2 Type II requirements.

---

## 5. ConfigMaps — Service Configuration

ConfigMaps store non-sensitive, environment-specific configuration for each service following 12-factor app methodology.

### 5.1 Deploy All ConfigMaps

```bash
# API Gateway configuration
kubectl apply -f infrastructure/kubernetes/api-gateway/configmap.yaml

# Generation Engine configuration
kubectl apply -f infrastructure/kubernetes/generation-engine/configmap.yaml

# Profiling Service configuration
kubectl apply -f infrastructure/kubernetes/profiling-service/configmap.yaml

# Quality Service configuration
kubectl apply -f infrastructure/kubernetes/quality-service/configmap.yaml

# Compliance Service configuration
kubectl apply -f infrastructure/kubernetes/compliance-service/configmap.yaml

# Provisioning Service configuration
kubectl apply -f infrastructure/kubernetes/provisioning-service/configmap.yaml
```

### 5.2 Key Configuration Parameters

#### API Gateway (`api-gateway-config`)

| Key | Default Value | Description |
|-----|---------------|-------------|
| `FLASK_ENV` | `production` | Flask environment mode |
| `LOG_LEVEL` | `INFO` | Structured JSON logging level |
| `LOG_FORMAT` | `json` | Log output format |
| `MONGODB_DATABASE` | `synthetic_erp_platform` | MongoDB database name |
| `API_PREFIX` | `/api/v1` | REST API URL prefix |
| `RATE_LIMIT_BASIC_TIER` | `60` | Requests/min for Developer, QA Engineer roles |
| `RATE_LIMIT_STANDARD_TIER` | `300` | Requests/min for Data Engineer, Data Analyst roles |
| `RATE_LIMIT_PREMIUM_TIER` | `1000` | Requests/min for Platform Admin role |
| `GUNICORN_WORKERS` | `4` | Gunicorn worker processes |
| `GUNICORN_THREADS` | `2` | Threads per Gunicorn worker |
| `GUNICORN_TIMEOUT` | `120` | Worker timeout in seconds |
| `JWT_ALGORITHM` | `RS256` | JWT token signing algorithm (Auth0) |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Failures before circuit opens |
| `CIRCUIT_BREAKER_RECOVERY_TIMEOUT` | `30` | Seconds before retry after open circuit |

#### Generation Engine (`generation-engine-config`)

| Key | Default Value | Description |
|-----|---------------|-------------|
| `BATCH_SIZE` | `10000` | Records per batch (tunable 1K–100K) |
| `GPU_ENABLED` | `false` | Enable GPU for GAN/VAE workloads |
| `MAX_CONCURRENT_JOBS` | `5` | Concurrent generation jobs per pod |
| `CHECKPOINT_INTERVAL` | `5000` | Records between checkpoint saves |
| `GAN_TRAINING_EPOCHS` | `100` | Default GAN training epochs |
| `VAE_LATENT_DIM` | `128` | VAE latent space dimension |
| `SUPPORTED_GENERATION_METHODS` | `ai_ml,rules,statistical,masking` | Enabled generation strategies |
| `SUPPORTED_OUTPUT_FORMATS` | `sql,csv,json,parquet` | Enabled export formats |
| `SUPPORTED_ERP_MODULES` | `financial_accounting,human_resources,sales_distribution,material_management` | Initial four ERP modules |
| `REFERENTIAL_INTEGRITY_ENABLED` | `true` | FK relationship enforcement |

### 5.3 Verify ConfigMaps

```bash
kubectl get configmaps -n synthetic-erp-platform
```

Expected output:

```
NAME                           DATA   AGE
api-gateway-config             25+    10s
generation-engine-config       20+    10s
profiling-service-config       15+    10s
quality-service-config         10+    10s
compliance-service-config      10+    10s
provisioning-service-config    15+    10s
```

### 5.4 Customizing ConfigMaps per Environment

For environment-specific overrides, use Kustomize overlays:

```bash
# Create overlay for staging
mkdir -p infrastructure/kubernetes/overlays/staging
cat > infrastructure/kubernetes/overlays/staging/kustomization.yaml <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../api-gateway/configmap.yaml
patches:
  - target:
      kind: ConfigMap
      name: api-gateway-config
    patch: |
      - op: replace
        path: /data/LOG_LEVEL
        value: DEBUG
      - op: replace
        path: /data/FLASK_ENV
        value: staging
EOF

kubectl apply -k infrastructure/kubernetes/overlays/staging/
```

---

## 6. StatefulSets — Data Stores

### 6.1 MongoDB 7.0 Replica Set

MongoDB serves as the platform's metadata repository, storing five core collections:

- `generation_profiles` — Generation job configurations and status
- `statistical_profiles` — Source data statistical distributions
- `schema_definitions` — ERP table/column metadata
- `audit_logs` — Tamper-evident audit trail (7-year retention)
- `tenant_configurations` — Per-tenant settings and quotas

#### 6.1.1 Deploy MongoDB StatefulSet and Service

```bash
# Deploy the headless Service first (required for StatefulSet DNS)
kubectl apply -f infrastructure/kubernetes/mongodb/service.yaml

# Deploy the StatefulSet
kubectl apply -f infrastructure/kubernetes/mongodb/statefulset.yaml
```

#### 6.1.2 MongoDB StatefulSet Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| Replicas | 3 | Replica set for HA (`rs0`) |
| Image | `mongo:7.0` | Official MongoDB 7.0 image |
| Storage Engine | WiredTiger | With 1.5 GB cache (`--wiredTigerCacheSizeGB 1.5`) |
| Authentication | Enabled | `--auth` with keyfile for internal replication |
| PVC Size | 50 Gi per replica | `ReadWriteOnce` access mode |
| CPU Request / Limit | 500m / 2 | Sufficient for metadata workloads |
| Memory Request / Limit | 1 Gi / 4 Gi | WiredTiger cache + working set |
| Liveness Probe | `mongosh --eval "db.adminCommand('ping')"` | 30s initial, 15s period |
| Readiness Probe | `mongosh --eval "db.adminCommand('ping')"` | 15s initial, 10s period |

#### 6.1.3 Wait for MongoDB Pods

```bash
kubectl rollout status statefulset/mongodb -n synthetic-erp-platform --timeout=300s
```

Verify all three pods are running:

```bash
kubectl get pods -l app=mongodb -n synthetic-erp-platform
```

Expected output:

```
NAME        READY   STATUS    RESTARTS   AGE
mongodb-0   1/1     Running   0          120s
mongodb-1   1/1     Running   0          90s
mongodb-2   1/1     Running   0          60s
```

#### 6.1.4 Initialize the Replica Set

After all three pods are running, initialize the MongoDB replica set:

```bash
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- mongosh --eval '
rs.initiate({
  _id: "rs0",
  members: [
    { _id: 0, host: "mongodb-0.mongodb.synthetic-erp-platform.svc.cluster.local:27017", priority: 2 },
    { _id: 1, host: "mongodb-1.mongodb.synthetic-erp-platform.svc.cluster.local:27017", priority: 1 },
    { _id: 2, host: "mongodb-2.mongodb.synthetic-erp-platform.svc.cluster.local:27017", priority: 1 }
  ]
})'
```

Verify the replica set status:

```bash
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- mongosh --eval 'rs.status().members.map(m => ({name: m.name, state: m.stateStr}))'
```

Expected output shows one `PRIMARY` and two `SECONDARY` members.

#### 6.1.5 Create Database and Collections

```bash
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- mongosh \
  -u admin -p '<your-mongodb-password>' --authenticationDatabase admin <<'EOF'
use synthetic_erp_platform;

// Create collections with validation
db.createCollection("generation_profiles");
db.createCollection("statistical_profiles");
db.createCollection("schema_definitions");
db.createCollection("audit_logs", {
  capped: false,
  validator: {
    $jsonSchema: {
      bsonType: "object",
      required: ["timestamp", "action", "actor", "resource", "checksum"],
      properties: {
        timestamp: { bsonType: "date" },
        action: { bsonType: "string" },
        actor: { bsonType: "string" },
        resource: { bsonType: "string" },
        checksum: { bsonType: "string" }
      }
    }
  }
});
db.createCollection("tenant_configurations");

// Create indexes for performance
db.generation_profiles.createIndex({ "tenant_id": 1, "status": 1 });
db.generation_profiles.createIndex({ "created_at": -1 });
db.generation_profiles.createIndex({ "tenant_id": 1, "created_at": -1 });

db.statistical_profiles.createIndex({ "tenant_id": 1, "schema_id": 1 });
db.statistical_profiles.createIndex({ "created_at": -1 });

db.schema_definitions.createIndex({ "tenant_id": 1, "erp_system": 1 });
db.schema_definitions.createIndex({ "erp_module": 1, "tenant_id": 1 });

db.audit_logs.createIndex({ "timestamp": -1 });
db.audit_logs.createIndex({ "tenant_id": 1, "timestamp": -1 });
db.audit_logs.createIndex({ "actor": 1, "timestamp": -1 });
db.audit_logs.createIndex({ "resource": 1, "action": 1 });

db.tenant_configurations.createIndex({ "tenant_id": 1 }, { unique: true });

print("Database initialization complete.");
EOF
```

#### 6.1.6 MongoDB DNS Endpoints

Backend services connect to MongoDB using the replica set connection string via Kubernetes DNS:

```
mongodb://admin:<password>@mongodb-0.mongodb.synthetic-erp-platform.svc.cluster.local:27017,mongodb-1.mongodb.synthetic-erp-platform.svc.cluster.local:27017,mongodb-2.mongodb.synthetic-erp-platform.svc.cluster.local:27017/synthetic_erp_platform?replicaSet=rs0&authSource=admin
```

### 6.2 Redis 7.x Cache

Redis provides session caching, API response caching, rate limiting counters, and real-time job progress tracking.

#### 6.2.1 Deploy Redis StatefulSet and Service

```bash
# Deploy the headless Service
kubectl apply -f infrastructure/kubernetes/redis/service.yaml

# Deploy the StatefulSet
kubectl apply -f infrastructure/kubernetes/redis/statefulset.yaml
```

#### 6.2.2 Redis StatefulSet Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| Replicas | 1 (dev) / 3 (prod HA) | Scale to 3 with Redis Sentinel for HA |
| Image | `redis:7-alpine` | Lightweight Alpine-based Redis 7.x |
| Persistence | AOF (`appendonly yes`) + RDB snapshots | Data survives pod restarts |
| Max Memory | 384 MB | With `allkeys-lru` eviction policy |
| PVC Size | 10 Gi | For AOF and RDB persistence files |
| CPU Request / Limit | 250m / 500m | Low resource footprint |
| Memory Request / Limit | 256 Mi / 512 Mi | Includes Redis + AOF buffer |
| Liveness Probe | `redis-cli ping` | 15s initial, 20s period |
| Readiness Probe | `redis-cli ping` | 5s initial, 10s period |

#### 6.2.3 Wait for Redis

```bash
kubectl rollout status statefulset/redis -n synthetic-erp-platform --timeout=120s
```

Verify the pod is running:

```bash
kubectl get pods -l app=redis -n synthetic-erp-platform
```

#### 6.2.4 Verify Redis Connectivity

```bash
kubectl exec -it redis-0 -n synthetic-erp-platform -- redis-cli -a '<your-redis-password>' ping
```

Expected response: `PONG`

---

## 7. Deployments — Application Services

All application services follow a consistent deployment pattern:

- **Rolling update strategy** with `maxSurge: 1` and `maxUnavailable: 0` for zero-downtime deployments
- **Health probes** (liveness, readiness, startup) for reliable operation
- **Environment variables** injected from ConfigMaps (non-sensitive) and Secrets (sensitive)
- **Pod anti-affinity** to distribute pods across nodes
- **Security context** with `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, and `capabilities: drop: ["ALL"]`
- **Prometheus annotations** for metrics scraping

### 7.1 API Gateway

The API Gateway is the central REST API entry point, built with Flask 3.1.x, Gunicorn 21.x, JWT authentication via Auth0, and tiered rate limiting via Redis.

```bash
kubectl apply -f infrastructure/kubernetes/api-gateway/deployment.yaml
kubectl apply -f infrastructure/kubernetes/api-gateway/service.yaml
```

| Parameter | Value |
|-----------|-------|
| Replicas | 2 (base, HPA scales to 10) |
| Port | 5000 |
| CPU Request / Limit | 500m / 1 |
| Memory Request / Limit | 512 Mi / 1 Gi |
| Health Endpoint | `/health` (liveness), `/ready` (readiness) |
| Service Type | LoadBalancer |
| Startup Probe Timeout | ~65s (5s initial + 12 failures × 5s period) |

```bash
kubectl rollout status deployment/api-gateway -n synthetic-erp-platform --timeout=180s
```

### 7.2 Generation Engine

The Generation Engine orchestrates multi-method synthetic data generation (AI/ML GAN/VAE, rules-based, statistical, masking) via LangChain. It has higher resource requirements for ML workloads.

```bash
kubectl apply -f infrastructure/kubernetes/generation-engine/deployment.yaml
kubectl apply -f infrastructure/kubernetes/generation-engine/service.yaml
```

| Parameter | Value |
|-----------|-------|
| Replicas | 2 (base, HPA scales to 20) |
| Port | 5001 |
| CPU Request / Limit | 1 / 4 |
| Memory Request / Limit | 2 Gi / 8 Gi |
| GPU (optional) | `nvidia.com/gpu: 1` when `GPU_ENABLED=true` |
| Health Endpoint | `/health` (liveness), `/ready` (readiness) |
| Service Type | ClusterIP (internal only) |
| Startup Probe Timeout | ~195s (allows ML model loading) |
| Grace Period | 120s (long-running batch completion) |

> **GPU Support:** To enable GPU-accelerated GAN/VAE generation, set `GPU_ENABLED=true` in the ConfigMap and ensure your cluster has nodes with NVIDIA GPUs and the [NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin) installed.

```bash
kubectl rollout status deployment/generation-engine -n synthetic-erp-platform --timeout=300s
```

### 7.3 Profiling Service

The Profiling Service connects to source ERP systems via JDBC to extract schema metadata without accessing raw production data.

```bash
kubectl apply -f infrastructure/kubernetes/profiling-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/profiling-service/service.yaml
```

| Parameter | Value |
|-----------|-------|
| Replicas | 1 (base, HPA scales to 5) |
| Port | 5002 |
| CPU Request / Limit | 500m / 2 |
| Memory Request / Limit | 512 Mi / 2 Gi |
| Service Type | ClusterIP |

```bash
kubectl rollout status deployment/profiling-service -n synthetic-erp-platform --timeout=180s
```

### 7.4 Quality Service

The Quality Service validates generated data quality using a weighted scoring model: 40% statistical fidelity + 30% business rules + 30% referential integrity, targeting ≥95% composite score.

```bash
kubectl apply -f infrastructure/kubernetes/quality-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/quality-service/service.yaml
```

| Parameter | Value |
|-----------|-------|
| Replicas | 1 (base, HPA scales to 8) |
| Port | 5003 |
| CPU Request / Limit | 500m / 2 |
| Memory Request / Limit | 512 Mi / 2 Gi |
| Service Type | ClusterIP |

```bash
kubectl rollout status deployment/quality-service -n synthetic-erp-platform --timeout=180s
```

### 7.5 Compliance Service

The Compliance Service performs PII detection using spaCy NLP entity recognition and regex pattern matching, with GDPR/HIPAA/CCPA regulatory verification.

```bash
kubectl apply -f infrastructure/kubernetes/compliance-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/compliance-service/service.yaml
```

| Parameter | Value |
|-----------|-------|
| Replicas | 1 (base, HPA scales to 5) |
| Port | 5004 |
| CPU Request / Limit | 500m / 2 |
| Memory Request / Limit | 1 Gi / 4 Gi |
| Service Type | ClusterIP |
| Note | Higher memory for spaCy NLP models |

```bash
kubectl rollout status deployment/compliance-service -n synthetic-erp-platform --timeout=180s
```

### 7.6 Provisioning Service

The Provisioning Service delivers generated data to target databases (PostgreSQL, Oracle, SQL Server, SAP HANA) via JDBC and to cloud storage (AWS S3, Azure Blob, GCP Cloud Storage) via cloud SDKs.

```bash
kubectl apply -f infrastructure/kubernetes/provisioning-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/provisioning-service/service.yaml
```

| Parameter | Value |
|-----------|-------|
| Replicas | 1 (base, HPA scales to 8) |
| Port | 5005 |
| CPU Request / Limit | 500m / 2 |
| Memory Request / Limit | 512 Mi / 2 Gi |
| Service Type | ClusterIP |

```bash
kubectl rollout status deployment/provisioning-service -n synthetic-erp-platform --timeout=180s
```

### 7.7 Web Console

The Web Console serves the React 19.x SPA via nginx.

```bash
kubectl apply -f infrastructure/kubernetes/web-console/deployment.yaml
kubectl apply -f infrastructure/kubernetes/web-console/service.yaml
```

| Parameter | Value |
|-----------|-------|
| Replicas | 2 |
| Port | 80 |
| CPU Request / Limit | 100m / 500m |
| Memory Request / Limit | 128 Mi / 256 Mi |
| Service Type | ClusterIP |

```bash
kubectl rollout status deployment/web-console -n synthetic-erp-platform --timeout=120s
```

### 7.8 Verify All Deployments

```bash
kubectl get deployments -n synthetic-erp-platform
```

Expected output:

```
NAME                    READY   UP-TO-DATE   AVAILABLE   AGE
api-gateway             2/2     2            2           5m
generation-engine       2/2     2            2           4m
profiling-service       1/1     1            1           3m
quality-service         1/1     1            1           3m
compliance-service      1/1     1            1           2m
provisioning-service    1/1     1            1           2m
web-console             2/2     2            2           1m
```

---

## 8. Services — Networking

Services are created alongside their respective Deployments (see Section 7). This section summarizes the service topology.

### 8.1 Service Summary

| Service Name | Type | Port | Target | Purpose |
|-------------|------|------|--------|---------|
| `api-gateway` | LoadBalancer | 5000 | API Gateway pods | External REST API entry point |
| `generation-engine` | ClusterIP | 5001 | Generation Engine pods | Internal job processing |
| `profiling-service` | ClusterIP | 5002 | Profiling Service pods | Internal schema discovery |
| `quality-service` | ClusterIP | 5003 | Quality Service pods | Internal quality validation |
| `compliance-service` | ClusterIP | 5004 | Compliance Service pods | Internal compliance checks |
| `provisioning-service` | ClusterIP | 5005 | Provisioning Service pods | Internal data export |
| `web-console` | ClusterIP | 80 | Web Console pods | Frontend SPA serving |
| `mongodb` | Headless (None) | 27017 | MongoDB StatefulSet | Stable DNS for replica set |
| `mongodb-read` | ClusterIP | 27017 | MongoDB pods | General read access |
| `redis` | Headless (None) | 6379 | Redis StatefulSet | Stable DNS for StatefulSet |
| `redis-read` | ClusterIP | 6379 | Redis pods | General client access |

### 8.2 Verify Services

```bash
kubectl get services -n synthetic-erp-platform
```

### 8.3 Kubernetes DNS-Based Service Discovery

All inter-service communication uses Kubernetes DNS:

```
http://<service-name>.<namespace>.svc.cluster.local:<port>
```

Within the same namespace, the short form is used:

```
http://generation-engine:5001
http://profiling-service:5002
http://quality-service:5003
http://compliance-service:5004
http://provisioning-service:5005
```

These URLs are configured in each service's ConfigMap.

---

## 9. Ingress — TLS 1.3 and Routing

The NGINX Ingress Controller terminates TLS and routes external traffic to backend services based on URL paths.

### 9.1 Install NGINX Ingress Controller

If not already installed:

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update

helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.replicaCount=2 \
  --set controller.resources.requests.cpu=100m \
  --set controller.resources.requests.memory=128Mi \
  --set controller.resources.limits.cpu=500m \
  --set controller.resources.limits.memory=512Mi \
  --set controller.config.ssl-protocols="TLSv1.3" \
  --set controller.config.ssl-ciphers="TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_128_GCM_SHA256" \
  --set controller.config.use-forwarded-headers="true" \
  --set controller.config.compute-full-forwarded-for="true" \
  --set controller.config.proxy-body-size="100m" \
  --set controller.metrics.enabled=true \
  --set controller.metrics.serviceMonitor.enabled=true \
  --wait
```

### 9.2 Apply Ingress Resource

```bash
kubectl apply -f infrastructure/kubernetes/ingress.yaml
```

### 9.3 Ingress Configuration Details

The Ingress resource (`synthetic-erp-ingress`) defines:

#### TLS Configuration

- **TLS 1.3 enforcement** via `nginx.ingress.kubernetes.io/ssl-protocols: "TLSv1.3"`
- **Mandatory HTTPS redirect** via `nginx.ingress.kubernetes.io/force-ssl-redirect: "true"`
- **TLS secret** referencing `synthetic-erp-tls-secret` created in Section 4

#### Path-Based Routing

| Path | Path Type | Target Service | Port | Purpose |
|------|-----------|----------------|------|---------|
| `/api/v1/` | Prefix | `api-gateway` | 5000 | REST API traffic |
| `/health` | Exact | `api-gateway` | 5000 | Health check endpoint |
| `/` | Prefix | `web-console` | 80 | React SPA (frontend) |

#### CORS Configuration

| Annotation | Value |
|------------|-------|
| `enable-cors` | `true` |
| `cors-allow-methods` | `GET, POST, PUT, DELETE, OPTIONS` |
| `cors-allow-headers` | `Authorization, Content-Type, X-Tenant-ID` |

#### Rate Limiting (Ingress-Level)

| Annotation | Value |
|------------|-------|
| `rate-limit-connections` | `10` |
| `rate-limit-rps` | `20` |

> **Note:** Application-level rate limiting (60/300/1000 req/min by user tier) is handled by the API Gateway's Redis-backed rate limiter middleware. Ingress-level rate limiting provides an additional coarse-grained protection layer.

#### Proxy Timeouts

| Annotation | Value | Notes |
|------------|-------|-------|
| `proxy-body-size` | `100m` | Supports large dataset uploads |
| `proxy-read-timeout` | `300` | 5-minute timeout for long-running generation requests |
| `proxy-send-timeout` | `300` | 5-minute timeout for large response payloads |

### 9.4 Verify Ingress

```bash
kubectl get ingress -n synthetic-erp-platform
```

Expected output:

```
NAME                     CLASS   HOSTS                         ADDRESS          PORTS     AGE
synthetic-erp-ingress    nginx   synthetic-erp.example.com     <EXTERNAL-IP>    80, 443   30s
```

### 9.5 DNS Configuration

After the Ingress is created and an external IP is assigned, configure your DNS to point to the Ingress external IP:

```
synthetic-erp.example.com  →  <EXTERNAL-IP>  (A record)
```

Or for cloud load balancers with hostnames:

```
synthetic-erp.example.com  →  <LB-HOSTNAME>  (CNAME record)
```

### 9.6 Verify End-to-End HTTPS Access

```bash
# Test the API health endpoint
curl -k https://synthetic-erp.example.com/health

# Test the API Gateway
curl -k https://synthetic-erp.example.com/api/v1/health

# Test the Web Console (should return HTML)
curl -k https://synthetic-erp.example.com/
```

---

## 10. HPA — Horizontal Pod Autoscaler

The platform uses Kubernetes HPA (`autoscaling/v2`) to automatically scale services based on CPU utilization and custom metrics, targeting 1M+ records per minute throughput.

### 10.1 Prerequisites

Ensure the Kubernetes Metrics Server is installed (required for CPU/memory-based HPA):

```bash
kubectl get deployment metrics-server -n kube-system
```

If not installed:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

For custom metrics (e.g., queue depth), install the [Prometheus Adapter](https://github.com/kubernetes-sigs/prometheus-adapter):

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts

helm install prometheus-adapter prometheus-community/prometheus-adapter \
  --namespace monitoring \
  --set prometheus.url=http://prometheus-server.monitoring.svc \
  --set prometheus.port=9090 \
  --wait
```

### 10.2 Apply HPA Manifests

```bash
kubectl apply -f infrastructure/kubernetes/hpa.yaml
```

### 10.3 HPA Configuration Summary

| Service | Min Replicas | Max Replicas | CPU Target | Behavior |
|---------|-------------|--------------|------------|----------|
| API Gateway | 2 | 10 | 70% avg utilization | Scale-up: 60s stabilization; Scale-down: 300s |
| Generation Engine | 2 | 20 | 60% avg utilization | Scale-up: 30s (fast burst); Scale-down: 600s |
| Profiling Service | 1 | 5 | 70% avg utilization | Default stabilization |
| Quality Service | 1 | 8 | 65% avg utilization | Default stabilization |
| Compliance Service | 1 | 5 | 70% avg utilization | Default stabilization |
| Provisioning Service | 1 | 8 | 65% avg utilization | Default stabilization |

#### Generation Engine Scaling Strategy

The Generation Engine has the most aggressive scaling configuration to meet the 1M+ records/minute throughput target:

- **Min 2 / Max 20 replicas** — Largest scale range of any service
- **60% CPU target** — Lower threshold triggers earlier scaling
- **30-second scale-up stabilization** — Fast response to generation bursts
- **10-minute scale-down stabilization** — Prevents thrashing during variable workloads
- **Custom queue-depth metric** (when Prometheus Adapter is configured) — Scales based on pending job queue size rather than only CPU

### 10.4 Verify HPA

```bash
kubectl get hpa -n synthetic-erp-platform
```

Expected output:

```
NAME                    REFERENCE                          TARGETS         MINPODS   MAXPODS   REPLICAS   AGE
api-gateway             Deployment/api-gateway             15%/70%         2         10        2          60s
generation-engine       Deployment/generation-engine       10%/60%         2         20        2          60s
profiling-service       Deployment/profiling-service       8%/70%          1         5         1          60s
quality-service         Deployment/quality-service         5%/65%          1         8         1          60s
compliance-service      Deployment/compliance-service      5%/70%          1         5         1          60s
provisioning-service    Deployment/provisioning-service    8%/65%          1         8         1          60s
```

### 10.5 Throughput Capacity Estimation

With the Generation Engine scaled to maximum (20 replicas), each processing 10K records/batch:

| Scenario | Replicas | Batch Size | Batches/Min per Pod | Throughput |
|----------|----------|-----------|---------------------|------------|
| Baseline (idle) | 2 | 10,000 | 5 | ~100K records/min |
| Moderate load | 8 | 10,000 | 5 | ~400K records/min |
| High load | 15 | 10,000 | 8 | ~1.2M records/min |
| Maximum burst | 20 | 10,000 | 8 | ~1.6M records/min |

> **Note:** Actual throughput depends on record complexity, generation method (AI/ML is slower than statistical), ERP module, and cluster hardware. Statistical generation is the fastest method; GAN/VAE generation benefits from GPU acceleration.

---

## 11. NetworkPolicies — Zero-Trust Segmentation

NetworkPolicies enforce zero-trust network segmentation, ensuring each service can only communicate with explicitly allowed peers. This is critical for SOC 2 Type II compliance and multi-tenant isolation.

### 11.1 Prerequisites

Ensure your CNI plugin supports NetworkPolicies:

- **Calico** — Full NetworkPolicy support (recommended)
- **Cilium** — Full NetworkPolicy support with advanced features
- **AWS VPC CNI + Calico** — For EKS clusters
- **Azure CNI + Calico** — For AKS clusters

> **Warning:** The default kubenet CNI does **not** support NetworkPolicies. If your cluster uses kubenet, NetworkPolicy resources will be accepted but **not enforced**.

### 11.2 Apply NetworkPolicies

```bash
kubectl apply -f infrastructure/kubernetes/network-policies.yaml
```

### 11.3 Policy Summary

#### Default Deny

A default-deny-ingress policy is applied to all pods in the `synthetic-erp-platform` namespace:

```yaml
# All pods deny ingress by default
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-ingress
  namespace: synthetic-erp-platform
spec:
  podSelector: {}      # Applies to all pods
  policyTypes:
    - Ingress          # Deny all ingress unless explicitly allowed
```

#### Per-Service Allow Policies

| Policy | Pod Selector | Ingress From | Egress To |
|--------|-------------|-------------|-----------|
| `allow-api-gateway-ingress` | `app=api-gateway` | Ingress Controller (nginx namespace), Web Console | Generation Engine, Profiling, Quality, Compliance, Provisioning, MongoDB, Redis |
| `allow-generation-engine` | `app=generation-engine` | API Gateway | Quality Service, Compliance Service, Provisioning Service, MongoDB, Redis |
| `allow-profiling-service` | `app=profiling-service` | API Gateway | MongoDB (27017), External ERP systems (configurable CIDR) |
| `allow-quality-service` | `app=quality-service` | API Gateway, Generation Engine | MongoDB (27017) |
| `allow-compliance-service` | `app=compliance-service` | API Gateway, Generation Engine | MongoDB (27017) |
| `allow-provisioning-service` | `app=provisioning-service` | API Gateway, Generation Engine | MongoDB (27017), External databases (JDBC), Cloud storage (HTTPS 443) |
| `allow-mongodb` | `app=mongodb` | All backend services | — (port 27017 only) |
| `allow-redis` | `app=redis` | API Gateway, Generation Engine | — (port 6379 only) |

### 11.4 Network Flow Diagram

```
Internet
    │
    ▼
┌─────────────────┐
│ NGINX Ingress   │ ← Only entry point for external traffic
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐
│  API Gateway    │────▶│ Generation Engine │
│  (port 5000)    │     │  (port 5001)      │
└────────┬────────┘     └────────┬─────────┘
         │                       │
    ┌────┼────────────┬──────────┼──────────────┐
    │    │            │          │              │
    ▼    ▼            ▼          ▼              ▼
┌──────┐ ┌────────┐ ┌────────┐ ┌────────────┐ ┌────────────┐
│ Prof.│ │Quality │ │Complnc.│ │Provisioning│ │ Web Console│
│ 5002 │ │ 5003   │ │ 5004   │ │ 5005       │ │ 80         │
└──┬───┘ └──┬─────┘ └──┬─────┘ └──┬─────────┘ └────────────┘
   │        │          │          │
   ▼        ▼          ▼          ▼
┌──────────────────────────────────┐     ┌───────────┐
│         MongoDB (27017)          │     │Redis(6379)│
└──────────────────────────────────┘     └───────────┘
```

### 11.5 Verify NetworkPolicies

```bash
kubectl get networkpolicies -n synthetic-erp-platform
```

Test connectivity (from inside a pod):

```bash
# Should succeed: API Gateway → Generation Engine
kubectl exec -it deploy/api-gateway -n synthetic-erp-platform -- \
  wget -qO- --timeout=5 http://generation-engine:5001/health

# Should fail (timeout): Generation Engine → API Gateway (not allowed)
kubectl exec -it deploy/generation-engine -n synthetic-erp-platform -- \
  wget -qO- --timeout=5 http://api-gateway:5000/health || echo "Blocked by NetworkPolicy (expected)"
```

---

## 12. ResourceQuotas and LimitRanges

ResourceQuotas and LimitRanges enforce per-namespace resource boundaries, preventing any single tenant or runaway pod from consuming excessive cluster resources.

### 12.1 Apply ResourceQuotas

```bash
kubectl apply -f infrastructure/kubernetes/resource-quotas.yaml
```

### 12.2 Platform ResourceQuota

The platform namespace (`synthetic-erp-platform`) has the following resource quota:

| Resource | Limit |
|----------|-------|
| `requests.cpu` | 20 cores |
| `requests.memory` | 40 Gi |
| `limits.cpu` | 40 cores |
| `limits.memory` | 80 Gi |
| `pods` | 100 |
| `services` | 20 |
| `configmaps` | 30 |
| `secrets` | 30 |
| `persistentvolumeclaims` | 20 |
| `services.loadbalancers` | 3 |

### 12.3 Tenant ResourceQuota Template

For multi-tenant deployments, each tenant namespace receives a separate ResourceQuota:

| Resource | Per-Tenant Limit |
|----------|------------------|
| `requests.cpu` | 4 cores |
| `requests.memory` | 8 Gi |
| `limits.cpu` | 8 cores |
| `limits.memory` | 16 Gi |
| `pods` | 20 |
| `services` | 10 |
| `configmaps` | 10 |
| `secrets` | 10 |
| `persistentvolumeclaims` | 5 |

To create a tenant namespace with quota:

```bash
# Create tenant namespace
kubectl create namespace tenant-<tenant-id>

# Apply the tenant quota template
kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: tenant-resource-quota
  namespace: tenant-<tenant-id>
spec:
  hard:
    requests.cpu: "4"
    requests.memory: "8Gi"
    limits.cpu: "8"
    limits.memory: "16Gi"
    pods: "20"
    services: "10"
    configmaps: "10"
    secrets: "10"
    persistentvolumeclaims: "5"
EOF
```

### 12.4 LimitRange Defaults

The platform LimitRange sets default resource requests and limits for containers that do not explicitly specify them:

| Type | Default Request | Default Limit | Max | Min |
|------|----------------|---------------|-----|-----|
| Container CPU | 100m | 500m | 4 | 50m |
| Container Memory | 128 Mi | 512 Mi | 8 Gi | 64 Mi |
| Pod CPU | — | — | 8 | — |
| Pod Memory | — | — | 16 Gi | — |

### 12.5 Verify ResourceQuotas

```bash
kubectl describe resourcequota platform-resource-quota -n synthetic-erp-platform
```

Expected output includes `Used` vs. `Hard` columns showing current consumption against limits.

---

## 13. Monitoring Stack

The monitoring namespace hosts Prometheus (metrics collection) and Grafana (visualization) for platform observability.

### 13.1 Install Prometheus

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.retention=30d \
  --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=50Gi \
  --set grafana.enabled=true \
  --set grafana.adminPassword="<your-grafana-admin-password>" \
  --set grafana.persistence.enabled=true \
  --set grafana.persistence.size=10Gi \
  --set alertmanager.enabled=true \
  --wait
```

### 13.2 Verify Prometheus Targets

All backend services include Prometheus scrape annotations:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "<service-port>"
  prometheus.io/path: "/metrics"
```

Verify targets are discovered:

```bash
kubectl port-forward svc/prometheus-kube-prometheus-prometheus -n monitoring 9090:9090
```

Open `http://localhost:9090/targets` and verify all services appear as `UP`.

### 13.3 Install OpenTelemetry Collector

For distributed tracing support:

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts

helm install otel-collector open-telemetry/opentelemetry-collector \
  --namespace monitoring \
  --set mode=deployment \
  --set config.receivers.otlp.protocols.grpc.endpoint="0.0.0.0:4317" \
  --set config.receivers.otlp.protocols.http.endpoint="0.0.0.0:4318" \
  --set config.exporters.prometheus.endpoint="0.0.0.0:8889" \
  --wait
```

All backend services are configured to send traces to `http://otel-collector.monitoring:4317`.

### 13.4 Access Grafana Dashboard

```bash
kubectl port-forward svc/prometheus-grafana -n monitoring 3001:80
```

Open `http://localhost:3001` (default credentials: `admin` / `<your-grafana-admin-password>`).

### 13.5 Recommended Grafana Dashboards

Import the following community dashboards:

| Dashboard | ID | Purpose |
|-----------|-----|---------|
| Kubernetes Cluster Monitoring | 315 | Cluster-wide resource utilization |
| NGINX Ingress Controller | 9614 | Ingress traffic and latency |
| MongoDB Metrics | 2583 | MongoDB operations and connections |
| Redis Dashboard | 11835 | Redis memory, commands, connections |
| Flask Application | Custom | API Gateway request rates, latencies, errors |

---

## 14. Deployment Order and Verification

Execute the complete deployment in the following order to satisfy all dependencies:

### 14.1 Step-by-Step Deployment Order

```bash
# ─────────────────────────────────────────────────
# Step 1: Namespaces
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/namespace.yaml
kubectl get namespaces -l app.kubernetes.io/part-of=synthetic-erp-platform

# ─────────────────────────────────────────────────
# Step 2: Secrets (must exist before any service)
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/secrets.yaml
kubectl get secrets -n synthetic-erp-platform

# ─────────────────────────────────────────────────
# Step 3: ConfigMaps (must exist before Deployments)
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/api-gateway/configmap.yaml
kubectl apply -f infrastructure/kubernetes/generation-engine/configmap.yaml
kubectl apply -f infrastructure/kubernetes/profiling-service/configmap.yaml
kubectl apply -f infrastructure/kubernetes/quality-service/configmap.yaml
kubectl apply -f infrastructure/kubernetes/compliance-service/configmap.yaml
kubectl apply -f infrastructure/kubernetes/provisioning-service/configmap.yaml

# ─────────────────────────────────────────────────
# Step 4: Data Stores (MongoDB and Redis)
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/mongodb/service.yaml
kubectl apply -f infrastructure/kubernetes/mongodb/statefulset.yaml
kubectl rollout status statefulset/mongodb -n synthetic-erp-platform --timeout=300s

kubectl apply -f infrastructure/kubernetes/redis/service.yaml
kubectl apply -f infrastructure/kubernetes/redis/statefulset.yaml
kubectl rollout status statefulset/redis -n synthetic-erp-platform --timeout=120s

# ─────────────────────────────────────────────────
# Step 5: Initialize MongoDB Replica Set
# ─────────────────────────────────────────────────
# (See Section 6.1.4 for the rs.initiate() command)

# ─────────────────────────────────────────────────
# Step 6: Backend Services (order matters: API GW first)
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/api-gateway/deployment.yaml
kubectl apply -f infrastructure/kubernetes/api-gateway/service.yaml
kubectl rollout status deployment/api-gateway -n synthetic-erp-platform --timeout=180s

kubectl apply -f infrastructure/kubernetes/generation-engine/deployment.yaml
kubectl apply -f infrastructure/kubernetes/generation-engine/service.yaml
kubectl rollout status deployment/generation-engine -n synthetic-erp-platform --timeout=300s

kubectl apply -f infrastructure/kubernetes/profiling-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/profiling-service/service.yaml
kubectl rollout status deployment/profiling-service -n synthetic-erp-platform --timeout=180s

kubectl apply -f infrastructure/kubernetes/quality-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/quality-service/service.yaml
kubectl rollout status deployment/quality-service -n synthetic-erp-platform --timeout=180s

kubectl apply -f infrastructure/kubernetes/compliance-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/compliance-service/service.yaml
kubectl rollout status deployment/compliance-service -n synthetic-erp-platform --timeout=180s

kubectl apply -f infrastructure/kubernetes/provisioning-service/deployment.yaml
kubectl apply -f infrastructure/kubernetes/provisioning-service/service.yaml
kubectl rollout status deployment/provisioning-service -n synthetic-erp-platform --timeout=180s

# ─────────────────────────────────────────────────
# Step 7: Web Console
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/web-console/deployment.yaml
kubectl apply -f infrastructure/kubernetes/web-console/service.yaml
kubectl rollout status deployment/web-console -n synthetic-erp-platform --timeout=120s

# ─────────────────────────────────────────────────
# Step 8: Ingress (TLS termination and routing)
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/ingress.yaml
kubectl get ingress -n synthetic-erp-platform

# ─────────────────────────────────────────────────
# Step 9: HPA (auto-scaling)
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/hpa.yaml
kubectl get hpa -n synthetic-erp-platform

# ─────────────────────────────────────────────────
# Step 10: NetworkPolicies (zero-trust isolation)
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/network-policies.yaml
kubectl get networkpolicies -n synthetic-erp-platform

# ─────────────────────────────────────────────────
# Step 11: ResourceQuotas
# ─────────────────────────────────────────────────
kubectl apply -f infrastructure/kubernetes/resource-quotas.yaml
kubectl describe resourcequota platform-resource-quota -n synthetic-erp-platform
```

### 14.2 Full Deployment Verification

Run the following checks to verify the complete deployment:

```bash
echo "=== Namespaces ==="
kubectl get namespaces -l app.kubernetes.io/part-of=synthetic-erp-platform

echo ""
echo "=== Pods ==="
kubectl get pods -n synthetic-erp-platform -o wide

echo ""
echo "=== Deployments ==="
kubectl get deployments -n synthetic-erp-platform

echo ""
echo "=== StatefulSets ==="
kubectl get statefulsets -n synthetic-erp-platform

echo ""
echo "=== Services ==="
kubectl get services -n synthetic-erp-platform

echo ""
echo "=== Ingress ==="
kubectl get ingress -n synthetic-erp-platform

echo ""
echo "=== HPA ==="
kubectl get hpa -n synthetic-erp-platform

echo ""
echo "=== NetworkPolicies ==="
kubectl get networkpolicies -n synthetic-erp-platform

echo ""
echo "=== ResourceQuotas ==="
kubectl get resourcequotas -n synthetic-erp-platform

echo ""
echo "=== Secrets ==="
kubectl get secrets -n synthetic-erp-platform

echo ""
echo "=== ConfigMaps ==="
kubectl get configmaps -n synthetic-erp-platform

echo ""
echo "=== PVCs ==="
kubectl get pvc -n synthetic-erp-platform
```

### 14.3 Health Check Verification

```bash
# API Gateway health
kubectl exec -it deploy/api-gateway -n synthetic-erp-platform -- \
  wget -qO- http://localhost:5000/health
# Expected: {"status": "healthy", ...}

# API Gateway readiness
kubectl exec -it deploy/api-gateway -n synthetic-erp-platform -- \
  wget -qO- http://localhost:5000/ready
# Expected: {"status": "ready", "dependencies": {...}}

# Generation Engine health
kubectl exec -it deploy/generation-engine -n synthetic-erp-platform -- \
  wget -qO- http://localhost:5001/health

# MongoDB connectivity
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- \
  mongosh --eval 'db.adminCommand("ping")'

# Redis connectivity
kubectl exec -it redis-0 -n synthetic-erp-platform -- \
  redis-cli -a '<password>' ping
```

---

## 15. Scaling and Operations

### 15.1 Manual Scaling

Scale a specific deployment manually:

```bash
# Scale API Gateway to 4 replicas
kubectl scale deployment/api-gateway -n synthetic-erp-platform --replicas=4

# Scale Generation Engine for a large batch job
kubectl scale deployment/generation-engine -n synthetic-erp-platform --replicas=10

# Reset to HPA-managed scaling
kubectl scale deployment/generation-engine -n synthetic-erp-platform --replicas=2
```

> **Note:** Manual scaling overrides HPA temporarily. The HPA will adjust replicas back toward its target once the manual change is made, based on current metrics.

### 15.2 Rolling Updates

Deploy a new version of a service with zero downtime:

```bash
# Update the API Gateway image
kubectl set image deployment/api-gateway \
  api-gateway=synthetic-erp-platform/api-gateway:v1.1.0 \
  -n synthetic-erp-platform

# Monitor the rollout
kubectl rollout status deployment/api-gateway -n synthetic-erp-platform

# Verify the new version
kubectl get pods -l app=api-gateway -n synthetic-erp-platform -o jsonpath='{.items[*].spec.containers[*].image}'
```

The rolling update strategy (`maxSurge: 1`, `maxUnavailable: 0`) ensures:

1. A new pod with the updated image is created
2. The new pod passes readiness checks
3. Traffic is routed to the new pod
4. An old pod is terminated
5. Repeat until all pods are updated

### 15.3 Rollback

If a deployment fails or introduces errors:

```bash
# View rollout history
kubectl rollout history deployment/api-gateway -n synthetic-erp-platform

# Rollback to the previous revision
kubectl rollout undo deployment/api-gateway -n synthetic-erp-platform

# Rollback to a specific revision
kubectl rollout undo deployment/api-gateway -n synthetic-erp-platform --to-revision=2

# Monitor the rollback
kubectl rollout status deployment/api-gateway -n synthetic-erp-platform
```

### 15.4 Database Backup and Restore

#### MongoDB Backup

```bash
# Create a backup using mongodump
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- \
  mongodump --uri="mongodb://admin:<password>@localhost:27017/synthetic_erp_platform?authSource=admin" \
  --out=/tmp/backup-$(date +%Y%m%d)

# Copy the backup to local machine
kubectl cp synthetic-erp-platform/mongodb-0:/tmp/backup-$(date +%Y%m%d) ./mongodb-backup-$(date +%Y%m%d)
```

#### MongoDB Restore

```bash
# Copy backup to pod
kubectl cp ./mongodb-backup-20250101 synthetic-erp-platform/mongodb-0:/tmp/restore

# Restore from backup
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- \
  mongorestore --uri="mongodb://admin:<password>@localhost:27017/?authSource=admin" \
  --drop /tmp/restore
```

### 15.5 Log Access

```bash
# View logs for a specific service
kubectl logs -f deploy/api-gateway -n synthetic-erp-platform --tail=100

# View logs for all pods of a service
kubectl logs -l app=generation-engine -n synthetic-erp-platform --tail=50

# View previous container logs (after a crash)
kubectl logs deploy/api-gateway -n synthetic-erp-platform --previous

# Stream logs with timestamps
kubectl logs -f deploy/api-gateway -n synthetic-erp-platform --timestamps
```

### 15.6 Resource Monitoring

```bash
# Node resource usage
kubectl top nodes

# Pod resource usage in the platform namespace
kubectl top pods -n synthetic-erp-platform --sort-by=cpu

# Container-level resource usage
kubectl top pods -n synthetic-erp-platform --containers
```

---

## 16. Troubleshooting

### 16.1 Pod Not Starting

**Symptoms:** Pod stuck in `Pending`, `CrashLoopBackOff`, or `ImagePullBackOff`.

```bash
# Check pod events
kubectl describe pod <pod-name> -n synthetic-erp-platform

# Check pod logs
kubectl logs <pod-name> -n synthetic-erp-platform

# Check previous container logs (CrashLoopBackOff)
kubectl logs <pod-name> -n synthetic-erp-platform --previous
```

**Common Causes and Fixes:**

| Issue | Cause | Fix |
|-------|-------|-----|
| `ImagePullBackOff` | Image not found in registry or incorrect tag | Verify image exists: `docker pull <image>:<tag>`. Check imagePullSecrets if using private registry. |
| `Pending` (no events) | Insufficient cluster resources | Check `kubectl describe pod` for scheduling failures. Scale cluster nodes or reduce resource requests. |
| `Pending` (PVC) | StorageClass not available | Verify `kubectl get sc` shows the required StorageClass. Ensure PV provisioner is installed. |
| `CrashLoopBackOff` | Application error on startup | Check logs with `kubectl logs --previous`. Common causes: missing env vars, incorrect MongoDB/Redis connection strings, invalid secrets. |
| `CreateContainerConfigError` | Missing ConfigMap or Secret | Verify all ConfigMaps and Secrets exist: `kubectl get cm,secret -n synthetic-erp-platform`. |

### 16.2 Service Connectivity Issues

**Symptoms:** Services cannot communicate with each other; API calls between services fail.

```bash
# Test DNS resolution from inside a pod
kubectl exec -it deploy/api-gateway -n synthetic-erp-platform -- nslookup generation-engine

# Test HTTP connectivity
kubectl exec -it deploy/api-gateway -n synthetic-erp-platform -- wget -qO- --timeout=5 http://generation-engine:5001/health

# Check NetworkPolicies
kubectl get networkpolicies -n synthetic-erp-platform -o yaml
```

**Common Causes and Fixes:**

| Issue | Cause | Fix |
|-------|-------|-----|
| DNS resolution fails | CoreDNS not running or service not created | Check `kubectl get pods -n kube-system -l k8s-app=kube-dns`. Verify Service exists. |
| Connection refused | Target pod not ready or wrong port | Check `kubectl get endpoints <service-name>`. Verify pod readiness. |
| Connection timeout | NetworkPolicy blocking traffic | Review NetworkPolicies. Ensure the correct labels match between policy selectors and pod labels. |
| `503 Service Unavailable` | No healthy backends | Check `kubectl get endpoints` for empty endpoint lists. Verify readiness probes pass. |

### 16.3 MongoDB Issues

```bash
# Check replica set status
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- mongosh --eval 'rs.status()'

# Check if authentication works
kubectl exec -it mongodb-0 -n synthetic-erp-platform -- mongosh -u admin -p '<password>' --authenticationDatabase admin --eval 'db.serverStatus()'

# Check PVC status
kubectl get pvc -l app=mongodb -n synthetic-erp-platform
```

**Common Causes and Fixes:**

| Issue | Cause | Fix |
|-------|-------|-----|
| `MongoServerError: not primary` | Connecting to a secondary node | Use the full replica set connection string with all three hostnames. |
| Authentication failed | Incorrect credentials in Secret | Verify the `mongodb-credentials` Secret matches the MongoDB init credentials. |
| PVC stuck in Pending | StorageClass provisioner not working | Check `kubectl describe pvc <pvc-name>`. Verify StorageClass and provisioner. |
| Replica set not initialized | `rs.initiate()` not run | Run the replica set initialization command from Section 6.1.4. |

### 16.4 Redis Issues

```bash
# Test Redis connectivity and auth
kubectl exec -it redis-0 -n synthetic-erp-platform -- redis-cli -a '<password>' info server

# Check memory usage
kubectl exec -it redis-0 -n synthetic-erp-platform -- redis-cli -a '<password>' info memory

# Check connected clients
kubectl exec -it redis-0 -n synthetic-erp-platform -- redis-cli -a '<password>' info clients
```

**Common Causes and Fixes:**

| Issue | Cause | Fix |
|-------|-------|-----|
| `NOAUTH Authentication required` | Missing or wrong Redis password | Verify `redis-credentials` Secret. Check service ConfigMap `REDIS_URL` format. |
| `OOM command not allowed` | Redis max memory exceeded | Increase `--maxmemory` in StatefulSet args or scale Redis. `allkeys-lru` eviction should handle gracefully. |
| Connection refused | Redis pod not ready | Check `kubectl get pods -l app=redis`. Verify readiness probe passes. |

### 16.5 Ingress Issues

```bash
# Check Ingress status and address
kubectl describe ingress synthetic-erp-ingress -n synthetic-erp-platform

# Check NGINX Ingress Controller logs
kubectl logs -l app.kubernetes.io/name=ingress-nginx -n ingress-nginx --tail=100

# Verify TLS certificate
kubectl get secret synthetic-erp-tls-secret -n synthetic-erp-platform -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -text -noout | head -20
```

**Common Causes and Fixes:**

| Issue | Cause | Fix |
|-------|-------|-----|
| No external IP assigned | Cloud LB provisioning delay or quota | Wait 2-5 minutes. Check cloud provider LB quotas and IAM permissions. |
| `404 Not Found` | Incorrect path or service name in Ingress | Verify Ingress rules match service names and ports. Check `kubectl describe ingress`. |
| `502 Bad Gateway` | Backend pods not ready | Check backend pod readiness. Verify the target service has healthy endpoints. |
| TLS certificate error | Wrong certificate or missing Secret | Verify the TLS secret exists and the certificate matches the hostname. |
| `SSL: TLSV1_ALERT_PROTOCOL_VERSION` | Client doesn't support TLS 1.3 | Ensure clients support TLS 1.3. For testing, use `curl --tls-max 1.3`. |

### 16.6 HPA Not Scaling

```bash
# Check HPA status
kubectl describe hpa <service-name> -n synthetic-erp-platform

# Check metrics server
kubectl get --raw "/apis/metrics.k8s.io/v1beta1/namespaces/synthetic-erp-platform/pods" | jq '.items[].containers[].usage'

# Check for conditions
kubectl get hpa -n synthetic-erp-platform -o yaml | grep -A5 conditions
```

**Common Causes and Fixes:**

| Issue | Cause | Fix |
|-------|-------|-----|
| `<unknown>/70%` target | Metrics server not installed or not reporting | Install metrics server: `kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml` |
| Not scaling up | CPU below threshold | Verify actual CPU usage with `kubectl top pods`. Lower the target percentage if needed. |
| Not scaling down | Stabilization window active | Wait for the stabilization period to expire (300-600s depending on service). |
| Max replicas reached | All replicas in use | Increase `maxReplicas` in HPA or add cluster nodes for more capacity. |

### 16.7 ResourceQuota Violations

```bash
# Check quota usage
kubectl describe resourcequota platform-resource-quota -n synthetic-erp-platform

# Check for quota-related events
kubectl get events -n synthetic-erp-platform --field-selector reason=FailedCreate
```

**If pods fail to schedule due to quota:**

1. Check current resource usage against quota limits
2. Scale down non-critical services
3. Increase quota limits if cluster capacity allows
4. Review pod resource requests to optimize allocation

---

## Appendix A: Quick Reference Commands

```bash
# Full status overview
kubectl get all -n synthetic-erp-platform

# Watch pods in real-time
kubectl get pods -n synthetic-erp-platform -w

# Get events sorted by time
kubectl get events -n synthetic-erp-platform --sort-by='.lastTimestamp'

# Port-forward to a specific service for debugging
kubectl port-forward svc/api-gateway -n synthetic-erp-platform 5000:5000
kubectl port-forward svc/web-console -n synthetic-erp-platform 3000:80

# Enter a pod shell for debugging
kubectl exec -it deploy/api-gateway -n synthetic-erp-platform -- /bin/sh

# Delete and recreate a deployment
kubectl delete -f infrastructure/kubernetes/api-gateway/deployment.yaml
kubectl apply -f infrastructure/kubernetes/api-gateway/deployment.yaml

# Force restart all pods of a deployment
kubectl rollout restart deployment/api-gateway -n synthetic-erp-platform

# Drain a node for maintenance
kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data

# Uncordon a node after maintenance
kubectl uncordon <node-name>
```

## Appendix B: Environment-Specific Configurations

### B.1 AWS EKS

```bash
# Use gp3 StorageClass for persistent volumes
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  fsType: ext4
  encrypted: "true"
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain
EOF

# Install AWS Load Balancer Controller (alternative to NGINX Ingress)
helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system \
  --set clusterName=<your-cluster-name> \
  --set serviceAccount.create=true
```

### B.2 Azure AKS

```bash
# Use managed-premium StorageClass
kubectl get sc managed-premium  # Should exist by default on AKS

# Install Azure Application Gateway Ingress Controller (alternative)
helm install ingress-azure application-gateway-kubernetes-ingress/ingress-azure \
  --namespace kube-system \
  --set appgw.name=<your-app-gw-name> \
  --set appgw.resourceGroup=<your-rg>
```

### B.3 GCP GKE

```bash
# Use pd-ssd StorageClass for better IOPS
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: pd-ssd
provisioner: pd.csi.storage.gke.io
parameters:
  type: pd-ssd
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain
EOF
```

## Appendix C: Security Hardening Checklist

- [ ] Pod Security Standards set to `restricted` on the `synthetic-erp-platform` namespace
- [ ] All containers run as non-root (`runAsNonRoot: true`)
- [ ] All containers drop all capabilities (`capabilities: drop: ["ALL"]`)
- [ ] All containers use read-only root filesystem where possible
- [ ] All Secrets encrypted with Sealed Secrets controller
- [ ] TLS 1.3 enforced on Ingress
- [ ] Default-deny NetworkPolicy applied
- [ ] Per-service NetworkPolicies restrict traffic to only allowed peers
- [ ] ResourceQuotas prevent resource exhaustion
- [ ] LimitRanges enforce minimum and maximum resource boundaries
- [ ] RBAC ServiceAccounts configured per service (least privilege)
- [ ] Image pull policies set to `Always` for production
- [ ] Audit logging enabled on the Kubernetes API server
- [ ] AES-256 encryption configured for data at rest (MongoDB, cloud storage)
- [ ] Secret rotation procedures documented and scheduled
- [ ] Container images scanned for vulnerabilities (Trivy) before deployment
