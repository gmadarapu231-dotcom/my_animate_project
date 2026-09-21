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
    ['POST', /^\/api\/documents\/w2\/(text|file)$/, () => {
      // The demo answers as a readable payroll PDF would: the boxes come out
      // of the file. A real scan would come back at zero confidence, which the
      // upload tab explains.
      state.documents = [F.document];
      return Object.assign({}, F.document, {
        extraction: { method: 'pdf_text', readable: true, pages: 1, notes: [], strategy: 'layout' },
      });
    }],
    ['DELETE', /^\/api\/documents\/\d+$/, () => { state.documents = []; return { deleted: 1 }; }],

    ['POST', /^\/api\/estimates\/compare$/, () => F.compare],
    ['POST', /^\/api\/estimates$/, (body) =>
      (body && body.method === 'planning') ? F.planning : F.regular],
    ['GET', /^\/api\/filings\/history/, () => F.filings],
    ['POST', /^\/api\/payments\/choose$/, () => F.payment],
    ['GET', /^\/api\/payments\/options/, () => ({
      direction: 'refund', balance: '-14346.68',
      options: F.regular.payment.options,
    })],
  ];

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
