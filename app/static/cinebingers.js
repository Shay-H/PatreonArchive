// Cinebingers sync UI

(function () {
  const btn = document.getElementById('cinebingersButton');
  const statusEl = document.getElementById('cinebingersStatus');

  let poller = null;

  function setStatus(msg) {
    statusEl.textContent = msg;
    statusEl.classList.remove('hidden');
  }

  function stopPolling() {
    if (poller) { clearInterval(poller); poller = null; }
    btn.disabled = false;
  }

  async function poll() {
    try {
      const res = await fetch('/api/cinebingers/status');
      if (!res.ok) return;
      const s = await res.json();

      if (s.status === 'completed') {
        setStatus(`Done — ${s.imported} videos imported into database.`);
        stopPolling();
        if (typeof loadCreators === 'function') loadCreators();
        if (typeof searchPosts === 'function') searchPosts();
        return;
      }
      if (s.status === 'failed') {
        setStatus(`Failed: ${s.error || s.last_message}`);
        stopPolling();
        return;
      }

      const page = s.current_page ? ` (page ${s.current_page})` : '';
      const scraped = s.scraped ? ` — ${s.scraped} scraped` : '';
      setStatus(`${s.last_message}${page}${scraped}`);
    } catch {
      // ignore transient errors
    }
  }

  async function startSync() {
    btn.disabled = true;
    setStatus('Starting sync…');
    try {
      const res = await fetch('/api/cinebingers/sync', { method: 'POST' });
      if (!res.ok) {
        setStatus(`Error: ${await res.text()}`);
        btn.disabled = false;
        return;
      }
      poller = setInterval(poll, 1500);
      poll();
    } catch (err) {
      setStatus(`Error: ${err}`);
      btn.disabled = false;
    }
  }

  btn.addEventListener('click', startSync);
})();

// ---------------------------------------------------------------------------
// Tag all posts UI
// ---------------------------------------------------------------------------
(function () {
  const btn = document.getElementById('tagButton');
  const statusEl = document.getElementById('tagStatus');

  let poller = null;

  function setStatus(msg) {
    statusEl.textContent = msg;
    statusEl.classList.remove('hidden');
  }

  function stopPolling() {
    if (poller) { clearInterval(poller); poller = null; }
    btn.disabled = false;
  }

  async function poll() {
    try {
      const res = await fetch('/api/tag/status');
      if (!res.ok) return;
      const s = await res.json();

      if (s.status === 'completed') {
        setStatus(`Done — ${s.processed} posts tagged.`);
        stopPolling();
        if (typeof searchPosts === 'function') searchPosts();
        return;
      }
      if (s.status === 'failed') {
        setStatus(`Failed: ${s.error || s.last_message}`);
        stopPolling();
        return;
      }
      const pct = s.total ? ` (${s.processed}/${s.total})` : '';
      setStatus(`${s.last_message}${pct}`);
    } catch {
      // ignore transient errors
    }
  }

  async function startTagging() {
    btn.disabled = true;
    setStatus('Starting…');
    try {
      const res = await fetch('/api/tag', { method: 'POST' });
      if (!res.ok) {
        setStatus(`Error: ${await res.text()}`);
        btn.disabled = false;
        return;
      }
      poller = setInterval(poll, 1500);
      poll();
    } catch (err) {
      setStatus(`Error: ${err}`);
      btn.disabled = false;
    }
  }

  btn.addEventListener('click', startTagging);
})();

