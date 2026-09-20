/* TaxVault client.
 *
 * No framework and no build step, for two reasons. A tax client is used once a
 * year, often on a borrowed phone on a bad connection, and every kilobyte is
 * paid for at exactly the wrong moment. And a dependency tree is an attack
 * surface on a page that handles Social Security numbers.
 *
 * The session token is kept in sessionStorage rather than localStorage. That
 * means closing the tab ends the session, which is mildly inconvenient and the
 * right trade for this data on a shared or family device.
 */
'use strict';

const API = '';
const TOKEN_KEY = 'taxvault.token';

const state = {
  token: null,
  session: null,
  view: 'account',
  years: null,
  states: null,
  documents: [],
  estimate: null,
  compare: null,
  method: 'regular',
  chosen: null,          // null = "apply everything that helps"
  taxYear: null,
  situation: loadSituation(),
  pendingMobile: '',     // the number verified this session; the input is gone after re-render
  busy: false,
};

/* --------------------------------------------------------------- storage */
function readToken() {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}
function writeToken(value) {
  try { value ? sessionStorage.setItem(TOKEN_KEY, value) : sessionStorage.removeItem(TOKEN_KEY); }
  catch { /* private mode: the session simply lives in memory */ }
  state.token = value;
}
function loadSituation() {
  // The situation is not identifying on its own (no name, no SSN), so keeping
  // it makes re-running an estimate painless. It is cleared on sign-out.
  try { return JSON.parse(sessionStorage.getItem('taxvault.situation') || '{}'); }
  catch { return {}; }
}
function saveSituation() {
  try { sessionStorage.setItem('taxvault.situation', JSON.stringify(state.situation)); } catch {}
}

/* ------------------------------------------------------------------- api */
async function api(path, { method = 'GET', body, form } = {}) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  let response;
  try {
    response = await fetch(API + path, {
      method,
      headers,
      body: form ? form : (body !== undefined ? JSON.stringify(body) : undefined),
    });
  } catch {
    throw new Error('Could not reach the server. Check your connection and try again.');
  }
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }

  if (!response.ok) {
    let detail = (payload && payload.detail) || `Request failed (${response.status}).`;
    if (Array.isArray(detail)) {
      detail = detail.map((d) => `${(d.loc || []).slice(-1)[0] || 'field'}: ${d.msg}`).join('; ');
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return payload;
}

/* ----------------------------------------------------------------- utils */
const money = (value) => {
  const number = Number(value || 0);
  return number.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
};
const money2 = (value) => Number(value || 0).toLocaleString('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const percent = (value) => `${(Number(value || 0) * 100).toFixed(2)}%`;
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const day = (iso) => iso ? new Date(iso + 'T00:00:00').toLocaleDateString('en-US',
  { day: 'numeric', month: 'short', year: 'numeric' }) : '';

let toastTimer = null;
function toast(message, kind = '') {
  document.querySelectorAll('.toast').forEach((t) => t.remove());
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.setAttribute('role', kind === 'bad' ? 'alert' : 'status');
  node.textContent = message;
  document.body.appendChild(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), kind === 'bad' ? 7000 : 4000);
}

function el(id) { return document.getElementById(id); }

async function guard(button, work) {
  const original = button && button.textContent;
  if (button) { button.disabled = true; button.textContent = 'Working…'; }
  try {
    await work();
  } catch (error) {
    toast(error.message, 'bad');
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

/* ---------------------------------------------------------------- render */
function render() {
  const main = el('main');
  const views = {
    account: viewAccount,
    documents: viewDocuments,
    estimate: viewEstimate,
    filings: viewFilings,
    payment: viewPayment,
  };
  main.innerHTML = (views[state.view] || viewAccount)();
  main.appendChild(el('tpl-foot').content.cloneNode(true));
  main.scrollTop = 0;
  window.scrollTo(0, 0);
  wire();
  renderChrome();
}

function renderChrome() {
  const verified = !!(state.session && state.session.identity_verified);
  document.querySelectorAll('#tabs button').forEach((button) => {
    const view = button.dataset.view;
    button.toggleAttribute('disabled', button.hasAttribute('data-needs-identity') && !verified);
    if (view === state.view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  const who = el('who');
  if (state.session && state.session.signed_in) {
    const tp = state.session.taxpayer;
    who.innerHTML = verified && tp
      ? `${esc(tp.name)}<br>${esc(tp.ssn || '')}`
      : esc(state.session.account.email);
  } else {
    who.textContent = '';
  }
}

function go(view) {
  state.view = view;
  render();
  if (view === 'documents') refreshDocuments();
  if (view === 'filings') refreshFilings();
}

function steps(current) {
  const order = ['Sign in', 'Verify identity', 'Add W-2', 'Estimate', 'Pay'];
  const index = order.indexOf(current);
  return `<div class="steps">${order.map((label, i) =>
    `<span class="${i < index ? 'done' : i === index ? 'now' : ''}">${label}</span>`).join('')}</div>`;
}

/* =========================================================== 1. ACCOUNT */
function viewAccount() {
  const session = state.session;
  if (!session || !session.signed_in) return viewSignIn();
  if (!session.identity_verified) return viewIdentity();

  const tp = session.taxpayer || {};
  return `
  ${steps('Add W-2')}
  <div class="card">
    <h2>You are signed in</h2>
    <p class="sub">Identity confirmed. Everything below is scoped to this account alone.</p>
    <dl class="kv">
      <dt>Name</dt><dd>${esc(tp.name || '—')}</dd>
      <dt>Social Security number</dt><dd>${esc(tp.ssn || '—')}</dd>
      <dt>Email</dt><dd>${esc(session.account.email)}</dd>
      <dt>Mobile</dt><dd>${esc(session.account.mobile || '—')}</dd>
      <dt>Resident state</dt><dd>${esc(tp.resident_state || 'not set')}</dd>
    </dl>
    <div class="note info">
      Your Social Security number is encrypted before it is stored and is never shown
      in full again — only the last four digits, above. It is never sent back to this
      device, written to a log, or used as a database key.
    </div>
    <div class="actions">
      <button class="btn" data-go="documents">Add a W-2</button>
      <button class="btn ghost" id="sign-out">Sign out</button>
    </div>
  </div>
  ${statesCard()}`;
}

function viewSignIn() {
  return `
  <img class="lockup" src="/static/logo.svg"
       alt="TaxVault — AI-powered tax filing. Secure. Accurate. Trusted.">
  ${steps('Sign in')}
  <div class="card">
    <h2>Sign in</h2>
    <p class="sub">We email you a code. There is no password to choose, forget, or have stolen.</p>
    <div id="signin-step-email">
      <div class="field">
        <label for="email">Email address</label>
        <input id="email" type="email" autocomplete="email" inputmode="email"
               placeholder="you@example.com" enterkeyhint="send">
      </div>
      <button class="btn" id="send-code">Email me a code</button>
    </div>
    <div id="signin-step-code" hidden>
      <div class="note info" id="code-sent"></div>
      <div class="field">
        <label for="code">6-digit code</label>
        <input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
               placeholder="000000" enterkeyhint="go">
      </div>
      <div class="actions">
        <button class="btn" id="verify-code">Sign in</button>
        <button class="btn ghost" id="back-to-email">Use a different address</button>
      </div>
    </div>
  </div>
  ${statesCard()}`;
}

function viewIdentity() {
  const account = state.session.account;
  const mobileVerified = account.mobile_verified;
  return `
  ${steps('Verify identity')}
  <div class="card">
    <h2>Confirm it is you</h2>
    <p class="sub">
      Three things together: a code to your mobile, your email address (already confirmed),
      and your Social Security number. Tax data stays locked until all three agree.
    </p>

    <h3>1 · Mobile number</h3>
    ${mobileVerified ? `
      <div class="note good">Verified: ${esc(account.mobile)}</div>
    ` : `
      <div id="mobile-step-number">
        <div class="field">
          <label for="mobile">Mobile number <span class="hint">US, 10 digits</span></label>
          <input id="mobile" type="tel" inputmode="tel" autocomplete="tel" placeholder="(415) 555-0132">
        </div>
        <button class="btn" id="send-sms">Text me a code</button>
      </div>
      <div id="mobile-step-code" hidden>
        <div class="note info" id="sms-sent"></div>
        <div class="field">
          <label for="sms-code">6-digit code</label>
          <input id="sms-code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000">
        </div>
        <button class="btn" id="verify-sms">Confirm number</button>
      </div>
    `}

    <h3>2 · Your details</h3>
    <div class="row two">
      <div class="field">
        <label for="first-name">First name</label>
        <input id="first-name" autocomplete="given-name" ${mobileVerified ? '' : 'disabled'}>
      </div>
      <div class="field">
        <label for="last-name">Last name</label>
        <input id="last-name" autocomplete="family-name" ${mobileVerified ? '' : 'disabled'}>
      </div>
    </div>
    <div class="field">
      <label for="ssn">Social Security number <span class="hint">or ITIN</span></label>
      <input id="ssn" inputmode="numeric" autocomplete="off" placeholder="123-45-6789"
             maxlength="11" ${mobileVerified ? '' : 'disabled'}>
    </div>
    <div class="field">
      <label for="home-state">State you live in</label>
      <select id="home-state" ${mobileVerified ? '' : 'disabled'}>
        <option value="">Choose…</option>
        ${stateOptions('')}
      </select>
    </div>
    <button class="btn" id="submit-identity" ${mobileVerified ? '' : 'disabled'}>
      ${mobileVerified ? 'Confirm my identity' : 'Verify your mobile first'}
    </button>
    <div class="note info">
      This confirms the number matches this account and that codes reached your email
      and phone. It is not a government identity check — when a return is actually
      filed, the IRS matches your name and number against Social Security records itself.
    </div>
    <div class="actions"><button class="btn ghost" id="sign-out">Sign out</button></div>
  </div>`;
}

function statesCard() {
  const data = state.states;
  if (!data) return '';
  const free = data.no_income_tax.map((code) =>
    `<span class="chip none">${code}</span>`).join('');
  return `
  <div class="card">
    <h2>Where you live changes the answer</h2>
    <p class="sub">
      ${data.counts.no_income_tax} states take nothing from wages.
      ${data.counts.flat} charge one flat rate, and ${data.counts.graduated} use a rate
      schedule that climbs with income.
    </p>
    <div class="legend">${free}</div>
    <p class="sub" style="margin-top:12px;margin-bottom:0">
      Washington taxes large long-term capital gains despite having no wage tax, and New
      Hampshire became a no-tax state in 2025 when its interest-and-dividends tax ended.
    </p>
  </div>`;
}

function stateOptions(selected) {
  if (!state.states) return '';
  return state.states.states.map((s) =>
    `<option value="${s.code}" ${s.code === selected ? 'selected' : ''}>${esc(s.name)}${
      s.has_income_tax ? '' : ' — no income tax'}</option>`).join('');
}

/* ========================================================= 2. DOCUMENTS */
function viewDocuments() {
  const year = state.taxYear || (state.years && state.years.current) || 2025;
  const docs = state.documents || [];
  return `
  ${steps('Add W-2')}
  <div class="card">
    <h2>Your W-2s</h2>
    <p class="sub">Everything on the estimate comes from these, so they are checked as they go in.</p>
    ${docs.length === 0 ? `
      <div class="empty"><span class="glyph">▤</span>Nothing uploaded yet.<br>Add your first W-2 below.</div>
    ` : docs.map(documentRow).join('')}
  </div>

  <div class="card">
    <h2>Add a W-2</h2>
    <p class="sub">Typing the boxes is the most accurate route and takes about a minute.</p>
    <div class="choices" style="margin-bottom:16px">
      <div class="row three">
        <button class="choice" data-doc-mode="boxes" aria-pressed="true"><strong>Type the boxes</strong><span>Most accurate</span></button>
        <button class="choice" data-doc-mode="text" aria-pressed="false"><strong>Paste text</strong><span>From a payroll portal</span></button>
        <button class="choice" data-doc-mode="file" aria-pressed="false"><strong>Upload the file</strong><span>Stored, not read</span></button>
      </div>
    </div>

    <div class="field">
      <label for="doc-year">Tax year</label>
      <select id="doc-year">
        ${(state.years ? state.years.supported : [year]).slice().reverse().map((y) =>
          `<option value="${y}" ${y === year ? 'selected' : ''}>${y}</option>`).join('')}
      </select>
    </div>

    <div id="doc-boxes">
      <div class="field">
        <label for="employer">Employer name</label>
        <input id="employer" placeholder="Acme Corporation" autocomplete="organization">
      </div>
      <div class="row two">
        ${boxField('box1', 'Box 1 — Wages, tips, other compensation')}
        ${boxField('box2', 'Box 2 — Federal income tax withheld')}
        ${boxField('box3', 'Box 3 — Social security wages')}
        ${boxField('box4', 'Box 4 — Social security tax withheld')}
        ${boxField('box5', 'Box 5 — Medicare wages and tips')}
        ${boxField('box6', 'Box 6 — Medicare tax withheld')}
        ${boxField('box7', 'Box 7 — Social security tips')}
        ${boxField('box10', 'Box 10 — Dependent care benefits')}
        ${boxField('box12d', 'Box 12 code D — 401(k) deferrals')}
        ${boxField('box12w', 'Box 12 code W — HSA through payroll')}
      </div>
      <div class="check">
        <input type="checkbox" id="box13" checked>
        <label for="box13">Box 13 — covered by a retirement plan at work</label>
      </div>
      <h3>State lines (boxes 15–17)</h3>
      <div class="row three">
        <div class="field">
          <label for="w2-state">State</label>
          <select id="w2-state"><option value="">None</option>${stateOptions('')}</select>
        </div>
        ${boxField('box16', 'Box 16 — State wages')}
        ${boxField('box17', 'Box 17 — State tax withheld')}
      </div>
      <button class="btn" id="save-boxes">Save this W-2</button>
    </div>

    <div id="doc-text" hidden>
      <div class="field">
        <label for="paste">Paste the W-2 text</label>
        <textarea id="paste" placeholder="1 Wages, tips, other compensation  95,000.00&#10;2 Federal income tax withheld  11,800.00&#10;…"></textarea>
      </div>
      <button class="btn" id="save-text">Read it</button>
      <div class="note info">Anything that cannot be read is flagged for you to fill in by hand rather than guessed at.</div>
    </div>

    <div id="doc-file" hidden>
      <div class="field">
        <label for="upload">W-2 file <span class="hint">PDF, photo, or text</span></label>
        <input id="upload" type="file" accept=".pdf,.png,.jpg,.jpeg,.heic,.tif,.tiff,.txt,.csv">
      </div>
      <button class="btn" id="save-file">Upload</button>
      <div class="note warn">
        A PDF or photo is stored encrypted but is <strong>not</strong> read — this server
        does not run OCR. You will still need to type the boxes for an estimate. Better to
        type them now.
      </div>
    </div>
  </div>

  ${docs.length ? `<div class="actions"><button class="btn" data-go="estimate">Continue to the estimate</button></div>` : ''}`;
}

function boxField(id, label) {
  return `<div class="field">
    <label for="${id}">${label}</label>
    <input id="${id}" inputmode="decimal" placeholder="0.00">
  </div>`;
}

function documentRow(doc) {
  const problems = (doc.warnings || []).filter((w) => w.severity === 'error');
  const notes = (doc.warnings || []).filter((w) => w.severity !== 'error');
  const wages = (doc.payload && doc.payload.box1_wages) || 0;
  return `
  <div class="docrow">
    <div class="grow">
      <div class="name">${esc(doc.employer_name || 'Unnamed employer')} · ${doc.tax_year}</div>
      <div class="meta">
        Box 1 ${money(wages)}${doc.state_code ? ` · ${esc(doc.state_code)}` : ''} ·
        <span class="pill ${doc.status === 'needs_review' ? 'attention' : 'ok'}">${esc(doc.status.replace('_', ' '))}</span>
      </div>
    </div>
    <button data-delete-doc="${doc.id}" aria-label="Remove this W-2">Remove</button>
  </div>
  ${problems.length ? `<div class="note bad"><strong>Check this form:</strong><ul>${
    problems.map((w) => `<li>${esc(w.message)}</li>`).join('')}</ul></div>` : ''}
  ${notes.length ? `<div class="note info"><ul>${
    notes.map((w) => `<li>${esc(w.message)}</li>`).join('')}</ul></div>` : ''}`;
}

/* ========================================================== 3. ESTIMATE */
function viewEstimate() {
  const s = state.situation;
  const result = state.estimate;
  const compare = state.compare;

  return `
  ${steps('Estimate')}
  <div class="card">
    <h2>Your situation</h2>
    <p class="sub">Only what your W-2 cannot know. Leave anything blank and it counts as zero.</p>
    <div class="row two">
      <div class="field">
        <label for="filing-status">Filing status</label>
        <select id="filing-status">
          ${[['single', 'Single'], ['married_jointly', 'Married filing jointly'],
             ['married_separately', 'Married filing separately'],
             ['head_of_household', 'Head of household'],
             ['qualifying_surviving_spouse', 'Qualifying surviving spouse']]
            .map(([v, l]) => `<option value="${v}" ${s.filing_status === v ? 'selected' : ''}>${l}</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label for="resident-state">State you live in</label>
        <select id="resident-state"><option value="">From your W-2</option>${stateOptions(s.resident_state || '')}</select>
      </div>
    </div>
    <div class="row three">
      ${numField('age', 'Your age', s.age ?? 40)}
      ${numField('spouse_age', 'Spouse age', s.spouse_age ?? '')}
      ${numField('children_under_17', 'Children under 17', s.children_under_17 ?? 0)}
    </div>
    <div class="row three">
      ${numField('other_dependents', 'Other dependents', s.other_dependents ?? 0)}
      ${numField('dependent_care_expenses', 'Childcare paid', s.dependent_care_expenses ?? '')}
      ${numField('estimated_payments', 'Estimated tax paid', s.estimated_payments ?? '')}
    </div>

    <details>
      <summary style="cursor:pointer;color:var(--accent);font-weight:550;margin:8px 0">
        Other income, deductions and planning details
      </summary>
      <h3>Other income</h3>
      <div class="row three">
        ${numField('taxable_interest', 'Interest', s.taxable_interest ?? '')}
        ${numField('ordinary_dividends', 'Dividends', s.ordinary_dividends ?? '')}
        ${numField('qualified_dividends', 'of which qualified', s.qualified_dividends ?? '')}
        ${numField('long_term_gains', 'Long-term gains', s.long_term_gains ?? '')}
        ${numField('short_term_gains', 'Short-term gains', s.short_term_gains ?? '')}
        ${numField('self_employment_income', 'Self-employment profit', s.self_employment_income ?? '')}
        ${numField('social_security_benefits', 'Social Security received', s.social_security_benefits ?? '')}
        ${numField('retirement_distributions', 'Retirement withdrawals', s.retirement_distributions ?? '')}
        ${numField('unemployment', 'Unemployment', s.unemployment ?? '')}
      </div>
      <h3>Deductions you might itemise</h3>
      <div class="row three">
        ${numField('state_local_income_tax', 'State income tax paid', s.state_local_income_tax ?? '')}
        ${numField('property_tax', 'Property tax', s.property_tax ?? '')}
        ${numField('mortgage_interest', 'Mortgage interest', s.mortgage_interest ?? '')}
        ${numField('charitable_cash', 'Charitable giving', s.charitable_cash ?? '')}
        ${numField('medical_expenses', 'Medical costs', s.medical_expenses ?? '')}
        ${numField('student_loan_interest', 'Student loan interest', s.student_loan_interest ?? '')}
      </div>
      <h3>For planning mode</h3>
      <div class="row three">
        ${numField('existing_401k', '401(k) so far this year', s.existing_401k ?? '')}
        ${numField('existing_hsa', 'HSA so far this year', s.existing_hsa ?? '')}
        ${numField('traditional_ira', 'IRA already contributed', s.traditional_ira ?? '')}
      </div>
      <div class="check">
        <input type="checkbox" id="has_hdhp" ${s.has_hdhp ? 'checked' : ''}>
        <label for="has_hdhp">I have a high-deductible health plan (so an HSA is available)</label>
      </div>
      <div class="check">
        <input type="checkbox" id="hdhp_family" ${s.hdhp_family ? 'checked' : ''}>
        <label for="hdhp_family">That plan covers my family, not just me</label>
      </div>
    </details>
  </div>

  <div class="card">
    <h2>How should we work it out?</h2>
    <p class="sub">Both use the same figures. Planning also shows what you could still change.</p>
    <div class="choices two">
      <button class="choice" data-method="regular" aria-pressed="${state.method === 'regular'}">
        <strong>Regular</strong>
        <span>Your return exactly as your documents stand today.</span>
        ${compare ? `<span class="figure">${money(compare.regular.total_tax)} tax</span>` : ''}
      </button>
      <button class="choice" data-method="planning" aria-pressed="${state.method === 'planning'}">
        <strong>Planning</strong>
        <span>The same year with the moves you can still make applied.</span>
        ${compare ? `<span class="figure">${money(compare.planning.total_tax)} tax</span>` : ''}
      </button>
    </div>
    ${compare ? (Number(compare.difference) > 0 ? `
      <div class="note good" style="margin-top:12px">
        Planning is ${money(compare.difference)} better on these figures, across
        ${compare.planning.available_moves} move(s) still open to you${
          compare.planning.closed_moves ? ` — ${compare.planning.closed_moves} more have already closed for this year` : ''}.
      </div>` : `
      <div class="note ${compare.planning.closed_moves ? 'warn' : 'info'}" style="margin-top:12px">
        ${compare.planning.closed_moves
          ? `Both come to the same figure: all ${compare.planning.closed_moves} planning
             move(s) for this year have passed their deadline. Planning mode still lists
             them, so you know what to do next year.`
          : 'Both come to the same figure — there is no move available that lowers this return.'}
      </div>`) : ''}
    <div class="actions">
      <button class="btn" id="run-estimate">${result ? 'Run it again' : 'Work out my tax'}</button>
      <button class="btn ghost" id="run-compare">Compare both</button>
    </div>
  </div>

  ${result ? estimateResult(result) : `
    <div class="card"><div class="empty">
      <span class="glyph">◎</span>No estimate yet.<br>Fill in what applies and run it.
    </div></div>`}`;
}

function numField(id, label, value) {
  return `<div class="field">
    <label for="${id}">${label}</label>
    <input id="${id}" inputmode="decimal" value="${value === '' || value === null || value === undefined ? '' : esc(value)}" placeholder="0">
  </div>`;
}

function estimateResult(result) {
  const totals = result.totals;
  const balance = Number(totals.total_balance);
  const refund = balance < 0;
  const federal = result.federal;

  return `
  <div class="headline ${refund ? '' : 'owed'}">
    <div class="label">${refund ? 'Estimated refund' : balance > 0 ? 'Estimated balance to pay' : 'Estimated result'}</div>
    <div class="amount ${refund ? 'good' : 'bad'}">${money2(Math.abs(balance))}</div>
    <div class="note">${esc(result.headline)} · ${result.tax_year} · ${esc(result.method)} method</div>
  </div>

  ${result.baseline ? `
    <div class="note good">
      Filing as-is would cost ${money(result.baseline.totals.total_tax)}.
      With the moves below applied it is ${money(totals.total_tax)} —
      <strong>${money(result.saving_against_baseline)} less</strong>.
    </div>` : ''}

  ${(result.warnings || []).length ? `
    <div class="note ${result.warnings.some((w) => w.severity === 'error') ? 'bad' : 'warn'}">
      <strong>Worth checking on your forms:</strong>
      <ul>${result.warnings.map((w) => `<li>${esc(w.message)}</li>`).join('')}</ul>
    </div>` : ''}

  <div class="card">
    <h2>Federal</h2>
    <dl class="kv">
      <dt>Adjusted gross income</dt><dd>${money(federal.agi)}</dd>
      <dt>${esc(federal.deduction_kind === 'itemised' ? 'Itemised' : 'Standard')} deduction and others</dt><dd>−${money(federal.deduction_taken)}</dd>
      <dt>Taxable income</dt><dd>${money(federal.taxable_income)}</dd>
      <dt>Tax before credits</dt><dd>${money(Number(federal.ordinary_tax) + Number(federal.preferential_tax))}</dd>
      ${Number(federal.nonrefundable_credits) ? `<dt>Credits against tax</dt><dd>−${money(federal.nonrefundable_credits)}</dd>` : ''}
      ${Number(federal.self_employment_tax) ? `<dt>Self-employment tax</dt><dd>${money(federal.self_employment_tax)}</dd>` : ''}
      ${Number(federal.net_investment_income_tax) ? `<dt>Net investment income tax</dt><dd>${money(federal.net_investment_income_tax)}</dd>` : ''}
      ${Number(federal.additional_medicare_tax) ? `<dt>Additional Medicare tax</dt><dd>${money(federal.additional_medicare_tax)}</dd>` : ''}
      <dt><strong>Total federal tax</strong></dt><dd><strong>${money(federal.total_tax)}</strong></dd>
      <dt>Withheld and paid</dt><dd>${money(federal.total_payments)}</dd>
      <dt><strong>${Number(federal.balance) > 0 ? 'Federal owed' : 'Federal refund'}</strong></dt>
      <dd><strong>${money(Math.abs(Number(federal.balance)))}</strong></dd>
      <dt>Effective rate</dt><dd>${percent(totals.effective_rate)}</dd>
      <dt>Top rate on your next dollar</dt><dd>${percent(totals.marginal_rate)}</dd>
    </dl>
    <details style="margin-top:12px">
      <summary style="cursor:pointer;color:var(--accent);font-weight:550">Show every line</summary>
      <table class="lines" style="margin-top:10px">
        <tbody>${(federal.lines || []).map((line) => `
          <tr><td>${esc(line.label)}<span class="form">${esc(line.form)}${line.note ? ` · ${esc(line.note)}` : ''}</span></td>
              <td class="num">${money2(line.amount)}</td></tr>`).join('')}
        </tbody>
      </table>
    </details>
  </div>

  ${(result.states || []).map(stateCard).join('')}

  ${result.method === 'planning' ? strategiesCard(result) : ''}

  ${(result.notes || []).length ? `
    <div class="card">
      <h2>How this was worked out</h2>
      <ul style="margin:0;padding-left:18px;color:var(--muted);font-size:13.5px">
        ${result.notes.map((n) => `<li style="margin-bottom:6px">${esc(n)}</li>`).join('')}
      </ul>
    </div>` : ''}

  <div class="actions">
    <button class="btn" data-go="payment">${refund ? 'Choose how to get paid' : 'Choose how to pay'}</button>
    <button class="btn ghost" data-go="filings">Check earlier years</button>
  </div>`;
}

function stateCard(s) {
  const balance = Number(s.balance);
  return `
  <div class="card">
    <h2>${esc(s.name)}${s.is_resident ? '' : ' (non-resident)'}</h2>
    <p class="sub">${s.kind === 'none' ? 'No income tax on wages.'
      : s.kind === 'flat' ? 'Flat rate state.' : 'Graduated rate schedule.'}</p>
    ${s.kind === 'none' && !Number(s.total_tax) ? '' : `
    <dl class="kv">
      <dt>Taxable income</dt><dd>${money(s.taxable_income)}</dd>
      <dt>State tax</dt><dd>${money(s.tax)}</dd>
      ${Number(s.local_tax) ? `<dt>Local income tax</dt><dd>${money(s.local_tax)}</dd>` : ''}
      ${Number(s.surtax) ? `<dt>Surtax</dt><dd>${money(s.surtax)}</dd>` : ''}
      ${Number(s.credits) ? `<dt>Credits</dt><dd>−${money(s.credits)}</dd>` : ''}
      <dt><strong>Total</strong></dt><dd><strong>${money(s.total_tax)}</strong></dd>
      <dt>Withheld</dt><dd>${money(s.withheld)}</dd>
      <dt><strong>${balance > 0 ? 'Owed' : 'Refund'}</strong></dt><dd><strong>${money(Math.abs(balance))}</strong></dd>
    </dl>`}
    ${(s.notes || []).length ? `<div class="note info"><ul>${
      s.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul></div>` : ''}
  </div>`;
}

function strategiesCard(result) {
  const list = result.strategies || [];
  if (!list.length) return `<div class="card"><h2>Planning</h2>
    <p class="sub">Nothing available changes this year's outcome.</p></div>`;

  const open = list.filter((s) => s.still_available && s.actionable);
  const shut = list.filter((s) => !s.still_available);
  const info = list.filter((s) => s.still_available && !s.actionable);
  const applied = new Set(result.applied || []);

  // The honest headline when the year is already over: nothing here can change
  // this return, and saying so first is better than leading with dollar figures
  // for moves whose deadlines have passed.
  const missed = shut.reduce((sum, s) => sum + Number(s.total_saving), 0);

  return `
  <div class="card">
    <h2>Planning options</h2>
    <p class="sub">
      Each figure is what that move actually saves, measured by running your whole
      return again with it applied — not a rate multiplied by a contribution.
    </p>

    ${open.length === 0 && shut.length ? `
      <div class="note warn">
        <strong>Every planning move for ${result.tax_year} has closed.</strong>
        Nothing below can change this return now — the figures are what each one
        <em>would</em> have saved${missed > 0 ? `, ${money(missed)} between them` : ''}.
        Treat this as the list to act on for ${result.tax_year + 1}, and set a
        reminder before the deadlines shown.
      </div>` : ''}

    ${open.length ? `
      <h3>Still open to you</h3>
      ${open.map((s) => strategyRow(s, applied.has(s.id))).join('')}
      <div class="note info">
        Ticked moves are included in the figures above. Untick to see the return
        without them; savings interact, so the combined total is not always the
        sum of the parts.
      </div>` : ''}

    ${shut.length ? `<h3>${open.length ? `Closed for ${result.tax_year}` : `Missed for ${result.tax_year}`}</h3>${
      shut.map((s) => strategyRow(s, false)).join('')}` : ''}
    ${info.length ? `<h3>Worth knowing</h3>${info.map((s) => strategyRow(s, false)).join('')}` : ''}
  </div>`;
}

function strategyRow(s, applied) {
  const saving = Number(s.total_saving);
  return `
  <div class="strategy ${s.still_available ? '' : 'shut'}">
    <header>
      ${s.actionable && s.still_available ? `
        <input type="checkbox" class="strategy-toggle" data-strategy="${esc(s.id)}"
               ${applied ? 'checked' : ''} aria-label="Apply ${esc(s.label)}"
               style="width:18px;height:18px;margin-top:3px;flex:none">` : ''}
      <div class="grow">
        <h4>${esc(s.label)}</h4>
        <p>${esc(s.summary)}</p>
      </div>
      ${saving > 0 ? `<div class="save" title="${s.still_available ? 'Saving if you do this' : 'What this would have saved'}">${
        s.still_available ? '' : '<span style="font-size:11px;color:var(--muted);display:block;font-weight:400">would have saved</span>'
      }${money(saving)}</div>` : ''}
    </header>
    <details>
      <summary>Why this works${s.caveats && s.caveats.length ? ' · what to watch' : ''}</summary>
      <p>${esc(s.how_it_works)}</p>
      ${s.caveats && s.caveats.length ? `<ul style="margin-top:8px;padding-left:18px">${
        s.caveats.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}
    </details>
    <div class="meta">
      ${Number(s.cash_required) > 0 ? `Costs ${money(s.cash_required)} of cash · saves ${
        (Number(s.return_on_cash) * 100).toFixed(0)}¢ per dollar · ` : ''}
      ${s.closes_on ? (s.still_available
        ? `<span class="pill ok">closes ${day(s.closes_on)}</span>`
        : `<span class="pill shut">closed ${day(s.closes_on)}</span>`)
        : '<span class="pill ok">no deadline</span>'}
      ${s.confidence !== 'high' ? ` · <span class="pill attention">${esc(s.confidence)} confidence</span>` : ''}
    </div>
  </div>`;
}

/* ============================================================ 4. FILINGS */
function viewFilings() {
  const data = state.filings;
  if (!data) return `<div class="card"><div class="skeleton" style="width:70%"></div>
    <div class="skeleton" style="width:90%"></div><div class="skeleton" style="width:55%"></div></div>`;

  const summary = data.summary;
  const bad = summary.unfiled_years.length > 0;
  return `
  <div class="card">
    <h2>Earlier years</h2>
    <p class="sub">Checked against ${esc(data.taxpayer.ssn || 'your record')} — federal and every state your documents touch.</p>
    <div class="note ${bad ? 'warn' : 'good'}"><strong>${esc(summary.headline)}</strong></div>
    ${bad ? `<dl class="kv" style="margin-top:12px">
      <dt>Estimated tax owed</dt><dd>${money(summary.estimated_owed)}</dd>
      <dt>Penalties and interest so far</dt><dd>${money(summary.estimated_penalties)}</dd>
      <dt>Refunds still claimable</dt><dd>${money(summary.claimable_refunds)}</dd>
      ${Number(summary.forfeited_refunds) ? `<dt>Refunds already forfeited</dt><dd>${money(summary.forfeited_refunds)}</dd>` : ''}
    </dl>` : ''}
  </div>

  <div class="card">
    <h2>Year by year</h2>
    ${(() => {
      // Years with no return on record AND no documents say the same thing each
      // time. Five identical rows bury the years that actually need attention,
      // so they collapse into one line that can be opened.
      const known = data.years.filter((y) => y.status !== 'unknown');
      const unknown = data.years.filter((y) => y.status === 'unknown');
      return known.map(yearRow).join('') + (unknown.length ? `
        <details style="margin-top:${known.length ? '14px' : '0'}">
          <summary style="cursor:pointer;color:var(--accent);font-weight:550;font-size:13.5px">
            ${unknown.length} earlier year(s) with nothing on record
            (${unknown.map((y) => y.tax_year).join(', ')})
          </summary>
          <div class="note info" style="margin-top:8px">
            No return recorded and no documents held for these years. That is not
            evidence of a missing return — it may be before you joined, or a year
            with no filing requirement at all. Upload a W-2 for any of them and it
            will be checked properly.
          </div>
        </details>` : '');
    })()}
  </div>

  <div class="card">
    <h2>Already filed one of these?</h2>
    <p class="sub">Tell us and it stops being flagged. It is recorded as your word, not as IRS confirmation.</p>
    <div class="row three">
      <div class="field">
        <label for="filed-year">Year</label>
        <select id="filed-year">${(data.years || []).map((y) => y.tax_year)
          .filter((v, i, a) => a.indexOf(v) === i)
          .map((y) => `<option value="${y}">${y}</option>`).join('')}</select>
      </div>
      <div class="field">
        <label for="filed-where">Return</label>
        <select id="filed-where">
          <option value="federal">Federal</option>
          ${data.years.filter((y) => y.state_code).map((y) => y.state_code)
            .filter((v, i, a) => a.indexOf(v) === i)
            .map((c) => `<option value="state:${c}">${c} state</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label for="filed-on">Date filed</label>
        <input id="filed-on" type="date">
      </div>
    </div>
    <button class="btn" id="record-filing">Record it as filed</button>
  </div>

  <div class="note info">${esc(data.source_note)}</div>`;
}

function yearRow(y) {
  return `
  <div class="year">
    <div class="when">${y.tax_year}</div>
    <div class="grow">
      <div class="what">
        <span class="pill ${esc(y.severity)}">${esc(y.status.replace('_', ' '))}</span>
        ${y.state_code ? `<span class="pill shut">${esc(y.state_code)}</span>` : '<span class="pill shut">federal</span>'}
        ${y.source !== 'none' ? `<span class="pill shut">${esc(y.source.replace('_', ' '))}</span>` : ''}
      </div>
      <div class="detail">${esc(y.action)}</div>
      ${y.penalties && Number(y.penalties.total) ? `
        <div class="detail">
          Penalties and interest so far: <strong>${money(y.penalties.total)}</strong>
          (${money(y.penalties.failure_to_file)} for not filing,
           ${money(y.penalties.failure_to_pay)} for not paying,
           ${money(y.penalties.interest)} interest)
        </div>` : ''}
      ${(y.notes || []).map((n) => `<div class="detail">${esc(n)}</div>`).join('')}
    </div>
  </div>`;
}

/* ============================================================ 5. PAYMENT */
function viewPayment() {
  const result = state.estimate;
  if (!result) return `<div class="card"><div class="empty">
    <span class="glyph">◈</span>Run an estimate first and the payment options follow from it.
    <div class="actions" style="justify-content:center;margin-top:16px">
      <button class="btn" data-go="estimate">Go to the estimate</button></div>
  </div></div>`;

  const payment = result.payment || {};
  const options = payment.options || [];
  const refund = payment.direction === 'refund';
  const balance = Number(result.totals.total_balance);
  const nextYear = result.next_year || {};

  return `
  ${steps('Pay')}
  <div class="headline ${refund ? '' : 'owed'}">
    <div class="label">${refund ? 'To come back to you' : 'To pay'}</div>
    <div class="amount ${refund ? 'good' : 'bad'}">${money2(Math.abs(balance))}</div>
    <div class="note">
      Federal ${money(Math.abs(Number(result.totals.federal_balance)))}
      ${Number(result.totals.state_balance) ? ` · state ${money(Math.abs(Number(result.totals.state_balance)))}` : ''}
    </div>
  </div>

  <div class="card">
    <h2>${refund ? 'How to receive it' : 'How to settle it'}</h2>
    <p class="sub">${refund
      ? 'Direct deposit is both the fastest and the hardest to lose.'
      : 'Every route is priced to its total cost, including the fees and interest of spreading it.'}</p>
    ${options.map(paymentRow).join('')}
  </div>

  ${!refund ? `
  <div class="card">
    <h2>Bank details</h2>
    <p class="sub">Only needed for direct debit. Encrypted before storage; only the last four digits are ever shown again.</p>
    <div class="row two">
      <div class="field">
        <label for="routing">Routing number <span class="hint">9 digits</span></label>
        <input id="routing" inputmode="numeric" maxlength="9" placeholder="121000248" autocomplete="off">
      </div>
      <div class="field">
        <label for="account">Account number</label>
        <input id="account" inputmode="numeric" placeholder="000123456789" autocomplete="off">
      </div>
    </div>
    <div class="field">
      <label for="account-type">Account type</label>
      <select id="account-type"><option value="checking">Checking</option><option value="savings">Savings</option></select>
    </div>
    <div id="chosen-method" class="note info">Pick a method above, then save.</div>
    <button class="btn" id="save-payment" disabled>Save my choice</button>
  </div>` : `
  <div class="card">
    <h2>Where to send it</h2>
    <p class="sub">Encrypted before storage; only the last four digits are ever shown again.</p>
    <div class="row two">
      <div class="field">
        <label for="routing">Routing number</label>
        <input id="routing" inputmode="numeric" maxlength="9" placeholder="121000248" autocomplete="off">
      </div>
      <div class="field">
        <label for="account">Account number</label>
        <input id="account" inputmode="numeric" placeholder="000123456789" autocomplete="off">
      </div>
    </div>
    <div id="chosen-method" class="note info">Pick a method above, then save.</div>
    <button class="btn" id="save-payment" disabled>Save my choice</button>
  </div>`}

  ${Number(nextYear.shortfall) > 0 ? `
  <div class="card">
    <h2>Staying ahead next year</h2>
    <p class="sub">What to pay in quarterly so next year does not bring a penalty.</p>
    <dl class="kv">
      <dt>Safe-harbour target</dt><dd>${money(nextYear.target_payments)}</dd>
      <dt>Expected withholding</dt><dd>${money(nextYear.expected_withholding)}</dd>
      <dt>Shortfall to cover</dt><dd>${money(nextYear.shortfall)}</dd>
      <dt><strong>Each quarter</strong></dt><dd><strong>${money(nextYear.quarterly_amount)}</strong></dd>
    </dl>
    ${(nextYear.schedule || []).length ? `<table class="lines" style="margin-top:12px">
      <thead><tr><th>Due</th><th style="text-align:right">Amount</th></tr></thead>
      <tbody>${nextYear.schedule.map((p) =>
        `<tr><td>${day(p.due_on)}</td><td class="num">${money2(p.amount)}</td></tr>`).join('')}</tbody>
    </table>` : ''}
    <div class="note info">${esc(nextYear.note || '')}</div>
  </div>` : ''}`;
}

function paymentRow(option) {
  const extra = Number(option.cost_of_delay);
  return `
  <button class="choice" data-payment="${esc(option.method)}" aria-pressed="false"
          ${option.available ? '' : 'disabled style="opacity:.55;cursor:not-allowed"'}>
    <strong>${esc(option.label)}${option.recommended ? ' · recommended' : ''}</strong>
    ${option.instalments > 1
      ? `<span>${option.instalments} payments of ${money2(option.instalment_amount)}</span>`
      : `<span>${option.first_due_on ? `By ${day(option.first_due_on)}` : 'No deadline to wait for'}</span>`}
    ${extra > 0 ? `<span style="color:var(--warn)">Costs ${money(extra)} extra in fees, penalty and interest</span>` : ''}
    ${Number(option.total_cost) ? `<span class="figure">${money2(option.total_cost)} in total</span>` : ''}
    ${(option.notes || []).slice(0, 2).map((n) => `<span style="margin-top:6px">${esc(n)}</span>`).join('')}
  </button>`;
}

/* ------------------------------------------------------------------ wire */
function wire() {
  document.querySelectorAll('[data-go]').forEach((node) =>
    node.addEventListener('click', () => go(node.dataset.go)));

  const signOut = el('sign-out');
  if (signOut) signOut.addEventListener('click', () => {
    writeToken(null);
    try { sessionStorage.removeItem('taxvault.situation'); } catch {}
    Object.assign(state, {
      session: null, documents: [], estimate: null, compare: null,
      filings: null, situation: {}, view: 'account', chosen: null,
    });
    render();
    toast('Signed out.');
  });

  wireSignIn();
  wireIdentity();
  wireDocuments();
  wireEstimate();
  wireFilings();
  wirePayment();
}

function wireSignIn() {
  const send = el('send-code');
  if (send) {
    const submit = () => guard(send, async () => {
      const email = el('email').value.trim();
      if (!email) throw new Error('Enter your email address first.');
      const result = await api('/api/auth/sign-in', { method: 'POST', body: { email } });
      el('signin-step-email').hidden = true;
      el('signin-step-code').hidden = false;
      el('code-sent').innerHTML = result.development_code
        ? `Development mode — your code is <strong class="mono">${esc(result.development_code)}</strong>. `
          + 'On a real deployment this arrives by email.'
        : `Code sent to ${esc(result.sent_to)}. It expires in ${
            Math.round(result.expires_in_seconds / 60)} minutes.`;
      el('code').focus();
    });
    send.addEventListener('click', submit);
    el('email').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  }

  const verify = el('verify-code');
  if (verify) {
    const submit = () => guard(verify, async () => {
      const result = await api('/api/auth/verify', {
        method: 'POST',
        body: { email: el('email').value.trim(), code: el('code').value.trim() },
      });
      writeToken(result.token);
      await refreshSession();
      toast(result.created ? 'Account created.' : 'Welcome back.');
      go('account');
    });
    verify.addEventListener('click', submit);
    el('code').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  }

  const back = el('back-to-email');
  if (back) back.addEventListener('click', () => {
    el('signin-step-code').hidden = true;
    el('signin-step-email').hidden = false;
  });
}

function wireIdentity() {
  const sendSms = el('send-sms');
  if (sendSms) sendSms.addEventListener('click', () => guard(sendSms, async () => {
    const mobile = el('mobile').value.trim();
    if (!mobile) throw new Error('Enter your mobile number first.');
    const result = await api('/api/auth/mobile/start', { method: 'POST', body: { mobile } });
    // Verifying re-renders this view and the input disappears, so hold on to it.
    state.pendingMobile = mobile;
    el('mobile-step-number').hidden = true;
    el('mobile-step-code').hidden = false;
    el('sms-sent').innerHTML = result.development_code
      ? `Development mode — your code is <strong class="mono">${esc(result.development_code)}</strong>.`
      : `Code sent to ${esc(result.sent_to)}.`;
    el('sms-code').focus();
  }));

  const verifySms = el('verify-sms');
  if (verifySms) verifySms.addEventListener('click', () => guard(verifySms, async () => {
    await api('/api/auth/mobile/verify', { method: 'POST', body: { code: el('sms-code').value.trim() } });
    await refreshSession();
    toast('Mobile number confirmed.');
    render();
  }));

  const ssn = el('ssn');
  if (ssn) ssn.addEventListener('input', () => {
    // Format as they type, so the field reads like the card in their hand.
    const digits = ssn.value.replace(/\D/g, '').slice(0, 9);
    ssn.value = digits.length > 5 ? `${digits.slice(0, 3)}-${digits.slice(3, 5)}-${digits.slice(5)}`
      : digits.length > 3 ? `${digits.slice(0, 3)}-${digits.slice(3)}` : digits;
  });

  const submit = el('submit-identity');
  if (submit) submit.addEventListener('click', () => guard(submit, async () => {
    const result = await api('/api/auth/identity', {
      method: 'POST',
      body: {
        ssn: el('ssn').value.trim(),
        email: state.session.account.email,
        // Empty means "the number already verified on this account", which the
        // server resolves itself -- it knows it, and the code proved it.
        mobile: state.pendingMobile,
        first_name: el('first-name').value.trim(),
        last_name: el('last-name').value.trim(),
        resident_state: el('home-state').value,
      },
    });
    writeToken(result.token);
    await refreshSession();
    toast('Identity confirmed.');
    go('documents');
  }));
}

function wireDocuments() {
  document.querySelectorAll('[data-doc-mode]').forEach((button) =>
    button.addEventListener('click', () => {
      const mode = button.dataset.docMode;
      document.querySelectorAll('[data-doc-mode]').forEach((b) =>
        b.setAttribute('aria-pressed', String(b === button)));
      ['boxes', 'text', 'file'].forEach((m) => {
        const pane = el(`doc-${m}`);
        if (pane) pane.hidden = m !== mode;
      });
    }));

  const num = (id) => {
    const node = el(id);
    return node ? (parseFloat(String(node.value).replace(/[^0-9.\-]/g, '')) || 0) : 0;
  };

  const saveBoxes = el('save-boxes');
  if (saveBoxes) saveBoxes.addEventListener('click', () => guard(saveBoxes, async () => {
    if (!num('box1')) throw new Error('Box 1 is the wages figure, and it cannot be empty.');
    const box12 = {};
    if (num('box12d')) box12.D = num('box12d');
    if (num('box12w')) box12.W = num('box12w');
    const stateCode = el('w2-state').value;
    await api('/api/documents/w2/boxes', { method: 'POST', body: {
      tax_year: parseInt(el('doc-year').value, 10),
      employer_name: el('employer').value.trim(),
      box1_wages: num('box1'), box2_federal_withheld: num('box2'),
      box3_social_security_wages: num('box3'), box4_social_security_withheld: num('box4'),
      box5_medicare_wages: num('box5'), box6_medicare_withheld: num('box6'),
      box7_social_security_tips: num('box7'), box10_dependent_care: num('box10'),
      box12, box13: { retirement_plan: el('box13').checked },
      states: stateCode ? [{ state: stateCode, state_wages: num('box16') || num('box1'),
                             state_withheld: num('box17') }] : [],
    }});
    toast('W-2 saved.');
    await refreshDocuments();
    render();
  }));

  const saveText = el('save-text');
  if (saveText) saveText.addEventListener('click', () => guard(saveText, async () => {
    const text = el('paste').value.trim();
    if (!text) throw new Error('Paste the W-2 text first.');
    const doc = await api('/api/documents/w2/text', { method: 'POST', body: {
      tax_year: parseInt(el('doc-year').value, 10), text,
    }});
    toast(doc.parse_confidence >= 1
      ? 'Every box was read.'
      : `Read ${Math.round(doc.parse_confidence * 100)}% of the boxes — fill in the rest.`);
    await refreshDocuments();
    render();
  }));

  const saveFile = el('save-file');
  if (saveFile) saveFile.addEventListener('click', () => guard(saveFile, async () => {
    const input = el('upload');
    if (!input.files || !input.files[0]) throw new Error('Choose a file first.');
    const form = new FormData();
    form.append('file', input.files[0]);
    form.append('tax_year', el('doc-year').value);
    const doc = await api('/api/documents/w2/file', { method: 'POST', form });
    const read = Number(doc.parse_confidence || 0);
    toast(read >= 1
      ? 'Read it. Every box came out of the file — check them below.'
      : read > 0
        ? `Read ${Math.round(read * 100)}% of the boxes. Fill in the rest below.`
        : 'Stored securely, but it could not be read. Type the boxes to get an estimate.');
    await refreshDocuments();
    render();
  }));

  document.querySelectorAll('[data-delete-doc]').forEach((button) =>
    button.addEventListener('click', () => guard(null, async () => {
      await api(`/api/documents/${button.dataset.deleteDoc}`, { method: 'DELETE' });
      await refreshDocuments();
      render();
      toast('Removed.');
    })));
}

function collectSituation() {
  const num = (id) => {
    const node = el(id);
    if (!node || node.value === '') return 0;
    return parseFloat(String(node.value).replace(/[^0-9.\-]/g, '')) || 0;
  };
  const int = (id) => Math.round(num(id));
  const situation = {
    filing_status: el('filing-status') ? el('filing-status').value : 'single',
    resident_state: el('resident-state') ? el('resident-state').value : '',
    age: int('age') || 40,
    spouse_age: int('spouse_age'),
    children_under_17: int('children_under_17'),
    other_dependents: int('other_dependents'),
    has_hdhp: el('has_hdhp') ? el('has_hdhp').checked : false,
    hdhp_family: el('hdhp_family') ? el('hdhp_family').checked : false,
  };
  [
    'dependent_care_expenses', 'estimated_payments', 'taxable_interest', 'ordinary_dividends',
    'qualified_dividends', 'long_term_gains', 'short_term_gains', 'self_employment_income',
    'social_security_benefits', 'retirement_distributions', 'unemployment',
    'state_local_income_tax', 'property_tax', 'mortgage_interest', 'charitable_cash',
    'medical_expenses', 'student_loan_interest', 'existing_401k', 'existing_hsa',
    'traditional_ira',
  ].forEach((key) => { situation[key] = num(key); });
  // Self-employment profit is qualified business income unless told otherwise.
  situation.qbi_income = situation.self_employment_income;
  state.situation = situation;
  saveSituation();
  return situation;
}

function estimateBody(method) {
  return {
    tax_year: state.taxYear || (state.years && state.years.current) || 2025,
    method,
    situation: collectSituation(),
    strategies: method === 'planning' ? state.chosen : null,
  };
}

function wireEstimate() {
  document.querySelectorAll('[data-method]').forEach((button) =>
    button.addEventListener('click', () => {
      state.method = button.dataset.method;
      state.chosen = null;
      document.querySelectorAll('[data-method]').forEach((b) =>
        b.setAttribute('aria-pressed', String(b === button)));
    }));

  const run = el('run-estimate');
  if (run) run.addEventListener('click', () => guard(run, async () => {
    state.estimate = await api('/api/estimates', { method: 'POST', body: estimateBody(state.method) });
    render();
    toast(state.estimate.headline);
  }));

  const compare = el('run-compare');
  if (compare) compare.addEventListener('click', () => guard(compare, async () => {
    state.compare = await api('/api/estimates/compare', {
      method: 'POST', body: estimateBody(state.method),
    });
    render();
  }));

  document.querySelectorAll('.strategy-toggle').forEach((box) =>
    box.addEventListener('change', () => guard(null, async () => {
      const chosen = Array.from(document.querySelectorAll('.strategy-toggle'))
        .filter((b) => b.checked).map((b) => b.dataset.strategy);
      state.chosen = chosen;
      state.method = 'planning';
      state.estimate = await api('/api/estimates', {
        method: 'POST', body: estimateBody('planning'),
      });
      render();
    })));
}

function wireFilings() {
  const record = el('record-filing');
  if (!record) return;
  record.addEventListener('click', () => guard(record, async () => {
    const where = el('filed-where').value;
    const isState = where.startsWith('state:');
    await api('/api/filings/record', { method: 'POST', body: {
      tax_year: parseInt(el('filed-year').value, 10),
      jurisdiction: isState ? 'state' : 'federal',
      state_code: isState ? where.split(':')[1] : '',
      state: 'accepted',
      filed_on: el('filed-on').value || null,
    }});
    toast('Recorded.');
    await refreshFilings();
    render();
  }));
}

function wirePayment() {
  let selected = null;
  document.querySelectorAll('[data-payment]').forEach((button) =>
    button.addEventListener('click', () => {
      if (button.disabled) return;
      selected = button.dataset.payment;
      document.querySelectorAll('[data-payment]').forEach((b) =>
        b.setAttribute('aria-pressed', String(b === button)));
      const label = button.querySelector('strong').textContent;
      const note = el('chosen-method');
      if (note) note.textContent = `Selected: ${label}`;
      const save = el('save-payment');
      if (save) save.disabled = false;
    }));

  const save = el('save-payment');
  if (save) save.addEventListener('click', () => guard(save, async () => {
    if (!selected) throw new Error('Pick a method first.');
    if (!state.estimate || !state.estimate.estimate_id) {
      throw new Error('Run and save an estimate first.');
    }
    const routing = el('routing') ? el('routing').value.trim() : '';
    const account = el('account') ? el('account').value.trim() : '';
    const needsBank = ['direct_deposit', 'direct_debit'].includes(selected);
    if (needsBank && (!routing || !account)) {
      throw new Error('That method needs your routing and account numbers.');
    }
    const result = await api('/api/payments/choose', { method: 'POST', body: {
      estimate_id: state.estimate.estimate_id,
      method: selected,
      jurisdiction: 'federal',
      bank: needsBank ? {
        routing_number: routing, account_number: account,
        account_type: el('account-type') ? el('account-type').value : 'checking',
      } : null,
    }});
    toast(`Saved: ${result.label}${result.bank_account ? ` to ${result.bank_account}` : ''}.`);
  }));
}

/* ----------------------------------------------------------------- loads */
async function refreshSession() {
  state.session = await api('/api/auth/session');
  if (!state.session.signed_in) writeToken(null);
  return state.session;
}

async function refreshDocuments() {
  try {
    const data = await api('/api/documents');
    state.documents = data.documents;
    if (state.view === 'documents') render();
  } catch (error) {
    if (error.status !== 403) toast(error.message, 'bad');
  }
}

async function refreshFilings() {
  try {
    state.filings = await api('/api/filings/history');
    if (state.view === 'filings') render();
  } catch (error) {
    if (error.status !== 403) toast(error.message, 'bad');
  }
}

async function boot() {
  state.token = readToken();
  document.querySelectorAll('#tabs button').forEach((button) =>
    button.addEventListener('click', () => { if (!button.disabled) go(button.dataset.view); }));

  try {
    const [years, states] = await Promise.all([
      api('/api/reference/years'),
      api('/api/reference/states'),
    ]);
    state.years = years;
    state.states = states;
    state.taxYear = years.current;
  } catch (error) {
    toast(error.message, 'bad');
  }

  if (state.token) {
    try { await refreshSession(); } catch { writeToken(null); }
  }
  render();
  if (state.session && state.session.identity_verified) refreshDocuments();

  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
}

boot();
