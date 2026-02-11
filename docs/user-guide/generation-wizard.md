# Generation Wizard User Guide

The **Generation Wizard** is the primary interface for creating synthetic data generation jobs on the Synthetic ERP Data Generation Platform. It guides you through a streamlined four-step workflow to configure, validate, and submit generation requests that produce high-fidelity synthetic data mirroring your ERP system schemas.

**Wizard Workflow:**

```
Step 1              Step 2                Step 3                 Step 4
Method Selection → Schema & Table    → Parameter            → Review & Submit
                   Selection           Configuration
```

Each step must be completed before advancing to the next. You may navigate back to any previous step at any time to revise your selections without losing progress.

---

## Table of Contents

- [Prerequisites and Access](#prerequisites-and-access)
- [Step 1: Select Generation Method](#step-1-select-generation-method)
- [Step 2: Schema and Table Selection](#step-2-schema-and-table-selection)
- [Step 3: Generation Parameters](#step-3-generation-parameters)
- [Step 4: Review and Submit](#step-4-review-and-submit)
- [Understanding the Generation Pipeline](#understanding-the-generation-pipeline)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)
- [Role-Based Access Notes](#role-based-access-notes)
- [Related Features](#related-features)

---

## Prerequisites and Access

Before using the Generation Wizard, ensure the following prerequisites are met:

### Authentication

You must be authenticated via **Auth0 Single Sign-On (SSO)**. If you have not yet logged in, you will be redirected to the Auth0 login page when attempting to access the wizard.

### Required Permission

Access to the Generation Wizard requires the **`generation:create`** permission. This permission is included by default for the following roles:

| Role | Wizard Access |
|------|---------------|
| Platform Admin | ✅ Full access |
| Data Engineer | ✅ Full access |
| Developer | ✅ Access granted |
| QA Engineer | ✅ Access granted |
| Data Analyst | ✅ Access granted |

> **Note:** If you receive a "Permission denied" error when opening the wizard, contact your Platform Administrator to request the `generation:create` permission for your account.

### Schema Availability

At least one ERP schema must be discovered and profiled before you can create a generation job. Schemas are discovered via the [Schema Browser (Screen S-005)](../user-guide/generation-wizard.md#related-features). If no schemas are available, the wizard will display an empty state in Step 2.

### Accessing the Wizard

1. Log in to the Web Console via Auth0 SSO.
2. Navigate to the **Dashboard** (Screen S-001).
3. Click the **"New Generation Job"** button, or select **"Generation Wizard"** from the sidebar navigation.

---

## Step 1: Select Generation Method

![Step 1: Method Selection](../assets/screenshots/wizard-step1-method.png)

The first step presents four generation methods displayed as **interactive cards**. Each card includes a description, capability list, and use-case tags to help you select the most appropriate method for your requirements.

### AI/ML Generation

**Best for:** Large-scale generation, complex multi-table schemas, high-fidelity statistical matching.

AI/ML Generation leverages deep learning models — specifically **Generative Adversarial Networks (GANs)** and **Variational Autoencoders (VAEs)** — to learn and replicate the statistical distributions found in your profiled ERP data. The models train on the metadata and statistical profiles captured by the Profiling Service (never on raw production data) to produce synthetic records that are statistically indistinguishable from the source.

**Capabilities:**

- **GAN-based adversarial generation** — A Generator-Discriminator pair produces records that match source distributions through adversarial training.
- **VAE latent space synthesis** — An Encoder-Decoder architecture learns a continuous latent representation of tabular data for smooth synthetic sampling.
- **Statistical distribution matching** — Generated columns match the mean, variance, skewness, kurtosis, and distributional shape of profiled source columns.
- **Complex correlation preservation** — Multi-column and cross-table correlations are captured and reproduced in synthetic output.

> **When to use:** Choose AI/ML Generation when you need the highest statistical fidelity, when generating data for complex schemas with many inter-column dependencies, or when producing large volumes (100K+ records) where model-based generation provides the best throughput-to-quality ratio.

---

### Rules-Based Generation

**Best for:** Regulated data with strict formats, domain-specific business logic, deterministic test data.

Rules-Based Generation uses a configurable **business rules engine** that enforces domain-specific constraints, value formats, valid ranges, and cross-field dependencies. Rules are defined declaratively and can encode complex business logic such as "invoice totals must equal the sum of line items" or "employee hire dates must precede termination dates."

**Capabilities:**

- **Custom business rule definitions** — Define rules in a declarative format specifying constraints, validations, and value generation logic.
- **Format and range constraints** — Enforce strict formatting (e.g., SAP document number formats, Oracle EBS flex-field structures) and value ranges.
- **Cross-field dependency rules** — Express dependencies between columns within and across tables (e.g., order total = quantity × unit price).
- **Domain-specific value generators** — Built-in generators for common ERP field types: account codes, cost centers, material numbers, employee IDs.

> **When to use:** Choose Rules-Based Generation when your data must conform to strict business rules, regulatory formats, or deterministic test scenarios where reproducibility is essential.

---

### Statistical Synthesis

**Best for:** Analytics datasets, statistical testing, performance benchmarking.

Statistical Synthesis fits **statistical distributions** (normal, log-normal, Poisson, categorical, and others) to each column's profiled statistics and uses **copula-based multivariate correlation modeling** to preserve relationships between columns. This method produces data that is statistically representative without requiring deep learning model training.

**Capabilities:**

- **Distribution fitting** — Automatically fits the best-matching distribution (normal, log-normal, Poisson, uniform, categorical) to each column's profile.
- **Copula-based correlation modeling** — Preserves multivariate dependencies between columns using Gaussian or empirical copulas.
- **Column-level statistical fidelity** — Each generated column matches the source column's mean, variance, percentiles, and distribution shape.
- **Parametric and non-parametric synthesis** — Supports both parametric (distribution-based) and non-parametric (empirical resampling) generation modes.

> **When to use:** Choose Statistical Synthesis for quick generation of analytics-grade datasets, for performance testing where statistical accuracy matters more than business rule compliance, or when you need moderate fidelity with fast generation times.

---

### Intelligent Masking

**Best for:** Privacy-compliant data copies, dev/test environments, regulatory compliance datasets.

Intelligent Masking transforms existing data patterns using **privacy-preserving masking techniques** that maintain the structural and relational characteristics of the source data while eliminating all personally identifiable information (PII). This method guarantees zero PII leakage in the output.

**Capabilities:**

- **Format-preserving encryption** — Masked values retain the same format, length, and character-class composition as original values (e.g., a 10-digit phone number remains a 10-digit number).
- **Consistent cross-table masking** — The same source value always maps to the same masked value across all tables, preserving JOIN relationships and referential integrity.
- **PII elimination guarantee** — Every value identified as PII by the Compliance Service is replaced with a synthetic equivalent. Zero PII leakage is enforced.
- **Referential integrity preservation** — Foreign key relationships are maintained across all masked tables automatically.

> **When to use:** Choose Intelligent Masking when you need a privacy-safe copy of production-like data for development or testing environments, or when regulatory requirements (GDPR, HIPAA, CCPA) mandate that all PII be removed from non-production datasets.

---

### Method Comparison Table

| Criteria | AI/ML Generation | Rules-Based | Statistical Synthesis | Intelligent Masking |
|----------|-----------------|-------------|----------------------|---------------------|
| **Statistical Fidelity** | Highest | Medium | High | Medium |
| **Business Rule Compliance** | Medium | Highest | Low | Medium |
| **Generation Speed** | Moderate | Fast | Fastest | Fast |
| **Setup Complexity** | Low (auto-learns) | High (rule authoring) | Low (auto-fits) | Low (auto-detects) |
| **Best Record Volume** | 100K+ | Any | 10K–1M | Any |
| **PII Safety** | High | High | High | Highest |
| **Reproducibility** | Low (stochastic) | Highest (deterministic) | Medium | High |
| **Use Case** | Production-grade synthetic data | Regulated/formatted data | Analytics & benchmarking | Privacy-compliant copies |

Select a method card to continue to Step 2. The selected card is highlighted with a border accent. You can return to this step at any time to change your selection.

---

## Step 2: Schema and Table Selection

![Step 2: Schema Selection](../assets/screenshots/wizard-step2-schema.png)

In this step, you select the ERP schema and individual tables for which synthetic data will be generated.

### Filtering Schemas

Use the filter controls at the top of the schema list to narrow the available schemas:

#### Filter by ERP Type

Select one or more ERP system types to filter the displayed schemas:

| ERP Type | Description |
|----------|-------------|
| **SAP** | SAP ERP schemas discovered via RFC/BAPI connectors |
| **Oracle EBS** | Oracle E-Business Suite schemas discovered via OData/JDBC |
| **Microsoft Dynamics** | Microsoft Dynamics 365 schemas discovered via Web API/OData |
| **Legacy Systems** | Legacy ERP system schemas discovered via generic JDBC connectors |

#### Filter by ERP Module

Filter schemas by the functional ERP module they belong to. The initial release supports four modules per Constraint C-005:

| Module | Description | Example Tables |
|--------|-------------|----------------|
| **Financial Accounting** | General ledger, invoices, payments, accounts receivable/payable | GL Entries, AR Invoices, AP Payments, Chart of Accounts |
| **Human Resources** | Employee records, payroll, benefits, organizational structure | Employees, Payroll Records, Benefits Enrollment, Positions |
| **Sales & Distribution** | Sales orders, customers, pricing, shipping | Sales Orders, Customer Master, Pricing Conditions, Deliveries |
| **Material Management** | Inventory, purchase orders, vendors, goods movements | Purchase Orders, Vendor Master, Material Master, Goods Receipts |

#### Search Schemas

Use the **search bar** to find schemas by schema ID or ERP type. The search filters the displayed schema cards in real time.

### Selecting a Schema

Each schema is displayed as a card showing:

- **Schema Name** — The identified schema name from the ERP system.
- **ERP Type** — The source ERP system (SAP, Oracle EBS, Dynamics, Legacy).
- **ERP Module** — The functional module (Financial Accounting, HR, Sales & Distribution, Material Management).
- **Total Tables** — Number of tables discovered in this schema.
- **Relationships** — Number of foreign key relationships identified.
- **Discovery Date** — When the schema was last profiled.

Click a schema card to select it. The selected card is highlighted, and the table selection panel appears below.

> **Note:** If no schemas appear in the list, you need to first discover schemas using the **Schema Browser (Screen S-005)**. Navigate to the Schema Browser, connect to your ERP system, and run schema discovery before returning to the Generation Wizard.

### Selecting Tables

After selecting a schema, a **table checklist** appears showing all tables within the selected schema.

**Table list controls:**

- **Select All** — Check all tables in the schema for generation.
- **Deselect All** — Uncheck all tables.
- **Individual selection** — Check or uncheck each table independently.

Each table entry displays:

| Field | Description |
|-------|-------------|
| **Table Name** | The table name from the ERP schema |
| **Column Count** | Number of columns in the table |
| **Estimated Row Count** | Estimated record count from the profiled source |
| **Checkbox** | Select/deselect this table for generation |

### Configuring Per-Table Record Counts

When a table is checked, a **record count input field** appears next to it. Configure how many synthetic records to generate for each selected table:

- **Default:** 1,000 records per table.
- **Minimum:** 1 record.
- **Configurable:** Enter any positive integer based on your requirements.

> **Example:** For a Sales Orders table, you might generate 50,000 order records. For the associated Order Line Items table, you might generate 150,000 records (averaging 3 line items per order) to maintain realistic data proportions.

### Referential Integrity

When multiple tables with foreign key relationships are selected, the platform **automatically maintains referential integrity** during generation. This means:

- Foreign key values in child tables reference valid primary key values in parent tables.
- The table generation order is determined by the dependency graph — parent tables are generated before child tables.
- Cross-module relationships (e.g., a Sales Order referencing a Customer from the Customer Master) are preserved.

> **Important:** For best results, select all related tables within a relationship chain. If you select a child table without its parent, the Generation Engine will synthesize placeholder parent key values, which may reduce referential integrity scores.

---

## Step 3: Generation Parameters

![Step 3: Parameters](../assets/screenshots/wizard-step3-parameters.png)

Configure the generation parameters that control output format, batch processing, quality expectations, and optional template usage.

### Output Format

Select the desired output format for the generated synthetic data. Four formats are supported:

| Format | Description | Best For |
|--------|-------------|----------|
| **SQL** | Generates `INSERT` or `COPY` statements compatible with the target database dialect. | Direct database provisioning, migration scripts, database seeding |
| **CSV** | Comma-separated values with configurable delimiters. Includes header row by default. | Spreadsheet analysis, ETL pipelines, data exchange between systems |
| **JSON** | Structured JSON or JSONL (JSON Lines) format with nested object support. | API testing, NoSQL database seeding, application test fixtures |
| **Parquet** | Apache Parquet columnar storage format with Snappy compression. | Large-scale analytics, data lake ingestion, Spark/Databricks workflows |

> **Tip:** For large datasets exceeding 100,000 records, **Parquet** is recommended due to superior compression ratios and faster read performance compared to CSV or JSON. Use **SQL** when you intend to provision data directly into a target database.

### Batch Size

The **batch size** determines how many records the Generation Engine processes in each internal processing cycle.

| Parameter | Value |
|-----------|-------|
| **Range** | 1,000 – 100,000 records per batch |
| **Default** | 10,000 records per batch |

**Guidance on batch size selection:**

- **1,000 – 5,000:** Smaller batches use less memory and are suitable for development and testing environments or for schemas with very wide tables (many columns per row).
- **10,000 (default):** Balanced choice for most generation jobs. Provides good throughput with moderate memory consumption.
- **50,000 – 100,000:** Larger batches maximize throughput for high-volume generation jobs. Ensure your environment has sufficient memory allocated.

> **Note:** Batch size affects internal processing only. The final output file contains all records regardless of batch size. Progress is reported per batch in the Job Monitoring screen.

### Quality Threshold

Set the **minimum acceptable quality score** for the generated dataset using the quality threshold slider.

| Parameter | Value |
|-----------|-------|
| **Range** | 80% – 100% |
| **Default** | 95% |
| **Recommended Minimum** | 95% |

The quality score is a **weighted composite** of three validation dimensions:

| Dimension | Weight | Description |
|-----------|--------|-------------|
| **Statistical Fidelity** | 40% | How closely generated column distributions match profiled source distributions (mean, variance, percentiles, distribution shape) |
| **Business Rules Compliance** | 30% | Percentage of generated records that satisfy defined business rules (format constraints, value ranges, cross-field dependencies) |
| **Referential Integrity** | 30% | Percentage of foreign key references that resolve to valid parent records across all selected tables |

**Quality Score Formula:**

```
Quality Score = (0.40 × Statistical Fidelity) + (0.30 × Business Rules Compliance) + (0.30 × Referential Integrity)
```

> **⚠️ Warning:** Setting the quality threshold below **95%** is below the platform's recommended minimum for production-grade synthetic data. Data generated below this threshold may exhibit noticeable statistical deviations or referential integrity gaps. Use lower thresholds only for rapid prototyping or non-critical test scenarios.

If the generated data does not meet the configured threshold on the first pass, the Generation Engine will attempt re-generation of failing batches up to the configured retry limit before marking the job as requiring manual review.

### Template (Optional)

Optionally select a **pre-configured template** from the Template Library to auto-populate the generation parameters.

Templates are reusable configurations created by Data Engineers or Platform Admins that bundle a generation method, schema selection, table configuration, and parameter settings into a single selectable preset.

**To use a template:**

1. Click the **"Select Template"** dropdown.
2. Browse or search available templates. Each template shows its name, description, target ERP type, and creation date.
3. Select a template to auto-fill all wizard parameters.
4. Review and adjust any auto-filled values as needed.

> **Note:** Templates are managed in the **Template Library (Screen S-003)**. If no templates appear, ask your Data Engineer or Platform Admin to create templates for common generation scenarios. Templates do not lock parameters — you can always override auto-filled values.

---

## Step 4: Review and Submit

![Step 4: Review](../assets/screenshots/wizard-step4-review.png)

The final step displays a **read-only summary** of your complete generation job configuration. Review all settings carefully before submitting.

### Configuration Summary

The review screen is organized into three sections, each with an **Edit** button to jump back to the corresponding wizard step:

#### Generation Method

| Field | Example Value |
|-------|---------------|
| **Selected Method** | AI/ML Generation |
| **Description** | GAN/VAE-based statistical distribution learning |

**[Edit]** — Returns to Step 1.

#### Schema and Tables

| Field | Example Value |
|-------|---------------|
| **Schema** | SAP Financial Accounting — FI Module |
| **ERP Type** | SAP |
| **Tables Selected** | 5 of 12 tables |

**Selected Tables:**

| Table Name | Records to Generate |
|------------|-------------------|
| GL_JOURNAL_ENTRIES | 50,000 |
| AR_INVOICES | 25,000 |
| AP_PAYMENTS | 15,000 |
| CHART_OF_ACCOUNTS | 500 |
| COST_CENTERS | 200 |
| **Total** | **90,700** |

**[Edit]** — Returns to Step 2.

#### Parameters

| Field | Value |
|-------|-------|
| **Output Format** | Parquet |
| **Batch Size** | 10,000 records/batch |
| **Quality Threshold** | 95% |
| **Template** | (None) |

**[Edit]** — Returns to Step 3.

### Submitting the Job

After reviewing all settings, click the **"Submit Generation Job"** button.

**What happens on submit:**

1. The wizard assembles the complete job configuration.
2. A `POST /api/v1/generation/jobs` request is dispatched to the API Gateway.
3. The API Gateway creates a job record in the Metadata Repository (MongoDB) with a status of **Submitted**.
4. The Generation Engine is notified to begin processing.
5. You are automatically redirected to **Job Monitoring (Screen S-004)** where your newly submitted job is highlighted at the top of the job list.

> **Note:** After generation completes, the data automatically flows through the **compliance pipeline** before being released:
>
> 1. **Quality Service** validates the generated data against the configured quality threshold.
> 2. **Compliance Service** scans the output for PII using both NLP entity recognition (spaCy) and regex pattern matching — enforcing the **zero PII leakage guarantee** in compliance with GDPR, HIPAA, and CCPA regulations.
> 3. Only after passing both quality validation and compliance verification is the data exported to the selected destination by the **Provisioning Service**.

---

## Understanding the Generation Pipeline

Once you submit a generation job, it progresses through a multi-stage pipeline. Understanding this pipeline helps you monitor progress and interpret job statuses.

### Pipeline Stages

```
Submitted → Generating → Validating → Certifying → Provisioning → Completed
```

| Stage | Status | Description |
|-------|--------|-------------|
| **Submitted** | `SUBMITTED` | Job configuration saved to MongoDB. Queued for processing by the Generation Engine. |
| **Generating** | `GENERATING` | The Generation Engine is actively producing synthetic records in batches. Progress is tracked in real time and reported per batch via Redis. |
| **Validating** | `VALIDATING` | The Quality Service validates the generated dataset against the quality threshold (≥95% target). Checks statistical fidelity (40%), business rules (30%), and referential integrity (30%). |
| **Certifying** | `CERTIFYING` | The Compliance Service scans all generated data for PII. Performs NLP entity recognition and regex pattern matching. Issues a compliance certificate if zero PII is detected. |
| **Provisioning** | `PROVISIONING` | The Provisioning Service exports the validated and certified data to the selected output format and destination (file download, database, or cloud storage). |
| **Completed** | `COMPLETED` | All stages passed. Synthetic data is available for download or has been provisioned to the target system. Quality report and compliance certificate are available. |

### Failure Handling

If any stage fails, the job transitions to a corresponding error state:

| Error Status | Cause | Resolution |
|-------------|-------|------------|
| `GENERATION_FAILED` | The Generation Engine encountered an unrecoverable error during batch processing. | Review error logs in Job Monitoring. Adjust parameters or schema selection and retry. |
| `VALIDATION_FAILED` | Generated data did not meet the configured quality threshold after retry attempts. | Review the Quality Report (Screen S-007) for specific dimensions that failed. Consider adjusting the quality threshold, using a different generation method, or re-profiling the source schema. |
| `COMPLIANCE_FAILED` | PII was detected in the generated output. | Review findings in the Compliance Dashboard (Screen S-008). The flagged data is quarantined — no PII-containing output is ever released. Adjust generation method or masking parameters and retry. |
| `PROVISIONING_FAILED` | Export to the target destination failed (connection timeout, permission error, storage quota). | Verify target system connectivity and credentials. Check available storage. Retry provisioning from Job Monitoring. |

### Real-Time Progress Tracking

During the **Generating** stage, progress is tracked at the batch level and reported in real time through the Job Monitoring screen:

- **Batches Completed** — Number of completed batches out of total (e.g., "15 / 50 batches").
- **Records Generated** — Running count of records produced so far.
- **Estimated Time Remaining** — Based on average batch processing time.
- **Progress Bar** — Visual percentage indicator.

Progress data is pushed via WebSocket and cached in Redis for low-latency retrieval.

---

## Best Practices

Follow these recommendations to achieve the best results from the Generation Wizard:

### Start Small, Then Scale

Begin with smaller record counts (1,000–5,000 per table) for initial testing. Review the generated output and quality reports before scaling to full-volume production runs. This approach saves time and helps identify configuration issues early.

### Match Method to Use Case

- Use **Statistical Synthesis** for quick turnaround on analytics datasets where speed matters more than business rule compliance.
- Use **AI/ML Generation** when you need the highest statistical fidelity, especially for complex schemas with many inter-column dependencies.
- Use **Rules-Based Generation** when data must conform to strict formatting and business rules (e.g., regulatory test data).
- Use **Intelligent Masking** when you need a privacy-safe copy of production-like data with guaranteed PII elimination.

### Maintain High Quality Thresholds

Set the quality threshold to **95% or higher** for production-grade synthetic data. Lower thresholds should only be used for rapid prototyping or non-critical test scenarios. The 95% threshold ensures that the generated data passes all three quality dimensions at levels suitable for downstream testing and analysis.

### Leverage Templates

Create and use **templates** for repeatable generation configurations. Templates eliminate manual re-entry of common settings and ensure consistency across team members. Data Engineers should create templates for each standard scenario (e.g., "SAP FI Full Dataset," "Oracle HR Test Data," "Dynamics SD Performance Load").

### Verify Schema Freshness

Before running large generation jobs, verify that the schema profile is up to date. If the source ERP system has undergone schema changes, **re-profile the schema** using the Schema Browser (Screen S-005) to ensure the Generation Engine works with current metadata.

### Optimize Output Format

- **Parquet** — Best for large datasets. Offers superior compression (often 5–10× smaller than CSV) and faster read performance for analytics workloads.
- **SQL** — Best when provisioning directly to a target database. Generates dialect-appropriate INSERT or COPY statements.
- **CSV** — Best for interoperability with spreadsheet tools and legacy ETL pipelines.
- **JSON** — Best for API testing fixtures and NoSQL database seeding.

### Check Relationships First

Before configuring a generation job, review the table relationships in the **Schema Browser (Screen S-005)**. Understanding foreign key dependencies helps you select the right set of related tables and configure appropriate record counts that maintain realistic data proportions between parent and child tables.

---

## Troubleshooting

### No Schemas Found

**Symptom:** Step 2 shows an empty schema list with no schemas to select.

**Cause:** No ERP schemas have been discovered yet.

**Solution:** Navigate to the **Schema Browser (Screen S-005)** and run schema discovery against your ERP system. Once schemas are discovered and profiled, they will appear in the Generation Wizard.

---

### Permission Denied

**Symptom:** An error message "Permission denied" appears when opening the wizard or submitting a job.

**Cause:** Your user account does not have the `generation:create` permission.

**Solution:** Contact your **Platform Administrator** to grant the `generation:create` permission to your account. Verify your role assignment includes wizard access (see [Role-Based Access Notes](#role-based-access-notes)).

---

### Quality Below Threshold

**Symptom:** The generation job completes with a `VALIDATION_FAILED` status, or the quality score in the report is below the configured threshold.

**Cause:** The generated data did not meet the quality threshold across one or more scoring dimensions (statistical fidelity, business rules, referential integrity).

**Solution:**

1. Open the **Quality Reports (Screen S-007)** to identify which specific dimension(s) scored below the threshold.
2. If **statistical fidelity** is low: Consider using AI/ML Generation for better distribution matching, or re-profile the source schema for fresher statistics.
3. If **business rules compliance** is low: Switch to Rules-Based Generation or review/update the business rules defined for the schema.
4. If **referential integrity** is low: Ensure all related parent and child tables are selected in Step 2. Verify that record count proportions between parent/child tables are realistic.
5. Alternatively, adjust the quality threshold downward if the current level is acceptable for your use case.

---

### Job Stuck in Generating

**Symptom:** The job status shows `GENERATING` for an unusually long time without progress updates.

**Cause:** Possible causes include large batch sizes with insufficient memory, network issues between services, or a stalled processing batch.

**Solution:**

1. Open **Job Monitoring (Screen S-004)** and check the batch progress indicator. If batches are advancing slowly, the job may simply need more time.
2. Check the job logs in Job Monitoring for error messages or warnings.
3. If no progress has been made for an extended period, consider canceling the job and resubmitting with a smaller batch size or fewer tables.
4. Verify that the Generation Engine service is healthy via the platform's monitoring tools.

---

### PII Detected in Output

**Symptom:** The job status shows `COMPLIANCE_FAILED` with a message indicating PII was detected.

**Cause:** The Compliance Service identified patterns matching personally identifiable information (SSN, email addresses, phone numbers, names, physical addresses, or financial account numbers) in the generated output.

**Solution:**

1. Open the **Compliance Dashboard (Screen S-008)** to review the specific PII findings.
2. The flagged data has been **quarantined** — no PII-containing data is ever released from the platform.
3. If using AI/ML Generation or Statistical Synthesis: Switch to **Intelligent Masking** which provides the strongest PII elimination guarantee.
4. If using Rules-Based Generation: Review the rule definitions to ensure PII-like patterns are excluded from value generators.
5. Re-submit the job after adjusting the generation method or parameters.

---

### Template Not Loading

**Symptom:** A selected template fails to auto-fill wizard parameters, or the template dropdown shows no templates.

**Cause:** The template may have been deleted, may be incompatible with the currently selected schema, or no templates have been created yet.

**Solution:**

1. Navigate to the **Template Library (Screen S-003)** and verify the template exists.
2. Check that the template's target ERP type and module match the schema you selected in Step 2.
3. If no templates exist, ask your Data Engineer or Platform Admin to create templates for common scenarios.
4. If a template exists but fails to load, try refreshing the page and re-selecting the template.

---

## Role-Based Access Notes

The Synthetic ERP Data Generation Platform implements **role-based access control (RBAC)** with five user roles. Each role has graduated permissions for the Generation Wizard:

| Capability | Platform Admin | Data Engineer | Developer | QA Engineer | Data Analyst |
|------------|:--------------:|:------------:|:---------:|:-----------:|:------------:|
| **Access Wizard** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **AI/ML Generation** | ✅ | ✅ | ✅ | ✅ | ❌ |
| **Rules-Based Generation** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Statistical Synthesis** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Intelligent Masking** | ✅ | ✅ | ✅ | ✅ | ❌ |
| **Create Templates** | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Manage Templates** | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Use Templates** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **System-Wide Templates** | ✅ | ❌ | ❌ | ❌ | ❌ |
| **All Schemas** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Adjust Quality Threshold** | ✅ | ✅ | ✅ | ✅ | ✅ |

> **Key differences:**
>
> - **Platform Admins** have unrestricted access including system-wide template management and all generation methods.
> - **Data Engineers** have full wizard access with the ability to create and manage templates for their team.
> - **Developers and QA Engineers** can use all generation methods and apply existing templates but cannot create or manage templates.
> - **Data Analysts** can access the wizard with Statistical Synthesis and Rules-Based methods and use existing templates. AI/ML Generation and Intelligent Masking methods are restricted for Data Analysts.

---

## Related Features

The Generation Wizard integrates with several other platform features. Use these screens to prepare for, monitor, and analyze your generation jobs:

| Screen | Name | Purpose | When to Use |
|--------|------|---------|-------------|
| **S-001** | [Dashboard](./generation-wizard.md) | Overview of recent generation jobs, system health, and throughput metrics. | Before starting — check system health and recent job results. |
| **S-003** | [Template Library](./generation-wizard.md) | Browse, create, and manage reusable generation configuration templates. | Before starting — select a template to auto-fill wizard parameters. |
| **S-004** | [Job Monitoring](./generation-wizard.md) | Real-time monitoring of generation job progress, batch status, and logs. | After submitting — track generation progress and view logs. |
| **S-005** | [Schema Browser](./generation-wizard.md) | Discover, explore, and profile ERP schemas and table structures. | Before starting — discover schemas and verify table relationships. |
| **S-006** | [Profile Viewer](./generation-wizard.md) | Visualize statistical profiles with distribution charts and column statistics. | Before starting — verify profile freshness and review source data characteristics. |
| **S-007** | [Quality Reports](./generation-wizard.md) | Detailed quality score reports with per-dimension drill-down. | After completion — review quality scores and identify improvement areas. |
| **S-008** | [Compliance Dashboard](./generation-wizard.md) | Compliance certification history, PII scan results, and regulatory status. | After completion — verify compliance certification and review any PII findings. |

---

## API Reference

For programmatic job creation, the Generation Wizard submits jobs via the following API endpoint:

**Create Generation Job**

```
POST /api/v1/generation/jobs
```

**Request Body (JSON):**

```json
{
  "generation_method": "ai_ml",
  "schema_id": "schema_abc123",
  "tables": [
    {
      "table_name": "GL_JOURNAL_ENTRIES",
      "record_count": 50000
    },
    {
      "table_name": "AR_INVOICES",
      "record_count": 25000
    }
  ],
  "output_format": "parquet",
  "batch_size": 10000,
  "quality_threshold": 0.95,
  "template_id": null
}
```

**Response (JSON):**

```json
{
  "job_id": "job_xyz789",
  "status": "SUBMITTED",
  "created_at": "2025-01-15T10:30:00Z",
  "estimated_duration_seconds": 300
}
```

**Valid `generation_method` values:** `ai_ml`, `rules_based`, `statistical`, `masking`

**Valid `output_format` values:** `sql`, `csv`, `json`, `parquet`

For complete API documentation, refer to the [OpenAPI Specification](../api/openapi.yaml).

---

*This guide covers the Generation Wizard (Screen S-002) of the Synthetic ERP Data Generation Platform. For additional help, contact your Platform Administrator or refer to the [Architecture Overview](../architecture/overview.md).*
