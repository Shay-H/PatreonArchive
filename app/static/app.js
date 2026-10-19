function getCreatorValue() {
  return document.getElementById('creatorFilter').value.trim();
}

function setStatus(message) {
  document.getElementById('syncStatus').textContent = message;
}

// Convert fully-uppercase words (e.g. "MEAN GIRLS", "SPIDER-NOIR") to
// capitalized form. Mixed-case words, tokens with digits (e.g. "S02E09",
// "1978"), and roman-numeral sequel markers ("II", "IV") are left untouched.
const _ROMAN_NUMERALS = new Set([
  'II', 'III', 'IV', 'VI', 'VII', 'VIII', 'IX', 'XI', 'XII', 'XIII',
  'XIV', 'XV', 'XVI', 'XVII', 'XVIII', 'XIX', 'XX',
]);
function prettifyTitle(title) {
  if (!title) return title;
  return title.replace(/[\p{L}][\p{L}'’\-]*/gu, (word) => {
    if (_ROMAN_NUMERALS.has(word)) return word;
    const letters = (word.match(/\p{L}/gu) || []).length;
    if (letters >= 2 && word === word.toUpperCase() && word !== word.toLowerCase()) {
      return word.toLowerCase()
        .replace(/^\p{L}/u, (c) => c.toUpperCase())                         // first letter
        .replace(/-(\p{L})/gu, (_, c) => '-' + c.toUpperCase())             // after a hyphen
        .replace(/(['’])(\p{L})(?=\p{L})/gu, (_, ap, c) => ap + c.toUpperCase()); // after apostrophe only if more letters follow
    }
    return word;
  });
}

let syncPoller = null;
let syncProgress = null;

async function pollSyncStatus(creator) {
  // Every Patreon archive (video and content) runs through the FlareSolverr
  // scraper, which has its own job-state endpoint and field names.
  return pollContentStatus(creator);
}

async function pollContentStatus(creator) {
  try {
    const res = await fetch(`/api/patreon/status/${encodeURIComponent(creator)}`);
    if (!res.ok) return;
    const s = await res.json();
    if (!syncProgress || syncProgress.creator !== creator) return;

    const message = s.last_message || s.status || 'Working';
    if (s.status === 'failed') {
      setStatus(`Archive failed: ${s.error || message}`);
      stopSyncProgress();
      return;
    }
    if (s.status === 'completed') {
      setStatus(`Done. ${message}. Fetched ${s.fetched ?? 0}, imported ${s.imported ?? 0}.`);
      stopSyncProgress();
      await loadCreators();
      await searchPosts();
      return;
    }
    setStatus(`Archiving ${creator}... ${message} | Fetched: ${s.fetched ?? 0} | Imported: ${s.imported ?? 0}`);
  } catch {
    // ignore transient errors
  }
}

function startSyncProgress(creator, mode = 'video') {
  document.getElementById('syncButton').disabled = true;
  syncProgress = { creator, mode };
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

// Modes whose artifact is the post body rather than a watchable link. They
// render sanitized HTML and carry no movie/tv/genre tags. Mirrors TEXT_MODES
// in app/main.py.
const TEXT_MODES = ['content', 'links'];

// Maps creator name -> archive_mode ("video" | "content" | "links"), kept
// fresh by loadCreators so the UI can adapt (hide type filters, etc.).
let _creatorModes = {};

async function loadCreators() {
  const res = await fetch('/api/creators');
  const creators = await res.json();

  _creatorModes = Object.fromEntries(creators.map(c => [c.name, c.archive_mode]));

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
  // Restore selection: prefer a persisted choice on first load, else keep current
  const desired = _savedCreator !== undefined ? _savedCreator : current;
  if ([...filter.options].some(o => o.value === desired)) filter.value = desired;
  _savedCreator = undefined;

  // Same list for the Favourites view's own filter.
  const favFilter = document.getElementById('favCreatorFilter');
  const favCurrent = favFilter.value;
  favFilter.innerHTML = '<option value="">All sources</option>';
  for (const c of creators) {
    const opt = document.createElement('option');
    opt.value = c.name;
    opt.textContent = c.name;
    favFilter.appendChild(opt);
  }
  const favDesired = _savedFavCreator !== undefined ? _savedFavCreator : favCurrent;
  if ([...favFilter.options].some(o => o.value === favDesired)) favFilter.value = favDesired;
  _savedFavCreator = undefined;

  applyModeToTypeFilters();
}

// Hide the movie/tv/anime type filters when a text-mode creator is selected
// (those tags are video-only). Shown for "All sources" and video creators.
function applyModeToTypeFilters() {
  const selected = getCreatorValue();
  const isTextMode = selected && TEXT_MODES.includes(_creatorModes[selected]);
  const typeFilters = document.querySelector('.type-filters');
  if (typeFilters) typeFilters.classList.toggle('hidden', !!isTextMode);
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
  const mode = document.getElementById('modeSelect').value;
  const fetchFlag = document.getElementById('downloadToggle').checked;
  if (!creator) { setStatus('Please enter a creator vanity first.'); return; }

  startSyncProgress(creator, mode);
  try {
    // All Patreon archives (video and content) go through the FlareSolverr
    // scraper — patreon-dl is Cloudflare-blocked on the server and is no longer
    // used. `mode` sets the creator's archive_mode (video = links + TMDB tags,
    // content = rich body + downloaded attachments, links = rich body + every
    // URL the post references, with nothing downloaded).
    const url = `/api/patreon/sync/${encodeURIComponent(creator)}?mode=${encodeURIComponent(mode)}`;
    const res = await fetch(url, { method: 'POST' });
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

  document.getElementById('postTitle').textContent = prettifyTitle(post.title);
  document.getElementById('postMeta').textContent =
    `${post.creator} | ${post.published_at ?? ''} | ${post.tags.join(', ')}`;
  noteTracked(post);
  renderTrackToggle(post);

  const isTextMode = TEXT_MODES.includes(post.archive_mode);
  const postBody = document.getElementById('postBody');
  const postHtml = document.getElementById('postContentHtml');
  if (isTextMode && post.content_html) {
    // content_html is sanitized server-side (app/sanitize.py).
    postHtml.innerHTML = post.content_html;
    postHtml.classList.remove('hidden');
    postBody.textContent = '';
    postBody.classList.add('hidden');
  } else {
    postHtml.innerHTML = '';
    postHtml.classList.add('hidden');
    if (post.content) {
      postBody.textContent = post.content;
      postBody.classList.remove('hidden');
    } else {
      postBody.textContent = '';
      postBody.classList.add('hidden');
    }
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

  // Attachments. Content-mode posts link to the archived file on disk;
  // links-mode posts list the same metadata with nothing behind it, since
  // their files are deliberately never downloaded.
  const attHeading = document.getElementById('attachmentsHeading');
  const attList = document.getElementById('postAttachments');
  attList.innerHTML = '';
  const atts = post.attachments || [];
  for (const a of atts) {
    const li = document.createElement('li');
    const sizeStr = a.size_bytes ? ` (${formatBytes(a.size_bytes)})` : '';
    const kind = document.createElement('span');
    kind.className = 'tag';
    kind.textContent = a.kind;
    li.append(kind, document.createTextNode(' '));
    if (a.downloaded) {
      const link = document.createElement('a');
      link.href = `/api/posts/${post.id}/attachments/${a.id}`;
      link.textContent = `${a.filename}${sizeStr}`;
      link.setAttribute('download', '');
      li.appendChild(link);
    } else {
      const span = document.createElement('span');
      span.className = 'muted';
      span.textContent = `${a.filename}${sizeStr} — not archived`;
      li.appendChild(span);
    }
    attList.appendChild(li);
  }
  attHeading.classList.toggle('hidden', atts.length === 0);
  attList.classList.toggle('hidden', atts.length === 0);

  // Links come split by the API: `links` is everything pointing off Patreon
  // (the part worth archiving), `patreon_links` the self-referential rest,
  // listed below it and muted.
  renderLinkList('postLinks', post.links);
  renderLinkList('postPatreonLinks', post.patreon_links || []);
  // Hide the Links heading for posts without any (common for content posts).
  document.getElementById('linksHeading').classList.toggle('hidden', post.links.length === 0);
  const patreonCount = (post.patreon_links || []).length;
  document.getElementById('patreonLinksHeading').classList.toggle('hidden', patreonCount === 0);
  document.getElementById('postPatreonLinks').classList.toggle('hidden', patreonCount === 0);
}

// Fill a <ul> with anchors. Built via the DOM rather than innerHTML so a URL
// containing quotes or markup can't break out into the page.
// The detail pane's own track control, mirroring the card button.
function renderTrackToggle(post) {
  const btn = document.getElementById('trackToggle');
  const info = document.getElementById('trackInfo');

  function paint() {
    const on = isTracked(post.id);
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = on ? 'Tracking for updates' : 'Track for updates';
    const bits = [];
    if (post.last_checked_at) {
      bits.push(`last checked ${new Date(post.last_checked_at).toLocaleString()}`);
    }
    if (post.content_updated_at) {
      bits.push(`last changed ${new Date(post.content_updated_at).toLocaleString()}`);
    }
    info.textContent = bits.join(' · ');
  }

  btn.onclick = async () => {
    await toggleTracked(post);
    paint();
    // Keep the list card in step with the detail pane.
    refreshVisibleTrackButtons();
  };
  paint();
}

function renderLinkList(elementId, urls) {
  const list = document.getElementById(elementId);
  list.innerHTML = '';
  for (const url of urls) {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = url;
    li.appendChild(a);
    list.appendChild(li);
  }
}

function formatBytes(n) {
  if (!n) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

// ---------------------------------------------------------------------------
// Favourites — stored server-side against the logged-in user (see app/main.py).
// ---------------------------------------------------------------------------
// favSummaries: the user's favourited posts (PostSummary), newest first.
// favIds: id set for O(1) star-state lookups on post cards.
let favSummaries = [];
let favIds = new Set();
let currentView = 'posts'; // 'posts' | 'favourites'

// fetch wrapper: an expired/absent session (401) bounces the user to /login.
async function apiFetch(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('unauthenticated');
  }
  return res;
}

async function loadFavourites() {
  try {
    const res = await apiFetch('/api/favourites');
    if (!res.ok) return;
    favSummaries = await res.json();
    favIds = new Set(favSummaries.map(p => p.id));
  } catch {
    // network/auth error already handled by apiFetch; keep last-known state
  }
  updateFavCount();
}

// ---------------------------------------------------------------------------
// Tracking — "re-check this post on every sync and refresh the saved copy".
// Unlike favourites, tracking is a property of the post rather than the
// viewer: it changes what the scraper fetches, so it is shared by everyone.
// ---------------------------------------------------------------------------
const trackedIds = new Set();

function isTracked(id) {
  return trackedIds.has(id);
}

function noteTracked(post) {
  if (post.tracked) trackedIds.add(post.id);
  else trackedIds.delete(post.id);
}

// Optimistic like toggleFavourite: flips local state synchronously, reverts on
// failure. Returns the new state so callers can repaint immediately.
async function toggleTracked(post) {
  const wasTracked = trackedIds.has(post.id);
  if (wasTracked) trackedIds.delete(post.id);
  else trackedIds.add(post.id);
  post.tracked = !wasTracked;
  try {
    const res = await apiFetch(`/api/posts/${post.id}/track`, {
      method: wasTracked ? 'DELETE' : 'POST',
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
  } catch (err) {
    if (wasTracked) trackedIds.add(post.id);
    else trackedIds.delete(post.id);
    post.tracked = wasTracked;
    setStatus(`Could not change tracking: ${err.message}`);
    if (currentView === 'favourites') renderFavourites();
    else refreshVisibleTrackButtons();
  }
  return !wasTracked;
}

// Repaint every track button in the list against trackedIds, used after a
// failed toggle so the UI can't drift from the server.
function refreshVisibleTrackButtons() {
  for (const btn of document.querySelectorAll('.track-btn')) {
    const on = trackedIds.has(Number(btn.dataset.id));
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
}

function isFavourite(id) {
  return favIds.has(id);
}

function updateFavCount() {
  document.getElementById('favCount').textContent = favIds.size;
}

// Optimistically flips local state, then persists to the server; reverts on
// failure. The synchronous part runs before the first await, so callers can
// read isFavourite()/favSummaries immediately after calling.
async function toggleFavourite(post) {
  const wasFav = favIds.has(post.id);
  if (wasFav) {
    favIds.delete(post.id);
    favSummaries = favSummaries.filter(p => p.id !== post.id);
  } else {
    favIds.add(post.id);
    favSummaries.unshift(post);
  }
  updateFavCount();
  try {
    const res = await apiFetch(`/api/favourites/${post.id}`, { method: wasFav ? 'DELETE' : 'POST' });
    if (!res.ok) throw new Error(String(res.status));
  } catch {
    // Revert the optimistic change.
    if (wasFav) { favIds.add(post.id); favSummaries.unshift(post); }
    else { favIds.delete(post.id); favSummaries = favSummaries.filter(p => p.id !== post.id); }
    updateFavCount();
    if (currentView === 'favourites') renderFavourites();
  }
}

// The Search card is hidden in the Favourites view (its query/tag/type
// controls don't apply), so favourites get their own source filter, applied
// client-side over the already-loaded list.
function favouriteCreatorFilter() {
  const select = document.getElementById('favCreatorFilter');
  return select ? select.value : '';
}

function visibleFavourites() {
  const creator = favouriteCreatorFilter();
  return creator ? favSummaries.filter(p => p.creator === creator) : favSummaries;
}

function renderFavourites() {
  const list = document.getElementById('postList');
  const counter = document.getElementById('resultsCount');
  while (list.firstChild && list.firstChild !== _sentinel) list.removeChild(list.firstChild);

  const shown = visibleFavourites();
  if (favSummaries.length === 0) {
    const li = document.createElement('li');
    li.className = 'post-item empty-note';
    li.textContent = 'No favourites yet. Tap the ☆ on any post to save it here.';
    list.insertBefore(li, _sentinel);
  } else if (shown.length === 0) {
    const li = document.createElement('li');
    li.className = 'post-item empty-note';
    li.textContent = `No favourites from ${favouriteCreatorFilter()}.`;
    list.insertBefore(li, _sentinel);
  } else {
    for (const p of shown) list.insertBefore(_makePostItem(p), _sentinel);
  }
  counter.textContent = `${shown.length} favourite${shown.length === 1 ? '' : 's'}`;
}

async function setView(view) {
  if (view === currentView) return;
  currentView = view;
  document.getElementById('tabPosts').classList.toggle('active', view === 'posts');
  document.getElementById('tabFavourites').classList.toggle('active', view === 'favourites');
  document.getElementById('tabPosts').setAttribute('aria-selected', view === 'posts');
  document.getElementById('tabFavourites').setAttribute('aria-selected', view === 'favourites');
  // The search controls only apply to the Posts view; favourites get their
  // own source filter in the results header instead.
  document.querySelector('.search-controls').classList.toggle('hidden', view === 'favourites');
  document.getElementById('favCreatorFilter').classList.toggle('hidden', view !== 'favourites');

  if (view === 'favourites') {
    await loadFavourites(); // re-sync with the DB before rendering
    renderFavourites();
  } else {
    _lastQueryKey = null; // force a rebuild of the posts list
    searchPosts();
  }
}

// ---------------------------------------------------------------------------
// Active tag filters (removable bubbles) + persisted UI state
// ---------------------------------------------------------------------------
const STORAGE_KEY = 'patreonArchive.filters';
let activeTags = [];
let _savedCreator;    // creator to restore once the dropdown is populated
let _savedFavCreator; // same, for the Favourites view's source filter

function persistState() {
  const state = {
    q: document.getElementById('query').value,
    tags: activeTags,
    creator: getCreatorValue(),
    favCreator: favouriteCreatorFilter(),
    types: [...document.querySelectorAll('.typeCheck:checked')].map(cb => cb.value),
  };
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch {}
}

function restoreState() {
  let state;
  try { state = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null'); } catch {}
  if (!state) return;
  if (typeof state.q === 'string') document.getElementById('query').value = state.q;
  if (Array.isArray(state.tags)) activeTags = state.tags.filter(t => typeof t === 'string');
  if (Array.isArray(state.types)) {
    document.querySelectorAll('.typeCheck').forEach(cb => {
      cb.checked = state.types.includes(cb.value);
    });
  }
  if (typeof state.creator === 'string') _savedCreator = state.creator;
  if (typeof state.favCreator === 'string') _savedFavCreator = state.favCreator;
  renderTagFilters();
  updateQueryClear();
}

function renderTagFilters() {
  const bar = document.getElementById('tagFilters');
  bar.innerHTML = '';
  for (const tag of activeTags) {
    const bubble = document.createElement('span');
    bubble.className = 'tag-bubble';
    bubble.textContent = tag;
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'tag-bubble-x';
    x.setAttribute('aria-label', `Remove ${tag} filter`);
    x.textContent = '×';
    x.addEventListener('click', () => removeTag(tag));
    bubble.appendChild(x);
    bar.appendChild(bubble);
  }
  if (activeTags.length) {
    const clearAll = document.createElement('button');
    clearAll.type = 'button';
    clearAll.className = 'tag-clear-all';
    clearAll.textContent = 'Clear all';
    clearAll.addEventListener('click', clearAllTags);
    bar.appendChild(clearAll);
  }
  bar.classList.toggle('hidden', activeTags.length === 0);
}

function clearAllTags() {
  if (!activeTags.length) return;
  activeTags = [];
  renderTagFilters();
  persistState();
  searchPosts();
}

function addTag(tag) {
  tag = String(tag).trim().toLowerCase();
  if (!tag || activeTags.includes(tag)) return;
  activeTags.push(tag);
  renderTagFilters();
  persistState();
  searchPosts();
}

function removeTag(tag) {
  const i = activeTags.indexOf(tag);
  if (i === -1) return;
  activeTags.splice(i, 1);
  renderTagFilters();
  persistState();
  searchPosts();
}

// Commit whatever the user typed in the tag input into one or more bubbles.
function commitTagInput() {
  const input = document.getElementById('tag');
  let added = false;
  for (const part of input.value.split(',')) {
    const t = part.trim().toLowerCase();
    if (t && !activeTags.includes(t)) { activeTags.push(t); added = true; }
  }
  input.value = '';
  if (added) { renderTagFilters(); persistState(); searchPosts(); }
}

function updateQueryClear() {
  const hasText = document.getElementById('query').value.length > 0;
  document.getElementById('queryClear').classList.toggle('hidden', !hasText);
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

  const link = document.createElement('a');
  link.href = '#';
  link.dataset.id = post.id;
  link.textContent = prettifyTitle(post.title);
  link.addEventListener('click', (e) => {
    e.preventDefault();
    loadPost(post.id);
  });

  const fav = document.createElement('button');
  fav.type = 'button';
  fav.className = 'fav-btn';
  fav.innerHTML =
    '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">'
    + '<path d="M12 2.6l2.9 5.9 6.5.9-4.7 4.6 1.1 6.5L12 18l-5.8 3 1.1-6.5L2.6 9.4l6.5-.9z"/>'
    + '</svg>';
  function refreshStar() {
    const on = isFavourite(post.id);
    fav.classList.toggle('on', on);
    fav.setAttribute('aria-pressed', on ? 'true' : 'false');
    fav.title = on ? 'Remove from favourites' : 'Add to favourites';
    fav.setAttribute('aria-label', fav.title);
  }
  refreshStar();
  fav.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    toggleFavourite(post); // optimistic; local state updates synchronously
    refreshStar();
    // In the Favourites view, re-render so an un-favourited card drops out.
    if (currentView === 'favourites') renderFavourites();
  });

  const track = document.createElement('button');
  track.type = 'button';
  track.className = 'track-btn';
  track.dataset.id = post.id;
  // Circular arrows: "keep this in sync".
  track.innerHTML =
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
    + '<path d="M20 11a8 8 0 1 0-.6 3M20 5v6h-6"/>'
    + '</svg>';
  function refreshTrack() {
    const on = isTracked(post.id);
    track.classList.toggle('on', on);
    track.setAttribute('aria-pressed', on ? 'true' : 'false');
    track.title = on
      ? 'Tracked — re-checked on every sync'
      : 'Track for updates';
    track.setAttribute('aria-label', track.title);
  }
  noteTracked(post);
  refreshTrack();
  track.addEventListener('click', async (e) => {
    e.preventDefault();
    e.stopPropagation();
    await toggleTracked(post);
    refreshTrack();
  });

  const meta = document.createElement('div');
  meta.className = 'muted';
  meta.textContent = published ? `${post.creator} · ${published}` : post.creator;
  if (post.content_updated_at) {
    const upd = document.createElement('span');
    upd.className = 'updated-badge';
    upd.textContent = `updated ${new Date(post.content_updated_at).toLocaleDateString()}`;
    upd.title = 'Tracking last saw this post change on this date';
    meta.append(' ', upd);
  }

  const tagLine = document.createElement('div');
  tagLine.className = 'tag-line';
  if (post.tags.length) {
    for (const tag of post.tags) {
      const span = document.createElement('span');
      span.className = 'tag clickable';
      span.textContent = tag;
      span.title = `Filter by "${tag}"`;
      span.addEventListener('click', () => addTag(tag));
      tagLine.appendChild(span);
    }
  } else {
    tagLine.textContent = 'No tags';
  }

  li.append(fav, track, link, meta, tagLine);
  return li;
}

async function _loadMore() {
  if (currentView !== 'posts') return; // Favourites view isn't paginated
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
  const allTags = [checkedTypes, ...activeTags].filter(Boolean).join(',');
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
  updateQueryClear();
  persistState();
  clearTimeout(_debounce);
  _debounce = setTimeout(searchPosts, 250);
}

document.getElementById('query').addEventListener('input', _onInput);

document.getElementById('queryClear').addEventListener('click', () => {
  const q = document.getElementById('query');
  q.value = '';
  updateQueryClear();
  persistState();
  q.focus();
  searchPosts();
});

// Tag input: Enter/comma commits a bubble; Backspace on empty removes the last.
const _tagInput = document.getElementById('tag');
_tagInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    commitTagInput();
  } else if (e.key === 'Backspace' && !e.target.value && activeTags.length) {
    removeTag(activeTags[activeTags.length - 1]);
  }
});
_tagInput.addEventListener('blur', commitTagInput);

document.getElementById('creatorFilter').addEventListener('change', () => {
  applyModeToTypeFilters();
  persistState();
  searchPosts();
});

document.getElementById('favCreatorFilter').addEventListener('change', () => {
  persistState();
  renderFavourites();
});
document.querySelectorAll('.typeCheck').forEach(cb => cb.addEventListener('change', () => {
  persistState();
  searchPosts();
}));
document.getElementById('searchButton').addEventListener('click', () => {
  commitTagInput();
  searchPosts();
});
document.getElementById('syncButton').addEventListener('click', syncCreator);
document.getElementById('refreshCreatorsButton').addEventListener('click', loadCreators);
document.getElementById('tabPosts').addEventListener('click', () => setView('posts'));
document.getElementById('tabFavourites').addEventListener('click', () => setView('favourites'));

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

loadFavourites();
restoreState();
loadConfigStatus();
// Restore the creator selection before the first search, then search.
loadCreators().then(searchPosts);
loadSchedulerStatus();