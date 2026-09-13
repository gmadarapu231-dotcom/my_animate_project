/**
 * The CareerOS API client, and the types the API actually returns.
 *
 * One client for all three platforms. The base URL and bearer token come from
 * settings (see `src/settings.ts`) because a phone cannot assume `localhost`
 * and a networked server requires a credential.
 */

// ---------------------------------------------------------------------------
// Types -- mirroring careeros/api/serializers.py
// ---------------------------------------------------------------------------
export type Health = {
  status: string;
  auth_required: boolean;
  database: string;
  llm_provider: string;
  llm_available: boolean;
  countries: string[];
  seed_domains: number;
  skill_nodes: number;
};

export type SalaryView = {
  raw: string | null;
  display: string;
  currency: string | null;
  annual_min: number | null;
  annual_max: number | null;
  period: string | null;
  components: string[];
};

export type SkillRef = { name: string; level?: string; skill_id?: string | null };

export type Classification = {
  domain_id: string;
  domain_label: string;
  function: string | null;
  specialization: string | null;
  industry: string | null;
  seniority: string;
  min_experience_years: number | null;
  education_requirement: string | null;
  required_skills: SkillRef[];
  preferred_skills: SkillRef[];
  required_certifications: string[];
  soft_skills: string[];
  ats_keywords: string[];
  confidence: number;
  method: string;
  rationale: string | null;
};

export type Eligibility = {
  verdict: 'compatible' | 'potentially_compatible' | 'unknown' | 'not_compatible';
  score: number;
  status: string;
  employment_type_match: boolean;
  reasons: string[];
  evidence: { signal: string; quote: string }[];
  disclaimer: string;
};

export type Priority = {
  overall: number;
  bucket: string;
  flag: string;
  emoji: string;
  days_to_deadline: number | null;
  explanation: string[];
  best_track: string | null;
};

export type JobCard = {
  id: number;
  title: string;
  company: string;
  url: string | null;
  source: string;
  country: string;
  country_name: string;
  location: string;
  work_arrangement: string | null;
  employment_type: string | null;
  salary: SalaryView;
  posted_on: string | null;
  deadline_on: string | null;
  applicant_count: number | null;
  archived: boolean;
  classification: Classification | null;
  eligibility: Eligibility | null;
  scores: {
    match: number | null;
    eligibility: number | null;
    urgency: number | null;
    competition: number | null;
    priority: number | null;
    ats: number | null;
  };
  priority: Priority | null;
  application: { id: number; status: string; applied_on: string | null; notes: string | null } | null;
  // present on the detail endpoint only
  description?: string;
  ats?: AtsReport | null;
  matches?: MatchRow[];
};

export type AtsReport = {
  overall: number;
  keyword_match: number;
  required_skills_match: number;
  preferred_skills_match: number;
  experience_match: number;
  title_match: number;
  education_match: number;
  certification_match: number;
  formatting_score: number;
  matched_keywords: string[];
  missing_keywords: string[];
  suggestions: string[];
};

export type MatchRow = {
  track_id: number;
  match_score: number;
  direct: number;
  related: number;
  partial: number;
  missing: number;
  gaps: string[];
  skill_matches: {
    requirement: string;
    kind: string;
    claimable: boolean;
    via_skill_id: string | null;
    note: string | null;
    level: string;
  }[];
};

export type Facets = {
  domains: { id: string; label: string; origin: string; job_count: number }[];
  countries: { code: string; name: string }[];
  all_countries: { code: string; name: string }[];
  buckets: { id: string; label: string }[];
};

export type Recommendation = {
  for_date: string;
  top_track_id: number | null;
  track_reasons: string[];
  top_jobs: { job_id: number; title: string; company: string; priority: number; flag: string; why: string }[];
  narrative: string | null;
};

export type LearningSignal = {
  kind: string;
  subject: string;
  rationale: string;
  confidence: string;
  evidence: Record<string, unknown>;
};

export type FunnelStats = {
  label: string;
  applications: number;
  responses: number;
  interviews: number;
  assessments: number;
  offers: number;
  rejections: number;
  response_rate: number;
  interview_rate: number;
  offer_rate: number;
  confident: boolean;
  note: string | null;
};

export type Analytics = {
  overall: FunnelStats & { pipeline: Record<string, number> };
  by_track: FunnelStats[];
  by_domain: FunnelStats[];
  by_country: FunnelStats[];
  by_visa_verdict: FunnelStats[];
  by_match_band: FunnelStats[];
  by_source: FunnelStats[];
  rejections: {
    total: number;
    explicit_count: number;
    explicit_reasons: string[];
    hypotheses: { reason: string; count: number; disclaimer: string }[];
  };
  learning_signals: LearningSignal[];
};

export type ApplicationRow = {
  id: number;
  job_id: number;
  title: string;
  company: string;
  country: string;
  status: string;
  applied_on: string | null;
  last_activity_on: string | null;
  priority: number | null;
  notes: string | null;
};

export type Profile = {
  personal: Record<string, any>;
  professional: Record<string, any>;
  automation_mode: string;
  work_authorization: {
    country: string;
    country_name: string | null;
    status: string;
    status_label: string;
    valid_until: string | null;
    needs_sponsorship: boolean;
    employment_preferences: string[];
  }[];
  career_tracks: {
    id: number;
    name: string;
    domain: string;
    active: boolean;
    priority: number;
    preferred_titles: string[];
    min_match_score: number;
    countries: string[];
  }[];
  evidence_count: number;
};

export type TailorResult = {
  id: number;
  name: string;
  kind: string;
  version: number;
  is_final: boolean;
  storage_path: string | null;
  rendered_text: string | null;
  notes: string[];
  factuality: {
    passed: boolean;
    unsupported_count: number;
    summary: string;
    claims: { text: string; verdict: string; issues: string[]; section: string | null }[];
  } | null;
};

export type AgentToolCall = {
  turn: number;
  name: string;
  arguments: Record<string, unknown>;
  mutating: boolean;
  is_error: boolean;
  summary?: string;
};

export type AgentAnswer = {
  answer: string;
  turns: number;
  stop_reason: string | null;
  tool_calls: AgentToolCall[];
  mutations: string[];
  input_tokens: number;
  output_tokens: number;
  run_id: number | null;
  ok: boolean;
  error: string | null;
};

export type AgentTools = {
  count: number;
  tools: { name: string; description: string; mutating: boolean; parameters: string[] }[];
  withheld_capabilities: { name: string; reason: string }[];
  note: string;
};

export type SearchResult = {
  query: string;
  filters_description: string;
  count: number;
  jobs: JobCard[];
};

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }

  /** True when the server wants a bearer token we do not have. */
  get isAuthError(): boolean {
    return this.status === 401;
  }

  /** True when the feature needs model access that is not configured. */
  get isUnavailable(): boolean {
    return this.status === 503;
  }
}

export type ClientConfig = { baseUrl: string; token?: string | null };

const TIMEOUT_MS = 120_000;   // the agent loop can legitimately take a while

export class CareerOsClient {
  constructor(private config: ClientConfig) {}

  private url(path: string, query?: Record<string, string | number | boolean | undefined>): string {
    const base = this.config.baseUrl.replace(/\/+$/, '');
    const url = new URL(base + path);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value));
      }
    }
    return url.toString();
  }

  private async request<T>(
    path: string,
    options: { method?: 'GET' | 'POST'; body?: unknown; query?: Record<string, any> } = {},
  ): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (this.config.token) headers.Authorization = `Bearer ${this.config.token}`;

    try {
      const response = await fetch(this.url(path, options.query), {
        method: options.method ?? 'GET',
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal,
      });
      const text = await response.text();
      const payload = text ? safeJson(text) : null;
      if (!response.ok) {
        throw new ApiError(detailOf(payload) ?? `HTTP ${response.status}`, response.status);
      }
      return payload as T;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if ((error as Error)?.name === 'AbortError') {
        throw new ApiError('The request timed out.', 408);
      }
      throw new ApiError(
        `Cannot reach ${this.config.baseUrl}. Is the server running, and is the address right for this device?`,
        0,
      );
    } finally {
      clearTimeout(timer);
    }
  }

  // -- discovery ---------------------------------------------------------
  health = () => this.request<Health>('/api/health');

  // -- jobs --------------------------------------------------------------
  jobs = (query: { country?: string; domain?: string; bucket?: string; limit?: number } = {}) =>
    this.request<{ count: number; jobs: JobCard[] }>('/api/jobs', { query });

  job = (id: number) => this.request<JobCard>(`/api/jobs/${id}`);

  facets = () => this.request<Facets>('/api/jobs/facets');

  search = (query: string) =>
    this.request<SearchResult>('/api/jobs/search', {
      method: 'POST',
      body: { query, use_ai: true },
    });

  analyzeJob = (id: number) => this.request<JobCard>(`/api/jobs/${id}/analyze`, { method: 'POST' });

  // -- resumes -----------------------------------------------------------
  tailorResume = (jobId: number) =>
    this.request<TailorResult>('/api/resumes/tailor', {
      method: 'POST',
      body: { job_id: jobId, use_ai: true },
    });

  coverLetter = (jobId: number) =>
    this.request<{ id: number; body: string; unclaimed_gaps: string[]; evidence_ids: number[] }>(
      '/api/resumes/cover-letter',
      { method: 'POST', body: { job_id: jobId, use_ai: true } },
    );

  // -- applications ------------------------------------------------------
  applications = (status?: string) =>
    this.request<{ count: number; lifecycle: string[]; applications: ApplicationRow[] }>(
      '/api/applications',
      { query: { status } },
    );

  setStatus = (applicationId: number, status: string, detail?: string) =>
    this.request<{ id: number; status: string }>(`/api/applications/${applicationId}/status`, {
      method: 'POST',
      body: { status, detail },
    });

  prepare = (jobId: number) =>
    this.request<{
      application_id: number;
      resume_id: number | null;
      resume_final: boolean | null;
      blockers: string[];
      suggested_answers: Record<string, unknown>;
      submission_mode: string;
      notice: string;
    }>('/api/applications/prepare', { method: 'POST', body: { job_id: jobId } });

  // -- insight -----------------------------------------------------------
  recommendations = (refresh = false) =>
    this.request<Recommendation>('/api/recommendations', { query: { refresh } });

  analytics = () => this.request<Analytics>('/api/analytics');

  learning = () =>
    this.request<{ count: number; signals: LearningSignal[]; disclaimer: string }>(
      '/api/analytics/learning',
    );

  profile = () => this.request<Profile>('/api/profile');

  // -- agent -------------------------------------------------------------
  askAgent = (prompt: string, maxTurns = 12) =>
    this.request<AgentAnswer>('/api/agent/ask', {
      method: 'POST',
      body: { prompt, max_turns: maxTurns, effort: 'high' },
    });

  agentTools = () => this.request<AgentTools>('/api/agent/tools');

  agentRuns = (limit = 10) =>
    this.request<{ count: number; runs: (AgentAnswer & { prompt: string; started_at: string })[] }>(
      '/api/agent/runs',
      { query: { limit } },
    );

  // -- email -------------------------------------------------------------
  emails = () =>
    this.request<{ count: number; messages: any[] }>('/api/email/messages');

  drafts = () => this.request<{ count: number; drafts: any[] }>('/api/email/drafts');
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text.slice(0, 300) };
  }
}

function detailOf(payload: unknown): string | null {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    // FastAPI validation errors arrive as a list of objects.
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (typeof d === 'object' && d && 'msg' in d ? String((d as any).msg) : String(d)))
        .join('; ');
    }
  }
  return null;
}
