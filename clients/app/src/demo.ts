/**
 * Demo mode: the real app, the real UI, real engine output — no backend.
 *
 * `demoFixtures.json` is not hand-written sample data. Every response in it
 * was captured from the actual FastAPI server running the actual engines
 * against `data/sample_profile.yaml` and `data/sample_jobs.json`. The scores,
 * the work-authorization verdicts and their quoted source phrases, the ATS
 * keywords, the factuality verdicts — all genuine output, frozen.
 *
 * What demo mode CANNOT be honest about is anything that needs a live model or
 * a live database:
 *
 * - Natural-language search only answers the six queries that were recorded;
 *   anything else says so rather than silently returning the full list.
 * - The agent returns a RECORDED transcript and labels itself as such. It is
 *   not thinking.
 * - Status changes are in-memory and vanish on reload.
 *
 * Every one of those limits is surfaced in the UI. A demo that quietly fakes
 * a live system teaches the wrong thing about what was built.
 */
import {
  AgentAnswer,
  AgentTools,
  Analytics,
  ApiError,
  ApplicationRow,
  CareerOsClient,
  Facets,
  Health,
  JobCard,
  LearningSignal,
  Profile,
  Recommendation,
  SearchResult,
  TailorResult,
} from './api';
import fixtures from './demoFixtures.json';

/** Inlined at build time by `EXPO_PUBLIC_CAREEROS_DEMO=1 expo export`. */
export function isDemo(): boolean {
  return process.env.EXPO_PUBLIC_CAREEROS_DEMO === '1';
}

const F = fixtures as any;

/** A touch of latency so loading states are visible rather than skipped. */
function settle<T>(value: T, ms = 220): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

export class DemoClient extends CareerOsClient {
  /** Status changes are local to the session; the UI says so. */
  private statusOverrides = new Map<number, string>();

  constructor() {
    super({ baseUrl: 'demo://careeros', token: null });
  }

  health = () => settle<Health>({ ...F.health, llm_available: false }, 80);

  facets = () => settle<Facets>(F.facets);

  jobs = () => settle<{ count: number; jobs: JobCard[] }>(F.jobs);

  job = (id: number) => {
    const found = F.jobDetail[String(id)];
    if (!found) {
      return Promise.reject(new ApiError(`No job with id ${id} in the demo data.`, 404));
    }
    return settle<JobCard>(found);
  };

  search = (query: string) => {
    const recorded = F.search[query];
    if (recorded) return settle<SearchResult>(recorded);
    // Be explicit rather than pretending the query was understood.
    const known = Object.keys(F.search)
      .map((q) => `“${q}”`)
      .join(', ');
    return Promise.reject(
      new ApiError(
        `Demo mode only has recorded results for: ${known}. ` +
          'Run the real server to search freely — every query is compiled live there.',
        501,
      ),
    );
  };

  analyzeJob = (id: number) => this.job(id);

  profile = () => settle<Profile>(F.profile);

  recommendations = () => settle<Recommendation>(F.recommendations);

  analytics = () => settle<Analytics>(F.analytics);

  learning = () =>
    settle<{ count: number; signals: LearningSignal[]; disclaimer: string }>(F.learning);

  applications = (status?: string) => {
    const rows: ApplicationRow[] = F.applications.applications.map((row: ApplicationRow) => ({
      ...row,
      status: this.statusOverrides.get(row.id) ?? row.status,
    }));
    const filtered = status ? rows.filter((row) => row.status === status) : rows;
    return settle({ ...F.applications, count: filtered.length, applications: filtered });
  };

  setStatus = (applicationId: number, status: string) => {
    this.statusOverrides.set(applicationId, status);
    return settle({ id: applicationId, status }, 160);
  };

  tailorResume = (jobId: number) => {
    const recorded = F.tailor[String(jobId)];
    if (recorded) return settle<TailorResult>(recorded, 700);
    return Promise.reject(
      new ApiError(
        'Demo mode has a recorded tailored resume for the top two jobs only. ' +
          'The real server tailors any job and re-runs the factuality check each time.',
        501,
      ),
    );
  };

  coverLetter = (jobId: number) => {
    const recorded = F.coverLetter[String(jobId)];
    if (recorded) return settle(recorded, 600);
    return Promise.reject(
      new ApiError('Demo mode has a recorded cover letter for the top two jobs only.', 501),
    );
  };

  prepare = () =>
    settle(
      {
        application_id: 1,
        resume_id: 1,
        resume_final: true,
        blockers: [],
        suggested_answers: {
          work_authorization:
            'Answer from your Master Profile. CareerOS does not auto-answer immigration questions on your behalf.',
          years_of_experience: 8.5,
          willing_to_relocate: true,
        },
        submission_mode: 'assisted',
        notice:
          'ASSISTED mode: review every field, then submit yourself. CareerOS never bypasses ' +
          'CAPTCHA, MFA or bot protection, and never submits without you.',
      },
      500,
    );

  agentTools = () => settle<AgentTools>(F.agentTools);

  agentRuns = () => settle({ count: 0, runs: [] as any[] });

  /**
   * A recorded transcript, labelled as one.
   *
   * The agent loop needs a live model; there is no honest way to fake it. The
   * answer text says so in its first line so nobody mistakes this for the
   * model reasoning.
   */
  askAgent = (prompt: string) =>
    settle<AgentAnswer>(
      {
        answer:
          '[Recorded demo response — the agent is not running here. It needs model access ' +
          'on the server (ANTHROPIC_API_KEY).]\n\n' +
          'Here is what a real run looks like for “What should I apply for today?”:\n\n' +
          'Start with Senior Network Security Engineer at Cobalt Bank. Its final application ' +
          'date is today, so it is pinned to the top regardless of score — and it happens to ' +
          'be your strongest match at 88.8, with 6 of the 8 required skills directly ' +
          'evidenced (BGP/OSPF, Cisco ISE, Zero Trust segmentation, SD-WAN).\n\n' +
          'The posting says it will sponsor and transfer H1B visas, so the verdict is ' +
          '"potentially compatible" rather than unknown — but a transfer petition is still ' +
          'required, and that is the employer\'s statement, not a guarantee. Verify it on the ' +
          'screening call.\n\n' +
          'Second: Network Engineer - Data Centre at Sahyadri Technologies (97.1 match, no ' +
          'sponsorship needed in India) — but 220 applicants, so it is a weaker bet than the ' +
          'score suggests.\n\n' +
          'I would skip the SAP Security contract today. Your evidence supports access ' +
          'governance and SoD work, but nothing SAP-specific, so the tailored resume leaves ' +
          'the Firefighter requirement unclaimed. That is honest, and it will not survive a ' +
          'technical screen.',
        turns: 4,
        stop_reason: 'end_turn',
        tool_calls: [
          { turn: 1, name: 'get_profile', arguments: {}, mutating: false, is_error: false },
          { turn: 1, name: 'list_jobs', arguments: { limit: 10 }, mutating: false, is_error: false },
          { turn: 2, name: 'get_job', arguments: { job_id: 1 }, mutating: false, is_error: false },
          {
            turn: 2,
            name: 'get_match_detail',
            arguments: { job_id: 1 },
            mutating: false,
            is_error: false,
          },
          {
            turn: 3,
            name: 'get_match_detail',
            arguments: { job_id: 3 },
            mutating: false,
            is_error: false,
          },
          { turn: 3, name: 'get_analytics', arguments: {}, mutating: false, is_error: false },
        ],
        mutations: [],
        input_tokens: 18432,
        output_tokens: 742,
        run_id: null,
        ok: true,
        error: null,
      },
      1200,
    );

  emails = () => settle({ count: 0, messages: [] as any[] });

  drafts = () => settle({ count: 0, drafts: [] as any[] });
}
