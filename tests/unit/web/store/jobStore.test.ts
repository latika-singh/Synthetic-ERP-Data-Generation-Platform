/**
 * @fileoverview Vitest unit tests for the jobStore Zustand store.
 *
 * Tests all generation job lifecycle state management including: paginated
 * job listing (fetchJobs), single job detail retrieval (fetchJobById),
 * job creation from wizard configuration (createJob), job cancellation
 * with auto-stop polling (cancelJob), retrying failed jobs (retryJob),
 * binary output download (downloadOutput), batch progress tracking
 * (fetchJobProgress), quality report retrieval (fetchJobQualityReport),
 * interval-based progress polling with auto-stop on terminal status
 * (startPolling/stopPolling), selected job management (setSelectedJob),
 * and state cleanup (clearErrors, reset).
 *
 * All generationApi service calls are globally mocked via vi.mock() to
 * isolate the store logic from HTTP communication. vi.useFakeTimers()
 * controls setInterval-based polling tests. State is accessed directly
 * via useJobStore.getState() since the store is pure Zustand (no React).
 *
 * @module tests/unit/web/store/jobStore.test
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { useJobStore } from '@/store/jobStore';
import type {
  GenerationJob,
  JobConfig,
  BatchProgress,
} from '@/types/generation';
import {
  GenerationMethod,
  JobStatus,
  OutputFormat,
} from '@/types/generation';
import type { ApiResponse, PaginatedResult } from '@/types/api';
import type { QualityReport } from '@/services/generationApi';
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

// ---------------------------------------------------------------------------
// Global Module Mock
// ---------------------------------------------------------------------------

/**
 * Globally mock the generationApi module so all 8 exported functions are
 * replaced with vi.fn() instances. This ensures the jobStore's internal
 * calls to these functions are intercepted without real HTTP traffic.
 *
 * vi.mock() is hoisted by Vitest to the top of the file, so this runs
 * before any imports are resolved.
 */
vi.mock('@/services/generationApi');

// Typed references to the mocked functions for per-test configuration
const mockedCreateJob = vi.mocked(createJobApi);
const mockedGetJobs = vi.mocked(getJobs);
const mockedGetJobById = vi.mocked(getJobById);
const mockedCancelJob = vi.mocked(cancelJobApi);
const mockedGetJobProgress = vi.mocked(getJobProgress);
const mockedGetJobQualityReport = vi.mocked(getJobQualityReport);
const mockedDownloadJobOutput = vi.mocked(downloadJobOutput);
const mockedRetryJob = vi.mocked(retryJobApi);

// ---------------------------------------------------------------------------
// Test Fixtures
// ---------------------------------------------------------------------------

/** Helper to wrap data in the standard ApiResponse envelope. */
function makeApiResponse<T>(data: T): ApiResponse<T> {
  return {
    success: true,
    data,
    timestamp: '2025-01-15T10:00:00.000Z',
  };
}

/** Active/generating generation job fixture. */
const mockJob: GenerationJob = {
  job_id: 'job-001',
  status: JobStatus.GENERATING,
  method: GenerationMethod.STATISTICAL,
  progress: 45,
  total_records: 100000,
  generated_records: 45000,
  quality_score: undefined,
  output_location: undefined,
  error_message: undefined,
  created_at: '2025-01-15T09:00:00.000Z',
  updated_at: '2025-01-15T09:30:00.000Z',
  completed_at: undefined,
  tenant_id: 'tenant-1',
};

/** Completed generation job fixture with quality score and output. */
const mockCompletedJob: GenerationJob = {
  job_id: 'job-002',
  status: JobStatus.COMPLETED,
  method: GenerationMethod.AI_ML,
  progress: 100,
  total_records: 50000,
  generated_records: 50000,
  quality_score: 0.97,
  output_location: 's3://bucket/output/job-002.csv',
  error_message: undefined,
  created_at: '2025-01-14T08:00:00.000Z',
  updated_at: '2025-01-14T10:00:00.000Z',
  completed_at: '2025-01-14T10:00:00.000Z',
  tenant_id: 'tenant-1',
};

/** Failed generation job fixture with error message. */
const mockFailedJob: GenerationJob = {
  job_id: 'job-003',
  status: JobStatus.FAILED,
  method: GenerationMethod.RULES_BASED,
  progress: 30,
  total_records: 200000,
  generated_records: 60000,
  quality_score: undefined,
  output_location: undefined,
  error_message:
    'Referential integrity constraint violation in GL_JOURNAL_ENTRIES',
  created_at: '2025-01-13T12:00:00.000Z',
  updated_at: '2025-01-13T12:45:00.000Z',
  completed_at: undefined,
  tenant_id: 'tenant-1',
};

/** Job configuration fixture for createJob tests. */
const mockJobConfig: JobConfig = {
  method: GenerationMethod.STATISTICAL,
  schema_id: 'schema-abc-123',
  tables: [
    {
      table_name: 'GL_JOURNAL_ENTRIES',
      record_count: 50000,
      preserve_relationships: true,
    },
    {
      table_name: 'AP_INVOICES',
      record_count: 25000,
      preserve_relationships: true,
    },
  ],
  output_format: OutputFormat.CSV,
  batch_size: 10000,
  quality_threshold: 0.95,
};

/** Batch progress fixture for progress tracking tests. */
const mockBatchProgress: BatchProgress = {
  current_batch: 5,
  total_batches: 10,
  records_generated: 45000,
  records_total: 100000,
  percentage: 45,
  estimated_remaining_seconds: 120,
};

/** Quality validation report fixture. */
const mockQualityReport: QualityReport = {
  overall_score: 0.97,
  statistical_score: 0.98,
  business_rules_score: 0.96,
  referential_integrity_score: 0.95,
  details: {
    tables_validated: 5,
    columns_checked: 42,
    violations: [],
  },
  validated_at: '2025-01-15T10:30:00.000Z',
};

/** Paginated jobs list fixture for fetchJobs tests. */
const mockPaginatedJobs: PaginatedResult<GenerationJob> = {
  items: [mockJob, mockCompletedJob, mockFailedJob],
  total: 3,
  page: 1,
  page_size: 20,
  total_pages: 1,
};

// ============================================================================
// Test Suite
// ============================================================================

describe('jobStore', () => {
  // -------------------------------------------------------------------------
  // Setup / Teardown
  // -------------------------------------------------------------------------

  beforeEach(() => {
    // Reset the Zustand store to initial state before each test
    useJobStore.getState().reset();
    // Clear all mock call history and implementations
    vi.clearAllMocks();
    // Enable fake timers for polling interval tests
    vi.useFakeTimers();
  });

  afterEach(() => {
    // Ensure any active polling is stopped to prevent interval leaks
    useJobStore.getState().stopPolling();
    // Restore real timers after each test
    vi.useRealTimers();
    // Restore all mocks to their original implementations
    vi.restoreAllMocks();
  });

  // =========================================================================
  // 1. Initial State
  // =========================================================================

  describe('Initial State', () => {
    it('should have empty jobs list with default pagination', () => {
      const state = useJobStore.getState();
      expect(state.jobs).toEqual([]);
      expect(state.jobsTotal).toBe(0);
      expect(state.jobsPage).toBe(1);
      expect(state.jobsPageSize).toBe(20);
    });

    it('should have null selected job and no active progress or quality report', () => {
      const state = useJobStore.getState();
      expect(state.selectedJob).toBeNull();
      expect(state.activeJobProgress).toBeNull();
      expect(state.activeJobQualityReport).toBeNull();
    });

    it('should have no active polling', () => {
      const state = useJobStore.getState();
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
    });

    it('should have all loading states set to false', () => {
      const state = useJobStore.getState();
      expect(state.isJobsLoading).toBe(false);
      expect(state.isJobDetailLoading).toBe(false);
      expect(state.isCreatingJob).toBe(false);
      expect(state.isCancellingJob).toBe(false);
      expect(state.isDownloading).toBe(false);
    });

    it('should have all error states set to null', () => {
      const state = useJobStore.getState();
      expect(state.jobsError).toBeNull();
      expect(state.jobDetailError).toBeNull();
      expect(state.createError).toBeNull();
      expect(state.cancelError).toBeNull();
      expect(state.downloadError).toBeNull();
    });
  });

  // =========================================================================
  // 2. fetchJobs
  // =========================================================================

  describe('fetchJobs', () => {
    it('should fetch paginated jobs and update list state', async () => {
      mockedGetJobs.mockResolvedValueOnce(
        makeApiResponse(mockPaginatedJobs),
      );

      await useJobStore.getState().fetchJobs();

      const state = useJobStore.getState();
      expect(state.jobs).toEqual(mockPaginatedJobs.items);
      expect(state.jobsTotal).toBe(3);
      expect(state.jobsPage).toBe(1);
      expect(state.jobsPageSize).toBe(20);
      expect(state.isJobsLoading).toBe(false);
      expect(state.jobsError).toBeNull();
    });

    it('should set isJobsLoading to true during fetch and false after', async () => {
      let capturedLoadingState = false;
      mockedGetJobs.mockImplementationOnce(() => {
        // Capture loading state while the promise is pending
        capturedLoadingState = useJobStore.getState().isJobsLoading;
        return Promise.resolve(makeApiResponse(mockPaginatedJobs));
      });

      await useJobStore.getState().fetchJobs();

      expect(capturedLoadingState).toBe(true);
      expect(useJobStore.getState().isJobsLoading).toBe(false);
    });

    it('should clear jobsError before fetching', async () => {
      // Seed an initial error to verify it gets cleared
      useJobStore.setState({ jobsError: 'Previous error' });
      mockedGetJobs.mockResolvedValueOnce(
        makeApiResponse(mockPaginatedJobs),
      );

      await useJobStore.getState().fetchJobs();

      expect(useJobStore.getState().jobsError).toBeNull();
    });

    it('should set jobsError on API failure', async () => {
      mockedGetJobs.mockRejectedValueOnce(new Error('Network timeout'));

      await useJobStore.getState().fetchJobs();

      const state = useJobStore.getState();
      expect(state.jobsError).toBe('Network timeout');
      expect(state.jobs).toEqual([]);
      expect(state.isJobsLoading).toBe(false);
    });

    it('should set fallback error message for non-Error rejections', async () => {
      mockedGetJobs.mockRejectedValueOnce('string error');

      await useJobStore.getState().fetchJobs();

      expect(useJobStore.getState().jobsError).toBe('Failed to fetch jobs');
    });

    it('should pass pagination and filter parameters to getJobs', async () => {
      mockedGetJobs.mockResolvedValueOnce(
        makeApiResponse(mockPaginatedJobs),
      );
      const params = { page: 2, page_size: 10, status: 'completed' as const };

      await useJobStore.getState().fetchJobs(params);

      expect(mockedGetJobs).toHaveBeenCalledWith(params);
    });
  });

  // =========================================================================
  // 3. fetchJobById
  // =========================================================================

  describe('fetchJobById', () => {
    it('should fetch job details and set selectedJob', async () => {
      mockedGetJobById.mockResolvedValueOnce(makeApiResponse(mockJob));

      await useJobStore.getState().fetchJobById('job-001');

      const state = useJobStore.getState();
      expect(state.selectedJob).toEqual(mockJob);
      expect(state.isJobDetailLoading).toBe(false);
      expect(state.jobDetailError).toBeNull();
    });

    it('should update existing job in the jobs array when fetching by ID', async () => {
      // Pre-populate jobs list with an outdated version of the job
      const outdatedJob: GenerationJob = {
        ...mockJob,
        progress: 20,
        generated_records: 20000,
      };
      useJobStore.setState({ jobs: [outdatedJob, mockCompletedJob] });

      const updatedJob: GenerationJob = {
        ...mockJob,
        progress: 60,
        generated_records: 60000,
      };
      mockedGetJobById.mockResolvedValueOnce(makeApiResponse(updatedJob));

      await useJobStore.getState().fetchJobById('job-001');

      const state = useJobStore.getState();
      expect(state.selectedJob).toEqual(updatedJob);
      // First item in the array should be the updated version
      expect(state.jobs[0]).toEqual(updatedJob);
      // Other jobs in the array remain untouched
      expect(state.jobs[1]).toEqual(mockCompletedJob);
    });

    it('should not modify jobs array when fetched job is not in the list', async () => {
      useJobStore.setState({ jobs: [mockCompletedJob] });
      mockedGetJobById.mockResolvedValueOnce(makeApiResponse(mockJob));

      await useJobStore.getState().fetchJobById('job-001');

      const state = useJobStore.getState();
      expect(state.selectedJob).toEqual(mockJob);
      // Jobs array should remain unchanged — job-001 was not in the list
      expect(state.jobs).toHaveLength(1);
      expect(state.jobs[0]).toEqual(mockCompletedJob);
    });

    it('should set isJobDetailLoading during fetch', async () => {
      let capturedLoading = false;
      mockedGetJobById.mockImplementationOnce(() => {
        capturedLoading = useJobStore.getState().isJobDetailLoading;
        return Promise.resolve(makeApiResponse(mockJob));
      });

      await useJobStore.getState().fetchJobById('job-001');

      expect(capturedLoading).toBe(true);
      expect(useJobStore.getState().isJobDetailLoading).toBe(false);
    });

    it('should set jobDetailError on API failure', async () => {
      mockedGetJobById.mockRejectedValueOnce(new Error('Job not found'));

      await useJobStore.getState().fetchJobById('nonexistent');

      const state = useJobStore.getState();
      expect(state.jobDetailError).toBe('Job not found');
      expect(state.selectedJob).toBeNull();
      expect(state.isJobDetailLoading).toBe(false);
    });

    it('should set fallback error for non-Error rejections', async () => {
      mockedGetJobById.mockRejectedValueOnce(42);

      await useJobStore.getState().fetchJobById('job-001');

      expect(useJobStore.getState().jobDetailError).toBe(
        'Failed to fetch job details',
      );
    });
  });

  // =========================================================================
  // 4. createJob
  // =========================================================================

  describe('createJob', () => {
    it('should create a job and prepend it to the jobs list', async () => {
      useJobStore.setState({ jobs: [mockCompletedJob] });

      const newJob: GenerationJob = {
        ...mockJob,
        job_id: 'job-new',
        status: JobStatus.SUBMITTED,
        progress: 0,
        generated_records: 0,
      };
      mockedCreateJob.mockResolvedValueOnce(makeApiResponse(newJob));

      const result = await useJobStore.getState().createJob(mockJobConfig);

      expect(result).toEqual(newJob);
      const state = useJobStore.getState();
      // New job should be at the front of the list
      expect(state.jobs[0]).toEqual(newJob);
      expect(state.jobs[1]).toEqual(mockCompletedJob);
      expect(state.selectedJob).toEqual(newJob);
    });

    it('should pass the job configuration to the API', async () => {
      mockedCreateJob.mockResolvedValueOnce(makeApiResponse(mockJob));

      await useJobStore.getState().createJob(mockJobConfig);

      expect(mockedCreateJob).toHaveBeenCalledWith(mockJobConfig);
    });

    it('should set isCreatingJob during creation', async () => {
      let capturedCreating = false;
      mockedCreateJob.mockImplementationOnce(() => {
        capturedCreating = useJobStore.getState().isCreatingJob;
        return Promise.resolve(makeApiResponse(mockJob));
      });

      await useJobStore.getState().createJob(mockJobConfig);

      expect(capturedCreating).toBe(true);
      expect(useJobStore.getState().isCreatingJob).toBe(false);
    });

    it('should set createError and return null on API failure', async () => {
      mockedCreateJob.mockRejectedValueOnce(
        new Error('Validation failed: invalid schema_id'),
      );

      const result = await useJobStore.getState().createJob(mockJobConfig);

      expect(result).toBeNull();
      expect(useJobStore.getState().createError).toBe(
        'Validation failed: invalid schema_id',
      );
      expect(useJobStore.getState().isCreatingJob).toBe(false);
    });

    it('should set fallback error message for non-Error rejections', async () => {
      mockedCreateJob.mockRejectedValueOnce(undefined);

      const result = await useJobStore.getState().createJob(mockJobConfig);

      expect(result).toBeNull();
      expect(useJobStore.getState().createError).toBe(
        'Failed to create generation job',
      );
    });

    it('should clear createError before creation attempt', async () => {
      useJobStore.setState({ createError: 'Previous creation error' });
      mockedCreateJob.mockResolvedValueOnce(makeApiResponse(mockJob));

      await useJobStore.getState().createJob(mockJobConfig);

      expect(useJobStore.getState().createError).toBeNull();
    });
  });

  // =========================================================================
  // 5. cancelJob
  // =========================================================================

  describe('cancelJob', () => {
    it('should cancel a job and update it in the jobs list', async () => {
      useJobStore.setState({ jobs: [mockJob, mockCompletedJob] });

      const cancelledJob: GenerationJob = {
        ...mockJob,
        status: JobStatus.FAILED,
        error_message: 'Cancelled by user',
      };
      mockedCancelJob.mockResolvedValueOnce(makeApiResponse(cancelledJob));

      const result = await useJobStore.getState().cancelJob('job-001');

      expect(result).toBe(true);
      const state = useJobStore.getState();
      expect(state.jobs[0]).toEqual(cancelledJob);
      expect(state.jobs[1]).toEqual(mockCompletedJob);
    });

    it('should update selectedJob when the cancelled job is currently selected', async () => {
      useJobStore.setState({
        jobs: [mockJob],
        selectedJob: mockJob,
      });

      const cancelledJob: GenerationJob = {
        ...mockJob,
        status: JobStatus.FAILED,
        error_message: 'Cancelled by user',
      };
      mockedCancelJob.mockResolvedValueOnce(makeApiResponse(cancelledJob));

      await useJobStore.getState().cancelJob('job-001');

      expect(useJobStore.getState().selectedJob).toEqual(cancelledJob);
    });

    it('should not update selectedJob when a different job is selected', async () => {
      useJobStore.setState({
        jobs: [mockJob],
        selectedJob: mockCompletedJob,
      });

      const cancelledJob: GenerationJob = {
        ...mockJob,
        status: JobStatus.FAILED,
      };
      mockedCancelJob.mockResolvedValueOnce(makeApiResponse(cancelledJob));

      await useJobStore.getState().cancelJob('job-001');

      // selectedJob should remain as the completed job, not the cancelled one
      expect(useJobStore.getState().selectedJob).toEqual(mockCompletedJob);
    });

    it('should auto-stop polling when the cancelled job is being polled', async () => {
      // Start real polling for the job that will be cancelled
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );
      useJobStore.setState({ jobs: [mockJob] });
      useJobStore.getState().startPolling('job-001', 5000);

      expect(useJobStore.getState().pollingJobId).toBe('job-001');

      const cancelledJob: GenerationJob = {
        ...mockJob,
        status: JobStatus.FAILED,
      };
      mockedCancelJob.mockResolvedValueOnce(makeApiResponse(cancelledJob));

      await useJobStore.getState().cancelJob('job-001');

      const state = useJobStore.getState();
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
    });

    it('should set isCancellingJob during cancellation', async () => {
      let capturedCancelling = false;
      mockedCancelJob.mockImplementationOnce(() => {
        capturedCancelling = useJobStore.getState().isCancellingJob;
        return Promise.resolve(
          makeApiResponse({
            ...mockJob,
            status: JobStatus.FAILED,
          } as GenerationJob),
        );
      });

      await useJobStore.getState().cancelJob('job-001');

      expect(capturedCancelling).toBe(true);
      expect(useJobStore.getState().isCancellingJob).toBe(false);
    });

    it('should set cancelError and return false on API failure', async () => {
      mockedCancelJob.mockRejectedValueOnce(
        new Error('Cannot cancel completed job'),
      );

      const result = await useJobStore.getState().cancelJob('job-002');

      expect(result).toBe(false);
      expect(useJobStore.getState().cancelError).toBe(
        'Cannot cancel completed job',
      );
      expect(useJobStore.getState().isCancellingJob).toBe(false);
    });

    it('should set fallback error for non-Error rejections', async () => {
      mockedCancelJob.mockRejectedValueOnce(null);

      const result = await useJobStore.getState().cancelJob('job-001');

      expect(result).toBe(false);
      expect(useJobStore.getState().cancelError).toBe(
        'Failed to cancel job',
      );
    });
  });

  // =========================================================================
  // 6. retryJob
  // =========================================================================

  describe('retryJob', () => {
    it('should retry a failed job and prepend the new job to the list', async () => {
      useJobStore.setState({ jobs: [mockFailedJob] });

      const retriedJob: GenerationJob = {
        ...mockJob,
        job_id: 'job-004',
        status: JobStatus.SUBMITTED,
        progress: 0,
        generated_records: 0,
      };
      mockedRetryJob.mockResolvedValueOnce(makeApiResponse(retriedJob));

      const result = await useJobStore.getState().retryJob('job-003');

      expect(result).toEqual(retriedJob);
      const state = useJobStore.getState();
      // Retried job is prepended to the array
      expect(state.jobs[0]).toEqual(retriedJob);
      expect(state.jobs[1]).toEqual(mockFailedJob);
    });

    it('should call retryJob API with the correct job ID', async () => {
      mockedRetryJob.mockResolvedValueOnce(
        makeApiResponse({
          ...mockJob,
          job_id: 'job-retry',
          status: JobStatus.SUBMITTED,
        } as GenerationJob),
      );

      await useJobStore.getState().retryJob('job-003');

      expect(mockedRetryJob).toHaveBeenCalledWith('job-003');
    });

    it('should set jobDetailError and return null on API failure', async () => {
      mockedRetryJob.mockRejectedValueOnce(
        new Error('Job not eligible for retry'),
      );

      const result = await useJobStore.getState().retryJob('job-003');

      expect(result).toBeNull();
      expect(useJobStore.getState().jobDetailError).toBe(
        'Job not eligible for retry',
      );
    });

    it('should set fallback error for non-Error rejections', async () => {
      mockedRetryJob.mockRejectedValueOnce({ code: 500 });

      const result = await useJobStore.getState().retryJob('job-003');

      expect(result).toBeNull();
      expect(useJobStore.getState().jobDetailError).toBe(
        'Failed to retry job',
      );
    });
  });

  // =========================================================================
  // 7. downloadOutput
  // =========================================================================

  describe('downloadOutput', () => {
    it('should download job output and return a Blob', async () => {
      const mockBlob = new Blob(['col1,col2\nval1,val2'], {
        type: 'text/csv',
      });
      mockedDownloadJobOutput.mockResolvedValueOnce(mockBlob);

      const result = await useJobStore
        .getState()
        .downloadOutput('job-002');

      expect(result).toEqual(mockBlob);
      expect(mockedDownloadJobOutput).toHaveBeenCalledWith(
        'job-002',
        undefined,
      );
    });

    it('should pass format parameter to the API', async () => {
      const mockBlob = new Blob(['{}'], { type: 'application/json' });
      mockedDownloadJobOutput.mockResolvedValueOnce(mockBlob);

      await useJobStore.getState().downloadOutput('job-002', 'json');

      expect(mockedDownloadJobOutput).toHaveBeenCalledWith(
        'job-002',
        'json',
      );
    });

    it('should set isDownloading during download', async () => {
      let capturedDownloading = false;
      mockedDownloadJobOutput.mockImplementationOnce(() => {
        capturedDownloading = useJobStore.getState().isDownloading;
        return Promise.resolve(new Blob(['data']));
      });

      await useJobStore.getState().downloadOutput('job-002');

      expect(capturedDownloading).toBe(true);
      expect(useJobStore.getState().isDownloading).toBe(false);
    });

    it('should set downloadError and return null on failure', async () => {
      mockedDownloadJobOutput.mockRejectedValueOnce(
        new Error('File not available'),
      );

      const result = await useJobStore
        .getState()
        .downloadOutput('job-002');

      expect(result).toBeNull();
      expect(useJobStore.getState().downloadError).toBe(
        'File not available',
      );
      expect(useJobStore.getState().isDownloading).toBe(false);
    });

    it('should set fallback error for non-Error rejections', async () => {
      mockedDownloadJobOutput.mockRejectedValueOnce('unexpected');

      const result = await useJobStore
        .getState()
        .downloadOutput('job-002');

      expect(result).toBeNull();
      expect(useJobStore.getState().downloadError).toBe(
        'Failed to download output',
      );
    });

    it('should clear downloadError before starting download', async () => {
      useJobStore.setState({ downloadError: 'Previous download error' });
      mockedDownloadJobOutput.mockResolvedValueOnce(new Blob(['data']));

      await useJobStore.getState().downloadOutput('job-002');

      expect(useJobStore.getState().downloadError).toBeNull();
    });
  });

  // =========================================================================
  // 8. fetchJobProgress
  // =========================================================================

  describe('fetchJobProgress', () => {
    it('should fetch and set active job progress', async () => {
      mockedGetJobProgress.mockResolvedValueOnce(
        makeApiResponse(mockBatchProgress),
      );

      await useJobStore.getState().fetchJobProgress('job-001');

      expect(useJobStore.getState().activeJobProgress).toEqual(
        mockBatchProgress,
      );
    });

    it('should call getJobProgress with the correct job ID', async () => {
      mockedGetJobProgress.mockResolvedValueOnce(
        makeApiResponse(mockBatchProgress),
      );

      await useJobStore.getState().fetchJobProgress('job-001');

      expect(mockedGetJobProgress).toHaveBeenCalledWith('job-001');
    });

    it('should not set error state on failure (non-blocking)', async () => {
      const consoleSpy = vi
        .spyOn(console, 'error')
        .mockImplementation(() => {});
      mockedGetJobProgress.mockRejectedValueOnce(
        new Error('Progress unavailable'),
      );

      await useJobStore.getState().fetchJobProgress('job-001');

      // Progress fetch errors are logged to console, not stored in state
      expect(useJobStore.getState().activeJobProgress).toBeNull();
      expect(consoleSpy).toHaveBeenCalledWith(
        'Failed to fetch job progress:',
        expect.any(Error),
      );
      consoleSpy.mockRestore();
    });
  });

  // =========================================================================
  // 9. fetchJobQualityReport
  // =========================================================================

  describe('fetchJobQualityReport', () => {
    it('should fetch and set the quality report', async () => {
      mockedGetJobQualityReport.mockResolvedValueOnce(
        makeApiResponse(mockQualityReport),
      );

      await useJobStore.getState().fetchJobQualityReport('job-002');

      expect(useJobStore.getState().activeJobQualityReport).toEqual(
        mockQualityReport,
      );
    });

    it('should call getJobQualityReport with the correct job ID', async () => {
      mockedGetJobQualityReport.mockResolvedValueOnce(
        makeApiResponse(mockQualityReport),
      );

      await useJobStore.getState().fetchJobQualityReport('job-002');

      expect(mockedGetJobQualityReport).toHaveBeenCalledWith('job-002');
    });

    it('should not set error state on failure (non-blocking)', async () => {
      const consoleSpy = vi
        .spyOn(console, 'error')
        .mockImplementation(() => {});
      mockedGetJobQualityReport.mockRejectedValueOnce(
        new Error('Report not ready'),
      );

      await useJobStore.getState().fetchJobQualityReport('job-001');

      expect(useJobStore.getState().activeJobQualityReport).toBeNull();
      expect(consoleSpy).toHaveBeenCalledWith(
        'Failed to fetch quality report:',
        expect.any(Error),
      );
      consoleSpy.mockRestore();
    });
  });

  // =========================================================================
  // 10. Polling (startPolling / stopPolling)
  // =========================================================================

  describe('Polling', () => {
    it('should set pollingJobId and pollingIntervalId when starting polling', () => {
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      useJobStore.getState().startPolling('job-001', 2000);

      const state = useJobStore.getState();
      expect(state.pollingJobId).toBe('job-001');
      expect(state.pollingIntervalId).not.toBeNull();
    });

    it('should call fetchJobById and fetchJobProgress on each interval tick', async () => {
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      useJobStore.getState().startPolling('job-001', 1000);

      // Advance past first interval tick and flush microtasks
      await vi.advanceTimersByTimeAsync(1000);

      expect(mockedGetJobById).toHaveBeenCalledWith('job-001');
      expect(mockedGetJobProgress).toHaveBeenCalledWith('job-001');
    });

    it('should call APIs multiple times across multiple ticks', async () => {
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      useJobStore.getState().startPolling('job-001', 1000);

      // Advance through 3 interval ticks
      await vi.advanceTimersByTimeAsync(1000);
      await vi.advanceTimersByTimeAsync(1000);
      await vi.advanceTimersByTimeAsync(1000);

      expect(mockedGetJobById).toHaveBeenCalledTimes(3);
      expect(mockedGetJobProgress).toHaveBeenCalledTimes(3);
    });

    it('should auto-stop polling when job reaches completed status', async () => {
      const completedJobResponse: GenerationJob = {
        ...mockJob,
        job_id: 'job-001',
        status: JobStatus.COMPLETED,
        progress: 100,
        generated_records: 100000,
        quality_score: 0.97,
      };
      mockedGetJobById.mockResolvedValue(
        makeApiResponse(completedJobResponse),
      );
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );
      mockedGetJobQualityReport.mockResolvedValue(
        makeApiResponse(mockQualityReport),
      );

      useJobStore.getState().startPolling('job-001', 1000);

      // Advance to trigger the interval callback
      await vi.advanceTimersByTimeAsync(1000);

      const state = useJobStore.getState();
      // Polling should have been stopped automatically
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
    });

    it('should fetch quality report when job completes during polling', async () => {
      const completedJob: GenerationJob = {
        ...mockJob,
        job_id: 'job-001',
        status: JobStatus.COMPLETED,
        progress: 100,
      };
      mockedGetJobById.mockResolvedValue(makeApiResponse(completedJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );
      mockedGetJobQualityReport.mockResolvedValue(
        makeApiResponse(mockQualityReport),
      );

      useJobStore.getState().startPolling('job-001', 1000);
      await vi.advanceTimersByTimeAsync(1000);

      expect(mockedGetJobQualityReport).toHaveBeenCalledWith('job-001');
      expect(useJobStore.getState().activeJobQualityReport).toEqual(
        mockQualityReport,
      );
    });

    it('should auto-stop polling on failed status without fetching quality report', async () => {
      const failedJob: GenerationJob = {
        ...mockJob,
        job_id: 'job-001',
        status: JobStatus.FAILED,
        error_message: 'Generation failed',
      };
      mockedGetJobById.mockResolvedValue(makeApiResponse(failedJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      useJobStore.getState().startPolling('job-001', 1000);
      await vi.advanceTimersByTimeAsync(1000);

      const state = useJobStore.getState();
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
      // Quality report should NOT be fetched for failed jobs
      expect(mockedGetJobQualityReport).not.toHaveBeenCalled();
    });

    it('should clear existing polling interval before starting a new one', () => {
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      // Start polling for first job
      useJobStore.getState().startPolling('job-001', 1000);
      const firstIntervalId = useJobStore.getState().pollingIntervalId;
      expect(firstIntervalId).not.toBeNull();

      // Start polling for a different job — should replace the previous one
      useJobStore.getState().startPolling('job-002', 1000);
      const secondIntervalId = useJobStore.getState().pollingIntervalId;

      expect(useJobStore.getState().pollingJobId).toBe('job-002');
      expect(secondIntervalId).not.toBeNull();
      // The interval IDs should differ (old one cleared, new one created)
      expect(secondIntervalId).not.toBe(firstIntervalId);
    });

    it('should stop polling and clear polling state', () => {
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      useJobStore.getState().startPolling('job-001', 1000);
      expect(useJobStore.getState().pollingJobId).toBe('job-001');

      useJobStore.getState().stopPolling();

      const state = useJobStore.getState();
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
    });

    it('should be safe to call stopPolling when no polling is active', () => {
      // Should not throw even when there's no active polling
      expect(() => useJobStore.getState().stopPolling()).not.toThrow();

      const state = useJobStore.getState();
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
    });

    it('should use default interval of 2000ms when not specified', async () => {
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      useJobStore.getState().startPolling('job-001');

      // Advance 1999ms — should NOT trigger interval callback
      await vi.advanceTimersByTimeAsync(1999);
      expect(mockedGetJobById).not.toHaveBeenCalled();

      // Advance 1 more ms to hit the 2000ms mark — should trigger
      await vi.advanceTimersByTimeAsync(1);
      expect(mockedGetJobById).toHaveBeenCalledTimes(1);
    });

    it('should continue polling when interval callbacks encounter errors', async () => {
      // First tick: transient error
      mockedGetJobById.mockRejectedValueOnce(
        new Error('Transient error'),
      );
      // Second tick: success with still-generating status
      mockedGetJobById.mockResolvedValueOnce(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      useJobStore.getState().startPolling('job-001', 1000);

      // First tick — error should be caught silently, polling continues
      await vi.advanceTimersByTimeAsync(1000);
      expect(useJobStore.getState().pollingJobId).toBe('job-001');

      // Second tick — should succeed normally
      await vi.advanceTimersByTimeAsync(1000);
      expect(mockedGetJobById).toHaveBeenCalledTimes(2);
    });
  });

  // =========================================================================
  // 11. setSelectedJob
  // =========================================================================

  describe('setSelectedJob', () => {
    it('should set the selected job', () => {
      useJobStore.getState().setSelectedJob(mockJob);

      expect(useJobStore.getState().selectedJob).toEqual(mockJob);
    });

    it('should clear activeJobProgress and activeJobQualityReport when setting a job', () => {
      useJobStore.setState({
        activeJobProgress: mockBatchProgress,
        activeJobQualityReport: mockQualityReport,
      });

      useJobStore.getState().setSelectedJob(mockCompletedJob);

      const state = useJobStore.getState();
      expect(state.selectedJob).toEqual(mockCompletedJob);
      expect(state.activeJobProgress).toBeNull();
      expect(state.activeJobQualityReport).toBeNull();
    });

    it('should clear selected job when passed null', () => {
      useJobStore.setState({
        selectedJob: mockJob,
        activeJobProgress: mockBatchProgress,
        activeJobQualityReport: mockQualityReport,
      });

      useJobStore.getState().setSelectedJob(null);

      const state = useJobStore.getState();
      expect(state.selectedJob).toBeNull();
      expect(state.activeJobProgress).toBeNull();
      expect(state.activeJobQualityReport).toBeNull();
    });
  });

  // =========================================================================
  // 12. clearErrors
  // =========================================================================

  describe('clearErrors', () => {
    it('should reset all error states to null', () => {
      useJobStore.setState({
        jobsError: 'Jobs fetch error',
        jobDetailError: 'Detail error',
        createError: 'Create error',
        cancelError: 'Cancel error',
        downloadError: 'Download error',
      });

      useJobStore.getState().clearErrors();

      const state = useJobStore.getState();
      expect(state.jobsError).toBeNull();
      expect(state.jobDetailError).toBeNull();
      expect(state.createError).toBeNull();
      expect(state.cancelError).toBeNull();
      expect(state.downloadError).toBeNull();
    });

    it('should be safe to call when no errors exist', () => {
      useJobStore.getState().clearErrors();

      const state = useJobStore.getState();
      expect(state.jobsError).toBeNull();
      expect(state.jobDetailError).toBeNull();
      expect(state.createError).toBeNull();
      expect(state.cancelError).toBeNull();
      expect(state.downloadError).toBeNull();
    });
  });

  // =========================================================================
  // 13. reset
  // =========================================================================

  describe('reset', () => {
    it('should reset all state to initial values', () => {
      // Set up dirty state across all fields
      useJobStore.setState({
        jobs: [mockJob, mockCompletedJob],
        jobsTotal: 2,
        jobsPage: 3,
        jobsPageSize: 50,
        selectedJob: mockJob,
        activeJobProgress: mockBatchProgress,
        activeJobQualityReport: mockQualityReport,
        isJobsLoading: true,
        isJobDetailLoading: true,
        isCreatingJob: true,
        isCancellingJob: true,
        isDownloading: true,
        jobsError: 'error',
        jobDetailError: 'error',
        createError: 'error',
        cancelError: 'error',
        downloadError: 'error',
      });

      useJobStore.getState().reset();

      const state = useJobStore.getState();
      expect(state.jobs).toEqual([]);
      expect(state.jobsTotal).toBe(0);
      expect(state.jobsPage).toBe(1);
      expect(state.jobsPageSize).toBe(20);
      expect(state.selectedJob).toBeNull();
      expect(state.activeJobProgress).toBeNull();
      expect(state.activeJobQualityReport).toBeNull();
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
      expect(state.isJobsLoading).toBe(false);
      expect(state.isJobDetailLoading).toBe(false);
      expect(state.isCreatingJob).toBe(false);
      expect(state.isCancellingJob).toBe(false);
      expect(state.isDownloading).toBe(false);
      expect(state.jobsError).toBeNull();
      expect(state.jobDetailError).toBeNull();
      expect(state.createError).toBeNull();
      expect(state.cancelError).toBeNull();
      expect(state.downloadError).toBeNull();
    });

    it('should stop active polling before resetting', () => {
      mockedGetJobById.mockResolvedValue(makeApiResponse(mockJob));
      mockedGetJobProgress.mockResolvedValue(
        makeApiResponse(mockBatchProgress),
      );

      // Start polling to create an active interval
      useJobStore.getState().startPolling('job-001', 1000);
      expect(useJobStore.getState().pollingIntervalId).not.toBeNull();

      useJobStore.getState().reset();

      const state = useJobStore.getState();
      expect(state.pollingJobId).toBeNull();
      expect(state.pollingIntervalId).toBeNull();
    });

    it('should be safe to call reset multiple times', () => {
      useJobStore.getState().reset();
      useJobStore.getState().reset();

      const state = useJobStore.getState();
      expect(state.jobs).toEqual([]);
      expect(state.selectedJob).toBeNull();
    });
  });
});
