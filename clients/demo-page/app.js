"use strict";
const F = JSON.parse(document.getElementById("fixtures").textContent);

const FLAG = {
  today: ["\u{1F534}", "DEADLINE TODAY"],
  within_48h: ["\u{1F7E0}", "DEADLINE WITHIN 48 HOURS"],
  within_7d: ["\u{1F7E1}", "DEADLINE WITHIN 7 DAYS"],
  future: ["\u{1F7E2}", "FUTURE"],
  none: ["⚪", "NO DEADLINE"],
  expired: ["⚫", "EXPIRED"],
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v) => (v === null || v === undefined ? "—" : String(Math.round(v * 10) / 10));
const flagOf = (b) => (FLAG[b] ? FLAG[b][0] + " " + FLAG[b][1] : "");
const titleCase = (s) => String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/* --- state ---------------------------------------------------------------- */
const state = { tab: "jobs", domain: null, country: null, results: null, note: null };

/* --- job card ------------------------------------------------------------- */
function verdictHtml(job, withQuote) {
  const e = job.eligibility;
  if (!e) return "";
  const quote = e.evidence && e.evidence[0] ? e.evidence[0].quote : null;
  return `<div class="verdict ${esc(e.verdict)}">
    <div class="vh">Work authorization: ${esc(e.verdict.replace(/_/g, " "))}</div>
    ${e.reasons && e.reasons[0] ? `<div class="muted" style="margin-top:3px">${esc(e.reasons[0])}</div>` : ""}
    ${withQuote && quote ? `<q>Source: “${esc(quote)}”</q>` : ""}
    <div class="caveat">${esc(e.disclaimer)}</div>
  </div>`;
}

function cardHtml(job) {
  const p = job.priority || {};
  const c = job.classification || {};
  const s = job.scores || {};
  const chip = (t, cls) => `<span class="chip ${cls || ""}">${esc(t)}</span>`;
  return `<button class="card ${esc(p.bucket || "none")}" data-job="${job.id}">
    <div class="card-top">
      <div>
        <div class="title">${esc(job.title)}</div>
        <div class="sub">${esc(job.company)} · ${esc(job.location || job.country_name)} · ${esc(job.source)}</div>
      </div>
      <div class="flag" style="color:var(--${p.bucket === "today" ? "bad" : p.bucket === "within_7d" ? "warn" : p.bucket === "future" ? "ok" : "ink-2"})">
        ${esc(flagOf(p.bucket))}
      </div>
    </div>
    <div class="chips">
      ${chip(c.domain_label || "unclassified", "dom")}
      ${c.seniority ? chip(c.seniority) : ""}
      ${chip(job.country)}
      ${job.work_arrangement ? chip(job.work_arrangement) : ""}
      ${job.employment_type ? chip(job.employment_type) : ""}
      ${chip(job.salary.display)}
      ${job.applicant_count != null ? chip(job.applicant_count + " applicants") : ""}
    </div>
    <div class="scores">
      ${[["priority", "Priority"], ["match", "Match"], ["ats", "ATS"], ["eligibility", "Elig"], ["urgency", "Urgency"], ["competition", "Compete"]]
        .map(([k, l]) => `<div class="score"><b class="num">${num(s[k])}</b><span>${l}</span></div>`).join("")}
    </div>
    ${verdictHtml(job, false)}
  </button>`;
}

/* --- screens -------------------------------------------------------------- */
function renderJobs() {
  const facets = F.facets;
  const jobs = state.results ? state.results : F.jobs.jobs.filter((j) =>
    (!state.domain || (j.classification && j.classification.domain_id === state.domain)) &&
    (!state.country || j.country === state.country));

  const recorded = Object.keys(F.search);
  document.getElementById("view-jobs").innerHTML = `
    <div class="panel">
      <h2>Natural-language search</h2>
      <p class="small muted">These are the queries recorded for the demo. The real server compiles
      any sentence into filters and shows you its reading.</p>
      <div class="row" style="margin-top:10px">
        ${recorded.map((q, i) => `<button class="qbtn" data-q="${i}">${esc(q)}</button>`).join("")}
        ${state.results ? `<button class="btn" id="clearSearch">Clear</button>` : ""}
      </div>
      ${state.note ? `<p class="small" style="margin-top:10px"><b>Interpreted as:</b> <span class="kw">${esc(state.note)}</span></p>` : ""}
    </div>
    ${state.results ? "" : `
    <div class="filters">
      <button class="filter" aria-pressed="${!state.domain}" data-dom="">All domains</button>
      ${facets.domains.map((d) => `<button class="filter" aria-pressed="${state.domain === d.id}" data-dom="${esc(d.id)}">${esc(d.label)} (${d.job_count})</button>`).join("")}
    </div>
    <div class="filters">
      <button class="filter" aria-pressed="${!state.country}" data-ctry="">All countries</button>
      ${facets.countries.map((c) => `<button class="filter" aria-pressed="${state.country === c.code}" data-ctry="${esc(c.code)}">${esc(c.name)}</button>`).join("")}
    </div>`}
    <p class="small muted" style="margin:0 0 10px">${jobs.length} job(s) · ordered by deadline tier first, then priority score</p>
    ${jobs.length ? jobs.map(cardHtml).join("") : `<div class="panel"><p class="muted">No jobs match those filters.</p></div>`}
  `;
}

function renderDetail(id) {
  const job = F.jobDetail[String(id)];
  if (!job) return;
  const c = job.classification || {};
  const p = job.priority || {};
  const s = job.scores || {};
  const m = job.matches && job.matches[0];
  const ats = job.ats;
  const tailor = F.tailor[String(id)];
  const letter = F.coverLetter[String(id)];
  const chips = (arr, cls) => (arr && arr.length ? arr.map((x) => `<span class="chip ${cls || ""}">${esc(x.name || x)}</span>`).join("") : `<span class="muted small">none detected</span>`);

  document.getElementById("view-detail").innerHTML = `
    <button class="back" id="backBtn">&larr; Back to jobs</button>
    <div class="panel" style="border-left:4px solid var(--${p.bucket === "today" ? "bad" : p.bucket === "within_7d" ? "warn" : "ok"})">
      <div class="title" style="font-size:17px">${esc(job.title)}</div>
      <div class="sub">${esc(job.company)} · ${esc(job.location)} · ${esc(job.source)}</div>
      <div class="flag" style="margin-top:8px;text-align:left;color:var(--${p.bucket === "today" ? "bad" : "warn"})">
        ${esc(flagOf(p.bucket))}${p.days_to_deadline != null ? " · " + p.days_to_deadline + " day(s) left" : ""}
      </div>
      <div class="scores">
        ${[["priority", "Priority"], ["match", "Match"], ["ats", "ATS"], ["eligibility", "Elig"], ["urgency", "Urgency"], ["competition", "Compete"]]
          .map(([k, l]) => `<div class="score"><b class="num">${num(s[k])}</b><span>${l}</span></div>`).join("")}
      </div>
      <div class="chips">
        <span class="chip">${esc(job.salary.display)}</span>
        ${job.employment_type ? `<span class="chip">${esc(job.employment_type)}</span>` : ""}
        ${job.posted_on ? `<span class="chip">posted ${esc(job.posted_on)}</span>` : ""}
        ${job.deadline_on ? `<span class="chip">closes ${esc(job.deadline_on)}</span>` : ""}
        ${p.best_track ? `<span class="chip dom">track: ${esc(p.best_track)}</span>` : ""}
      </div>
      ${verdictHtml(job, true)}
    </div>

    ${p.explanation && p.explanation.length ? `<div class="panel"><h2>Why this rank</h2>
      <ul class="tight small">${p.explanation.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}

    ${tailor ? `<div class="panel"><h2>Tailored resume</h2>
      <div class="row">
        <span class="chip ${tailor.is_final ? "ok" : "bad"}">${tailor.is_final ? "Resume ready" : "BLOCKED — not sendable"}</span>
        <span class="mono small muted">${esc(tailor.storage_path || "")}</span>
      </div>
      ${tailor.factuality ? `<p class="small" style="margin-top:9px">${esc(tailor.factuality.summary)}</p>` : ""}
      <ul class="tight small">${(tailor.notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
      <p class="caveat">The factuality check runs on every tailoring pass; a resume with any
      unsupported claim comes back <code>is_final: false</code> and cannot be sent.</p>
    </div>` : ""}

    ${letter ? `<div class="panel"><h2>Cover letter</h2>
      <pre class="answer">${esc(letter.body)}</pre>
      ${letter.unclaimed_gaps && letter.unclaimed_gaps.length
        ? `<p class="caveat">Gaps acknowledged rather than hidden: ${esc(letter.unclaimed_gaps.join(", "))}</p>` : ""}
    </div>` : ""}

    <div class="panel"><h2>Classification</h2>
      <div class="chips">
        <span class="chip dom">${esc(c.domain_label)}</span>
        ${c.function ? `<span class="chip">${esc(c.function)}</span>` : ""}
        ${c.industry ? `<span class="chip">${esc(c.industry)}</span>` : ""}
        <span class="chip">${esc(c.seniority)}</span>
      </div>
      <p class="small muted" style="margin-top:9px">
        ${c.min_experience_years ?? "—"} yrs required · education: ${esc(c.education_requirement || "unspecified")}
        · confidence ${num((c.confidence || 0) * 100)}% (${esc(c.method)})
      </p>
      <h3 class="grp">Required skills</h3><div class="chips">${chips(c.required_skills)}</div>
      <h3 class="grp">Preferred skills</h3><div class="chips">${chips(c.preferred_skills)}</div>
      ${c.required_certifications && c.required_certifications.length
        ? `<h3 class="grp">Certifications named</h3><div class="chips">${chips(c.required_certifications, "warn")}</div>` : ""}
      <h3 class="grp">ATS keywords derived from this posting</h3>
      <p class="kw small muted">${esc((c.ats_keywords || []).slice(0, 26).join(" · "))}</p>
      <p class="caveat">No industry keyword list exists anywhere in the system — these come from
      this posting's own text, which is why the same code works for a veterinary role.</p>
    </div>

    ${m ? `<div class="panel"><h2>Match detail</h2>
      <div class="tw"><table><thead><tr><th>Requirement</th><th>Kind</th><th>Claimable</th><th>Note</th></tr></thead>
      <tbody>${m.skill_matches.map((x) => `<tr>
        <td>${esc(x.requirement)}</td>
        <td><span class="chip ${x.kind === "direct" ? "ok" : x.kind === "missing" ? "bad" : x.claimable ? "ok" : "warn"}">${esc(x.kind)}</span></td>
        <td>${x.claimable ? "yes" : "no"}</td>
        <td class="muted">${esc(x.note || "")}</td></tr>`).join("")}</tbody></table></div>
      ${m.gaps && m.gaps.length ? `<p class="caveat">Gaps (required by the posting, unevidenced): ${esc(m.gaps.join(", "))}</p>` : ""}
    </div>` : ""}

    ${ats ? `<div class="panel"><h2>ATS report</h2>
      <div class="stats">
        ${[["overall", "Overall"], ["keyword_match", "Keywords"], ["required_skills_match", "Required"],
           ["experience_match", "Experience"], ["title_match", "Title"], ["certification_match", "Certs"]]
          .map(([k, l]) => `<div class="stat"><b class="num">${num(ats[k])}</b><span>${l}</span></div>`).join("")}
      </div>
      ${ats.missing_keywords && ats.missing_keywords.length
        ? `<h3 class="grp">Highest-value terms missing</h3><p class="kw small muted">${esc(ats.missing_keywords.slice(0, 14).join(" · "))}</p>` : ""}
      <ul class="tight small">${(ats.suggestions || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    </div>` : ""}

    ${job.description ? `<div class="panel"><h2>The posting</h2>
      <pre class="answer small muted">${esc(job.description)}</pre></div>` : ""}
  `;
}

function renderToday() {
  const r = F.recommendations;
  const signals = F.learning.signals || [];
  const track = (F.profile.career_tracks.find((t) => t.id === r.top_track_id) || {}).name;
  document.getElementById("view-today").innerHTML = `
    <div class="panel">
      <h2>What should I apply for today?</h2>
      ${track ? `<p style="font-weight:600;font-size:15px">Top career track today: ${esc(track)}</p>` : ""}
      <ul class="tight small">${(r.track_reasons || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
      ${(r.top_jobs || []).map((j, i) => `
        <div style="border-top:1px solid var(--line);padding:12px 0 4px">
          <div class="row"><span class="chip dom">#${i + 1}</span><span class="title">${esc(j.title)}</span></div>
          <div class="sub">${esc(j.company)} · ${esc(j.flag)} · priority ${Math.round(j.priority)}</div>
          <div class="small muted" style="margin-top:4px">${esc(j.why)}</div>
          <button class="btn" data-job="${j.job_id}" style="margin-top:9px">Open</button>
        </div>`).join("")}
    </div>
    <div class="panel">
      <h2>Career learning signals</h2>
      ${signals.length ? signals.map((s) => `
        <div style="margin-bottom:11px">
          <div class="row"><span class="chip">${esc(titleCase(s.kind))}</span><b>${esc(s.subject)}</b></div>
          <div class="small muted" style="margin-top:3px">${esc(s.rationale)} <i>(confidence: ${esc(s.confidence)})</i></div>
        </div>`).join("")
        : `<p class="muted small">Not enough outcome history yet — each gap here has to appear on at
           least two targeted jobs before it counts as a signal.</p>`}
      <p class="caveat">${esc(F.learning.disclaimer)}</p>
    </div>`;
}

function renderPipeline() {
  const rows = F.applications.applications;
  const counts = {};
  rows.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
  document.getElementById("view-pipeline").innerHTML = `
    <div class="panel">
      <h2>Application pipeline</h2>
      <div class="chips">${Object.entries(counts).map(([s, n]) => `<span class="chip">${esc(titleCase(s))} ${n}</span>`).join("")}</div>
      <p class="caveat">The full lifecycle has 20 states, from <code>discovered</code> through
      <code>offer</code>. Email can advance it automatically; here it is frozen.</p>
    </div>
    <div class="panel"><div class="tw"><table>
      <thead><tr><th>Role</th><th>Company</th><th>Country</th><th>Status</th><th class="n">Priority</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${esc(r.title)}</td><td>${esc(r.company)}</td><td>${esc(r.country)}</td>
        <td><span class="chip">${esc(titleCase(r.status))}</span></td>
        <td class="n num">${num(r.priority)}</td></tr>`).join("")}</tbody>
    </table></div></div>`;
}

function renderInsights() {
  const a = F.analytics;
  const o = a.overall;
  const tbl = (title, rows) => !rows.length ? "" : `
    <div class="panel"><h2>${esc(title)}</h2><div class="tw"><table>
      <thead><tr><th>Group</th><th class="n">Apps</th><th class="n">Resp</th><th class="n">Intv</th><th class="n">Offers</th><th class="n">Resp %</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${esc(r.label)}${r.confident ? "" : ` <span class="muted small">(low sample)</span>`}</td>
        <td class="n num">${r.applications}</td><td class="n num">${r.responses}</td>
        <td class="n num">${r.interviews}</td><td class="n num">${r.offers}</td>
        <td class="n num">${r.response_rate}</td></tr>`).join("")}</tbody>
    </table></div></div>`;

  document.getElementById("view-insights").innerHTML = `
    <div class="panel"><h2>Funnel</h2>
      <div class="stats">
        ${[["applications", "Applications"], ["responses", "Responses"], ["interviews", "Interviews"],
           ["assessments", "Assessments"], ["offers", "Offers"], ["rejections", "Rejections"]]
          .map(([k, l]) => `<div class="stat"><b class="num">${o[k]}</b><span>${l}</span></div>`).join("")}
        <div class="stat"><b class="num">${o.response_rate}%</b><span>Response rate</span></div>
        <div class="stat"><b class="num">${o.interview_rate}%</b><span>Interview rate</span></div>
        <div class="stat"><b class="num">${o.offer_rate}%</b><span>Offer rate</span></div>
      </div>
      ${o.note ? `<p class="caveat">${esc(o.note)}</p>` : ""}
    </div>
    ${tbl("By career track", a.by_track)}
    ${tbl("By domain", a.by_domain)}
    ${tbl("By country", a.by_country)}
    ${tbl("By visa verdict", a.by_visa_verdict)}
    ${tbl("By match band", a.by_match_band)}
    <div class="panel"><h2>Rejections</h2>
      <p class="small">${a.rejections.total} recorded · ${a.rejections.explicit_count} with a reason
      stated by the employer.</p>
      ${(a.rejections.explicit_reasons || []).map((r) => `<p class="small">Stated: “${esc(r)}”</p>`).join("")}
      ${a.rejections.hypotheses.length ? `
        <h3 class="grp">CareerOS hypotheses</h3>
        <div class="chips">${a.rejections.hypotheses.map((h) => `<span class="chip warn">${esc(h.reason)} (${h.count}×)</span>`).join("")}</div>
        <p class="caveat">These are guesses derived from our own scores — not reasons any employer gave.
        The two are never merged.</p>` : ""}
    </div>`;
}

function renderAgent() {
  const t = F.agentTools;
  const read = t.tools.filter((x) => !x.mutating);
  const write = t.tools.filter((x) => x.mutating);
  document.getElementById("view-agent").innerHTML = `
    <div class="panel">
      <h2>Ask the agent</h2>
      <p class="small muted">Claude drives these engines as tools and decides what to look at. It has
      autonomy over strategy, never over truthfulness.</p>
      <div class="row" style="margin-top:10px">
        <button class="btn primary" id="askBtn">Replay: “What should I apply for today?”</button>
      </div>
      <div id="agentOut"></div>
    </div>
    <div class="panel">
      <h2>Tool surface — ${t.count} tools</h2>
      <div class="tw"><table>
        <thead><tr><th>Tool</th><th>Type</th><th>What it does</th></tr></thead>
        <tbody>${read.concat(write).map((x) => `<tr>
          <td class="mono">${esc(x.name)}</td>
          <td><span class="chip ${x.mutating ? "warn" : ""}">${x.mutating ? "changes data" : "read"}</span></td>
          <td class="muted">${esc(x.description.slice(0, 150))}</td></tr>`).join("")}</tbody>
      </table></div>
    </div>
    <div class="panel">
      <h2>Deliberately withheld</h2>
      <p class="small muted">${esc(t.note)}</p>
      <div class="tw"><table>
        <thead><tr><th>Capability</th><th>Why it does not exist</th></tr></thead>
        <tbody>${t.withheld_capabilities.map((w) => `<tr>
          <td class="mono" style="color:var(--bad)">${esc(w.name)}</td>
          <td class="muted">${esc(w.reason)}</td></tr>`).join("")}</tbody>
      </table></div>
    </div>`;
}

function showAgentAnswer() {
  const a = F.agentAnswer;
  document.getElementById("agentOut").innerHTML = `
    <div class="panel" style="margin:14px 0 0;background:var(--panel-2)">
      <h2>Tool calls</h2>
      <div class="trace mono">${a.tool_calls.map((c) => `<div class="${c.is_error ? "e" : c.mutating ? "m" : ""}">
        ${c.is_error ? "!" : c.mutating ? "*" : "·"} turn ${c.turn} <b>${esc(c.name)}</b>(${esc(Object.entries(c.arguments || {}).map(([k, v]) => k + "=" + v).join(", "))})
      </div>`).join("")}</div>
      <p class="caveat">· read &nbsp; * changed something &nbsp; ! error</p>
    </div>
    <div class="panel" style="margin:12px 0 0">
      <pre class="answer">${esc(a.answer)}</pre>
      <p class="small muted" style="margin-top:10px">${a.turns} turns · ${a.tool_calls.length} tool calls · ${a.input_tokens + a.output_tokens} tokens</p>
    </div>`;
}

/* --- tabs (no router, no history) ---------------------------------------- */
const TABS = [["jobs", "Jobs"], ["today", "Today"], ["pipeline", "Pipeline"], ["insights", "Insights"], ["agent", "Agent"]];
const RENDER = { jobs: renderJobs, today: renderToday, pipeline: renderPipeline, insights: renderInsights, agent: renderAgent };

let booted = false;
function scrollToConsole() {
  if (!booted) return;
  const c = document.getElementById("console");
  if (c) c.scrollIntoView({ block: "start" });
  else window.scrollTo(0, 0);
}

function show(tab) {
  state.tab = tab;
  ["jobs", "detail", "today", "pipeline", "insights", "agent"].forEach((v) => {
    document.getElementById("view-" + v).hidden = v !== tab;
  });
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  });
  if (RENDER[tab]) RENDER[tab]();
  scrollToConsole();
}

function openJob(id) {
  renderDetail(id);
  ["jobs", "today", "pipeline", "insights", "agent"].forEach((v) => { document.getElementById("view-" + v).hidden = true; });
  document.getElementById("view-detail").hidden = false;
  scrollToConsole();
}

document.getElementById("tabs").innerHTML = TABS
  .map(([id, label]) => `<button role="tab" data-tab="${id}" aria-selected="${id === "jobs"}">${label}</button>`).join("");

document.getElementById("brandSub").textContent =
  `${F.health.countries.join(" · ")} · ${F.health.seed_domains} seed domains · ${F.health.skill_nodes} skills`;

/* One delegated listener for the whole page — nothing depends on the URL. */
document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-tab],[data-job],[data-dom],[data-ctry],[data-q],#backBtn,#clearSearch,#askBtn");
  if (!el) return;
  if (el.dataset.tab) return show(el.dataset.tab);
  if (el.dataset.job) return openJob(el.dataset.job);
  if (el.id === "backBtn") return show("jobs");
booted = true;
  if (el.id === "askBtn") return showAgentAnswer();
  if (el.id === "clearSearch") { state.results = null; state.note = null; return renderJobs(); }
  if (el.dataset.q !== undefined) {
    const q = Object.keys(F.search)[Number(el.dataset.q)];
    const r = F.search[q];
    state.results = r.jobs; state.note = r.filters_description;
    return renderJobs();
  }
  if (el.dataset.dom !== undefined) { state.domain = el.dataset.dom || null; state.results = null; state.note = null; return renderJobs(); }
  if (el.dataset.ctry !== undefined) { state.country = el.dataset.ctry || null; state.results = null; state.note = null; return renderJobs(); }
});

show("jobs");
booted = true;
