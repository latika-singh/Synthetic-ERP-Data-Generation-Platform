/**
 * QualityReports Page Component (Screen S-007)
 *
 * Displays data quality validation results with weighted composite scores.
 * Shows a list of quality reports from completed generation jobs, each with
 * the overall quality score and drill-down into three sub-dimensions:
 *  - Statistical Fidelity (40% weight)
 *  - Business Rules Compliance (30% weight)
 *  - Referential Integrity (30% weight)
 *
 * Uses QualityScoreGauge for visual score display, DataTable for report
 * listing, and provides detailed metric breakdown per table and column.
 * Consumes job data from jobStore and quality metrics from the generation API.
 *
 * @module pages/QualityReports
 * @version 1.0.0
 */

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useJobStore } from '@/store/jobStore';
import { getJobs, getJobQualityReport } from '@/services/generationApi';
import type { QualityReport } from '@/services/generationApi';
import QualityScoreGauge from '@/components/charts/QualityScoreGauge';
import DataTable from '@/components/common/DataTable';
import LoadingSpinner from '@/components/common/LoadingSpinner';
import type { GenerationJob } from '@/types/generation';

// =============================================================================
// Constants
// =============================================================================

/** Quality score threshold for pass/fail determination (95%) */
const QUALITY_THRESHOLD = 0.95;

/** Default page size for the reports DataTable */
const PAGE_SIZE = 20;

// =============================================================================
// Local TypeScript Interfaces
// =============================================================================

/** Result filter options for the quality reports list */
type FilterResult = 'all' | 'passed' | 'failed';

/** Individual failed quality check with diagnostic details */
interface FailedCheck {
  /** Name of the quality check that failed */
  check_name: string;
  /** Quality dimension this check belongs to */
  dimension: 'statistical' | 'business_rules' | 'referential_integrity';
  /** Expected value or range for the check */
  expected: string;
  /** Actual value observed in generated data */
  actual: string;
  /** Severity level of the failure */
  severity: 'critical' | 'warning' | 'info';
  /** Column the check applies to (empty string for table-level checks) */
  column_name: string;
}

/** Per-table quality metric breakdown */
interface TableQualityMetric {
  /** Full qualified table name */
  table_name: string;
  /** Number of records generated for this table */
  record_count: number;
  /** Composite quality score for this table (0.0 – 1.0) */
  overall_score: number;
  /** Statistical fidelity sub-score */
  statistical_score: number;
  /** Business rules compliance sub-score */
  business_rules_score: number;
  /** Referential integrity sub-score */
  referential_integrity_score: number;
  /** List of individual failed checks for this table */
  failed_checks: FailedCheck[];
}

/**
 * Extended quality report combining API quality data with job metadata.
 * Uses a type alias (not interface) to satisfy Record<string, unknown>
 * constraint required by the DataTable generic component.
 */
type ExtendedQualityReport = {
  /** Unique quality report identifier */
  report_id: string;
  /** Associated generation job ID */
  job_id: string;
  /** Human-readable display name derived from method + job_id */
  job_name: string;
  /** Weighted composite quality score (0.0 – 1.0) */
  overall_score: number;
  /** Statistical fidelity sub-score (40% weight) */
  statistical_score: number;
  /** Business rules compliance sub-score (30% weight) */
  business_rules_score: number;
  /** Referential integrity sub-score (30% weight) */
  referential_integrity_score: number;
  /** Whether the overall score meets the quality threshold */
  meets_threshold: boolean;
  /** Quality threshold used for pass/fail (0.95) */
  threshold: number;
  /** Total number of quality checks performed */
  total_checks: number;
  /** Number of quality checks that passed */
  passed_checks: number;
  /** Number of quality checks that failed */
  failed_checks: number;
  /** Per-table quality metrics breakdown */
  table_metrics: TableQualityMetric[];
  /** ISO 8601 timestamp of report creation / validation */
  created_at: string;
  /** Duration of quality validation in seconds */
  duration_seconds: number;
  /** Generation method used for the job */
  method: string;
};

// =============================================================================
// Helper Functions
// =============================================================================

/** Formats a score value (0.0 – 1.0) as a percentage string */
function formatScore(score: number): string {
  return `${(score * 100).toFixed(1)}%`;
}

/** Returns a TailwindCSS text-color class based on the quality score */
function getScoreColorClass(score: number): string {
  if (score >= 0.95) return 'text-green-600 dark:text-green-400';
  if (score >= 0.80) return 'text-amber-600 dark:text-amber-400';
  if (score >= 0.60) return 'text-orange-600 dark:text-orange-400';
  return 'text-red-600 dark:text-red-400';
}

/** Returns a severity-based TailwindCSS badge class */
function getSeverityBadgeClass(severity: string): string {
  switch (severity) {
    case 'critical':
      return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300';
    case 'warning':
      return 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300';
    case 'info':
      return 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300';
    default:
      return 'bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-300';
  }
}

/** Returns a human-readable label for a quality dimension key */
function getDimensionLabel(dimension: string): string {
  switch (dimension) {
    case 'statistical':
      return 'Statistical Fidelity';
    case 'business_rules':
      return 'Business Rules';
    case 'referential_integrity':
      return 'Referential Integrity';
    default:
      return dimension;
  }
}

/** Formats an ISO 8601 date string into a short human-readable representation */
function formatDate(dateStr: string): string {
  try {
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return dateStr;
    return date.toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return dateStr;
  }
}

/** Formats a duration in seconds to a human-readable string (e.g. "2m 34s") */
function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (mins < 60) return `${mins}m ${secs}s`;
  const hours = Math.floor(mins / 60);
  const remainMins = mins % 60;
  return `${hours}h ${remainMins}m`;
}

/** Converts a generation method key to a display label */
function formatMethod(method: string): string {
  switch (method) {
    case 'ai_ml':
      return 'AI/ML';
    case 'rules_based':
      return 'Rules-Based';
    case 'statistical':
      return 'Statistical';
    case 'masking':
      return 'Masking';
    default:
      return method.charAt(0).toUpperCase() + method.slice(1);
  }
}

/**
 * Builds an ExtendedQualityReport by merging a GenerationJob with its
 * QualityReport data returned from the API.
 */
function buildExtendedReport(
  job: GenerationJob,
  report: QualityReport,
): ExtendedQualityReport {
  const details = report.details ?? {};
  const tableMetrics = Array.isArray(details.table_metrics)
    ? (details.table_metrics as TableQualityMetric[])
    : [];
  const totalChecks =
    typeof details.total_checks === 'number' ? details.total_checks : 0;
  const passedChecks =
    typeof details.passed_checks === 'number' ? details.passed_checks : 0;
  const failedChecksCount =
    typeof details.failed_checks_count === 'number'
      ? details.failed_checks_count
      : Math.max(0, totalChecks - passedChecks);
  const durationSeconds =
    typeof details.duration_seconds === 'number' ? details.duration_seconds : 0;

  return {
    report_id: `qr-${job.job_id}`,
    job_id: job.job_id,
    job_name: `${formatMethod(job.method)} — ${job.job_id.substring(0, 8)}`,
    overall_score: report.overall_score,
    statistical_score: report.statistical_score,
    business_rules_score: report.business_rules_score,
    referential_integrity_score: report.referential_integrity_score,
    meets_threshold: report.overall_score >= QUALITY_THRESHOLD,
    threshold: QUALITY_THRESHOLD,
    total_checks: totalChecks,
    passed_checks: passedChecks,
    failed_checks: failedChecksCount,
    table_metrics: tableMetrics,
    created_at: report.validated_at || job.created_at,
    duration_seconds: durationSeconds,
    method: job.method,
  };
}

/**
 * Builds a partial ExtendedQualityReport from job data alone when the
 * detailed quality report fetch fails.
 */
function buildPartialReport(job: GenerationJob): ExtendedQualityReport {
  const score = job.quality_score ?? 0;
  return {
    report_id: `qr-${job.job_id}`,
    job_id: job.job_id,
    job_name: `${formatMethod(job.method)} — ${job.job_id.substring(0, 8)}`,
    overall_score: score,
    statistical_score: 0,
    business_rules_score: 0,
    referential_integrity_score: 0,
    meets_threshold: score >= QUALITY_THRESHOLD,
    threshold: QUALITY_THRESHOLD,
    total_checks: 0,
    passed_checks: 0,
    failed_checks: 0,
    table_metrics: [],
    created_at: job.completed_at ?? job.created_at,
    duration_seconds: 0,
    method: job.method,
  };
}

// =============================================================================
// Component
// =============================================================================

/**
 * QualityReports — Screen S-007
 *
 * Renders a master-detail view of data quality validation results.
 * The list view shows summary cards, filter controls, and a DataTable of
 * quality reports. The detail view shows the QualityScoreGauge, weighted
 * breakdown cards, per-table accordion metrics, and failed checks summary.
 */
function QualityReports(): React.JSX.Element {
  const [searchParams] = useSearchParams();

  // ---------------------------------------------------------------------------
  // Store state and actions
  // ---------------------------------------------------------------------------
  const {
    jobs,
    selectedJob,
    isJobsLoading,
    jobsError,
    fetchJobs,
    fetchJobQualityReport,
    activeJobQualityReport,
  } = useJobStore();

  // ---------------------------------------------------------------------------
  // Component local state
  // ---------------------------------------------------------------------------
  const [qualityReports, setQualityReports] = useState<ExtendedQualityReport[]>(
    [],
  );
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedReport, setSelectedReport] =
    useState<ExtendedQualityReport | null>(null);
  const [filterResult, setFilterResult] = useState<FilterResult>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [expandedTables, setExpandedTables] = useState<Set<string>>(new Set());
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [sortBy, setSortBy] = useState<string>('created_at');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');

  // ---------------------------------------------------------------------------
  // Data Fetching Effects
  // ---------------------------------------------------------------------------

  /**
   * On mount, populate the job store and fetch completed jobs with their
   * quality reports directly from the API for the reports list.
   */
  useEffect(() => {
    let cancelled = false;

    const loadData = async (): Promise<void> => {
      setIsLoading(true);
      setError(null);

      try {
        // Populate the global job store for cross-component state sharing
        fetchJobs({
          page: 1,
          page_size: 100,
          sort_by: 'created_at',
          sort_direction: 'desc',
        });

        // Fetch completed jobs directly from the API for quality report building
        const jobsResponse = await getJobs({
          page: 1,
          page_size: 100,
          sort_by: 'created_at',
          sort_direction: 'desc',
        });

        const completedJobs = jobsResponse.data.items.filter(
          (job: GenerationJob) =>
            job.status === 'completed' && job.quality_score != null,
        );

        if (completedJobs.length === 0) {
          if (!cancelled) {
            setQualityReports([]);
          }
          return;
        }

        // Fetch detailed quality reports for each completed job in parallel
        const reportPromises = completedJobs.map(
          async (job: GenerationJob) => {
            try {
              const reportResponse = await getJobQualityReport(job.job_id);
              return buildExtendedReport(job, reportResponse.data);
            } catch {
              // Fall back to partial report built from job-level quality_score
              return buildPartialReport(job);
            }
          },
        );

        const reports = await Promise.all(reportPromises);
        if (!cancelled) {
          setQualityReports(reports);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          setError(
            err instanceof Error
              ? err.message
              : 'Failed to load quality reports',
          );
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    };

    loadData();
    return () => {
      cancelled = true;
    };
  }, [fetchJobs]);

  /**
   * Auto-select a quality report when a ?report_id= URL parameter is present.
   * Enables deep-linking from the Job Monitoring or Dashboard pages.
   */
  useEffect(() => {
    const reportId = searchParams.get('report_id');
    if (reportId && qualityReports.length > 0) {
      const match = qualityReports.find(
        (r) => r.report_id === reportId || r.job_id === reportId,
      );
      if (match) {
        setSelectedReport(match);
        setExpandedTables(new Set());
        fetchJobQualityReport(match.job_id);
      }
    }
  }, [searchParams, qualityReports, fetchJobQualityReport]);

  // ---------------------------------------------------------------------------
  // Memoised Computed Values
  // ---------------------------------------------------------------------------

  /** Summary statistics for the top-level cards */
  const summaryStats = useMemo(() => {
    const total = qualityReports.length;
    const passed = qualityReports.filter((r) => r.meets_threshold).length;
    const failed = total - passed;
    const avgScore =
      total > 0
        ? qualityReports.reduce((sum, r) => sum + r.overall_score, 0) / total
        : 0;
    return { total, passed, failed, avgScore };
  }, [qualityReports]);

  /** Filtered, searched, and sorted reports list for the DataTable */
  const filteredReports = useMemo(() => {
    let reports = [...qualityReports];

    // Apply pass/fail filter
    if (filterResult === 'passed') {
      reports = reports.filter((r) => r.meets_threshold);
    } else if (filterResult === 'failed') {
      reports = reports.filter((r) => !r.meets_threshold);
    }

    // Apply text search across job name / id / report id
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      reports = reports.filter(
        (r) =>
          r.job_name.toLowerCase().includes(query) ||
          r.job_id.toLowerCase().includes(query) ||
          r.report_id.toLowerCase().includes(query),
      );
    }

    // Sort by the active sort key
    reports.sort((a, b) => {
      const aVal = a[sortBy as keyof ExtendedQualityReport];
      const bVal = b[sortBy as keyof ExtendedQualityReport];
      if (typeof aVal === 'number' && typeof bVal === 'number') {
        return sortDirection === 'asc' ? aVal - bVal : bVal - aVal;
      }
      const aStr = String(aVal ?? '');
      const bStr = String(bVal ?? '');
      return sortDirection === 'asc'
        ? aStr.localeCompare(bStr)
        : bStr.localeCompare(aStr);
    });

    return reports;
  }, [qualityReports, filterResult, searchQuery, sortBy, sortDirection]);

  /**
   * Enhanced detail report that merges the locally-cached selectedReport
   * with the store's activeJobQualityReport for richer per-table data.
   * Also overlays selectedJob metadata when available.
   */
  const detailReport = useMemo((): ExtendedQualityReport | null => {
    if (!selectedReport) return null;

    // Start from the selected report
    let report = { ...selectedReport };

    // Overlay store's quality report data when available (richer details)
    if (activeJobQualityReport) {
      const details = activeJobQualityReport.details ?? {};
      const tableMetrics = Array.isArray(details.table_metrics)
        ? (details.table_metrics as TableQualityMetric[])
        : report.table_metrics;
      const totalChecks =
        typeof details.total_checks === 'number'
          ? details.total_checks
          : report.total_checks;
      const passedChecks =
        typeof details.passed_checks === 'number'
          ? details.passed_checks
          : report.passed_checks;
      const failedChecksCount =
        typeof details.failed_checks_count === 'number'
          ? details.failed_checks_count
          : report.failed_checks;
      const durationSeconds =
        typeof details.duration_seconds === 'number'
          ? details.duration_seconds
          : report.duration_seconds;

      report = {
        ...report,
        overall_score: activeJobQualityReport.overall_score,
        statistical_score: activeJobQualityReport.statistical_score,
        business_rules_score: activeJobQualityReport.business_rules_score,
        referential_integrity_score:
          activeJobQualityReport.referential_integrity_score,
        meets_threshold:
          activeJobQualityReport.overall_score >= QUALITY_THRESHOLD,
        table_metrics: tableMetrics,
        total_checks: totalChecks,
        passed_checks: passedChecks,
        failed_checks: failedChecksCount,
        duration_seconds: durationSeconds,
      };
    }

    // Overlay selectedJob metadata when available (e.g. total_records)
    if (selectedJob && selectedJob.job_id === report.job_id) {
      report = {
        ...report,
        job_name: `${formatMethod(selectedJob.method)} — ${selectedJob.job_id.substring(0, 8)}`,
        method: selectedJob.method,
        created_at: selectedJob.completed_at ?? selectedJob.created_at,
      };
    }

    return report;
  }, [selectedReport, activeJobQualityReport, selectedJob]);

  /** Aggregated failed checks grouped by quality dimension for the detail view */
  const failedChecksByDimension = useMemo(() => {
    if (!detailReport) return new Map<string, FailedCheck[]>();
    const grouped = new Map<string, FailedCheck[]>();
    for (const table of detailReport.table_metrics) {
      for (const check of table.failed_checks) {
        const dim = check.dimension;
        const existing = grouped.get(dim) ?? [];
        existing.push({
          ...check,
          column_name: check.column_name || table.table_name,
        });
        grouped.set(dim, existing);
      }
    }
    return grouped;
  }, [detailReport]);

  // ---------------------------------------------------------------------------
  // Event Handlers (memoised)
  // ---------------------------------------------------------------------------

  /** Select a quality report for drill-down detail view */
  const handleReportSelect = useCallback(
    (row: Record<string, unknown>) => {
      const report = row as unknown as ExtendedQualityReport;
      setSelectedReport(report);
      setExpandedTables(new Set());
      fetchJobQualityReport(report.job_id);
    },
    [fetchJobQualityReport],
  );

  /** Navigate back from the detail view to the list view */
  const handleBack = useCallback(() => {
    setSelectedReport(null);
    setExpandedTables(new Set());
  }, []);

  /** Update the pass/fail filter and reset pagination */
  const handleFilterChange = useCallback((filter: FilterResult) => {
    setFilterResult(filter);
    setCurrentPage(1);
  }, []);

  /** Update the search query and reset pagination */
  const handleSearch = useCallback((value: string) => {
    setSearchQuery(value);
    setCurrentPage(1);
  }, []);

  /** Update the sort key and direction */
  const handleSort = useCallback((key: string, direction: 'asc' | 'desc') => {
    setSortBy(key);
    setSortDirection(direction);
  }, []);

  /** Toggle the expanded state of a per-table accordion section */
  const toggleTableExpand = useCallback((tableName: string) => {
    setExpandedTables((prev) => {
      const next = new Set(prev);
      if (next.has(tableName)) {
        next.delete(tableName);
      } else {
        next.add(tableName);
      }
      return next;
    });
  }, []);

  // ---------------------------------------------------------------------------
  // DataTable Column Definitions
  // ---------------------------------------------------------------------------

  const columns = useMemo(
    () => [
      {
        key: 'job_name',
        header: 'Job Name',
        sortable: true,
        width: 'min-w-[180px]',
        render: (value: unknown) => (
          <span className="font-medium text-gray-900 dark:text-white">
            {String(value)}
          </span>
        ),
      },
      {
        key: 'overall_score',
        header: 'Overall Score',
        sortable: true,
        width: 'w-32',
        align: 'center' as const,
        render: (value: unknown) => {
          const score = Number(value) || 0;
          return (
            <span className={`font-semibold ${getScoreColorClass(score)}`}>
              {formatScore(score)}
            </span>
          );
        },
      },
      {
        key: 'statistical_score',
        header: 'Statistical',
        sortable: true,
        width: 'w-28',
        align: 'center' as const,
        render: (value: unknown) => (
          <span className={getScoreColorClass(Number(value) || 0)}>
            {formatScore(Number(value) || 0)}
          </span>
        ),
      },
      {
        key: 'business_rules_score',
        header: 'Business Rules',
        sortable: true,
        width: 'w-28',
        align: 'center' as const,
        render: (value: unknown) => (
          <span className={getScoreColorClass(Number(value) || 0)}>
            {formatScore(Number(value) || 0)}
          </span>
        ),
      },
      {
        key: 'referential_integrity_score',
        header: 'Ref. Integrity',
        sortable: true,
        width: 'w-28',
        align: 'center' as const,
        render: (value: unknown) => (
          <span className={getScoreColorClass(Number(value) || 0)}>
            {formatScore(Number(value) || 0)}
          </span>
        ),
      },
      {
        key: 'total_checks',
        header: 'Checks',
        sortable: true,
        width: 'w-20',
        align: 'center' as const,
        render: (value: unknown) => (
          <span className="text-gray-600 dark:text-gray-400">
            {String(value)}
          </span>
        ),
      },
      {
        key: 'passed_checks',
        header: 'Passed',
        sortable: true,
        width: 'w-20',
        align: 'center' as const,
        render: (value: unknown) => (
          <span className="text-green-600 dark:text-green-400">
            {String(value)}
          </span>
        ),
      },
      {
        key: 'failed_checks',
        header: 'Failed',
        sortable: true,
        width: 'w-20',
        align: 'center' as const,
        render: (value: unknown) => {
          const num = Number(value) || 0;
          return (
            <span
              className={
                num > 0
                  ? 'text-red-600 dark:text-red-400 font-medium'
                  : 'text-gray-500 dark:text-gray-400'
              }
            >
              {String(num)}
            </span>
          );
        },
      },
      {
        key: 'created_at',
        header: 'Date',
        sortable: true,
        width: 'min-w-[140px]',
        render: (value: unknown) => (
          <span className="text-gray-500 dark:text-gray-400 text-sm">
            {formatDate(String(value))}
          </span>
        ),
      },
      {
        key: 'meets_threshold',
        header: 'Result',
        width: 'w-24',
        align: 'center' as const,
        render: (value: unknown) => (
          <span
            className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
              value
                ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                : 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
            }`}
          >
            {value ? 'Passed' : 'Failed'}
          </span>
        ),
      },
    ],
    [],
  );

  // ===========================================================================
  // Rendering
  // ===========================================================================

  // -- Full-page loading state ------------------------------------------------
  if ((isLoading || isJobsLoading) && qualityReports.length === 0) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <LoadingSpinner size="lg" message="Loading quality reports..." />
      </div>
    );
  }

  // -- Full-page error state --------------------------------------------------
  if ((error || jobsError) && qualityReports.length === 0) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          Quality Reports
        </h1>
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-6 text-center">
          <svg
            className="mx-auto h-12 w-12 text-red-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z"
            />
          </svg>
          <h3 className="mt-2 text-sm font-semibold text-red-800 dark:text-red-200">
            Error Loading Reports
          </h3>
          <p className="mt-1 text-sm text-red-600 dark:text-red-400">
            {error || jobsError}
          </p>
          <button
            onClick={() =>
              fetchJobs({
                page: 1,
                page_size: 100,
                sort_by: 'created_at',
                sort_direction: 'desc',
              })
            }
            className="mt-4 inline-flex items-center px-4 py-2 border border-transparent text-sm font-medium rounded-md text-white bg-red-600 hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-500 transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  // ==========================================================================
  // DETAIL VIEW — When a report is selected for drill-down
  // ==========================================================================
  if (selectedReport && detailReport) {
    return (
      <div className="space-y-6">
        {/* ---- Detail Header ---- */}
        <div className="flex items-center justify-between flex-wrap gap-4">
          <div className="flex items-center space-x-4">
            <button
              onClick={handleBack}
              className="inline-flex items-center px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md text-sm font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 hover:bg-gray-50 dark:hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500 transition-colors"
              aria-label="Back to reports list"
            >
              <svg
                className="mr-2 h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M15 19l-7-7 7-7"
                />
              </svg>
              Back
            </button>
            <div>
              <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
                Quality Report
              </h1>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {detailReport.job_name} &bull; {detailReport.report_id} &bull;{' '}
                {formatDate(detailReport.created_at)}
                {detailReport.duration_seconds > 0 && (
                  <span>
                    {' '}
                    &bull; Duration:{' '}
                    {formatDuration(detailReport.duration_seconds)}
                  </span>
                )}
                {selectedJob &&
                  selectedJob.job_id === detailReport.job_id && (
                    <span>
                      {' '}
                      &bull;{' '}
                      {selectedJob.total_records.toLocaleString()} records
                    </span>
                  )}
              </p>
            </div>
          </div>
        </div>

        {/* ---- Overall Quality Gauge ---- */}
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-6">
          <div className="flex flex-col items-center">
            <QualityScoreGauge
              overallScore={detailReport.overall_score}
              breakdown={{
                statistical: detailReport.statistical_score,
                businessRules: detailReport.business_rules_score,
                referentialIntegrity:
                  detailReport.referential_integrity_score,
              }}
              size="lg"
              showBreakdown={true}
              threshold={QUALITY_THRESHOLD}
              title="Overall Quality Score"
            />
          </div>
        </div>

        {/* ---- Score Breakdown Cards (3 columns) ---- */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Statistical Fidelity */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Statistical Fidelity
              </h3>
              <span className="text-xs font-medium text-gray-400 dark:text-gray-500">
                40% weight
              </span>
            </div>
            <p
              className={`text-3xl font-bold ${getScoreColorClass(detailReport.statistical_score)}`}
            >
              {formatScore(detailReport.statistical_score)}
            </p>
            <div className="mt-2 w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
              <div
                className="h-2 rounded-full bg-indigo-500 transition-all duration-500"
                style={{
                  width: `${Math.min(detailReport.statistical_score * 100, 100)}%`,
                }}
              />
            </div>
          </div>

          {/* Business Rules Compliance */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Business Rules
              </h3>
              <span className="text-xs font-medium text-gray-400 dark:text-gray-500">
                30% weight
              </span>
            </div>
            <p
              className={`text-3xl font-bold ${getScoreColorClass(detailReport.business_rules_score)}`}
            >
              {formatScore(detailReport.business_rules_score)}
            </p>
            <div className="mt-2 w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
              <div
                className="h-2 rounded-full bg-purple-500 transition-all duration-500"
                style={{
                  width: `${Math.min(detailReport.business_rules_score * 100, 100)}%`,
                }}
              />
            </div>
          </div>

          {/* Referential Integrity */}
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Referential Integrity
              </h3>
              <span className="text-xs font-medium text-gray-400 dark:text-gray-500">
                30% weight
              </span>
            </div>
            <p
              className={`text-3xl font-bold ${getScoreColorClass(detailReport.referential_integrity_score)}`}
            >
              {formatScore(detailReport.referential_integrity_score)}
            </p>
            <div className="mt-2 w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
              <div
                className="h-2 rounded-full bg-teal-500 transition-all duration-500"
                style={{
                  width: `${Math.min(detailReport.referential_integrity_score * 100, 100)}%`,
                }}
              />
            </div>
          </div>
        </div>

        {/* ---- Validation Checks Summary ---- */}
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
          <div className="flex items-center justify-between">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
              Validation Checks Summary
            </h3>
            <span
              className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${
                detailReport.meets_threshold
                  ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                  : 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
              }`}
            >
              {detailReport.meets_threshold ? '✓ Passed' : '✗ Failed'} (Target:
              ≥{formatScore(detailReport.threshold)})
            </span>
          </div>
          <div className="mt-4 grid grid-cols-3 gap-6 text-center">
            <div>
              <p className="text-2xl font-bold text-gray-900 dark:text-white">
                {detailReport.total_checks}
              </p>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Total Checks
              </p>
            </div>
            <div>
              <p className="text-2xl font-bold text-green-600 dark:text-green-400">
                {detailReport.passed_checks}
              </p>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Passed
              </p>
            </div>
            <div>
              <p className="text-2xl font-bold text-red-600 dark:text-red-400">
                {detailReport.failed_checks}
              </p>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Failed
              </p>
            </div>
          </div>
        </div>

        {/* ---- Per-Table Quality Metrics Accordion ---- */}
        {detailReport.table_metrics.length > 0 && (
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                Per-Table Quality Metrics
              </h3>
            </div>
            <div className="divide-y divide-gray-200 dark:divide-gray-700">
              {detailReport.table_metrics.map((table) => (
                <div key={table.table_name}>
                  {/* Accordion header – clickable */}
                  <button
                    onClick={() => toggleTableExpand(table.table_name)}
                    className="w-full px-6 py-4 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
                    aria-expanded={expandedTables.has(table.table_name)}
                  >
                    <div className="flex items-center space-x-4">
                      <svg
                        className={`h-4 w-4 text-gray-400 transition-transform duration-200 ${
                          expandedTables.has(table.table_name)
                            ? 'rotate-90'
                            : ''
                        }`}
                        fill="none"
                        viewBox="0 0 24 24"
                        stroke="currentColor"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M9 5l7 7-7 7"
                        />
                      </svg>
                      <div className="text-left">
                        <span className="font-medium text-gray-900 dark:text-white">
                          {table.table_name}
                        </span>
                        <span className="ml-2 text-sm text-gray-500 dark:text-gray-400">
                          ({table.record_count.toLocaleString()} records)
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center space-x-4">
                      <span
                        className={`text-sm font-semibold ${getScoreColorClass(table.overall_score)}`}
                      >
                        {formatScore(table.overall_score)}
                      </span>
                      {table.failed_checks.length > 0 && (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300">
                          {table.failed_checks.length} failed
                        </span>
                      )}
                    </div>
                  </button>

                  {/* Expanded detail panel */}
                  {expandedTables.has(table.table_name) && (
                    <div className="px-6 pb-4 bg-gray-50 dark:bg-gray-900/30">
                      {/* Sub-score breakdown for this table */}
                      <div className="grid grid-cols-3 gap-4 py-3">
                        <div className="text-center">
                          <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                            Statistical
                          </p>
                          <p
                            className={`text-lg font-semibold ${getScoreColorClass(table.statistical_score)}`}
                          >
                            {formatScore(table.statistical_score)}
                          </p>
                        </div>
                        <div className="text-center">
                          <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                            Business Rules
                          </p>
                          <p
                            className={`text-lg font-semibold ${getScoreColorClass(table.business_rules_score)}`}
                          >
                            {formatScore(table.business_rules_score)}
                          </p>
                        </div>
                        <div className="text-center">
                          <p className="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                            Ref. Integrity
                          </p>
                          <p
                            className={`text-lg font-semibold ${getScoreColorClass(table.referential_integrity_score)}`}
                          >
                            {formatScore(
                              table.referential_integrity_score,
                            )}
                          </p>
                        </div>
                      </div>

                      {/* Failed checks list for this table */}
                      {table.failed_checks.length > 0 ? (
                        <div className="mt-2">
                          <h4 className="text-xs font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wider mb-2">
                            Failed Checks
                          </h4>
                          <div className="space-y-2">
                            {table.failed_checks.map((check, idx) => (
                              <div
                                key={`${check.check_name}-${idx}`}
                                className="flex items-start justify-between bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 text-sm"
                              >
                                <div className="flex-1">
                                  <div className="flex items-center space-x-2">
                                    <span
                                      className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium ${getSeverityBadgeClass(check.severity)}`}
                                    >
                                      {check.severity}
                                    </span>
                                    <span className="font-medium text-gray-900 dark:text-white">
                                      {check.check_name}
                                    </span>
                                  </div>
                                  {check.column_name && (
                                    <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                                      Column: {check.column_name}
                                    </p>
                                  )}
                                </div>
                                <div className="text-right text-xs ml-4 flex-shrink-0">
                                  <p className="text-gray-500 dark:text-gray-400">
                                    Expected:{' '}
                                    <span className="font-medium">
                                      {check.expected}
                                    </span>
                                  </p>
                                  <p className="text-red-600 dark:text-red-400">
                                    Actual:{' '}
                                    <span className="font-medium">
                                      {check.actual}
                                    </span>
                                  </p>
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : (
                        <p className="text-sm text-green-600 dark:text-green-400 py-2">
                          ✓ All quality checks passed for this table.
                        </p>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ---- Failed Checks Summary by Dimension ---- */}
        {failedChecksByDimension.size > 0 && (
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700">
            <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
                Failed Checks by Dimension
              </h3>
            </div>
            <div className="p-6 space-y-4">
              {Array.from(failedChecksByDimension.entries()).map(
                ([dimension, checks]) => (
                  <div key={dimension}>
                    <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
                      {getDimensionLabel(dimension)} ({checks.length} failure
                      {checks.length !== 1 ? 's' : ''})
                    </h4>
                    <div className="space-y-1">
                      {checks.map((check, idx) => (
                        <div
                          key={`${dimension}-${idx}`}
                          className="flex items-center justify-between text-sm px-3 py-1.5 bg-gray-50 dark:bg-gray-900/30 rounded"
                        >
                          <div className="flex items-center space-x-2">
                            <span
                              className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium ${getSeverityBadgeClass(check.severity)}`}
                            >
                              {check.severity}
                            </span>
                            <span className="text-gray-900 dark:text-white">
                              {check.check_name}
                            </span>
                            <span className="text-gray-500 dark:text-gray-400">
                              &bull; {check.column_name}
                            </span>
                          </div>
                          <span className="text-red-600 dark:text-red-400 text-xs flex-shrink-0 ml-4">
                            {check.actual} (expected: {check.expected})
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ),
              )}
            </div>
          </div>
        )}
      </div>
    );
  }

  // ==========================================================================
  // LIST VIEW — Default view with summary cards, filters, and DataTable
  // ==========================================================================
  return (
    <div className="space-y-6">
      {/* ---- Page Header ---- */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Quality Reports
          </h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Data quality validation results with weighted composite scoring
          </p>
        </div>
      </div>

      {/* ---- Empty State ---- */}
      {qualityReports.length === 0 && !isLoading && !isJobsLoading && (
        <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-12 text-center">
          <svg
            className="mx-auto h-16 w-16 text-gray-300 dark:text-gray-600"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1}
              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
            />
          </svg>
          <h3 className="mt-4 text-lg font-medium text-gray-900 dark:text-white">
            No quality reports available
          </h3>
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400 max-w-md mx-auto">
            Complete a generation job to see quality results. Quality reports are
            generated automatically after each successful data generation run.
          </p>
        </div>
      )}

      {/* ---- Summary Cards + Filters + DataTable ---- */}
      {qualityReports.length > 0 && (
        <>
          {/* Summary Cards Row */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* Total Reports */}
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Total Reports
              </p>
              <p className="mt-1 text-3xl font-bold text-gray-900 dark:text-white">
                {summaryStats.total}
              </p>
            </div>

            {/* Passed */}
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Passed (≥95%)
              </p>
              <p className="mt-1 text-3xl font-bold text-green-600 dark:text-green-400">
                {summaryStats.passed}
              </p>
            </div>

            {/* Failed */}
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Failed (&lt;95%)
              </p>
              <p className="mt-1 text-3xl font-bold text-red-600 dark:text-red-400">
                {summaryStats.failed}
              </p>
            </div>

            {/* Average Score */}
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-5">
              <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Average Score
              </p>
              <p
                className={`mt-1 text-3xl font-bold ${getScoreColorClass(summaryStats.avgScore)}`}
              >
                {formatScore(summaryStats.avgScore)}
              </p>
            </div>
          </div>

          {/* Filters Bar */}
          <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
            {/* Result Filter Toggle Buttons */}
            <div className="inline-flex rounded-md shadow-sm" role="group">
              {(['all', 'passed', 'failed'] as FilterResult[]).map(
                (filter) => (
                  <button
                    key={filter}
                    onClick={() => handleFilterChange(filter)}
                    className={`px-4 py-2 text-sm font-medium border ${
                      filterResult === filter
                        ? 'bg-indigo-600 text-white border-indigo-600 dark:bg-indigo-500 dark:border-indigo-500'
                        : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700'
                    } ${
                      filter === 'all'
                        ? 'rounded-l-md'
                        : filter === 'failed'
                          ? 'rounded-r-md'
                          : ''
                    } focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:z-10 transition-colors`}
                  >
                    {filter === 'all'
                      ? 'All'
                      : filter === 'passed'
                        ? 'Passed'
                        : 'Failed'}
                    <span className="ml-1.5 text-xs opacity-75">
                      (
                      {filter === 'all'
                        ? summaryStats.total
                        : filter === 'passed'
                          ? summaryStats.passed
                          : summaryStats.failed}
                      )
                    </span>
                  </button>
                ),
              )}
            </div>
          </div>

          {/* Reports DataTable */}
          <DataTable
            data={
              filteredReports as unknown as Record<string, unknown>[]
            }
            columns={columns}
            keyField="report_id"
            totalItems={filteredReports.length}
            currentPage={currentPage}
            pageSize={PAGE_SIZE}
            onPageChange={setCurrentPage}
            sortBy={sortBy}
            sortDirection={sortDirection}
            onSort={handleSort}
            searchable
            searchPlaceholder="Search reports by job name or ID..."
            searchValue={searchQuery}
            onSearchChange={handleSearch}
            onRowClick={handleReportSelect}
            isLoading={isLoading || isJobsLoading}
            emptyMessage="No quality reports match your filters."
          />
        </>
      )}
    </div>
  );
}

export default QualityReports;
