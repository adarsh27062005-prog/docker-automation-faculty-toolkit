/*
 * Faculty Toolkit: GitHub Pages demo API.
 *
 * Loaded before the app's own script on the static demo site. It answers the
 * page's /api/* fetch() calls in the browser from demo/demo-data.json and the
 * pre-built demo workbooks, so the real interface works without a server.
 * Uploaded files are never read: any .xlsx loads the built-in demo class.
 */
(function () {
  'use strict';
  const realFetch = window.fetch.bind(window);
  const XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  let dataPromise = null;
  const jobs = {};

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

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/students\/([^/]+)\/(preview|export)$/))) {
      if (m[1] !== rosterId) return notFound(rosterGone);
      const id = decodeURIComponent(m[2]).toLowerCase();
      if (!d.previews[id]) return notFound("Student with Docker ID '" + m[2] + "' not found in this roster.");
      if (m[3] === 'preview') return json(d.previews[id]);
      return file('demo-files/students/' + encodeURIComponent(id) + '.xlsx', d.filenames[id]);
    }

    if ((m = path.match(/^\/api\/roster\/([^/]+)\/export-all\/start$/)) && method === 'POST') {
      if (m[1] !== rosterId) return notFound(rosterGone);
      const jobId = 'demo-job-' + Date.now();
      jobs[jobId] = { done: 0, total: d.students.length };
      return json({ job_id: jobId });
    }

    if ((m = path.match(/^\/api\/jobs\/([^/]+)$/))) {
      const job = jobs[m[1]];
      if (!job) return notFound('Job not found or expired.');
      // Each poll (every 2 s) advances the simulated export.
      job.done = Math.min(job.total, job.done + Math.ceil(job.total / 5));
      const running = job.done < job.total;
      return json({
        job_id: m[1],
        status: running ? 'running' : 'done',
        done: job.done,
        total: job.total,
        current: running ? d.students[job.done].docker_id : null,
        error: null,
      });
    }

    if ((m = path.match(/^\/api\/jobs\/([^/]+)\/download$/))) {
      const job = jobs[m[1]];
      if (!job) return notFound('Job not found or expired.');
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
