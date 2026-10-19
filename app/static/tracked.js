// Manage page for tracked posts: what is tracked, how often it is re-checked,
// what tracking gave up on, and a search for adding more.
//
// Tracking is a property of the post (it changes what the scraper fetches),
// not of the viewer, so everything here is shared state.

const REFRESH_CHOICES = [
  { value: 0, label: 'Every sync' },
  { value: 6, label: 'Every 6 hours' },
  { value: 12, label: 'Every 12 hours' },
  { value: 24, label: 'Daily' },
  { value: 168, label: 'Weekly' },
  { value: 720, label: 'Monthly' },
];

async function apiFetch(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('unauthenticated');
  }
  return res;
}

function fmtDate(iso) {
  return iso ? new Date(iso).toLocaleString() : 'never';
}

// Title-casing matches the main list; keeps "Foundry VTT Modules" readable.
function prettifyTitle(title) {
  return (title || '').replace(/\b[A-Z][A-Z0-9'’]{2,}\b/g, (word) =>
    word.charAt(0) + word.slice(1).toLowerCase()
  );
}

function setRefreshStatus(msg, show = true) {
  const el = document.getElementById('refreshStatus');
  el.textContent = msg;
  el.classList.toggle('hidden', !show);
}

// ---------------------------------------------------------------------------
// Currently tracked
// ---------------------------------------------------------------------------

async function loadTracked() {
  const res = await apiFetch('/api/tracked');
  if (!res.ok) { setRefreshStatus('Could not load tracked posts.'); return; }
  const rows = await res.json();

  const list = document.getElementById('trackedList');
  list.innerHTML = '';
  document.getElementById('trackedCount').textContent = rows.filter(r => r.tracked).length;

  if (rows.length === 0) {
    const li = document.createElement('li');
    li.className = 'tracked-item empty-note';
    li.textContent = 'Nothing tracked yet. Search above to add a post.';
    list.appendChild(li);
    return;
  }
  for (const row of rows) list.appendChild(makeTrackedRow(row));
}

function makeTrackedRow(row) {
  const li = document.createElement('li');
  li.className = row.tracked ? 'tracked-item' : 'tracked-item stopped';

  const main = document.createElement('div');
  main.className = 'tracked-main';

  const title = document.createElement('a');
  title.textContent = prettifyTitle(row.title);
  title.href = row.post_url || '#';
  if (row.post_url) { title.target = '_blank'; title.rel = 'noopener noreferrer'; }
  title.className = 'tracked-title';

  const meta = document.createElement('div');
  meta.className = 'muted';
  meta.textContent =
    `${row.creator} · last checked ${fmtDate(row.last_checked_at)}`
    + (row.content_updated_at ? ` · last changed ${fmtDate(row.content_updated_at)}` : '');

  main.append(title, meta);

  if (row.tracking_stopped_reason) {
    const warn = document.createElement('div');
    warn.className = 'tracked-stopped-note';
    warn.textContent = `Tracking stopped — ${row.tracking_stopped_reason}`;
    main.appendChild(warn);
  }

  const controls = document.createElement('div');
  controls.className = 'tracked-controls';

  if (row.tracked) {
    const select = document.createElement('select');
    select.setAttribute('aria-label', `How often to check "${row.title}"`);
    for (const choice of REFRESH_CHOICES) {
      const opt = document.createElement('option');
      opt.value = String(choice.value);
      opt.textContent = choice.label;
      select.appendChild(opt);
    }
    // An interval set elsewhere (or by the API) may not be one of our presets.
    if (!REFRESH_CHOICES.some(c => c.value === row.refresh_interval_hours)) {
      const opt = document.createElement('option');
      opt.value = String(row.refresh_interval_hours);
      opt.textContent = `Every ${row.refresh_interval_hours} hours`;
      select.appendChild(opt);
    }
    select.value = String(row.refresh_interval_hours);
    select.addEventListener('change', async () => {
      const res = await apiFetch(`/api/posts/${row.id}/track`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_interval_hours: Number(select.value) }),
      });
      setRefreshStatus(res.ok
        ? `Updated check frequency for "${row.title}".`
        : 'Could not change the frequency.');
    });
    controls.appendChild(select);
  } else {
    const retrack = document.createElement('button');
    retrack.type = 'button';
    retrack.className = 'ghost';
    retrack.textContent = 'Resume tracking';
    retrack.addEventListener('click', async () => {
      const res = await apiFetch(`/api/posts/${row.id}/track`, { method: 'POST' });
      if (res.ok) loadTracked();
      else setRefreshStatus('Could not resume tracking.');
    });
    controls.appendChild(retrack);
  }

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'ghost danger';
  remove.textContent = row.tracked ? 'Stop tracking' : 'Dismiss';
  remove.title = 'The archived copy is kept either way';
  remove.addEventListener('click', async () => {
    const res = await apiFetch(`/api/posts/${row.id}/track`, { method: 'DELETE' });
    if (res.ok) loadTracked();
    else setRefreshStatus('Could not stop tracking.');
  });
  controls.appendChild(remove);

  li.append(main, controls);
  return li;
}

// ---------------------------------------------------------------------------
// Add to tracking
// ---------------------------------------------------------------------------

async function loadCreatorOptions() {
  const res = await apiFetch('/api/creators');
  if (!res.ok) return;
  const select = document.getElementById('addCreator');
  for (const c of await res.json()) {
    const opt = document.createElement('option');
    opt.value = c.name;
    opt.textContent = c.name;
    select.appendChild(opt);
  }
}

let _searchTimer = null;

async function searchToAdd() {
  const q = document.getElementById('addSearch').value.trim();
  const creator = document.getElementById('addCreator').value;
  const list = document.getElementById('addResults');
  list.innerHTML = '';
  if (q.length < 2) return;

  const params = new URLSearchParams({ q, limit: '15', require_links: 'false' });
  if (creator) params.set('creator', creator);
  const res = await apiFetch(`/api/posts?${params}`);
  if (!res.ok) return;
  const posts = await res.json();

  if (posts.length === 0) {
    const li = document.createElement('li');
    li.className = 'tracked-item empty-note';
    li.textContent = 'No archived posts match that search.';
    list.appendChild(li);
    return;
  }

  for (const post of posts) {
    const li = document.createElement('li');
    li.className = 'tracked-item';

    const main = document.createElement('div');
    main.className = 'tracked-main';
    const title = document.createElement('span');
    title.className = 'tracked-title';
    title.textContent = prettifyTitle(post.title);
    const meta = document.createElement('div');
    meta.className = 'muted';
    meta.textContent = post.creator;
    main.append(title, meta);

    const controls = document.createElement('div');
    controls.className = 'tracked-controls';
    const add = document.createElement('button');
    add.type = 'button';
    add.textContent = post.tracked ? 'Tracked' : 'Track';
    add.disabled = post.tracked;
    add.addEventListener('click', async () => {
      const res = await apiFetch(`/api/posts/${post.id}/track`, { method: 'POST' });
      if (res.ok) {
        add.textContent = 'Tracked';
        add.disabled = true;
        loadTracked();
      } else {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch {}
        setRefreshStatus(`Could not track: ${detail}`);
      }
    });
    controls.appendChild(add);

    li.append(main, controls);
    list.appendChild(li);
  }
}

// ---------------------------------------------------------------------------

document.getElementById('addSearch').addEventListener('input', () => {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(searchToAdd, 250);
});
document.getElementById('addCreator').addEventListener('change', searchToAdd);

// Each post costs a Cloudflare solve, so the refresh runs server-side as a
// background job and we poll it rather than holding a request open.
let _pollTimer = null;

async function pollRefresh() {
  const res = await apiFetch('/api/tracked/refresh/status');
  if (!res.ok) return;
  const job = await res.json();
  setRefreshStatus(job.last_message || job.status);

  if (job.status === 'running') return;
  clearInterval(_pollTimer);
  _pollTimer = null;
  document.getElementById('refreshNow').disabled = false;
  if (job.status === 'failed') {
    setRefreshStatus(`Check failed: ${job.error}`);
  }
  loadTracked();
}

document.getElementById('refreshNow').addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  setRefreshStatus('Checking tracked posts… each one goes out to Patreon, so this takes a while.');
  try {
    const res = await apiFetch('/api/tracked/refresh', { method: 'POST' });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      setRefreshStatus(`Check failed: ${detail}`);
      btn.disabled = false;
      return;
    }
    if (_pollTimer) clearInterval(_pollTimer);
    _pollTimer = setInterval(pollRefresh, 2000);
  } catch (err) {
    setRefreshStatus(`Check failed: ${err.message}`);
    btn.disabled = false;
  }
});

loadCreatorOptions();
loadTracked();
