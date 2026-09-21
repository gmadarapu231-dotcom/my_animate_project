/* Demo shim: the real client, running with no server behind it.
 *
 * `build_demo.py` drives the real API in-process, captures every response the
 * journey produces, and inlines them here as `window.TAXVAULT_FIXTURES`. This
 * file then stands in front of `fetch` and answers from those captures.
 *
 * The figures a visitor sees are therefore the figures the real engine
 * computed -- not numbers typed into a mockup. What is fake is only the
 * transport and the identity: any email is accepted, the code is always
 * 000000, and nothing is stored anywhere.
 *
 * Loaded only by the demo build. The normal app never sees this file.
 */
'use strict';

(function installDemoTransport() {
  const F = window.TAXVAULT_FIXTURES || {};

  // The demo advances through the same three states the real session does, so
  // the gates behave the way they really do rather than being skipped.
  const state = {
    signedIn: false,
    mobileVerified: false,
    identityVerified: false,
    documents: [],
    heldForTax: 0,
    feesPaid: 0,
    ledger: [],
    authorizations: [],
  };

  const json = (body, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  function session() {
    if (!state.signedIn) return { signed_in: false, identity_verified: false };
    const base = JSON.parse(JSON.stringify(F.session));
    base.identity_verified = state.identityVerified;
    base.account.mobile_verified = state.mobileVerified;
    if (!state.identityVerified) base.taxpayer = null;
    return base;
  }

  const ROUTES = [
    ['GET', /^\/api\/reference\/years$/, () => F.years],
    ['GET', /^\/api\/reference\/states$/, () => F.states],
    ['GET', /^\/api\/auth\/session$/, () => session()],
    ['GET', /^\/api\/reference\/irs/, () => F.irs],

    ['POST', /^\/api\/auth\/sign-in$/, () => ({
      channel: 'email', sent_to: 'y•••@example.com', delivered: false,
      code_length: 6, expires_in_seconds: 600, attempts_allowed: 5,
      development_code: '000000',
    })],
    ['POST', /^\/api\/auth\/verify$/, (body) => {
      if ((body.code || '').trim() !== '000000') {
        return [{ detail: 'In this demo the code is always 000000.' }, 401];
      }
      state.signedIn = true;
      return { token: 'demo', created: true, identity_verified: false,
               expires_in_seconds: 43200, account: session().account };
    }],
    ['POST', /^\/api\/auth\/mobile\/start$/, () => ({
      channel: 'sms', sent_to: '(•••) •••-0132', delivered: false,
      code_length: 6, expires_in_seconds: 600, attempts_allowed: 5,
      development_code: '000000',
    })],
    ['POST', /^\/api\/auth\/mobile\/verify$/, (body) => {
      if ((body.code || '').trim() !== '000000') {
        return [{ detail: 'In this demo the code is always 000000.' }, 400];
      }
      state.mobileVerified = true;
      return { mobile: '(•••) •••-0132', verified: true };
    }],
    ['POST', /^\/api\/auth\/identity$/, (body) => {
      // The SSN rules are the real ones, so a bad number is refused here too.
      const digits = String(body.ssn || '').replace(/\D/g, '');
      if (digits.length !== 9) {
        return [{ detail: 'A Social Security number has 9 digits.' }, 400];
      }
      const area = digits.slice(0, 3);
      if (['000', '666'].includes(area) || digits.slice(3, 5) === '00'
          || digits.slice(5) === '0000') {
        return [{ detail: 'That is not a valid Social Security number.' }, 400];
      }
      state.identityVerified = true;
      const out = JSON.parse(JSON.stringify(F.identity));
      out.ssn = `***-**-${digits.slice(5)}`;
      return out;
    }],

    ['PATCH', /^\/api\/auth\/name$/, (body) => {
      const name = [body.first_name, body.last_name].filter(Boolean).join(' ');
      F.session.taxpayer.name = name;
      return { name, note: 'Use the name exactly as it appears on your Social Security card.' };
    }],
    ['PATCH', /^\/api\/documents\/\d+$/, () => state.documents[0] || F.document],

    ['GET', /^\/api\/documents$/, () => ({
      documents: state.documents,
      years: [...new Set(state.documents.map((d) => d.tax_year))],
      needs_review: state.documents.filter((d) => d.status === 'needs_review'),
    })],
    ['POST', /^\/api\/documents\/w2\/boxes$/, () => {
      state.documents = [F.document];
      return F.document;
    }],
    // Uploads are handled outside this table, in `demoUpload`, because they
    // actually open the file rather than answering from a fixture.
    ['POST', /^\/api\/documents\/w2\/text$/, (body) => {
      const read = readPastedText(String((body && body.text) || ''));
      state.documents = [read];
      return read;
    }],
    ['DELETE', /^\/api\/documents\/\d+$/, () => { state.documents = []; return { deleted: 1 }; }],

    ['POST', /^\/api\/estimates\/compare$/, () => F.compare],
    ['POST', /^\/api\/estimates$/, (body) =>
      (body && body.method === 'planning') ? F.planning : F.regular],
    ['GET', /^\/api\/filings\/history/, () => F.filings],
    ['POST', /^\/api\/payments\/choose$/, () => F.payment],
    ['GET', /^\/api\/payments\/handoff/, () => F.handoff],

    // Billing. The demo keeps its own little ledger so the two buckets stay
    // visibly separate, which is the whole point of the screen.
    ['POST', /^\/api\/billing\/quote$/, () => F.fee],
    ['GET', /^\/api\/billing\/ledger$/, () => ({
      held_for_tax: state.heldForTax.toFixed(2),
      fees_received: state.feesPaid.toFixed(2),
      entries: state.ledger,
      note: 'Money held for tax is yours and is kept separate from what you have paid '
        + 'for preparation. It is only ever sent to the tax authority you authorised.',
    })],
    ['POST', /^\/api\/billing\/fee\/paid$/, (body) => {
      state.feesPaid += Number(body.amount) || 0;
      state.ledger.unshift({ id: state.ledger.length + 1, at: new Date().toISOString(),
        bucket: 'fee', direction: 'received', amount: String(body.amount),
        balance_after: state.feesPaid.toFixed(2), method: body.method,
        reference: body.reference, note: 'Preparation fee' });
      return { recorded: true, amount: String(body.amount),
               note: 'Recorded against your preparation fee. This is separate from your tax.' };
    }],
    ['POST', /^\/api\/billing\/funds$/, (body) => {
      state.heldForTax += Number(body.amount) || 0;
      state.ledger.unshift({ id: state.ledger.length + 1, at: new Date().toISOString(),
        bucket: 'tax', direction: 'received', amount: String(body.amount),
        balance_after: state.heldForTax.toFixed(2), method: body.method,
        reference: body.reference, note: 'Received for onward payment of tax' });
      return { recorded: true, amount: String(body.amount),
               held_for_tax: state.heldForTax.toFixed(2),
               note: 'Held for you and kept separate from the preparation fee.' };
    }],
    ['GET', /^\/api\/billing\/authorizations$/, () => ({
      held_for_tax: state.heldForTax.toFixed(2), authorizations: state.authorizations,
    })],
    ['POST', /^\/api\/billing\/authorize$/, (body) => {
      const amount = Number(body.amount) || 0;
      const row = {
        id: state.authorizations.length + 1, tax_year: body.tax_year,
        jurisdiction: body.jurisdiction || 'federal', state_code: '',
        amount: amount.toFixed(2), status: 'authorized', can_withdraw: true,
        authorized_at: new Date().toISOString(), remitted_at: null,
        confirmation_number: null,
        statement: 'I authorise TaxVault to pay $' + amount.toLocaleString('en-US',
          { minimumFractionDigits: 2 }) + ' to the IRS on my behalf for tax year '
          + body.tax_year + ', from funds I have sent for that purpose. I understand '
          + 'this money is held separately from the preparation fee, that I can '
          + 'withdraw this instruction at any time before it is sent, and that I '
          + 'remain responsible to the IRS for the tax itself.',
      };
      state.authorizations.unshift(row);
      const short = amount - state.heldForTax;
      return { ...row, held_for_tax: state.heldForTax.toFixed(2), funded: short <= 0,
               shortfall: Math.max(0, short).toFixed(2),
               note: short <= 0 ? 'Authorised and fully funded. It goes in the next batch.'
                 : 'Authorised, but more is needed before it can be sent.' };
    }],
    ['POST', /^\/api\/billing\/authorizations\/\d+\/withdraw$/, () => {
      state.authorizations.forEach((a) => {
        if (a.status === 'authorized') { a.status = 'revoked'; a.can_withdraw = false; }
      });
      return { status: 'revoked', note: 'Withdrawn. Nothing will be sent.' };
    }],
    ['GET', /^\/api\/payments\/service-fee$/, () => F.service_fee],
    ['POST', /^\/api\/payments\/record$/, (body) => ({
      id: 1, recorded: true, amount: String(body.amount || 0),
      confirmation_number: body.confirmation_number || null,
      note: body.confirmation_number
        ? 'Recorded as you reported it. Keep the confirmation number: it is the only '
          + 'evidence the payment was made, and the IRS does not issue another.'
        : 'Recorded, but without a confirmation number there is no proof the payment '
          + 'was made. Find it in your IRS account or your bank statement and add it.',
    })],

    ['GET', /^\/api\/payments\/options/, () => ({
      direction: 'refund', balance: '-14346.68',
      options: F.regular.payment.options,
    })],
  ];

  /** A blank document shell, shaped the way the API returns one. */
  function blankDocument(year) {
    return {
      id: 1, tax_year: year || 2025, kind: 'w2', status: 'needs_review', source: 'upload',
      employer_name: null, employee_name: '', employer_ein_last4: null, state_code: null,
      payload: {}, parse_confidence: 0, warnings: [], identity_checks: [],
      original_filename: '', uploaded_at: new Date().toISOString(),
      extraction: { method: 'none', readable: false, pages: 0, notes: [], strategy: 'none' },
    };
  }

  /** Turn what `w2read` found into the payload shape the screens expect. */
  function toDocument(found, meta) {
    const f = found.form;
    const document = blankDocument(meta.taxYear || f.taxYear || 2025);
    document.tax_year = meta.taxYear || f.taxYear || 2025;
    document.original_filename = meta.filename || '';
    document.employer_name = f.employerName || null;
    document.employee_name = [f.employeeFirstName, f.employeeLastName].filter(Boolean).join(' ');
    document.employer_ein_last4 = f.employerEin ? f.employerEin.slice(-4) : null;
    document.state_code = f.states.length ? f.states[0].state : null;
    document.parse_confidence = found.confidence;
    document.warnings = found.notes || [];
    document.status = found.confidence >= 0.75 ? 'parsed' : 'needs_review';
    document.extraction = {
      method: meta.method || 'pdf_text', readable: true, pages: meta.pages || 1,
      notes: [], strategy: 'layout',
    };
    const money = (value) => (Number(value) || 0).toFixed(2);
    document.payload = {
      employer_name: f.employerName || '',
      employee_first_name: f.employeeFirstName || '',
      employee_last_name: f.employeeLastName || '',
      tax_year: document.tax_year,
      box1_wages: money(f.wages),
      box2_federal_withheld: money(f.federalWithheld),
      box3_social_security_wages: money(f.socialSecurityWages),
      box4_social_security_withheld: money(f.socialSecurityWithheld),
      box5_medicare_wages: money(f.medicareWages),
      box6_medicare_withheld: money(f.medicareWithheld),
      box7_social_security_tips: money(f.socialSecurityTips),
      box8_allocated_tips: '0.00',
      box10_dependent_care: money(f.dependentCare),
      box11_nonqualified: '0.00',
      box12: Object.fromEntries(Object.entries(f.box12).map(([k, v]) => [k, money(v)])),
      box13: { retirement_plan: !!f.retirementPlan },
      box14: {},
      states: f.states.map((line) => ({
        state: line.state, state_id: '',
        state_wages: money(line.stateWages), state_withheld: money(line.stateWithheld),
        local_wages: '0.00', local_withheld: '0.00', locality: '',
      })),
    };
    return document;
  }

  /** Read an uploaded file for real. */
  async function demoUpload(formData) {
    const file = formData.get('file');
    const taxYear = parseInt(formData.get('tax_year'), 10) || 2025;
    if (!file) {
      const document = blankDocument(taxYear);
      document.warnings = [{ severity: 'error', box: '', message: 'No file was given.' }];
      return document;
    }

    const name = (file.name || '').toLowerCase();
    const isPdf = name.endsWith('.pdf') || (file.type || '').includes('pdf');

    if (!isPdf) {
      const document = blankDocument(taxYear);
      document.original_filename = file.name || '';
      document.warnings = [{
        severity: 'warning', box: '',
        message: 'A photograph or scan cannot be read without OCR, which this demo does '
          + 'not run. The boxes below are blank — fill them in and the estimate follows.',
      }];
      return document;
    }

    let buffer;
    try {
      buffer = await file.arrayBuffer();
    } catch {
      const document = blankDocument(taxYear);
      document.warnings = [{ severity: 'error', box: '', message: 'That file could not be opened.' }];
      return document;
    }

    const read = await window.TaxVaultPdf.readPdf(buffer);
    if (!read.ok) {
      const document = blankDocument(taxYear);
      document.original_filename = file.name || '';
      document.warnings = [{ severity: 'warning', box: '', message: read.reason }];
      return document;
    }

    const found = window.TaxVaultW2.readW2(read.runs);
    return toDocument(found, {
      taxYear, filename: file.name || '', method: 'pdf_text', pages: 1,
    });
  }

  /** Pasted text goes through the same finder, with runs faked from lines. */
  function readPastedText(text) {
    const runs = [];
    text.split(/\r?\n/).forEach((line, index) => {
      const trimmed = line.trim();
      if (trimmed) runs.push({ text: trimmed, x: 0, y: 1000 - index * 12 });
    });
    const found = window.TaxVaultW2.readW2(runs);
    return toDocument(found, { taxYear: found.form.taxYear || 2025, method: 'plain_text' });
  }

  const realFetch = window.fetch ? window.fetch.bind(window) : null;

  window.fetch = function demoFetch(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const method = ((init && init.method) || 'GET').toUpperCase();

    if (!path.startsWith('/api')) {
      return realFetch ? realFetch(input, init) : Promise.reject(new Error('offline'));
    }

    let body = {};
    if (init && typeof init.body === 'string') {
      try { body = JSON.parse(init.body); } catch { body = {}; }
    }

    // A file upload is the one request that must open what it was given. The
    // first version of this demo answered it from a fixture, so every upload
    // showed the same sample whatever file went in -- which is worse than not
    // offering the feature at all.
    if (method === 'POST' && /^\/api\/documents\/w2\/file$/.test(path)
        && init && init.body instanceof FormData) {
      return demoUpload(init.body).then((document) => {
        state.documents = [document];
        return json(document, 200);
      });
    }

    for (const [verb, pattern, handler] of ROUTES) {
      if (verb === method && pattern.test(path)) {
        const result = handler(body);
        const [payload, status] = Array.isArray(result) ? result : [result, 200];
        // A beat of latency, so the UI's loading states are visible rather
        // than flashing past.
        return new Promise((resolve) => setTimeout(() => resolve(json(payload, status)), 180));
      }
    }
    return Promise.resolve(json({ detail: `Not part of this demo: ${method} ${path}` }, 404));
  };

  // A standing banner. A tax figure with no provenance is worse than no figure,
  // so the page says what it is on every screen.
  document.addEventListener('DOMContentLoaded', () => {
    const banner = document.createElement('div');
    banner.className = 'demo-banner';
    banner.innerHTML =
      '<strong>Demo.</strong> No server, nothing stored, nothing sent. '
      + 'Any email works and the code is always <code>000000</code>. '
      + 'The figures are real output from the tax engine for a sample 2025 return — '
      + '<strong>do not enter a real Social Security number.</strong>';
    document.body.appendChild(banner);
  });
})();
