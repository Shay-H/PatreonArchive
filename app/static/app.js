function getCreatorValue() {
  return document.getElementById('creatorFilter').value.trim();
}

function setStatus(message) {
  document.getElementById('syncStatus').textContent = message;
}

let syncPoller = null;
let syncProgress = null;

async function pollSyncStatus(creator) {
  try {
    const res = await fetch(`/api/sync-status/${encodeURIComponent(creator)}`);
    if (!res.ok) return;
    const status = await res.json();
    if (!syncProgress || syncProgress.creator !== creator) return;

    const seenPosts = status.downloaded_posts ?? 0;
    const importedPosts = status.imported_posts ?? status.imported_posts_db ?? 0;
    const message = status.last_message || status.status || 'Working';

    if (status.status === 'failed') {
      setStatus(`Sync failed: ${status.error || message}`);
      stopSyncProgress();
      return;
    }

    if (status.status === 'completed') {
      setStatus(`Done. ${message}. Processed ${seenPosts} posts, imported ${importedPosts}.`);
      stopSyncProgress();
      await loadCreators();
      await searchPosts();
      return;
    }

    setStatus(`Syncing ${creator}... ${message} | Seen: ${seenPosts} | Imported: ${importedPosts}`);
  } catch {
    // ignore transient errors
  }
}

function startSyncProgress(creator) {
  document.getElementById('syncButton').disabled = true;
  syncProgress = { creator };
  pollSyncStatus(creator);
  syncPoller = setInterval(() => pollSyncStatus(creator), 1500);
}

function stopSyncProgress() {
  if (syncPoller !== null) { clearInterval(syncPoller); syncPoller = null; }
  syncProgress = null;
  document.getElementById('syncButton').disabled = false;
}

function setConfigPill(ok, message) {
  const pill = document.getElementById('configPill');
  pill.classList.remove('ok', 'warn');
  pill.classList.add(ok ? 'ok' : 'warn');
  pill.textContent = message;
}

async function loadConfigStatus() {
  const res = await fetch('/api/config-status');
  const cfg = await res.json();
  setConfigPill(
    cfg.patreon_cookie_configured,
    cfg.patreon_cookie_configured ? 'Patreon cookie configured' : 'PATREON_COOKIE missing in .env'
  );
}

async function loadCreators() {
  const res = await fetch('/api/creators');
  const creators = await res.json();

  // Populate ingest datalist
  const list = document.getElementById('creatorList');
  list.innerHTML = '';
  for (const c of creators) {
    const opt = document.createElement('option');
    opt.value = c.name;
    list.appendChild(opt);
  }

  // Populate search filter dropdown, preserving current selection
  const filter = document.getElementById('creatorFilter');
  const current = filter.value;
  filter.innerHTML = '<option value="">All sources</option>';
  for (const c of creators) {
    const opt = document.createElement('option');
    opt.value = c.name;
    opt.textContent = c.name;
    filter.appendChild(opt);
  }
  // Restore selection if it still exists
  if ([...filter.options].some(o => o.value === current)) filter.value = current;
}

function extractCreatorSlug(input) {
  // Accept a full Patreon URL or a bare vanity slug.
  // Handles: https://www.patreon.com/c/cinejump/posts
  //          https://www.patreon.com/cinejump/posts
  //          https://www.patreon.com/cinejump
  //          cinejump
  try {
    const url = new URL(input);
    if (url.hostname.includes('patreon.com')) {
      const parts = url.pathname.split('/').filter(Boolean);
      // /c/slug/... or /slug/...
      const idx = parts[0] === 'c' ? 1 : 0;
      return parts[idx] || input;
    }
  } catch {}
  return input; // bare slug
}

async function syncCreator() {
  const raw = document.getElementById('creatorInput').value.trim();
  const creator = extractCreatorSlug(raw);
  const fetchFlag = document.getElementById('downloadToggle').checked;
  if (!creator) { setStatus('Please enter a creator vanity first.'); return; }

  startSyncProgress(creator);
  try {
    const res = await fetch(
      `/api/sync/${encodeURIComponent(creator)}?fetch=${fetchFlag ? 'true' : 'false'}`,
      { method: 'POST' }
    );
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      setStatus(`Sync failed: ${detail}`);
      stopSyncProgress();
    }
  } catch (err) {
    setStatus(`Sync failed: ${err}`);
    stopSyncProgress();
  }
}

async function loadPost(id) {
  const res = await fetch(`/api/posts/${id}`);
  const post = await res.json();

  document.getElementById('postTitle').textContent = post.title;
  document.getElementById('postMeta').textContent =
    `${post.creator} | ${post.published_at ?? ''} | ${post.tags.join(', ')}`;
  const postBody = document.getElementById('postBody');
  if (post.content) {
    postBody.textContent = post.content;
    postBody.classList.remove('hidden');
  } else {
    postBody.textContent = '';
    postBody.classList.add('hidden');
  }

  const source = document.getElementById('postSource');
  if (post.post_url) {
    source.href = post.post_url;
    source.textContent = 'Open original post';
    source.classList.remove('hidden');
  } else {
    source.classList.add('hidden');
  }

  // YouTube embed: find first YouTube link in post links or post_url
  const embedDiv = document.getElementById('postEmbed');
  const youtubeRe = /(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]{11})/;
  const allLinks = [...post.links];
  if (post.post_url) allLinks.unshift(post.post_url);
  const ytLink = allLinks.find(l => youtubeRe.test(l));
  if (ytLink) {
    const videoId = ytLink.match(youtubeRe)[1];
    embedDiv.innerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${videoId}"
      frameborder="0" allowfullscreen allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"></iframe>`;
    embedDiv.classList.remove('hidden');
  } else {
    embedDiv.innerHTML = '';
    embedDiv.classList.add('hidden');
  }

  const links = document.getElementById('postLinks');
  links.innerHTML = '';
  for (const link of post.links) {
    const li = document.createElement('li');
    li.innerHTML = `<a href="${link}" target="_blank" rel="noopener noreferrer">${link}</a>`;
    links.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Live search + infinite scroll
// ---------------------------------------------------------------------------
const PAGE_SIZE = 50;
let _query = {};
let _offset = 0;
let _exhausted = false;
let _loading = false;
let _debounce = null;

async function _fetchPage(offset) {
  const { q, tag, creator } = _query;
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset });
  if (q) params.set('q', q);
  if (tag) params.set('tag', tag);
  if (creator) params.set('creator', creator);
  const res = await fetch(`/api/posts?${params}`);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

function _getCheckedTypes() {
  return [...document.querySelectorAll('.typeCheck:checked')].map(cb => cb.value).join(',');
}

function _makePostItem(post) {
  const li = document.createElement('li');
  li.className = 'post-item';
  const published = post.published_at
    ? new Date(post.published_at).toLocaleString()
    : '';
  li.innerHTML = `
    <a href="#" data-id="${post.id}">${post.title}</a>
    <div class="muted">${post.creator}${published ? ' &middot; ' + published : ''}</div>
    <div class="tag-line">${post.tags.length ? post.tags.join(', ') : 'No tags'}</div>
  `;
  li.querySelector('a').addEventListener('click', (e) => {
    e.preventDefault();
    loadPost(post.id);
  });
  return li;
}

async function _loadMore() {
  if (_loading || _exhausted) return;
  _loading = true;
  const list = document.getElementById('postList');
  const counter = document.getElementById('resultsCount');
  try {
    const posts = await _fetchPage(_offset);
    if (posts.length < PAGE_SIZE) _exhausted = true;
    _offset += posts.length;

    if (_offset === posts.length && posts.length === 0) {
      const li = document.createElement('li');
      li.className = 'post-item';
      li.textContent = 'No posts found.';
      list.insertBefore(li, _sentinel);
    } else {
      for (const p of posts) list.insertBefore(_makePostItem(p), _sentinel);
    }
    counter.textContent = _exhausted
      ? `${_offset} result${_offset === 1 ? '' : 's'}`
      : `${_offset}+ results`;
  } catch (err) {
    counter.textContent = `Error: ${err.message}`;
  } finally {
    _loading = false;
  }
}

let _lastQueryKey = null;

async function searchPosts() {
  const checkedTypes = _getCheckedTypes();
  const tagInput = document.getElementById('tag').value.trim();
  const allTags = [checkedTypes, tagInput].filter(Boolean).join(',');
  const newQuery = {
    q: document.getElementById('query').value.trim(),
    tag: allTags,
    creator: getCreatorValue(),
  };
  const newKey = JSON.stringify(newQuery);
  if (newKey === _lastQueryKey) return; // nothing changed
  _lastQueryKey = newKey;

  // Fetch first page before touching the DOM
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: 0 });
  if (newQuery.q) params.set('q', newQuery.q);
  if (newQuery.tag) params.set('tag', newQuery.tag);
  if (newQuery.creator) params.set('creator', newQuery.creator);

  const list = document.getElementById('postList');
  const counter = document.getElementById('resultsCount');

  let posts;
  try {
    const res = await fetch(`/api/posts?${params}`);
    if (!res.ok) throw new Error(res.statusText);
    posts = await res.json();
  } catch (err) {
    counter.textContent = `Error: ${err.message}`;
    return;
  }

  // If query changed while we were fetching, discard this result
  if (newKey !== _lastQueryKey) return;

  _query = newQuery;
  _offset = posts.length;
  _exhausted = posts.length < PAGE_SIZE;

  // Fade out, swap content, fade in
  list.style.transition = 'opacity 0.15s';
  list.style.opacity = '0';
  await new Promise(r => setTimeout(r, 150));

  while (list.firstChild && list.firstChild !== _sentinel) list.removeChild(list.firstChild);

  if (posts.length === 0) {
    const li = document.createElement('li');
    li.className = 'post-item';
    li.textContent = 'No posts found.';
    list.insertBefore(li, _sentinel);
  } else {
    for (const p of posts) list.insertBefore(_makePostItem(p), _sentinel);
  }

  counter.textContent = _exhausted
    ? `${_offset} result${_offset === 1 ? '' : 's'}`
    : `${_offset}+ results`;

  list.style.opacity = '1';
}

// Sentinel element triggers _loadMore when scrolled into view
const _postList = document.getElementById('postList');
const _sentinel = document.createElement('li');
_sentinel.style.height = '1px';
_postList.appendChild(_sentinel);
new IntersectionObserver(
  (entries) => { if (entries[0].isIntersecting) _loadMore(); },
  { root: _postList, rootMargin: '200px' }
).observe(_sentinel);

// Debounce: fire searchPosts 250 ms after the user stops typing
function _onInput() {
  clearTimeout(_debounce);
  _debounce = setTimeout(searchPosts, 250);
}

document.getElementById('query').addEventListener('input', _onInput);
document.getElementById('tag').addEventListener('input', _onInput);
document.getElementById('creatorFilter').addEventListener('change', searchPosts);
document.querySelectorAll('.typeCheck').forEach(cb => cb.addEventListener('change', searchPosts));
document.getElementById('searchButton').addEventListener('click', searchPosts);
document.getElementById('syncButton').addEventListener('click', syncCreator);
document.getElementById('refreshCreatorsButton').addEventListener('click', loadCreators);

// ---------------------------------------------------------------------------
// Daily scheduler: surface auth failures that paused the auto-sync
// ---------------------------------------------------------------------------
function _sourceLabel(src) {
  if (src === 'patreon') return 'Patreon';
  if (src === 'cinebingers') return 'Cinebingers';
  return src || 'A source';
}

function showAuthModal(failure) {
  const label = _sourceLabel(failure.source);
  const when = failure.at ? new Date(failure.at).toLocaleString() : '';
  const fix = failure.source === 'cinebingers'
    ? 'Refresh <code>data/cinebingers_cookies.json</code> with fresh session cookies.'
    : 'Refresh <code>PATREON_COOKIE</code> in <code>.env</code> and redeploy.';
  document.getElementById('authModalMsg').innerHTML =
    `The daily auto-sync was paused because <strong>${label}</strong> authentication failed`
    + `${when ? ` (${when})` : ''}.<br><br>${fix}<br><br>`
    + `<span class="muted">${failure.message || ''}</span>`;
  document.getElementById('authModal').classList.remove('hidden');
}

async function loadSchedulerStatus() {
  try {
    const res = await fetch('/api/scheduler/status');
    if (!res.ok) return;
    const s = await res.json();
    if (s.auth_failure) showAuthModal(s.auth_failure);
  } catch {
    // ignore transient errors
  }
}

document.getElementById('authDismissBtn').addEventListener('click', () => {
  document.getElementById('authModal').classList.add('hidden');
});
document.getElementById('authResumeBtn').addEventListener('click', async () => {
  try { await fetch('/api/scheduler/resume', { method: 'POST' }); } catch {}
  document.getElementById('authModal').classList.add('hidden');
});

loadConfigStatus();
loadCreators();
searchPosts();
loadSchedulerStatus();