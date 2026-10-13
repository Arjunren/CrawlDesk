'use strict';

const $ = id => document.getElementById(id);
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHtml = text => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const activeStatuses = new Set(['running', 'paused', 'stopping']);
let state = {status:'idle', pages:[], logs:[], settings:{}, elapsed:0};
let currentTab = 'pages', currentView = 'workspace', selectedPage = null, renderedSignature = '', polling = false, bridgeReady = false, exportBusy = false, toastTimer, focusBeforeDrawer;

function toast(message, error = false) {
  $('toast').textContent = message;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {$('toast').hidden = true;}, error ? 6500 : 4500);
}

async function call(method, ...args) {
  if (!bridgeReady) {toast('Open CrawlDesk with desktop.py to use the desktop controls.', true); return null;}
  try {
    const result = await window.pywebview.api[method](...args);
    if (result?.ok === false) toast(result.error, true);
    return result;
  } catch (error) {toast('The desktop connection failed. Please restart CrawlDesk.', true); return null;}
}

function settings() {
  const result = {url:$('url').value, exclude:$('exclude').value};
  for (const key of ['max_pages','max_depth','delay','timeout','max_bytes']) result[key] = Number($(key).value);
  return result;
}

function applySettings(values) {
  if (values.url) $('url').value = values.url;
  for (const key of ['max_pages','max_depth','delay','timeout','max_bytes']) {
    if (values[key] !== undefined) $(key).value = values[key];
  }
  if (values.exclude) $('exclude').value = values.exclude.join('\n');
  $('limit-summary').textContent = `Up to ${$('max_pages').value} pages`;
}

function duration(seconds) {
  return seconds < 60 ? `${Math.floor(seconds)}<span class="stat-unit">s</span>` : `${Math.floor(seconds/60)}<span class="stat-unit">m</span> ${Math.floor(seconds%60)}<span class="stat-unit">s</span>`;
}

function render(next) {
  const previousStatus = state.status;
  const previousId = state.id;
  state = next;
  const active = activeStatuses.has(state.status);
  const pages = state.pages || [];
  $('version').textContent = state.version || '2.0.0';
  $('stat-pages').textContent = pages.length;
  $('stat-pages-note').textContent = state.status === 'idle' ? 'Ready when you are' : `${state.saved || 0} HTML pages · ${state.attempted || 0} attempted`;
  $('stat-links').textContent = new Set(pages.flatMap(page => page.internal_links || [])).size;
  const issues = pages.filter(page => page.issues?.length).length;
  $('stat-issues').textContent = issues;
  $('stat-issues-note').textContent = pages.length ? `${pages.length - issues} pages with no flagged issues` : 'Room for improvement';
  $('stat-duration').innerHTML = duration(state.elapsed || 0);
  $('stat-duration-note').textContent = active ? 'Crawl in progress' : 'Time spent exploring';
  $('pages-count').textContent = pages.length;
  $('issues-count').textContent = issues;
  $('start-button').disabled = active || !bridgeReady;
  $('start-button').innerHTML = active ? `Crawl in progress${icon('clock')}` : `Start crawl${icon('arrow')}`;
  $('demo-button').disabled = $('empty-demo').disabled = active || !bridgeReady;
  $('export-button').disabled = !pages.length || !bridgeReady || exportBusy;
  for (const key of ['url','max_pages','max_depth','delay','timeout','max_bytes','exclude']) $(key).disabled = active;
  $('run-banner').hidden = state.status === 'idle';
  $('run-icon').classList.toggle('pulse', state.status === 'running');
  const labels = {running:'Exploring your website', paused:'Crawl paused', stopping:'Finishing the current request…', completed:'Crawl complete', stopped:'Crawl stopped', failed:'Crawl could not finish', demo:'Sample crawl · fictional data'};
  $('run-label').textContent = labels[state.status] || state.status;
  $('run-description').textContent = state.error || state.stop_reason || (state.status === 'demo' ? 'Explore the results below, then start a crawl of your own.' : active ? state.current_url || 'Preparing your crawl…' : `${state.attempted || 0} URLs attempted · ${state.saved || 0} HTML pages saved`);
  $('pause-button').hidden = !['running','paused'].includes(state.status);
  $('stop-button').hidden = !active;
  $('stop-button').disabled = state.status === 'stopping';
  $('pause-button').innerHTML = `${icon(state.status === 'paused' ? 'play' : 'pause')}<span>${state.status === 'paused' ? 'Resume' : 'Pause'}</span>`;
  const progress = active ? Math.min(97, 100*(state.attempted || 0)/(state.settings.max_pages || 50)) : 100;
  $('progress-fill').style.width = `${progress}%`;
  if (state.id !== previousId) closeDrawer();
  const signature = JSON.stringify([pages, state.logs, state.status, currentTab]);
  if (signature !== renderedSignature) {renderedSignature = signature; renderResults();}
  if (activeStatuses.has(previousStatus) && !active) {
    if (state.error) toast(state.error, true);
    else toast(state.status === 'completed' ? 'Crawl complete. Your report is saved in history.' : 'Crawl stopped. Partial results are saved in history.');
    refreshHistory();
  }
}

function filteredPages() {
  const query = $('search').value.trim().toLowerCase();
  const filter = $('status-filter').value;
  const pages = state.pages.map((page, index) => ({page,index})).filter(({page}) => {
    if (currentTab === 'issues' && !page.issues?.length) return false;
    if (filter === 'success' && page.status !== 200) return false;
    if (filter === 'errors' && page.status === 200) return false;
    if (filter === 'issues' && !page.issues?.length) return false;
    return !query || [page.url,page.title,page.description,...(page.issues || [])].join(' ').toLowerCase().includes(query);
  });
  const sort = $('sort-order').value;
  if (sort === 'url') pages.sort((a,b) => a.page.url.localeCompare(b.page.url));
  if (sort === 'issues') pages.sort((a,b) => (b.page.issues?.length || 0) - (a.page.issues?.length || 0));
  if (sort === 'slow') pages.sort((a,b) => (b.page.elapsed_ms || 0) - (a.page.elapsed_ms || 0));
  return pages;
}

function renderResults() {
  const activity = currentTab === 'activity';
  $('table-tools').hidden = activity;
  $('pages-panel').hidden = activity;
  $('activity-panel').hidden = !activity;
  if (activity) {
    $('activity-panel').innerHTML = state.logs.length ? state.logs.map(log => `<div class="log-row"><time>${escapeHtml(log.time)}</time><span>${escapeHtml(log.message)}</span></div>`).join('') : '<div class="activity-empty">Crawl activity will appear here as your website is explored.</div>';
    $('result-summary').textContent = `${state.logs.length} recent events`;
    return;
  }
  const rows = filteredPages();
  $('table-container').hidden = !rows.length;
  $('empty-state').hidden = !!rows.length;
  $('results-body').innerHTML = rows.map(({page,index}) => `<tr tabindex="0" data-index="${index}" aria-label="Inspect ${escapeHtml(page.title || page.url)}"><td>${index+1}</td><td><div class="page-title">${escapeHtml(page.title || (page.status === 200 ? 'Untitled page' : 'Page could not be fetched'))}</div><div class="page-url">${escapeHtml(page.url)}</div></td><td><span class="badge ${page.status === 200 ? 'success' : 'error'}">${page.status || 'Error'}</span></td><td>${page.depth ?? '—'}</td><td>${page.elapsed_ms || 0} ms</td><td>${page.issues?.length ? `<span class="badge warning">${page.issues.length} issue${page.issues.length === 1 ? '' : 's'}</span>` : `<span class="insight-clean">${icon('check')}All clear</span>`}</td><td>${icon('arrow')}</td></tr>`).join('');
  if (!rows.length) {
    const heading = $('empty-state').querySelector('h3');
    const message = $('empty-state').querySelector('p');
    const hasPages = state.pages.length > 0;
    heading.textContent = hasPages ? currentTab === 'issues' && !$('search').value && $('status-filter').value === 'all' ? 'No issues flagged. Looking good.' : 'No pages match your filters.' : activeStatuses.has(state.status) ? 'Your website is being explored.' : state.status === 'failed' ? 'This crawl returned no pages.' : 'Your next insight starts with a URL.';
    message.textContent = hasPages ? 'Try another search or change the status filter.' : activeStatuses.has(state.status) ? 'Results will appear here as pages are fetched.' : state.status === 'failed' ? 'Review the message above, then adjust your URL or settings.' : 'Enter a website above to discover its pages and links. We’ll take care of the exploring.';
    $('empty-demo').hidden = hasPages || activeStatuses.has(state.status);
  }
  $('result-summary').textContent = state.pages.length ? `Showing ${rows.length} of ${state.pages.length} pages${state.status === 'demo' ? ' · Sample data' : ''}` : 'No pages crawled yet';
}

function detail(title, content) {
  return `<section class="detail-section"><h3>${escapeHtml(title)}</h3><p>${escapeHtml(content || 'Not provided')}</p></section>`;
}

function openInspector(index) {
  selectedPage = state.pages[index];
  if (!selectedPage) return;
  const page = selectedPage;
  $('drawer-content').innerHTML = `<span class="badge ${page.status === 200 ? 'success' : 'error'}">HTTP ${page.status || 'fetch error'}</span><h2 id="drawer-title">${escapeHtml(page.title || 'Untitled page')}</h2><p class="inspector-url">${escapeHtml(page.url)}</p><button class="button small copy-button" id="copy-url">${icon('copy')}Copy URL</button><div class="inspector-meta"><div><strong>${page.depth ?? 0}</strong><span>Link depth</span></div><div><strong>${page.elapsed_ms || 0} ms</strong><span>Fetch + wait</span></div><div><strong>${((page.bytes || 0)/1000).toFixed(1)} KB</strong><span>Response size</span></div><div><strong>${page.links_found || 0}</strong><span>Links found</span></div></div><section class="detail-section"><h3>Page insights</h3>${page.issues?.length ? `<ul class="issue-list">${page.issues.map(issue => `<li>${escapeHtml(issue)}</li>`).join('')}</ul>` : '<p>No issues flagged by the available checks.</p>'}</section>${detail('Page title',page.title)}${detail('Meta description',page.description)}${detail('H1 heading',page.h1)}${detail('Canonical tag',page.canonical)}${detail('Images',`${page.images || 0} images · ${page.images_missing_alt || 0} missing alt attributes`)}<section class="detail-section"><h3>Internal links (${page.internal_links?.length || 0})</h3><div class="inspector-links">${page.internal_links?.length ? page.internal_links.map(url => `<p>${escapeHtml(url)}</p>`).join('') : '<p>No internal links found.</p>'}</div></section>`;
  focusBeforeDrawer = document.activeElement;
  $('drawer-backdrop').hidden = $('page-drawer').hidden = false;
  $('close-drawer').focus();
  $('copy-url').addEventListener('click', async () => {
    try {await navigator.clipboard.writeText(page.url); toast('URL copied to clipboard.');}
    catch {toast('Clipboard unavailable. Select and copy the URL above.', true);}
  });
}

function closeDrawer() {
  $('drawer-backdrop').hidden = $('page-drawer').hidden = true;
  selectedPage = null;
  if (focusBeforeDrawer?.isConnected) focusBeforeDrawer.focus();
  focusBeforeDrawer = null;
}

async function refreshHistory() {
  const history = await call('get_history');
  if (!history) return;
  $('history-count').textContent = history.length;
  $('history-list').innerHTML = history.length ? history.map(report => `<article class="history-row"><span class="stat-icon green">${icon('globe')}</span><div class="history-info"><h3>${escapeHtml(report.settings.url)}</h3><p>${escapeHtml(new Date(report.started_at).toLocaleString())} · ${report.saved} HTML pages · ${Math.round(report.elapsed)}s</p></div><span class="badge ${report.status === 'completed' ? 'success' : 'warning'}">${escapeHtml(report.status)}</span><button class="button small" data-history="${escapeHtml(report.id)}">Open report${icon('arrow')}</button></article>`).join('') : '<div class="history-empty"><h2>A fresh start.</h2><p>Your completed and stopped crawls will appear here automatically.</p></div>';
}

function setView(view) {
  currentView = view;
  for (const name of ['workspace','history','guide']) $(`view-${name}`).hidden = name !== view;
  document.querySelectorAll('[data-view]').forEach(button => button.classList.toggle('selected', button.dataset.view === view));
  $('breadcrumb').textContent = {workspace:'Overview',history:'Crawl history',guide:'Quick guide'}[view];
  if (view === 'history') refreshHistory();
}

async function poll() {
  if (!bridgeReady || polling) return;
  polling = true;
  try {const next = await call('get_state'); if (next) render(next);}
  finally {polling = false;}
}

function setTheme(dark, persist = true) {
  document.body.classList.toggle('dark', dark);
  $('theme-toggle').querySelector('span').textContent = dark ? 'Light appearance' : 'Dark appearance';
  try {localStorage.setItem('crawlDeskTheme', dark ? 'dark' : 'light');} catch {}
  if (persist && bridgeReady) call('save_preferences', {theme:dark ? 'dark' : 'light'});
}

async function initialize() {
  if (bridgeReady) return;
  bridgeReady = true;
  $('connection').textContent = 'Desktop connected';
  const preferences = await call('get_preferences');
  if (preferences) setTheme(preferences.theme === 'dark', false);
  await poll();
  await refreshHistory();
}

document.addEventListener('pywebviewready', initialize);
window.addEventListener('pywebviewready', initialize);
if (window.pywebview?.api) initialize();
setTimeout(() => {if (!bridgeReady) {$('connection').textContent = 'Preview · open desktop.py for controls'; render(state);}}, 2200);
setInterval(poll, 600);
try {setTheme(localStorage.getItem('crawlDeskTheme') === 'dark', false);} catch {}
render(state);

$('crawl-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('form-error').hidden = true;
  $('start-button').disabled = true;
  const result = await call('start_crawl', settings());
  if (result?.ok) {closeDrawer(); renderedSignature = ''; await poll();}
  else {if (result?.error) {$('form-error').textContent = result.error; $('form-error').hidden = false;} render(state);}
});
$('advanced-toggle').addEventListener('click', () => {
  $('advanced').hidden = !$('advanced').hidden;
  $('advanced-toggle').setAttribute('aria-expanded', String(!$('advanced').hidden));
});
$('max_pages').addEventListener('input', () => {$('limit-summary').textContent = `Up to ${$('max_pages').value || 0} pages`;});
$('pause-button').addEventListener('click', async () => {await call('pause_crawl'); await poll();});
$('stop-button').addEventListener('click', async () => {await call('stop_crawl'); await poll();});
async function loadDemo() {
  const result = await call('load_demo');
  if (result?.ok) {closeDrawer(); renderedSignature = ''; await poll(); toast('Sample data loaded. Enter your own website to run a live crawl.');}
}
$('demo-button').addEventListener('click', loadDemo);
$('empty-demo').addEventListener('click', loadDemo);
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
  currentTab = button.dataset.tab;
  document.querySelectorAll('[data-tab]').forEach(tab => {tab.classList.toggle('active', tab === button); tab.setAttribute('aria-selected', String(tab === button));});
  $('pages-panel').setAttribute('aria-labelledby', button.id);
  renderResults();
}));
$('search').addEventListener('input', renderResults);
$('status-filter').addEventListener('change', renderResults);
$('sort-order').addEventListener('change', renderResults);
$('results-body').addEventListener('click', event => {const row = event.target.closest('[data-index]'); if (row) openInspector(Number(row.dataset.index));});
$('results-body').addEventListener('keydown', event => {if (event.key === 'Enter' || event.key === ' ') {const row = event.target.closest('[data-index]'); if (row) {event.preventDefault(); openInspector(Number(row.dataset.index));}}});
$('close-drawer').addEventListener('click', closeDrawer);
$('drawer-backdrop').addEventListener('click', closeDrawer);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {closeDrawer(); $('export-menu').hidden = true; $('export-button').setAttribute('aria-expanded','false');}
  if (event.key === 'Tab' && !$('page-drawer').hidden) {
    const controls = $('page-drawer').querySelectorAll('button:not(:disabled), [tabindex="0"]');
    const first = controls[0], last = controls[controls.length-1];
    if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last.focus();}
    else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus();}
  }
  if (event.altKey && event.key.toLowerCase() === 'n') {setView('workspace'); $('url').focus();}
});
$('export-button').addEventListener('click', () => {$('export-menu').hidden = !$('export-menu').hidden; $('export-button').setAttribute('aria-expanded', String(!$('export-menu').hidden));});
document.addEventListener('click', event => {if (!event.target.closest('.export-wrap')) {$('export-menu').hidden = true; $('export-button').setAttribute('aria-expanded', 'false');}});
document.querySelectorAll('[data-export]').forEach(button => button.addEventListener('click', async () => {
  $('export-menu').hidden = true;
  $('export-button').setAttribute('aria-expanded','false');
  exportBusy = true; $('export-button').disabled = true;
  try {
    const result = await call('export_results',button.dataset.export);
    if (result?.ok && !result.cancelled) toast(`Exported ${result.count} pages to ${result.path}`);
  } finally {exportBusy = false; render(state);}
}));
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
document.querySelector('.brand').addEventListener('click', event => {event.preventDefault(); setView('workspace');});
$('refresh-history').addEventListener('click', refreshHistory);
$('history-list').addEventListener('click', async event => {
  const button = event.target.closest('[data-history]');
  if (!button) return;
  const result = await call('load_history', button.dataset.history);
  if (result?.ok) {await poll(); applySettings(state.settings); setView('workspace');}
});
$('theme-toggle').addEventListener('click', () => setTheme(!document.body.classList.contains('dark')));
