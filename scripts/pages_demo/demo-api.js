/*
 * Faculty Toolkit: GitHub Pages demo API.
 *
 * Loaded before the app's own script on the static demo site. It answers the
 * page's /api/* fetch() calls in the browser from demo/demo-data.json and the
 * pre-built demo workbooks, so the real interface works without a server.
 * Uploaded files are never read: any .xlsx loads the built-in demo class.
 *
 * Scoring: every check of every demo repo was scored at build time by the real
 * app.scoring code. Here we only redo the rubric arithmetic (on/off, points,
 * deadline, size limit, penalties, weighted average, overrides), mirroring
 * app/scoring.py and app/evaluation.py. State lives in memory until reload.
 */
(function () {
  'use strict';
  const realFetch = window.fetch.bind(window);
  const XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  let dataPromise = null;
  const jobs = {};
  const state = { rubric: null, students: {} };

  function loadData() {
    if (!dataPromise) {
      dataPromise = realFetch('demo/demo-data.json').then(function (r) {
        if (!r.ok) throw new Error('Demo data missing (' + r.status + ')');
        return r.json();
      });
    }
    return dataPromise;
  }

  function json(body, status) {
    return new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  function notFound(detail) {
    return json({ detail: detail }, 404);
  }

  function bad(detail) {
    return json({ detail: detail }, 400);
  }

  async function file(path, downloadName) {
    const r = await realFetch(path);
    if (!r.ok) return notFound('Demo file not found.');
    const blob = await r.blob();
    return new Response(blob, {
      status: 200,
      headers: {
        'Content-Type': XLSX_TYPE,
        'Content-Disposition': 'attachment; filename="' + downloadName + '"',
      },
    });
  }

  function body(init) {
    try {
      return init && typeof init.body === 'string' ? JSON.parse(init.body || '{}') : {};
    } catch (e) {
      return {};
    }
  }

  // ---------------------------------------------------------------- scoring helpers
  function clone(o) { return JSON.parse(JSON.stringify(o)); }

  function rubric(d) {
    if (!state.rubric) state.rubric = clone(d.scoring.rubric);
    return state.rubric;
  }

  function checkDef(d, id) {
    return d.scoring.checks.find(function (c) { return c.id === id; });
  }

  function toIso(v) {
    if (v === null || v === undefined || String(v).trim() === '') return null;
    let s = String(v).trim();
    if (!/(Z|[+-]\d\d:\d\d)$/i.test(s)) s = (s.length === 16 ? s + ':00' : s) + '+05:30';
    if (isNaN(new Date(s).getTime())) throw new Error("Deadline '" + v + "' is not a valid date and time.");
    return s;
  }

  function fmtLocal(dt) {
    if (!dt) return '-';
    const p = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(dt);
    const g = function (t) { return (p.find(function (x) { return x.type === t; }) || {}).value; };
    return g('year') + '-' + g('month') + '-' + g('day') + ' ' + g('hour') + ':' + g('minute') + ' IST';
  }

  function previewRepo(d, did, name) {
    return (d.previews[did].repos || []).find(function (r) { return r.name === name; });
  }

  function chooseTag(repo, deadline) {
    const pushed = (repo.tags || []).filter(function (t) { return t.pushed; });
    if (deadline) {
      const before = pushed.filter(function (t) { return new Date(t.pushed) <= deadline; });
      if (before.length) return before.sort(function (a, b) { return a.pushed < b.pushed ? 1 : -1; })[0].name;
    }
    if (pushed.length) return pushed.sort(function (a, b) { return a.pushed < b.pushed ? 1 : -1; })[0].name;
    const names = (repo.tags || []).map(function (t) { return t.name; });
    return names.indexOf('latest') >= 0 ? 'latest' : (names[0] || null);
  }

  function scoreRepo(d, did, sel) {
    const r = rubric(d);
    const f = d.scoring.repos[did][sel.repo];
    const prev = previewRepo(d, did, sel.repo);
    const deadlineIso = sel.deadline || r.deadline;
    const dl = deadlineIso ? new Date(deadlineIso) : null;
    const tag = sel.tag || chooseTag(prev, dl);
    const tagInfo = (prev.tags || []).find(function (t) { return t.name === tag; });
    const pushed = tagInfo && tagInfo.pushed ? new Date(tagInfo.pushed) : null;
    const out = {
      repo: sel.repo, tag: tag, status: 'scored', score: null, checks: [], flags: f.flags.slice(), penalties: [],
      base: f.base, confidence: f.confidence, own_steps: f.own_steps, total_steps: f.total_steps,
      platform: f.platform, pushed_at: tagInfo ? tagInfo.pushed : null, deadline: dl ? deadlineIso : null, note: '',
      fingerprint: f.fingerprint,
    };
    let late = false;
    const fractions = {};
    f.checks.forEach(function (c) {
      const id = c[0];
      let fraction = c[1], status = c[2], evidence = c[3];
      if (id === 'S1' && f.has_image) {
        evidence = "Tag '" + tag + "' has a pushed image (" + f.platform + '), pushed ' + fmtLocal(pushed) + '.';
      }
      if (id === 'S2') {
        if (!dl) { fraction = 0; status = 'skip'; evidence = 'No deadline set.'; }
        else if (!pushed) { fraction = 0; status = 'fail'; evidence = 'No push time; deadline ' + fmtLocal(dl) + '.'; }
        else if (pushed <= dl) { fraction = 1; status = 'pass'; evidence = 'Pushed ' + fmtLocal(pushed) + ', deadline ' + fmtLocal(dl) + '.'; }
        else { late = true; fraction = 0; status = 'fail'; evidence = 'Pushed ' + fmtLocal(pushed) + ', after the deadline ' + fmtLocal(dl) + '.'; }
      }
      if (id === 'L2' && f.has_image) {
        const lim = r.size_limit_mb;
        fraction = f.size_mb <= lim ? 1 : (f.size_mb <= 2 * lim ? 0.5 : 0);
        status = fraction >= 1 ? 'pass' : (fraction > 0 ? 'partial' : 'fail');
        evidence = f.size_mb + ' MB compressed (limit ' + lim + ' MB).';
      }
      const def = checkDef(d, id);
      const set = r.checks[id] || { enabled: true, points: def.default_points };
      if (!set.enabled) status = 'off';
      const max = Number(set.points);
      fractions[id] = fraction;
      out.checks.push({
        id: id, area: def.area, label: def.label, max_points: max, status: status, evidence: evidence,
        earned: (status === 'skip' || status === 'off') ? 0 : Math.round(max * fraction * 100) / 100,
      });
    });
    if (late) out.flags.push('F4');
    if (!f.has_image) {
      out.status = 'no_image';
      out.score = 0;
      return out;
    }
    let total = 0, raw = 0;
    out.checks.forEach(function (c) {
      if (c.status === 'skip' || c.status === 'off') return;
      total += c.max_points;
      raw += c.max_points * fractions[c.id];
    });
    let score = total > 0 ? 100 * raw / total : 0;
    if (late && r.late_penalty_percent) {
      score *= 1 - r.late_penalty_percent / 100;
      out.penalties.push('Late: -' + r.late_penalty_percent + '%');
    }
    if (f.secrets.length && r.secret_penalty) {
      score -= r.secret_penalty;
      out.penalties.push('Secret-like value (' + f.secrets.join(', ') + '): -' + r.secret_penalty);
    }
    out.score = Math.round(Math.max(0, Math.min(100, score)) * 10) / 10;
    return out;
  }

  function results(d, did) {
    const se = state.students[did];
    if (!se) return [];
    return se.selections.map(function (sel) {
      const res = scoreRepo(d, did, sel);
      const ov = se.overrides[sel.repo] || {};
      res.selection = sel;
      res.override_score = ov.score === undefined ? null : ov.score;
      res.override_note = ov.note || '';
      res.final_score = res.override_score !== null ? res.override_score : res.score;
      return res;
    });
  }

  function weighted(pairs) {
    let tw = 0, tv = 0;
    pairs.forEach(function (p) { tw += p[0]; tv += p[0] * p[1]; });
    return tw > 0 ? Math.round(tv / tw * 10) / 10 : null;
  }

  function classFlags(d) {
    const owners = {};
    Object.keys(state.students).forEach(function (did) {
      results(d, did).forEach(function (r) {
        if (r.fingerprint) (owners[r.fingerprint] = owners[r.fingerprint] || []).push([did, r.repo]);
      });
    });
    const out = {};
    Object.keys(owners).forEach(function (fp) {
      const items = owners[fp];
      const studs = new Set(items.map(function (i) { return i[0]; }));
      items.forEach(function (i) {
        const k = i[0] + '/' + i[1];
        out[k] = out[k] || [];
        if (studs.size > 1) out[k].push('F1');
        if (items.filter(function (x) { return x[0] === i[0]; }).length > 1) out[k].push('F2');
      });
    });
    return out;
  }

  function withFlags(did, res, extra) {
    const set = new Set(res.flags.concat(extra[did + '/' + res.repo] || []));
    res.flags = Array.from(set).sort();
    return res;
  }

  function summary(d, student, extra) {
    const did = student.docker_id.toLowerCase();
    const se = state.students[did];
    const base = { docker_id: student.docker_id, roll: student.roll, name: student.name };
    if (!se) {
      return Object.assign(base, { status: 'not_evaluated', repos: 0, suggested: null, final: null, flags: [], overridden: false, provisional: false });
    }
    const res = results(d, did).map(function (r) { return withFlags(did, r, extra); });
    const scored = res.filter(function (r) { return r.score !== null; });
    let final = weighted(scored.map(function (r) { return [r.selection.weight, r.final_score]; }));
    const overridden = se.override_score !== null || scored.some(function (r) { return r.override_score !== null; });
    if (se.override_score !== null) final = se.override_score;
    const flags = Array.from(new Set([].concat.apply([], res.map(function (r) { return r.flags; })))).sort();
    return Object.assign(base, {
      status: overridden ? 'overridden' : 'scored', repos: res.length,
      suggested: weighted(scored.map(function (r) { return [r.selection.weight, r.score]; })),
      final: final, flags: flags, overridden: overridden, provisional: false,
    });
  }

  function detail(d, student) {
    const did = student.docker_id.toLowerCase();
    const extra = classFlags(d);
    const se = state.students[did];
    return {
      summary: summary(d, student, extra),
      repos: se ? results(d, did).map(function (r) { const x = withFlags(did, r, extra); delete x.fingerprint; return x; }) : [],
      selections: se ? se.selections : [],
      override_score: se ? se.override_score : null,
      override_note: se ? se.override_note : '',
      flag_labels: d.scoring.flags,
    };
  }

  function setSelections(did, selections) {
    if (!selections.length) {
      delete state.students[did];
      return;
    }
    const prev = state.students[did];
    state.students[did] = {
      selections: selections,
      overrides: prev ? prev.overrides : {},
      override_score: prev ? prev.override_score : null,
      override_note: prev ? prev.override_note : '',
    };
  }

  function validateSelections(d, did, raw) {
    if (!Array.isArray(raw)) throw new Error("'repos' must be a list.");
    const seen = {};
    return raw.map(function (item) {
      const name = String(item.repo || '').trim().toLowerCase();
      const repo = previewRepo(d, did, name);
      if (!repo) throw new Error("Repository '" + name + "' is not in this student's Docker Hub profile.");
      if (seen[name]) throw new Error("Repository '" + name + "' is selected twice.");
      seen[name] = true;
      const tag = item.tag ? String(item.tag) : null;
      if (tag && !(repo.tags || []).some(function (t) { return t.name === tag; })) throw new Error("Tag '" + tag + "' does not exist in " + name + '.');
      const weight = item.weight === undefined || item.weight === null || item.weight === '' ? 1 : Number(item.weight);
      if (!(weight > 0 && weight <= 100)) throw new Error('Weight for ' + name + ' must be more than 0 and at most 100.');
      return { repo: name, tag: tag, weight: weight, deadline: toIso(item.deadline) };
    });
  }

  function autoSelect(d, did, rule, text) {
    const repos = d.previews[did].repos || [];
    if (rule === 'name_contains') {
      return repos.filter(function (r) { return r.name.toLowerCase().indexOf(text.toLowerCase()) >= 0; })
        .map(function (r) { return { repo: r.name, tag: null, weight: 1, deadline: null }; });
    }
    const pushed = repos.filter(function (r) { return r.submission_date; });
    if (rule === 'latest') {
      if (!pushed.length) return [];
      const latest = pushed.sort(function (a, b) { return a.submission_date < b.submission_date ? 1 : -1; })[0];
      return [{ repo: latest.name, tag: null, weight: 1, deadline: null }];
    }
    if (rule === 'all_pushed') return pushed.map(function (r) { return { repo: r.name, tag: null, weight: 1, deadline: null }; });
    return [];
  }

  function validateRubric(d, input) {
    const r = clone(d.scoring.rubric);
    const checks = input.checks || {};
    Object.keys(checks).forEach(function (id) {
      if (!checkDef(d, id)) throw new Error("Unknown check '" + id + "'.");
      const pts = Number(checks[id].points);
      if (isNaN(pts) || pts < 0 || pts > 100) throw new Error('Points for ' + id + ' must be between 0 and 100.');
      r.checks[id] = { enabled: checks[id].enabled !== false, points: pts };
    });
    if (!Object.keys(r.checks).some(function (id) { return r.checks[id].enabled && r.checks[id].points > 0; })) {
      throw new Error('Switch on at least one check with more than 0 points.');
    }
    function num(v, label, lo, hi, dflt) {
      const n = v === undefined || v === null || v === '' ? dflt : Number(v);
      if (isNaN(n) || n < lo || n > hi) throw new Error(label + ' must be between ' + lo + ' and ' + hi + '.');
      return n;
    }
    r.deadline = toIso(input.deadline);
    r.size_limit_mb = Math.round(num(input.size_limit_mb, 'Size limit', 1, 100000, 500));
    r.late_penalty_percent = num(input.late_penalty_percent, 'Late penalty', 0, 100, 0);
    r.secret_penalty = num(input.secret_penalty, 'Secret penalty', 0, 100, 10);
    return r;
  }

  function rubricResponse(d) {
    return { rubric: rubric(d), checks: d.scoring.checks, flags: d.scoring.flags };
  }

  // ---------------------------------------------------------------- routes
  async function handle(path, method, init) {
    const d = await loadData();
    const rosterId = d.roster.roster_id;
    const rosterGone = 'Roster expired or not found. Please upload your student list again.';
    let m;

    if (path === '/api/health') return json({ status: 'ok' });

    if (path === '/api/roster' && method === 'POST') {
      const f = init && init.body && typeof init.body.get === 'function' ? init.body.get('file') : null;
      const name = (f && f.name) || '';
      if (!name.toLowerCase().endsWith('.xlsx')) {
        return json({ detail: 'Only Excel (.xlsx) files are supported. Received: ' + name }, 400);
      }
      return json(d.roster);
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)$/))) {
      return m[1] === rosterId ? json(d.roster) : notFound(rosterGone);
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/students$/))) {
      return m[1] === rosterId ? json(d.students) : notFound(rosterGone);
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/rubric$/))) {
      if (m[1] !== rosterId) return notFound(rosterGone);
      if (method === 'PUT') {
        try { state.rubric = validateRubric(d, body(init)); } catch (e) { return bad(e.message); }
      } else if (method === 'DELETE') {
        state.rubric = clone(d.scoring.rubric);
      }
      return json(rubricResponse(d));
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/scores$/))) {
      if (m[1] !== rosterId) return notFound(rosterGone);
      const extra = classFlags(d);
      const rows = d.students.map(function (s) { return summary(d, s, extra); });
      return json({ students: rows, evaluated: rows.filter(function (r) { return r.status !== 'not_evaluated'; }).length });
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/students\/([^/]+)\/(preview|export|evaluation|evaluate|override)$/))) {
      if (m[1] !== rosterId) return notFound(rosterGone);
      const id = decodeURIComponent(m[2]).toLowerCase();
      const student = d.students.find(function (s) { return s.docker_id.toLowerCase() === id; });
      if (!d.previews[id] || !student) return notFound("Student with Docker ID '" + m[2] + "' not found in this roster.");
      if (m[3] === 'preview') return json(d.previews[id]);
      if (m[3] === 'export') return file('demo-files/students/' + encodeURIComponent(id) + '.xlsx', d.filenames[id]);
      if (m[3] === 'evaluation') return json(detail(d, student));
      if (m[3] === 'evaluate' && method === 'POST') {
        try { setSelections(id, validateSelections(d, id, body(init).repos || [])); } catch (e) { return bad(e.message); }
        return json(detail(d, student));
      }
      if (m[3] === 'override' && method === 'PUT') {
        const se = state.students[id];
        if (!se) return bad('Evaluate this student before overriding a score.');
        const b = body(init);
        let score = b.score === null || b.score === undefined || b.score === '' ? null : Math.round(Number(b.score) * 10) / 10;
        if (score !== null && (isNaN(score) || score < 0 || score > 100)) return bad('Score must be between 0 and 100.');
        const note = score === null ? '' : String(b.note || '').trim().slice(0, 500);
        if (b.repo) {
          if (!se.selections.some(function (s) { return s.repo === b.repo; })) return bad("Repository '" + b.repo + "' is not selected for this student.");
          se.overrides[b.repo] = { score: score, note: note };
        } else {
          se.override_score = score;
          se.override_note = note;
        }
        return json(detail(d, student));
      }
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/export-all\/start$/)) && method === 'POST') {
      if (m[1] !== rosterId) return notFound(rosterGone);
      const jobId = 'demo-job-' + Date.now();
      jobs[jobId] = { kind: 'export', done: 0, total: d.students.length, note: '' };
      return json({ job_id: jobId });
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/evaluate-all\/start$/)) && method === 'POST') {
      if (m[1] !== rosterId) return notFound(rosterGone);
      const b = body(init);
      const rule = b.rule || 'keep';
      const text = String(b.text || '').trim();
      if (['keep', 'name_contains', 'latest', 'all_pushed'].indexOf(rule) < 0) return bad("Unknown selection rule '" + rule + "'.");
      if (rule === 'name_contains' && !text) return bad('Type the text the repository name should contain.');
      const counts = { evaluated: 0, no_match: 0, skipped: 0 };
      d.students.forEach(function (s) {
        const did = s.docker_id.toLowerCase();
        if (state.students[did] && !b.overwrite) { counts.evaluated++; return; }
        if (rule === 'keep') { counts.skipped++; return; }
        const sel = autoSelect(d, did, rule, text);
        if (sel.length) { setSelections(did, sel); counts.evaluated++; } else { counts.no_match++; }
      });
      const note = 'Scored ' + counts.evaluated + ' student(s)' +
        (counts.no_match ? ', no matching repo for ' + counts.no_match : '') +
        (counts.skipped ? ', ' + counts.skipped + ' without a selection skipped' : '') + '.';
      const jobId = 'demo-eval-' + Date.now();
      jobs[jobId] = { kind: 'evaluate', done: 0, total: d.students.length, note: note };
      return json({ job_id: jobId });
    }

    if ((m = path.match(/^\/api\/jobs\/([^/]+)$/))) {
      const job = jobs[m[1]];
      if (!job) return notFound('Job not found or expired.');
      // Each poll (every 2 s) advances the simulated job.
      job.done = Math.min(job.total, job.done + Math.ceil(job.total / 5));
      const running = job.done < job.total;
      return json({
        job_id: m[1],
        kind: job.kind,
        status: running ? 'running' : 'done',
        done: job.done,
        total: job.total,
        current: running ? d.students[job.done].docker_id : null,
        error: null,
        note: running ? '' : job.note,
      });
    }

    if ((m = path.match(/^\/api\/jobs\/([^/]+)\/download$/))) {
      const job = jobs[m[1]];
      if (!job) return notFound('Job not found or expired.');
      if (job.kind !== 'export') return bad('This job has no file to download.');
      if (job.done < job.total) return json({ detail: 'Export job is not complete yet.' }, 409);
      return file('demo-files/All_Submissions_DEMO.xlsx', d.combined_filename);
    }

    return notFound('Not available in the demo.');
  }

  window.fetch = function (input, init) {
    const url = new URL(typeof input === 'string' ? input : input.url, window.location.href);
    const at = url.pathname.indexOf('/api/');
    if (url.origin !== window.location.origin || at === -1) return realFetch(input, init);
    const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
    return handle(url.pathname.slice(at), method, init).catch(function (err) {
      return json({ detail: 'Demo error: ' + err.message }, 500);
    });
  };
})();
