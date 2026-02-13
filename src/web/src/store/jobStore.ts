/**
 * @fileoverview Zustand state management store for generation job lifecycle.
 *
 * Manages the complete lifecycle of synthetic data generation jobs in the Web
 * Console including: paginated job listing, job creation from the Generation
 * Wizard (S-002), real-time progress polling for the Job Monitoring page (S-004),
 * job cancellation, retry of failed jobs, output file download, and quality
 * report retrieval.
 *
 * This store is consumed by:
 * - **Dashboard** (S-001) — Recent jobs display and generation statistics
 * - **GenerationWizard** (S-002) — Job submission from wizard configuration
 * - **TemplateLibrary** (S-003) — Template-based generation triggering
 * - **JobMonitoring** (S-004) — Real-time batch progress tracking with ETA
 *
 * Unlike the authStore which uses persist middleware, jobStore uses plain
 * `create()` without persistence since job state is transient and should be
 * freshly loaded from the API on each session.
 *
 * @module store/jobStore
 * @version 1.0.0
 */

import { create } from 'zustand';
import type {
  GenerationJob,
  JobConfig,
  JobStatus,
  BatchProgress,
} from '@/types/generation';
import type { PaginationParams } from '@/types/api';
import {
  createJob as createJobApi,
  getJobs,
  getJobById,
  cancelJob as cancelJobApi,
  getJobProgress,
  getJobQualityReport,
  downloadJobOutput,
  retryJob as retryJobApi,
} from '@/services/generationApi';
import type { QualityReport, JobFilterParams } from '@/services/generationApi';

// ============================================================================
// JobState Interface
// ============================================================================

/**
 * Complete state shape and action methods for the generation job store.
 *
 * Organizes state into logical groups:
 * - **Job list state** — Paginated list of jobs with total count and page info
 * - **Selected job state** — Currently viewed job with progress and quality data
 * - **Polling state** — Interval-based progress polling for active jobs
 * - **Loading states** — Per-action loading indicators for UI feedback
 * - **Error states** — Per-action error messages for error display
 * - **Actions** — Async operations for the full job lifecycle
 */
export interface JobState {
  // ---------------------------------------------------------------------------
  // Job list state
  // ---------------------------------------------------------------------------

  /** Paginated array of generation jobs for the current page */
  jobs: GenerationJob[];

  /** Total number of jobs across all pages (server-reported) */
  jobsTotal: number;

  /** Current page number (1-indexed) */
  jobsPage: number;

  /** Number of items per page */
  jobsPageSize: number;

  // ---------------------------------------------------------------------------
  // Selected / active job
  // ---------------------------------------------------------------------------

  /** Currently selected job for detail view or monitoring */
  selectedJob: GenerationJob | null;

  /** Real-time batch progress snapshot for the active job */
  activeJobProgress: BatchProgress | null;

  /** Quality validation report for the active job (available post-validation) */
  activeJobQualityReport: QualityReport | null;

  // ---------------------------------------------------------------------------
  // Polling state
  // ---------------------------------------------------------------------------

  /** Job ID currently being polled for progress updates */
  pollingJobId: string | null;

  /** Handle returned by setInterval for the active polling loop */
  pollingIntervalId: ReturnType<typeof setInterval> | null;

  // ---------------------------------------------------------------------------
  // Loading states
  // ---------------------------------------------------------------------------

  /** Whether the paginated jobs list is being fetched */
  isJobsLoading: boolean;

  /** Whether a single job detail is being fetched */
  isJobDetailLoading: boolean;

  /** Whether a new generation job is being created */
  isCreatingJob: boolean;

  /** Whether a job cancellation request is in flight */
  isCancellingJob: boolean;

  /** Whether a job output file is being downloaded */
  isDownloading: boolean;

  // ---------------------------------------------------------------------------
  // Error states
  // ---------------------------------------------------------------------------

  /** Error from the last fetchJobs call, or null if successful */
  jobsError: string | null;

  /** Error from the last fetchJobById call, or null if successful */
  jobDetailError: string | null;

  /** Error from the last createJob call, or null if successful */
  createError: string | null;

  /** Error from the last cancelJob call, or null if successful */
  cancelError: string | null;

  /** Error from the last downloadOutput call, or null if successful */
  downloadError: string | null;

  // ---------------------------------------------------------------------------
  // Job list actions
  // ---------------------------------------------------------------------------

  /**
   * Fetches a paginated list of generation jobs with optional filtering.
   *
   * Supports filtering by status, generation method, free-text search, and
   * date range. Results are paginated using page/page_size from PaginationParams
   * with optional sort_by and sort_direction controls.
   *
   * @param params - Combined pagination and filter parameters
   */
  fetchJobs: (
    params?: PaginationParams & {
      status?: JobStatus;
      method?: string;
      search?: string;
      date_from?: string;
      date_to?: string;
    }
  ) => Promise<void>;

  /**
   * Fetches full details for a single generation job by ID.
   *
   * Updates both the selectedJob state and the corresponding entry in the
   * jobs array (if present) to keep list and detail views in sync.
   *
   * @param jobId - Unique identifier of the generation job (UUID)
   */
  fetchJobById: (jobId: string) => Promise<void>;

  // ---------------------------------------------------------------------------
  // Job lifecycle actions
  // ---------------------------------------------------------------------------

  /**
   * Creates a new generation job with the given configuration.
   *
   * On success, prepends the new job to the jobs array and sets it as
   * the selectedJob for immediate navigation to the monitoring view.
   *
   * @param config - Complete job configuration from the Generation Wizard
   * @returns The created GenerationJob on success, or null on failure
   */
  createJob: (config: JobConfig) => Promise<GenerationJob | null>;

  /**
   * Cancels an in-progress generation job.
   *
   * Updates the job in both the jobs array and selectedJob state. If the
   * cancelled job is currently being polled, polling is automatically stopped.
   *
   * @param jobId - Unique identifier of the job to cancel (UUID)
   * @returns true on successful cancellation, false on failure
   */
  cancelJob: (jobId: string) => Promise<boolean>;

  /**
   * Retries a previously failed generation job.
   *
   * Creates a new job using the same configuration as the failed job. The
   * new job is prepended to the jobs array with a fresh job_id and
   * 'submitted' status.
   *
   * @param jobId - Unique identifier of the failed job to retry (UUID)
   * @returns The newly created retry job on success, or null on failure
   */
  retryJob: (jobId: string) => Promise<GenerationJob | null>;

  /**
   * Downloads the generated output for a completed job as a binary blob.
   *
   * Supports optional format override (sql, csv, json, parquet). Uses an
   * extended timeout to accommodate large dataset downloads.
   *
   * @param jobId - Unique identifier of the completed job (UUID)
   * @param format - Optional output format override
   * @returns A Blob containing the generated data file, or null on failure
   */
  downloadOutput: (jobId: string, format?: string) => Promise<Blob | null>;

  // ---------------------------------------------------------------------------
  // Progress tracking actions
  // ---------------------------------------------------------------------------

  /**
   * Fetches the real-time batch progress snapshot for a generation job.
   *
   * Updates activeJobProgress with current_batch, total_batches,
   * records_generated, percentage, and estimated_remaining_seconds.
   * Errors are logged but non-blocking (used in polling loops).
   *
   * @param jobId - Unique identifier of the generation job (UUID)
   */
  fetchJobProgress: (jobId: string) => Promise<void>;

  /**
   * Fetches the quality validation report for a generation job.
   *
   * Updates activeJobQualityReport with overall_score, statistical_score,
   * business_rules_score, referential_integrity_score, details, and
   * validated_at. Only available for jobs past the validation stage.
   *
   * @param jobId - Unique identifier of the generation job (UUID)
   */
  fetchJobQualityReport: (jobId: string) => Promise<void>;

  /**
   * Starts interval-based polling for a generation job's status and progress.
   *
   * Creates a repeating interval that fetches the latest job details and
   * batch progress. Automatically stops polling and fetches the quality
   * report when the job reaches 'completed' or 'failed' status.
   *
   * If polling is already active for another job, the existing interval
   * is cleared before starting the new one.
   *
   * @param jobId - Unique identifier of the job to poll (UUID)
   * @param intervalMs - Polling interval in milliseconds (default: 2000)
   */
  startPolling: (jobId: string, intervalMs?: number) => void;

  /**
   * Stops the active polling interval and clears polling state.
   *
   * Safe to call even when no polling is active (no-op in that case).
   */
  stopPolling: () => void;

  // ---------------------------------------------------------------------------
  // State management
  // ---------------------------------------------------------------------------

  /**
   * Sets the currently selected job for detail view.
   *
   * Also clears activeJobProgress and activeJobQualityReport to prevent
   * stale data from a previously selected job being displayed.
   *
   * @param job - The job to select, or null to deselect
   */
  setSelectedJob: (job: GenerationJob | null) => void;

  /**
   * Resets all error states to null.
   *
   * Useful for clearing error banners when navigating between views or
   * before retrying a failed operation.
   */
  clearErrors: () => void;

  /**
   * Resets the entire store to its initial state.
   *
   * Stops any active polling interval before resetting. Used during
   * logout or when the user navigates away from job-related views.
   */
  reset: () => void;
}

// ============================================================================
// Initial State
// ============================================================================

/**
 * Default values for all state fields. Used by the store constructor and
 * the reset() action to restore the store to a clean state.
 */
const initialState: Pick<
  JobState,
  | 'jobs'
  | 'jobsTotal'
  | 'jobsPage'
  | 'jobsPageSize'
  | 'selectedJob'
  | 'activeJobProgress'
  | 'activeJobQualityReport'
  | 'pollingJobId'
  | 'pollingIntervalId'
  | 'isJobsLoading'
  | 'isJobDetailLoading'
  | 'isCreatingJob'
  | 'isCancellingJob'
  | 'isDownloading'
  | 'jobsError'
  | 'jobDetailError'
  | 'createError'
  | 'cancelError'
  | 'downloadError'
> = {
  jobs: [],
  jobsTotal: 0,
  jobsPage: 1,
  jobsPageSize: 20,
  selectedJob: null,
  activeJobProgress: null,
  activeJobQualityReport: null,
  pollingJobId: null,
  pollingIntervalId: null,
  isJobsLoading: false,
  isJobDetailLoading: false,
  isCreatingJob: false,
  isCancellingJob: false,
  isDownloading: false,
  jobsError: null,
  jobDetailError: null,
  createError: null,
  cancelError: null,
  downloadError: null,
};

// ============================================================================
// Helper — Extract Error Message
// ============================================================================

/**
 * Extracts a human-readable error message from an unknown caught value.
 *
 * Handles both standard Error objects (including Axios errors which extend
 * Error) and non-Error throwables by falling back to a provided default.
 *
 * @param error - The caught error value (typed as `unknown` per strict TS)
 * @param fallback - Default message when the error is not an Error instance
 * @returns A string suitable for display in the UI error state
 */
function extractErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error) {
    return error.message;
  }
  return fallback;
}

// ============================================================================
// Store Creation
// ============================================================================

/**
 * Zustand store for generation job state management.
 *
 * Provides a centralized, reactive state container for all generation job
 * operations consumed by Dashboard (S-001), GenerationWizard (S-002),
 * TemplateLibrary (S-003), and JobMonitoring (S-004) pages.
 *
 * Uses plain `create()` without persistence middleware — job state is
 * transient and refreshed from the API on each session.
 *
 * @example
 * ```tsx
 * // In a React component
 * const { jobs, isJobsLoading, fetchJobs } = useJobStore();
 *
 * useEffect(() => {
 *   fetchJobs({ page: 1, page_size: 20, sort_by: 'created_at', sort_direction: 'desc' });
 * }, [fetchJobs]);
 * ```
 */
export const useJobStore = create<JobState>()((set, get) => ({
  // Spread initial state values for all non-action fields
  ...initialState,

  // ===========================================================================
  // Job List Actions
  // ===========================================================================

  fetchJobs: async (params): Promise<void> => {
    set({ isJobsLoading: true, jobsError: null });
    try {
      // Cast to PaginationParams & JobFilterParams to satisfy the getJobs
      // function signature, which requires the FilterParams index signature.
      // The inline type in fetchJobs is structurally compatible but lacks the
      // index signature declaration required by FilterParams extends.
      const response = await getJobs(
        params as (PaginationParams & JobFilterParams) | undefined
      );
      set({
        jobs: response.data.items,
        jobsTotal: response.data.total,
        jobsPage: response.data.page,
        jobsPageSize: response.data.page_size,
      });
    } catch (error: unknown) {
      set({
        jobsError: extractErrorMessage(error, 'Failed to fetch jobs'),
      });
    } finally {
      set({ isJobsLoading: false });
    }
  },

  fetchJobById: async (jobId): Promise<void> => {
    set({ isJobDetailLoading: true, jobDetailError: null });
    try {
      const response = await getJobById(jobId);
      const job = response.data;
      set((state) => ({
        selectedJob: job,
        // Keep the jobs array in sync if this job is already in the list
        jobs: state.jobs.map((existing) =>
          existing.job_id === jobId ? job : existing
        ),
      }));
    } catch (error: unknown) {
      set({
        jobDetailError: extractErrorMessage(
          error,
          'Failed to fetch job details'
        ),
      });
    } finally {
      set({ isJobDetailLoading: false });
    }
  },

  // ===========================================================================
  // Job Lifecycle Actions
  // ===========================================================================

  createJob: async (config): Promise<GenerationJob | null> => {
    set({ isCreatingJob: true, createError: null });
    try {
      const response = await createJobApi(config);
      const newJob = response.data;
      set((state) => ({
        jobs: [newJob, ...state.jobs],
        selectedJob: newJob,
      }));
      return newJob;
    } catch (error: unknown) {
      set({
        createError: extractErrorMessage(
          error,
          'Failed to create generation job'
        ),
      });
      return null;
    } finally {
      set({ isCreatingJob: false });
    }
  },

  cancelJob: async (jobId): Promise<boolean> => {
    set({ isCancellingJob: true, cancelError: null });
    try {
      const response = await cancelJobApi(jobId);
      const cancelledJob = response.data;

      set((state) => ({
        // Update the job in the list to reflect its cancelled status
        jobs: state.jobs.map((job) =>
          job.job_id === jobId ? cancelledJob : job
        ),
        // Update selectedJob if the cancelled job is currently selected
        selectedJob:
          state.selectedJob?.job_id === jobId
            ? cancelledJob
            : state.selectedJob,
      }));

      // Stop polling if we are currently polling this job
      const { pollingJobId } = get();
      if (pollingJobId === jobId) {
        get().stopPolling();
      }

      return true;
    } catch (error: unknown) {
      set({
        cancelError: extractErrorMessage(error, 'Failed to cancel job'),
      });
      return false;
    } finally {
      set({ isCancellingJob: false });
    }
  },

  retryJob: async (jobId): Promise<GenerationJob | null> => {
    try {
      const response = await retryJobApi(jobId);
      const retriedJob = response.data;
      set((state) => ({
        jobs: [retriedJob, ...state.jobs],
      }));
      return retriedJob;
    } catch (error: unknown) {
      set({
        jobDetailError: extractErrorMessage(error, 'Failed to retry job'),
      });
      return null;
    }
  },

  downloadOutput: async (jobId, format): Promise<Blob | null> => {
    set({ isDownloading: true, downloadError: null });
    try {
      const blob = await downloadJobOutput(jobId, format);
      return blob;
    } catch (error: unknown) {
      set({
        downloadError: extractErrorMessage(
          error,
          'Failed to download output'
        ),
      });
      return null;
    } finally {
      set({ isDownloading: false });
    }
  },

  // ===========================================================================
  // Progress Tracking Actions
  // ===========================================================================

  fetchJobProgress: async (jobId): Promise<void> => {
    try {
      const response = await getJobProgress(jobId);
      set({ activeJobProgress: response.data });
    } catch (error: unknown) {
      // Non-blocking for polling — progress fetch failures should not
      // disrupt the polling loop or display error banners to the user
      console.error('Failed to fetch job progress:', error);
    }
  },

  fetchJobQualityReport: async (jobId): Promise<void> => {
    try {
      const response = await getJobQualityReport(jobId);
      set({ activeJobQualityReport: response.data });
    } catch (error: unknown) {
      // Non-blocking — quality reports may not be available until the
      // job has passed the validation stage
      console.error('Failed to fetch quality report:', error);
    }
  },

  startPolling: (jobId, intervalMs = 2000): void => {
    // If already polling, stop the existing interval first to prevent leaks
    const { pollingIntervalId: existingIntervalId } = get();
    if (existingIntervalId !== null) {
      clearInterval(existingIntervalId);
    }

    // Set the polling job ID immediately; interval ID is set once created
    set({ pollingJobId: jobId, pollingIntervalId: null });

    const intervalId = setInterval(async () => {
      try {
        // Fetch the latest job status and batch progress in parallel-safe
        // sequence (fetchJobById updates selectedJob which is read below)
        await get().fetchJobById(jobId);
        await get().fetchJobProgress(jobId);

        // Check if the job has reached a terminal state
        const { selectedJob } = get();
        if (selectedJob && selectedJob.job_id === jobId) {
          const { status } = selectedJob;
          if (status === 'completed' || status === 'failed') {
            // Auto-stop polling on terminal status
            get().stopPolling();

            // Automatically fetch the quality report for completed jobs
            if (status === 'completed') {
              await get().fetchJobQualityReport(jobId);
            }
          }
        }
      } catch {
        // Non-blocking for polling — individual fetch errors are handled
        // within their respective action methods. The polling loop continues
        // to retry on the next interval tick.
      }
    }, intervalMs);

    // Store the interval handle so it can be cleared later
    set({ pollingIntervalId: intervalId });
  },

  stopPolling: (): void => {
    const { pollingIntervalId } = get();
    if (pollingIntervalId !== null) {
      clearInterval(pollingIntervalId);
    }
    set({ pollingJobId: null, pollingIntervalId: null });
  },

  // ===========================================================================
  // State Management
  // ===========================================================================

  setSelectedJob: (job): void => {
    set({
      selectedJob: job,
      // Clear stale progress and quality data from the previously selected job
      activeJobProgress: null,
      activeJobQualityReport: null,
    });
  },

  clearErrors: (): void => {
    set({
      jobsError: null,
      jobDetailError: null,
      createError: null,
      cancelError: null,
      downloadError: null,
    });
  },

  reset: (): void => {
    // Ensure any active polling interval is cleaned up before resetting
    const { pollingIntervalId } = get();
    if (pollingIntervalId !== null) {
      clearInterval(pollingIntervalId);
    }
    set(initialState);
  },
}));

// ============================================================================
// Exports
// ============================================================================

export default useJobStore;
