/* Finding the W-2 boxes in a page of positioned text runs.
 *
 * This mirrors `taxvault/forms/w2_layout.py` closely enough that the demo
 * shows what the server would find. Same idea: a W-2 is a grid, each box has
 * a printed label with its figure underneath, so the question is geometric --
 * what amount is drawn inside this label's box -- rather than textual.
 *
 * Two things that cost real debugging on the server and are repeated here:
 * whitespace inside a label is padding and must be collapsed before matching,
 * and a label anchors where the match *begins*, not where its line begins,
 * because a W-2 puts several boxes side by side.
 */
'use strict';

(function (global) {
  const AMOUNT = /^\$?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})$/;
  const LOOSE_AMOUNT = /(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})(?![\d])/g;

  const MONEY_BOXES = [
    ['wages', [/\b1\b.{0,4}wages,? tips/, /wages,? tips,? other comp/]],
    ['federalWithheld', [/\b2\b.{0,4}federal income tax/, /federal income tax withheld/]],
    ['socialSecurityWages', [/\b3\b.{0,4}social security wages/, /social security wages/]],
    ['socialSecurityWithheld', [/\b4\b.{0,4}social security tax/, /social security tax withheld/]],
    ['medicareWages', [/\b5\b.{0,4}medicare wages/, /medicare wages and tips/]],
    ['medicareWithheld', [/\b6\b.{0,4}medicare tax/, /medicare tax withheld/]],
    ['socialSecurityTips', [/\b7\b.{0,4}social security tips/, /social security tips/]],
    ['allocatedTips', [/\b8\b.{0,4}allocated tips/, /allocated tips/]],
    ['dependentCare', [/\b10\b.{0,4}dependent care/, /dependent care benefits/]],
  ];
  const CRITICAL = ['wages', 'federalWithheld', 'socialSecurityWages', 'medicareWages'];

  const BOX12_CODES = new Set(['A','B','C','D','E','F','G','H','J','K','L','M','N','P','Q','R',
                               'S','T','V','W','Y','Z','AA','BB','DD','EE','FF','GG','HH']);
  const STATES = new Set(['AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL',
    'IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM',
    'NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY']);

  const ACRONYMS = new Set(['LLC','LLP','LP','PLLC','PC','PA','USA','US','NA','PLC','AG','SA',
                            'BV','NV','II','III','IV','HR','IT']);
  const TITLE_SUFFIXES = new Set(['INC','CORP','CO','LTD','COMPANY','GMBH','HOLDINGS','GROUP']);
  const PARTICLES = new Set(['of','and','the','for','de','la','del','van','von','der','da']);

  const collapse = (text) => String(text || '').replace(/\s+/g, ' ').trim();

  function capToken(token) {
    const bare = token.replace(/^[.,]+|[.,]+$/g, '');
    if (ACRONYMS.has(bare.toUpperCase())) return bare.toUpperCase() + token.slice(bare.length);
    if (TITLE_SUFFIXES.has(bare.toUpperCase())) {
      return bare.charAt(0).toUpperCase() + bare.slice(1).toLowerCase() + token.slice(bare.length);
    }
    if (bare.length <= 1) return token.toUpperCase();
    if (/\d/.test(bare)) return token.toUpperCase();
    if (bare.includes('-')) return token.split('-').map(capToken).join('-');
    return token.charAt(0).toUpperCase() + token.slice(1).toLowerCase();
  }

  /** Case a shouted form entry like a name. `toTitleCase` renders LLC as Llc. */
  function tidyName(raw) {
    const text = collapse(raw);
    if (!text) return '';
    if (text !== text.toUpperCase() && text !== text.toLowerCase()) return text;
    return text.split(' ').map((token, index) =>
      (index && PARTICLES.has(token.toLowerCase())) ? token.toLowerCase() : capToken(token)
    ).join(' ');
  }

  const looksLikeName = (text) => {
    const value = collapse(text);
    if (value.length < 2 || value.length > 70) return false;
    return !/\d/.test(value) && !/^(suite|apt|apartment|unit|ste|floor|fl|po box)\b/i.test(value);
  };

  const amountOf = (run) => {
    const value = collapse(run.text).replace(/\$/g, '');
    return AMOUNT.test(collapse(run.text).replace(/\$/g, '')) ? parseFloat(value.replace(/,/g, '')) : null;
  };

  /** Group runs into visual lines, each left to right. */
  function linesOf(runs, tolerance) {
    const rows = [];
    const sorted = runs.slice().sort((a, b) => (b.y - a.y) || (a.x - b.x));
    for (const run of sorted) {
      const row = rows.find((r) => Math.abs(r[0].y - run.y) <= (tolerance || 3));
      if (row) row.push(run); else rows.push([run]);
    }
    return rows.map((row) => row.slice().sort((a, b) => a.x - b.x));
  }

  /** The run a label begins in — not the first run on its line. */
  function findLabel(runs, patterns) {
    for (const row of linesOf(runs)) {
      const pieces = row.map((r) => collapse(r.text));
      const offsets = [];
      let cursor = 0;
      for (const piece of pieces) { offsets.push(cursor); cursor += piece.length + 1; }
      const joined = pieces.join(' ').toLowerCase();

      for (const pattern of patterns) {
        const match = joined.match(pattern);
        if (!match) continue;
        const start = match.index;
        let anchor = row[0];
        for (let i = 0; i < row.length; i += 1) {
          if (offsets[i] <= start) anchor = row[i]; else break;
        }
        return anchor;
      }
    }
    return null;
  }

  /** The amount drawn inside this label's box. */
  function findBoxValue(runs, label, width, depth) {
    const candidates = [];
    for (const run of runs) {
      const value = amountOf(run);
      if (value === null) continue;
      const dx = run.x - label.x;
      const dy = label.y - run.y;
      if (dx < -6 || dx > (width || 118)) continue;
      if (dy > 0 && dy <= (depth || 46)) candidates.push([dy, value]);
      else if (Math.abs(dy) <= 3 && dx > 8) candidates.push([0.5, value]);
    }
    if (!candidates.length) return null;
    candidates.sort((a, b) => a[0] - b[0]);
    return candidates[0][1];
  }

  /** The lines of text inside a label's box, top to bottom. */
  function findTextBlock(runs, label, width, depth, maxLines) {
    const inside = runs.filter((run) => {
      const dx = run.x - label.x;
      const dy = label.y - run.y;
      return dx >= -6 && dx <= (width || 300) && dy > 4 && dy <= (depth || 90);
    });
    return linesOf(inside)
      .slice(0, maxLines || 4)
      .map((row) => collapse(row.map((r) => r.text).join(' ')))
      .filter(Boolean);
  }

  /**
   * Read a W-2 from positioned runs.
   *
   * Returns the boxes, the names, a confidence (the fraction of the boxes that
   * matter which were located) and the notes.
   */
  function readW2(runs) {
    const notes = [];
    const form = {
      employerName: '', employeeFirstName: '', employeeLastName: '',
      employerEin: '', employeeSsn: '', taxYear: 0,
      wages: 0, federalWithheld: 0, socialSecurityWages: 0, socialSecurityWithheld: 0,
      medicareWages: 0, medicareWithheld: 0, socialSecurityTips: 0, allocatedTips: 0,
      dependentCare: 0, box12: {}, retirementPlan: false, states: [],
    };

    const whole = linesOf(runs).map((row) => row.map((r) => r.text).join(' ')).join('\n');

    let found = 0;
    for (const [field, patterns] of MONEY_BOXES) {
      const label = findLabel(runs, patterns);
      if (!label) continue;
      const value = findBoxValue(runs, label);
      if (value === null) continue;
      form[field] = value;
      if (CRITICAL.includes(field)) found += 1;
    }

    const year = whole.match(/\b(20[12]\d)\b/);
    if (year) form.taxYear = parseInt(year[1], 10);
    const ein = whole.match(/\b(\d{2}-\d{7})\b/);
    if (ein) form.employerEin = ein[1];
    const ssn = whole.match(/(?<![\dA-Za-z])(\d{3}|[X*]{3})[-\s]?(\d{2}|[X*]{2})[-\s]?(\d{4})(?!\d)/i);
    if (ssn) form.employeeSsn = `${ssn[1]}-${ssn[2]}-${ssn[3]}`;

    // --- names -------------------------------------------------------------
    const employerLabel = findLabel(runs, [
      /employer'?s? name,? +address/, /employer'?s? name,? +and +address/,
      /\bc\b[^a-z]{0,4}employer'?s? name/, /employer'?s? name\b/, /employer name and address/,
    ]);
    if (employerLabel) {
      for (const line of findTextBlock(runs, employerLabel, 300, 90)) {
        if (looksLikeName(line)) { form.employerName = tidyName(line); break; }
      }
    }

    const employeeLabel = findLabel(runs, [
      /employee'?s? first name/, /employee'?s? name,? +address/,
      /e ?\/ ?f[^a-z]{0,4}employee'?s? name/, /\be\b[^a-z]{0,4}employee'?s? (first )?name/,
      /employee'?s? name\b/, /employee name and address/,
    ]);
    if (employeeLabel) {
      for (const line of findTextBlock(runs, employeeLabel, 300, 95)) {
        if (!looksLikeName(line)) continue;
        const parts = line.split(/\s+/);
        if (parts.length >= 2) {
          form.employeeFirstName = tidyName(parts.slice(0, -1).join(' '));
          form.employeeLastName = tidyName(parts[parts.length - 1]);
        } else {
          form.employeeFirstName = tidyName(line);
        }
        break;
      }
    }
    if (!form.employerName) {
      notes.push({ severity: 'warning', box: 'c',
                   message: "The employer's name could not be read from the form." });
    }
    if (!form.employeeFirstName && !form.employeeLastName) {
      notes.push({ severity: 'info', box: 'e',
                   message: "The employee's name could not be read from the form." });
    }

    // --- box 12, from each box rather than a line of text -------------------
    for (const slot of ['12a', '12b', '12c', '12d']) {
      const label = findLabel(runs, [new RegExp(`\\b${slot}\\b`)]);
      if (!label) continue;
      const inside = runs.filter((run) => {
        const dx = run.x - label.x;
        const dy = label.y - run.y;
        return dx >= -6 && dx <= 124 && dy > 0 && dy <= 30;
      });
      const text = inside.sort((a, b) => (b.y - a.y) || (a.x - b.x))
        .map((r) => r.text).join(' ');
      const match = text.match(/\b([A-Z]{1,2})\b\s*[|:\-]?\s*\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)/);
      if (match && BOX12_CODES.has(match[1]) && /[.,]/.test(match[2])) {
        form.box12[match[1]] = parseFloat(match[2].replace(/,/g, ''));
      }
    }
    if (/x\s*retirement plan|retirement plan\s*x/i.test(whole)) form.retirementPlan = true;

    // --- boxes 15 to 17 -----------------------------------------------------
    const stateLabel = findLabel(runs, [/\b15\b.{0,4}state/, /employer'?s? state id/]);
    let code = '';
    if (stateLabel) {
      const near = runs
        .filter((run) => {
          const dx = run.x - stateLabel.x;
          const dy = stateLabel.y - run.y;
          return dy > 0 && dy <= 46 && dx >= -6 && dx <= 60;
        })
        .sort((a, b) => (stateLabel.y - a.y) - (stateLabel.y - b.y));
      const hit = near.find((run) => STATES.has(collapse(run.text).toUpperCase()));
      if (hit) code = collapse(hit.text).toUpperCase();
    }
    if (!code) {
      const candidates = (whole.match(/\b([A-Z]{2})\b/g) || []).filter((c) => STATES.has(c));
      code = candidates[0] || '';
    }

    const wagesLabel = findLabel(runs, [/\b16\b.{0,4}state wages/, /state wages,? tips/]);
    const taxLabel = findLabel(runs, [/\b17\b.{0,4}state income tax/, /state income tax/]);
    const stateWages = wagesLabel ? findBoxValue(runs, wagesLabel, 110) : null;
    const stateTax = taxLabel ? findBoxValue(runs, taxLabel, 95) : null;
    if (code && (stateWages !== null || stateTax !== null)) {
      form.states.push({ state: code, stateWages: stateWages || 0, stateWithheld: stateTax || 0 });
    } else if (code) {
      notes.push({ severity: 'warning', box: '16',
                   message: `A ${code} state line is on this form but its wages and withholding `
                     + 'could not be read. Enter boxes 16 and 17 by hand or the state '
                     + 'estimate will be wrong.' });
    }

    const confidence = Math.round((found / CRITICAL.length) * 1000) / 1000;
    if (confidence < 1) {
      const missing = CRITICAL.filter((field) => !form[field])
        .map((field) => field.replace(/([A-Z])/g, ' $1').toLowerCase());
      notes.push({ severity: 'warning', box: '',
                   message: `Could not locate: ${missing.join(', ')}. Enter these boxes by hand.` });
    }
    return { form, confidence, notes };
  }

  global.TaxVaultW2 = { readW2, tidyName, findLabel, findBoxValue, linesOf };
})(window);
